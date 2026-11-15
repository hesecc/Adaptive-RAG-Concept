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

    async def run(self, request: QueryRequest, capabilities: dict) -> QueryResult:
        started = monotonic()
        deadline = started + self.settings.timeout_seconds
        save_reserve = min(self.settings.stage_timeout_seconds, self.settings.timeout_seconds * 0.15)
        failed = False
        trace = {
            "trace_id": uuid4().hex, "tenant_id": request.access.tenant_id,
            "subject_id": request.access.subject_id, "started_at": datetime.now(timezone.utc).isoformat(),
            "question": request.question, "history_turns": len(request.history),
            "history_used": False, "generation": None, "stages": [],
            "plugins": self.descriptions, "model_calls": 0, "network_calls": 0,
            "retrieved_chunk_ids": [], "retry_count": 0,
            "decisions": [], "retrieval_lanes": [], "rankings": [],
        }

        async def stage(name, function, *args):
            begin = monotonic()
            record = {"name": name, "status": "running"}
            trace["stages"].append(record)
            try:
                async with asyncio.timeout(self.settings.stage_timeout_seconds):
                    if inspect.iscoroutinefunction(function):
                        value = await function(*args)
                    else:
                        value = await asyncio.to_thread(function, *args)
                        if inspect.isawaitable(value):
                            value = await value
                record["status"] = "ok"
                return value
            except BaseException as exc:
                record["status"] = "timeout" if isinstance(exc, (TimeoutError, asyncio.CancelledError)) else "error"
                record["error_type"] = type(exc).__name__
                raise
            finally:
                record["duration_ms"] = round((monotonic() - begin) * 1000, 3)

        def disabled(role):
            trace["stages"].append({"name": role, "status": "disabled", "duration_ms": 0})

        def result(status, message, evidence=(), missing=(), draft=None):
            trace["status"] = status
            trace["evidence"] = [asdict(item) for item in evidence]
            return QueryResult(trace["trace_id"], status, message, trace["generation"],
                               tuple(evidence), draft.answer if draft else None, draft.claims if draft else (),
                               tuple(missing), capabilities)

        async def decision(checkpoint, missing=()):
            value = await stage(f"decision.{checkpoint}", self.c["decision"].decide, {
                "checkpoint": checkpoint, "missing_query_ids": tuple(missing),
                "gap_enabled": self.descriptions["gap"]["enabled"],
                "retry_count": trace["retry_count"],
            })
            if not isinstance(value, dict) or value.get("action") not in {"continue", "retry", "stop"}:
                raise ValidationError("decision plugin returned an invalid action")
            trace["decisions"].append({"checkpoint": checkpoint, "action": value["action"]})
            return value["action"]

        async def execute():
            if await decision("preflight") == "stop":
                return result("policy_stopped", "规则策略停止本次查询。")
            plan = await stage("planner", self.c["planner"].plan, request)
            if not isinstance(plan, Plan) or plan.sku != request.sku or plan.action not in {"retrieve", "clarify", "capability_unavailable"}:
                raise ValidationError("planner returned an invalid plan or changed trusted SKU")
            if plan.action != "retrieve":
                return result(plan.action, plan.message)
            queries = self._queries(plan.queries)
            original = {q.query_id: q for q in queries}
            trace["subqueries"] = [asdict(q) for q in queries]
            variants = queries
            for role in ("rewrite", "multi_query", "hyde"):
                if self.descriptions[role]["enabled"]:
                    variants = self._variants(await stage(role, self.c[role].transform, variants, request), original)
                else:
                    disabled(role)
            trace["query_variants"] = [asdict(q) for q in variants]
            snapshot = await stage("snapshot", self.c["store"].open_snapshot, request.access.tenant_id, request.generation)
            if snapshot is None:
                return result("no_index", "当前租户尚无已发布知识版本。")
            trace["generation"] = snapshot.generation
            chunks = await stage("authorized_chunks", self.c["store"].list_chunks, snapshot, request.access, request.sku)
            allowed = {chunk.chunk_id: chunk for chunk in chunks}
            trace["authorized_chunk_count"] = len(chunks)
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
                        hits = await stage(f"{role}.{variant.query_id}.{lane_count}", self.c[role].search, variant, chunks, self.settings.candidate_limit)
                        checked = self._hits(hits, variant.query_id, allowed, self.settings.candidate_limit)
                        lanes[variant.query_id].append(checked)
                        trace["retrieval_lanes"].append({"role": role, "query_id": variant.query_id,
                            "text": variant.text, "hits": [{"chunk_id": h.chunk.chunk_id, "score": h.score, "rank": h.rank} for h in checked]})
                trace["lane_count"] = lane_count

            async def assemble():
                ranked = {}
                for qid, query in original.items():
                    fused = await stage(f"fusion.{qid}", self.c["fusion"].fuse, query, lanes[qid], self.settings.candidate_limit)
                    lane_candidates = {hit.chunk.chunk_id: hit.chunk for lane in lanes[qid] for hit in lane}
                    fused = self._hits(fused, qid, lane_candidates, self.settings.candidate_limit)
                    hits = await stage(f"reranker.{qid}", self.c["reranker"].rank, query, fused, self.settings.candidate_limit)
                    candidates = {hit.chunk.chunk_id: hit.chunk for hit in fused}
                    ranked[qid] = self._hits(hits, qid, candidates, self.settings.candidate_limit)
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
                parents = await stage("parent_backfill", self.c["store"].get_parents, snapshot, selected, request.access)
                by_id = {parent.parent_id: parent for parent in parents}
                if set(by_id) != set(selected):
                    # Revocation between retrieval and backfill is fail-closed.
                    raise NotFoundError("evidence became unavailable; retry the query")
                budget = self.settings.evidence_chars
                items = []
                for index, parent_id in enumerate(selected):
                    parent = by_id[parent_id]
                    if parent.tenant_id != request.access.tenant_id or digest(parent.text) != parent.content_hash:
                        raise ValidationError("parent adapter returned invalid source integrity")
                    length = min(len(parent.text), max(1, budget // (len(selected) - index)))
                    items.append(make_evidence(parent, snapshot.generation, length, sorted(associations[parent_id])))
                    budget -= length
                return tuple(items)

            await retrieve(variants)
            evidence = await assemble()
            coverage = await stage("judge", self.c["judge"].assess, queries, evidence)
            self._coverage(coverage, original)
            action = await decision("gap", coverage.missing_query_ids)
            if action == "stop":
                return result("policy_stopped", "策略在证据检查后停止查询。", missing=coverage.missing_query_ids)
            if self.descriptions["gap"]["enabled"] and action == "retry" and coverage.missing_query_ids and self.settings.max_retries:
                missing = tuple(original[qid] for qid in coverage.missing_query_ids)
                gap = self._variants(await stage("gap", self.c["gap"].transform, missing, request), {q.query_id: q for q in missing})
                trace["retry_count"] = 1
                await retrieve(gap)
                evidence = await assemble()
                coverage = await stage("judge.retry", self.c["judge"].assess, queries, evidence)
                self._coverage(coverage, original)
            elif not self.descriptions["gap"]["enabled"]:
                disabled("gap")
            trace["coverage"] = asdict(coverage)
            if not evidence:
                return result("empty", "没有检索到可访问的词法证据；这不等同于语义拒答判断。", missing=coverage.missing_query_ids)
            if await decision("generate", coverage.missing_query_ids) == "stop":
                return result("policy_stopped", "策略停止生成阶段。", missing=coverage.missing_query_ids)
            if self.descriptions["generator"]["enabled"]:
                draft = await stage("generator", self.c["generator"].generate, replace(plan, queries=queries), evidence)
                if not isinstance(draft, Draft):
                    raise ValidationError("generator returned an invalid draft")
            else:
                disabled("generator")
                draft = Draft()
            verification = await stage("verifier", self.c["verifier"].verify, draft, evidence)
            if not isinstance(verification, Verification) or not verification.valid:
                raise ValidationError("citation verification failed")
            trace["verification"] = asdict(verification)
            final_parents = await stage("final_access_check", self.c["store"].get_parents, snapshot,
                                        tuple(e.parent_id for e in evidence), request.access)
            if {p.parent_id for p in final_parents} != {e.parent_id for e in evidence}:
                raise NotFoundError("evidence access changed during query; retry")
            return result("evidence_preview" if draft.answer is None else "answered",
                          "无模型证据预览，未生成答案，未验证语义支持。" if draft.answer is None else "本地插件输出；引文完整性检查通过。",
                          evidence, coverage.missing_query_ids, draft)

        try:
            async with asyncio.timeout(self.settings.timeout_seconds - save_reserve):
                return await execute()
        except TimeoutError as exc:
            failed = True
            trace["status"] = "timeout"
            raise CapabilityError(f"query timed out; trace_id={trace['trace_id']}") from exc
        except BaseException as exc:
            failed = True
            trace["status"] = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
            trace["error_type"] = type(exc).__name__
            if isinstance(exc, (RagError, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise CapabilityError(f"query plugin failed; trace_id={trace['trace_id']}") from exc
        finally:
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
