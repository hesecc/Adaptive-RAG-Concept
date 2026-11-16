"""Bounded orchestration with honest, deterministic model-free defaults.

Async plugins must cooperate with cancellation. Plugin code is trusted Python,
not a security sandbox; declarations prevent accidental model adapter loading.
"""

import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timezone
from hashlib import sha256
import inspect
import re
from time import monotonic
from uuid import uuid4

from .domain import (
    CapabilityError, Coverage, Draft, Evidence, Hit, NotFoundError, Plan,
    QueryRequest, QueryResult, RagError, SubQuery, ValidationError, Verification,
)
from .retrieval import tokenize
from .cloud import start_usage, usage_snapshot, reset_usage


def digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def make_evidence(parent, generation: str, length: int, query_ids=()) -> Evidence:
    text = parent.text[:length]
    excerpt_hash = digest(text)
    evidence_id = f"{parent.parent_id}.{len(text)}.{excerpt_hash[:16]}"
    return Evidence(
        evidence_id, tuple(query_ids), parent.tenant_id, generation,
        parent.document_id, parent.version, parent.revision_id, parent.parent_id,
        text, parent.locator, excerpt_hash, parent.content_hash, len(text) < len(parent.text),
        parent.heading_path,
    )


class RulePlanner:
    """Small documented routing rules, not semantic intent understanding."""
    def __init__(self, max_subqueries=3):
        self.max_subqueries = max_subqueries

    async def plan(self, request: QueryRequest) -> Plan:
        text = request.question.strip()
        if re.fullmatch(r"(?:你好|您好|hi|hello|谢谢|thanks)[!！。 .]*", text, re.I):
            return Plan((), request.sku, "capability_unavailable", "无模型模式不生成闲聊回复。")
        if re.search(r"实时库存|当前库存|订单状态|实时价格|物流状态|current stock|order status", text, re.I):
            return Plan((), request.sku, "capability_unavailable", "此问题需要尚未接入的实时业务工具。")
        if request.sku is None and re.search(r"这款|该款|这个商品|这件商品|this product|this item", text, re.I):
            return Plan((), None, "clarify", "请通过 sku 参数明确商品，再检索商品知识。")
        parts = tuple(part.strip() for part in re.split(r"[？?；;\n]+", text) if part.strip())
        if len(parts) > self.max_subqueries:
            raise ValidationError(f"question exceeds {self.max_subqueries} explicit subquestions; split the request")
        return Plan(tuple(SubQuery(f"q{i}", part) for i, part in enumerate(parts or (text,), 1)), request.sku)


class RuleDecisionPolicy:
    async def decide(self, state: dict) -> dict:
        return {"action": "retry" if state.get("checkpoint") == "gap" and state.get("gap_enabled") and state.get("missing_query_ids") else "continue"}


class LexicalCoverageJudge:
    """Token overlap is only a retrieval diagnostic, never factual support."""
    async def assess(self, queries, evidence) -> Coverage:
        covered = tuple(q.query_id for q in queries if any(
            q.query_id in item.query_ids and set(tokenize(q.text)).intersection(tokenize(item.text))
            for item in evidence
        ))
        return Coverage(covered, tuple(q.query_id for q in queries if q.query_id not in covered))


class DisabledGenerator:
    async def generate(self, plan, evidence) -> Draft:
        return Draft()


class CitationVerifier:
    """Verify citation existence and excerpt integrity; not entailment."""
    async def verify(self, draft: Draft, evidence) -> Verification:
        errors = []
        known = {item.evidence_id: item for item in evidence}
        for item in evidence:
            if digest(item.text) != item.content_hash:
                errors.append("evidence_hash_mismatch")
        if draft.answer is None and draft.claims:
            errors.append("claims_without_answer")
        if draft.answer is not None and (not draft.answer.strip() or not draft.claims):
            errors.append("answer_requires_cited_claims")
        for claim in draft.claims:
            if not claim.text.strip() or not claim.evidence_ids:
                errors.append("claim_requires_text_and_citations")
            if any(key not in known for key in claim.evidence_ids):
                errors.append("unknown_evidence_reference")
        return Verification(not errors, tuple(dict.fromkeys(errors)), support_checked=False)


class QueryPipeline:
    def __init__(self, settings, components, descriptions):
        self.settings, self.c, self.descriptions = settings, components, descriptions

    async def run(self, request: QueryRequest, capabilities: dict, observer=None) -> QueryResult:
        usage_token = start_usage()
        started = monotonic()
        deadline = started + self.settings.timeout_seconds
        save_reserve = min(self.settings.stage_timeout_seconds, self.settings.timeout_seconds * 0.15)
        failed = False
        trace = {
            "trace_id": uuid4().hex, "tenant_id": request.access.tenant_id,
            "subject_id": request.access.subject_id, "started_at": datetime.now(timezone.utc).isoformat(),
            "question": request.question, "sku": request.sku, "history_turns": len(request.history),
            "history_used": False, "generation": None, "stages": [],
            "plugins": self.descriptions, "model_calls": 0, "network_calls": 0,
            "retrieved_chunk_ids": [], "retry_count": 0,
            "decisions": [], "retrieval_lanes": [], "rankings": [], "source_parent_ids": [],
        }

        def emit(event, value):
            # The observer is trusted transport code. Only timing metadata is
            # published before the result's final permission checks complete.
            if observer is not None:
                observer(event, value)

        emit("trace", {"trace_id": trace["trace_id"]})

        def stage_event(record):
            emit("stage", {key: record[key] for key in (
                "index", "name", "status", "started_ms", "duration_ms", "error_type", "reason"
            ) if key in record})

        def output(name, value):
            record = next(row for row in reversed(trace["stages"]) if row["name"] == name)
            record["output"] = value

        def sources(parent_ids):
            trace["source_parent_ids"] = list(dict.fromkeys((*trace["source_parent_ids"], *parent_ids)))

        def hit_output(hits):
            sources(hit.chunk.parent_id for hit in hits)
            return {"hit_count": len(hits), "hits": [
                {"chunk_id": hit.chunk.chunk_id, "parent_id": hit.chunk.parent_id,
                 "document_id": hit.chunk.document_id, "rank": hit.rank, "score": hit.score,
                 "text_preview": hit.chunk.text[:320], "text_chars": len(hit.chunk.text),
                 "preview_truncated": len(hit.chunk.text) > 320}
                for hit in hits
            ]}

        async def stage(name, function, *args, validate=None):
            begin = monotonic()
            before_calls = usage_snapshot()["model_calls"]
            record = {"index": len(trace["stages"]), "name": name, "status": "running",
                      "started_ms": round((begin - started) * 1000, 3)}
            trace["stages"].append(record)
            stage_event(record)
            try:
                async with asyncio.timeout(self.settings.stage_timeout_seconds):
                    if inspect.iscoroutinefunction(function):
                        value = await function(*args)
                    else:
                        value = await asyncio.to_thread(function, *args)
                        if inspect.isawaitable(value):
                            value = await value
                    if validate is not None:
                        try:
                            value = validate(value)
                            if inspect.isawaitable(value):
                                value = await value
                        except Exception:
                            record["reason"] = "output_validation_failed"
                            raise
                record["status"] = "ok"
                return value
            except BaseException as exc:
                record["status"] = ("cancelled" if isinstance(exc, asyncio.CancelledError) else
                                    "timeout" if isinstance(exc, TimeoutError) else "error")
                record["error_type"] = type(exc).__name__
                raise
            finally:
                owner = getattr(function, "__self__", None)
                if (request.history and name in {"planner", "rewrite", "multi_query", "hyde"} and
                        getattr(owner, "uses_history", False) and usage_snapshot()["model_calls"] > before_calls):
                    trace["history_used"] = True
                record["duration_ms"] = round((monotonic() - begin) * 1000, 3)
                stage_event(record)

        def disabled(role, *, reason=None):
            record = {"index": len(trace["stages"]), "name": role,
                      "status": "skipped" if reason else "disabled", "duration_ms": 0,
                      "started_ms": round((monotonic() - started) * 1000, 3),
                      "output": {"executed": False}}
            if reason:
                record["reason"] = reason
            trace["stages"].append(record)
            stage_event(record)

        def result(status, message, evidence=(), missing=(), draft=None):
            trace["status"] = status
            trace["evidence"] = [asdict(item) for item in evidence]
            value = QueryResult(trace["trace_id"], status, message, trace["generation"],
                               tuple(evidence), draft.answer if draft else None, draft.claims if draft else (),
                               tuple(missing), capabilities | {"usage": usage_snapshot()})
            trace["final_result"] = asdict(value)
            return value

        async def decision(checkpoint, missing=()):
            def checked(value):
                if not isinstance(value, dict) or value.get("action") not in {"continue", "retry", "stop"}:
                    raise ValidationError("decision plugin returned an invalid action")
                return value
            value = await stage(f"decision.{checkpoint}", self.c["decision"].decide, {
                "checkpoint": checkpoint, "missing_query_ids": tuple(missing),
                "gap_enabled": self.descriptions["gap"]["enabled"],
                "retry_count": trace["retry_count"],
            }, validate=checked)
            trace["decisions"].append({"checkpoint": checkpoint, "action": value["action"]})
            output(f"decision.{checkpoint}", {"checkpoint": checkpoint, "action": value["action"],
                   "missing_query_ids": list(missing), "retry_count": trace["retry_count"]})
            return value["action"]

        async def execute():
            if await decision("preflight") == "stop":
                return result("policy_stopped", "规则策略停止本次查询。")
            def checked_plan(value):
                if not isinstance(value, Plan) or value.sku != request.sku or value.action not in {"retrieve", "clarify", "capability_unavailable"}:
                    raise ValidationError("planner returned an invalid plan or changed trusted SKU")
                return replace(value, queries=self._queries(value.queries)) if value.action == "retrieve" else value
            plan = await stage("planner", self.c["planner"].plan, request, validate=checked_plan)
            if plan.action != "retrieve":
                output("planner", {"action": plan.action, "queries": []})
                return result(plan.action, plan.message)
            queries = plan.queries
            output("planner", {"action": plan.action, "queries": [asdict(q) for q in queries]})
            original = {q.query_id: q for q in queries}
            trace["subqueries"] = [asdict(q) for q in queries]
            variants = queries
            role_count = sum(bool(self.descriptions[r]["enabled"]) for r in ("lexical", "dense"))
            minimum_lanes = len(queries) * role_count
            if minimum_lanes > self.settings.max_lanes:
                raise ValidationError("retrieval lane budget cannot cover original questions")
            reserve = (min(minimum_lanes, self.settings.max_lanes - minimum_lanes)
                       if self.descriptions["gap"]["enabled"] and self.settings.max_retries else 0)
            variant_budget = ((self.settings.max_lanes - reserve) // role_count
                              if role_count else len(queries))
            trace["reserved_repair_lanes"] = reserve
            for role in ("rewrite", "multi_query", "hyde"):
                if self.descriptions[role]["enabled"]:
                    if variant_budget <= len(variants):
                        disabled(role, reason="retrieval_lane_budget")
                        continue
                    variants = await stage(role, self.c[role].transform, variants, request,
                                           validate=lambda value: self._variants(value, original))
                    variants = self._budget_variants(variants, queries, variant_budget)
                    output(role, {"queries": [asdict(q) for q in variants], "variant_budget": variant_budget})
                else:
                    disabled(role)
            trace["query_variants"] = [asdict(q) for q in variants]
            snapshot = await stage("snapshot", self.c["store"].open_snapshot, request.access.tenant_id, request.generation)
            output("snapshot", {"generation": snapshot.generation if snapshot else None})
            if snapshot is None:
                return result("no_index", "当前租户尚无已发布知识版本。")
            trace["generation"] = snapshot.generation
            chunks = await stage("authorized_chunks", self.c["store"].list_chunks, snapshot, request.access, request.sku)
            allowed = {chunk.chunk_id: chunk for chunk in chunks}
            trace["authorized_chunk_count"] = len(chunks)
            output("authorized_chunks", {"authorized_chunk_count": len(chunks), "sku": request.sku})
            for role in ("dense",):
                if not self.descriptions[role]["enabled"]:
                    disabled(role)
            lanes = {qid: [] for qid in original}
            lane_count = 0

            async def retrieve(batch):
                nonlocal lane_count
                for variant in batch:
                    for role in ("lexical", "dense"):
                        if not self.descriptions[role]["enabled"]:
                            continue
                        if lane_count >= self.settings.max_lanes:
                            raise ValidationError("retrieval exceeds configured lane budget")
                        lane_count += 1
                        checked = await stage(f"{role}.{variant.query_id}.{lane_count}", self.c[role].search,
                                              variant, chunks, self.settings.candidate_limit,
                                              validate=lambda value: self._hits(value, variant.query_id, allowed, self.settings.candidate_limit))
                        output(f"{role}.{variant.query_id}.{lane_count}",
                               {"query_id": variant.query_id, "query": variant.text, **hit_output(checked)})
                        lanes[variant.query_id].append(checked)
                        trace["retrieval_lanes"].append({"role": role, "query_id": variant.query_id,
                            "text": variant.text, "hits": [{"chunk_id": h.chunk.chunk_id, "score": h.score, "rank": h.rank} for h in checked]})
                trace["lane_count"] = lane_count

            async def assemble():
                ranked = {}
                for qid, query in original.items():
                    lane_candidates = {hit.chunk.chunk_id: hit.chunk for lane in lanes[qid] for hit in lane}
                    fused = await stage(f"fusion.{qid}", self.c["fusion"].fuse, query, lanes[qid], self.settings.candidate_limit,
                                        validate=lambda value: self._hits(value, qid, lane_candidates, self.settings.candidate_limit))
                    output(f"fusion.{qid}", {"query_id": qid, **hit_output(fused)})
                    candidates = {hit.chunk.chunk_id: hit.chunk for hit in fused}
                    ranked[qid] = await stage(f"reranker.{qid}", self.c["reranker"].rank, query, fused, self.settings.candidate_limit,
                                              validate=lambda value: self._hits(value, qid, candidates, self.settings.candidate_limit))
                    output(f"reranker.{qid}", {"query_id": qid, **hit_output(ranked[qid])})
                    trace["rankings"].append({"query_id": qid, "retry": trace["retry_count"],
                        "fused_chunk_ids": [h.chunk.chunk_id for h in fused],
                        "ranked_chunk_ids": [h.chunk.chunk_id for h in ranked[qid]]})
                ordered = []
                for rank in range(max((len(v) for v in ranked.values()), default=0)):
                    for hits in ranked.values():
                        if rank < len(hits):
                            ordered.append(hits[rank])
                trace["retrieved_chunk_ids"] = list(dict.fromkeys(hit.chunk.chunk_id for hit in ordered))
                associations = {}
                for hit in ordered:
                    associations.setdefault(hit.chunk.parent_id, set()).add(hit.query_id)
                selected = tuple(associations)[:request.top_k]
                def checked_parents(values):
                    if {parent.parent_id for parent in values} != set(selected):
                        # Revocation between retrieval and backfill is fail-closed.
                        raise NotFoundError("evidence became unavailable; retry the query")
                    for parent in values:
                        if parent.tenant_id != request.access.tenant_id or digest(parent.text) != parent.content_hash:
                            raise ValidationError("parent adapter returned invalid source integrity")
                    return values
                parents = await stage("parent_backfill", self.c["store"].get_parents, snapshot, selected, request.access,
                                      validate=checked_parents)
                by_id = {parent.parent_id: parent for parent in parents}
                sources(selected)
                budget = self.settings.evidence_chars
                items = []
                for index, parent_id in enumerate(selected):
                    parent = by_id[parent_id]
                    length = min(len(parent.text), max(1, budget // (len(selected) - index)))
                    items.append(make_evidence(parent, snapshot.generation, length, sorted(associations[parent_id])))
                    budget -= length
                output("parent_backfill", {"parent_count": len(parents), "parents": [
                    {"parent_id": parent.parent_id, "document_id": parent.document_id,
                     "heading_path": list(parent.heading_path), "text_preview": parent.text[:600],
                     "text_chars": len(parent.text), "preview_truncated": len(parent.text) > 600,
                     "content_chars": len(parent.text)} for parent in parents
                ], "evidence_chars": sum(len(item.text) for item in items)})
                return tuple(items)

            async def finish(status, message, evidence, missing=(), draft=None):
                # All exits exposing evidence or claims recheck current permissions.
                def checked_access(values):
                    if {p.parent_id for p in values} != {e.parent_id for e in evidence}:
                        raise NotFoundError("evidence access changed during query; retry")
                    return values
                parents = await stage("final_access_check", self.c["store"].get_parents, snapshot,
                                      tuple(e.parent_id for e in evidence), request.access, validate=checked_access)
                output("final_access_check", {"authorized_parent_count": len(parents), "valid": True})
                return result(status, message, evidence, missing, draft)

            async def recover(missing, evidence, label):
                # Initial evidence repair and post-generation repair share one budget.
                nonlocal coverage
                if not (chunks and missing and self.descriptions["gap"]["enabled"] and
                        trace["retry_count"] < self.settings.max_retries):
                    return evidence, False
                role_count = sum(bool(self.descriptions[r]["enabled"]) for r in ("lexical", "dense"))
                if lane_count + len(missing) * role_count > self.settings.max_lanes:
                    trace["repair_skipped"] = "retrieval_lane_budget"
                    return evidence, False
                missing = self._missing_queries(missing, original)
                gap = await stage("gap." + label, self.c["gap"].transform, missing, request,
                                  validate=lambda value: self._variants(value, {q.query_id: q for q in missing}))
                output("gap." + label, {"queries": [asdict(q) for q in gap], "checkpoint": label})
                if lane_count + len(gap) * role_count > self.settings.max_lanes:
                    trace["repair_skipped"] = "retrieval_lane_budget"
                    return evidence, False
                before = {(item.parent_id, item.source_hash): item.text for item in evidence}
                trace["retry_count"] += 1
                trace["repair_checkpoint"] = label
                await retrieve(gap)
                updated = await assemble()
                coverage = await stage("judge.retry", self.c["judge"].assess, queries, updated,
                                       validate=lambda value: self._coverage(value, original))
                output("judge.retry", asdict(coverage))
                trace["coverage"] = asdict(coverage)
                changed = any((item.parent_id, item.source_hash) not in before or
                              len(item.text) > len(before[item.parent_id, item.source_hash])
                              for item in updated)
                trace["repair_new_evidence"] = changed
                return updated, changed

            await retrieve(variants)
            evidence = await assemble()
            coverage = await stage("judge", self.c["judge"].assess, queries, evidence,
                                   validate=lambda value: self._coverage(value, original))
            output("judge", asdict(coverage))
            action = await decision("gap", coverage.missing_query_ids)
            if action == "stop":
                return result("policy_stopped", "策略在证据检查后停止查询。", missing=coverage.missing_query_ids)
            if self.descriptions["gap"]["enabled"] and action == "retry" and coverage.missing_query_ids and self.settings.max_retries:
                missing = coverage.missing_queries or tuple(original[qid] for qid in coverage.missing_query_ids)
                evidence, _ = await recover(missing, evidence, "evidence")
            elif not self.descriptions["gap"]["enabled"]:
                disabled("gap")
            trace["coverage"] = asdict(coverage)
            if not evidence:
                return result("empty", "没有检索到可访问的词法证据；这不等同于语义拒答判断。", missing=coverage.missing_query_ids)
            semantic_coverage = coverage.basis == "cloud_semantic_sufficiency"
            if semantic_coverage and not coverage.covered_query_ids:
                return await finish("insufficient", "现有证据不足以完整支持问题，暂不生成答案。",
                                    evidence, coverage.missing_query_ids)
            if await decision("generate", coverage.missing_query_ids) == "stop":
                return result("policy_stopped", "策略停止生成阶段。", missing=coverage.missing_query_ids)
            async def checked_draft(value, items):
                if not isinstance(value, Draft):
                    raise ValidationError("generator returned an invalid draft")
                if not (await CitationVerifier().verify(value, items)).valid:
                    raise ValidationError("citation verification failed")
                return value
            if self.descriptions["generator"]["enabled"]:
                generation_queries = tuple(q for q in queries if not semantic_coverage or q.query_id in coverage.covered_query_ids)
                draft = await stage("generator", self.c["generator"].generate, replace(plan, queries=generation_queries), evidence,
                                    validate=lambda value: checked_draft(value, evidence))
                output("generator", {"answer_present": draft.answer is not None,
                       "claim_count": len(draft.claims), "verification_pending": True})
            else:
                disabled("generator")
                draft = Draft()
            async def verify(current, items, label):
                structural = await CitationVerifier().verify(current, items)
                if not structural.valid:
                    raise ValidationError("citation verification failed")
                def checked_verification(value):
                    self._verification(value, current, original)
                    if not value.valid and not value.support_checked:
                        raise ValidationError("citation verification failed")
                    return value
                evaluator = getattr(self.c["verifier"], "evaluate", None)
                if callable(evaluator):
                    checked = await stage(label, evaluator, replace(plan, queries=queries), current, items,
                                          validate=checked_verification)
                else:
                    checked = await stage(label, self.c["verifier"].verify, current, items, validate=checked_verification)
                output(label, asdict(checked))
                return checked

            verification = await verify(draft, evidence, "verifier")
            missing = tuple(dict.fromkeys((*coverage.missing_query_ids,
                                          *(q.query_id for q in verification.missing_queries))))
            if verification.support_checked and draft.answer is not None and (not verification.valid or missing):
                trace["verification_initial"] = asdict(verification)
                gap_queries = verification.missing_queries or coverage.missing_queries
                if not gap_queries:
                    gap_queries = tuple(original[qid] for qid in (missing or tuple(original)))
                previous_draft, previous_verification = draft, verification
                evidence, changed = await recover(gap_queries, evidence, "answer")
                if changed and (coverage.basis != "cloud_semantic_sufficiency" or coverage.covered_query_ids):
                    generation_queries = tuple(q for q in queries if coverage.basis != "cloud_semantic_sufficiency" or q.query_id in coverage.covered_query_ids)
                    draft = await stage("generator.retry", self.c["generator"].generate, replace(plan, queries=generation_queries), evidence,
                                        validate=lambda value: checked_draft(value, evidence))
                    output("generator.retry", {"answer_present": draft.answer is not None,
                           "claim_count": len(draft.claims), "verification_pending": True})
                    verification = await verify(draft, evidence, "verifier.retry")
                else:
                    # Retry may reorder/truncate parents. Keep only claims with exactly
                    # the same cited excerpts; never transplant a prior verdict.
                    current_ids = {e.evidence_id for e in evidence}
                    indexes = tuple(i for i in previous_verification.supported_claim_indexes
                                    if set(previous_draft.claims[i].evidence_ids) <= current_ids)
                    verification = replace(previous_verification, supported_claim_indexes=indexes)
                missing = tuple(dict.fromkeys((*coverage.missing_query_ids,
                                              *(q.query_id for q in verification.missing_queries))))
            trace["verification"] = asdict(verification)
            if verification.support_checked:
                safe_claims = tuple(draft.claims[i] for i in verification.supported_claim_indexes)
                safe_answer = "\n".join(claim.text + " " + " ".join(f"[{key}]" for key in claim.evidence_ids)
                                         for claim in safe_claims) or None
                draft = Draft(safe_answer, safe_claims, draft.mode)
                if not safe_claims:
                    return await finish("insufficient", "未获得可通过事实支持性检查的答案，需补充权威资料。",
                                        evidence, missing or tuple(original), draft)
                if not verification.valid or missing:
                    return await finish("partial", "仅返回通过支持性检查的部分；仍有缺项需要确认。",
                                        evidence, missing or tuple(original), draft)
                message = ("回答已通过云端事实支持性与完整性检查；模型判断仍可能出错。"
                           if verification.completeness_checked else
                           "回答已通过事实支持性检查，尚未检查完整性；模型判断仍可能出错。")
                return await finish("answered", message,
                                    evidence, (), draft)
            return await finish("evidence_preview" if draft.answer is None else "answered",
                                "证据预览，未生成答案，未验证语义支持。" if draft.answer is None else "已生成带引用回答；出处完整性检查通过，未验证语义支持。",
                                evidence, coverage.missing_query_ids, draft)

        try:
            async with asyncio.timeout(self.settings.timeout_seconds - save_reserve):
                return await execute()
        except TimeoutError as exc:
            failed = True
            trace["status"] = "timeout"
            # The outer query deadline cancels its active stage before the
            # timeout context translates that cancellation into TimeoutError.
            if trace["stages"] and trace["stages"][-1]["status"] == "cancelled":
                record = trace["stages"][-1]
                record.update(status="timeout", error_type="TimeoutError")
                stage_event(record)
            raise CapabilityError(f"query timed out; trace_id={trace['trace_id']}") from exc
        except BaseException as exc:
            failed = True
            trace["status"] = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
            trace["error_type"] = type(exc).__name__
            if isinstance(exc, (RagError, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise CapabilityError(f"query plugin failed; trace_id={trace['trace_id']}") from exc
        finally:
            usage = usage_snapshot()
            reset_usage(usage_token)
            trace.update(model_calls=usage["model_calls"], network_calls=usage["network_calls"], usage=usage)
            trace["duration_ms"] = round((monotonic() - started) * 1000, 3)
            # Persistence is required for success, bounded, and never blocks the
            # event loop. A failed sink cannot record its own failure; surface it.
            try:
                async with asyncio.timeout(max(0.001, min(self.settings.stage_timeout_seconds, deadline - monotonic()))):
                    await asyncio.to_thread(self.c["trace"].save, trace)
            except Exception as exc:
                if not failed:
                    raise CapabilityError(f"trace persistence failed; trace_id={trace['trace_id']}") from exc

    def _queries(self, queries):
        if not isinstance(queries, (tuple, list)) or not 1 <= len(queries) <= self.settings.max_subqueries:
            raise ValidationError("planner exceeded subquery budget")
        if len({q.query_id for q in queries if isinstance(q, SubQuery)}) != len(queries):
            raise ValidationError("subquery IDs must be unique")
        return self._variants(queries, {q.query_id: q for q in queries})

    def _variants(self, variants, originals):
        if not isinstance(variants, (tuple, list)) or not 1 <= len(variants) <= self.settings.max_lanes:
            raise ValidationError("transform exceeded variant budget")
        for query in variants:
            if not isinstance(query, SubQuery) or query.query_id not in originals or not isinstance(query.text, str) or not query.text.strip() or len(query.text) > 4000:
                raise ValidationError("transform returned invalid or reassigned subquery")
        if {q.query_id for q in variants} != set(originals):
            raise ValidationError("transform dropped an original subquery")
        return tuple(variants)

    @staticmethod
    def _budget_variants(variants, originals, limit):
        """Keep each original first, then distribute extension slots fairly."""
        selected = list(originals)
        seen = {(query.query_id, query.text) for query in selected}
        extras = {query.query_id: [] for query in originals}
        for query in variants:
            identity = (query.query_id, query.text)
            if identity not in seen:
                extras[query.query_id].append(query)
                seen.add(identity)
        for rank in range(max((len(items) for items in extras.values()), default=0)):
            for items in extras.values():
                if len(selected) >= limit:
                    return tuple(selected)
                if rank < len(items):
                    selected.append(items[rank])
        return tuple(selected)

    @staticmethod
    def _hits(hits, query_id, allowed, limit):
        if not isinstance(hits, (tuple, list)) or len(hits) > limit:
            raise ValidationError("retriever exceeded candidate budget")
        seen = set()
        for hit in hits:
            if not isinstance(hit, Hit) or hit.query_id != query_id or allowed.get(hit.chunk.chunk_id) != hit.chunk or hit.chunk.chunk_id in seen:
                raise ValidationError("plugin returned duplicate, unauthorized or mutated chunks")
            seen.add(hit.chunk.chunk_id)
        return tuple(hits)

    @staticmethod
    def _coverage(coverage, originals):
        if not isinstance(coverage, Coverage):
            raise ValidationError("judge returned invalid coverage")
        covered, missing = set(coverage.covered_query_ids), set(coverage.missing_query_ids)
        if covered & missing or covered | missing != set(originals):
            raise ValidationError("judge coverage must partition original subqueries")
        if len(covered) != len(coverage.covered_query_ids) or len(missing) != len(coverage.missing_query_ids):
            raise ValidationError("judge coverage must not repeat subqueries")
        if coverage.missing_queries:
            QueryPipeline._missing_queries(coverage.missing_queries, {qid: originals[qid] for qid in missing})
        return coverage

    @staticmethod
    def _missing_queries(queries, originals):
        if not isinstance(queries, (tuple, list)) or not 1 <= len(queries) <= len(originals):
            raise ValidationError("invalid missing-aspect queries")
        seen = set()
        for query in queries:
            if not isinstance(query, SubQuery) or query.query_id not in originals or query.query_id in seen or not isinstance(query.text, str) or not query.text.strip() or len(query.text) > 4000 or "\x00" in query.text:
                raise ValidationError("invalid missing-aspect queries")
            seen.add(query.query_id)
        return tuple(queries)

    @staticmethod
    def _verification(value, draft, originals):
        if not isinstance(value, Verification) or any(type(flag) is not bool for flag in
                (value.valid, value.support_checked, value.completeness_checked)):
            raise ValidationError("verifier returned invalid result")
        indexes = value.supported_claim_indexes
        if not isinstance(indexes, tuple) or any(type(i) is not int or not 0 <= i < len(draft.claims) for i in indexes) or len(set(indexes)) != len(indexes):
            raise ValidationError("verifier returned invalid supported claims")
        if value.support_checked and value.valid and set(indexes) != set(range(len(draft.claims))):
            raise ValidationError("verifier passed claims it did not check")
        if value.missing_queries:
            QueryPipeline._missing_queries(value.missing_queries, originals)
