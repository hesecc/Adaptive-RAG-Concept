"""Offline retrieval evaluation; no model judges or generated answers."""

from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any

from .domain import AccessContext, QueryRequest, ValidationError, identifier


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    """Read strict UTF-8 JSONL so fixture mistakes cannot silently pass."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValidationError("Cannot read evaluation fixture as UTF-8") from exc
    cases: list[dict[str, Any]] = []
    ids: set[str] = set()
    allowed = {"id", "question", "sku", "gold_document_ids", "gold_chunk_ids"}
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValidationError(f"Invalid JSON at fixture line {line_number}") from exc
        if not isinstance(case, dict) or set(case) - allowed:
            raise ValidationError(f"Unknown or invalid fields at fixture line {line_number}")
        if "gold_document_ids" in case and "gold_chunk_ids" in case:
            raise ValidationError("Use either gold_document_ids or gold_chunk_ids")
        case_id = case.get("id", f"case-{line_number}")
        identifier(case_id, "case id")
        if case_id in ids:
            raise ValidationError(f"Duplicate evaluation case id: {case_id}")
        ids.add(case_id)
        # Reuse runtime validation for question and SKU constraints.
        QueryRequest(AccessContext("fixture"), case.get("question"), sku=case.get("sku"))
        for field in ("gold_document_ids", "gold_chunk_ids"):
            gold = case.get(field, [])
            if not isinstance(gold, list):
                raise ValidationError(f"{field} must be an array")
            for value in gold:
                identifier(value, field)
            if len(gold) != len(set(gold)):
                raise ValidationError(f"{field} must not contain duplicates")
        cases.append({**case, "id": case_id})
    if not cases:
        raise ValidationError("Evaluation fixture must contain at least one case")
    return cases


def retrieval_metrics(retrieved: list[str], gold: list[str], top_k: int) -> dict[str, float]:
    """Binary relevance, unique ranked IDs, and a positive-only denominator."""
    if type(top_k) is not int or not 1 <= top_k <= 20:
        raise ValidationError("top_k must be an integer between 1 and 20")
    if not gold:
        raise ValidationError("Retrieval metrics require nonempty gold labels")
    ranked = list(dict.fromkeys(retrieved))[:top_k]
    expected = set(gold)
    relevant_ranks = [rank for rank, value in enumerate(ranked, 1) if value in expected]
    dcg = sum(1.0 / math.log2(rank + 1) for rank in relevant_ranks)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(len(expected), top_k) + 1))
    return {
        "hit_at_k": float(bool(relevant_ranks)),
        "recall_at_k": len(relevant_ranks) / len(expected),
        "mrr_at_k": 1.0 / relevant_ranks[0] if relevant_ranks else 0.0,
        "ndcg_at_k": dcg / idcg,
    }


async def run_evaluation(
    application: Any, fixture_path: str | Path, access: AccessContext, *,
    generation: str | None = None, top_k: int = 5, output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Evaluate the same application used by CLI/HTTP against local gold data."""
    cases = load_cases(fixture_path)
    # Validate before touching storage, including runs with no published data.
    QueryRequest(access, cases[0]["question"], generation=generation, top_k=top_k)
    rows: list[dict[str, Any]] = []
    positive: list[dict[str, float]] = []
    unlabelled_count = 0
    unlabelled_empty = 0
    # Pin once for repeatable evaluation even if another writer publishes.
    snapshot = application.store.open_snapshot(access.tenant_id, generation)
    if snapshot is None:
        raise ValidationError("Evaluation requires a published snapshot")
    pinned = snapshot.generation
    for case in cases:
        result = await application.query(QueryRequest(
            access, case["question"], sku=case.get("sku"), generation=pinned, top_k=top_k,
        ))
        gold_type = "chunk" if "gold_chunk_ids" in case else "document"
        gold = case.get("gold_chunk_ids", case.get("gold_document_ids", []))
        if gold_type == "document":
            retrieved = list(dict.fromkeys(item.document_id for item in result.evidence))[:top_k]
        else:
            trace = application.get_trace(access.tenant_id, result.trace_id)
            chunk_ids = trace.get("retrieved_chunk_ids")
            if not isinstance(chunk_ids, list) or any(not isinstance(value, str) for value in chunk_ids):
                raise ValidationError("Chunk evaluation requires trace.retrieved_chunk_ids")
            retrieved = list(dict.fromkeys(chunk_ids))[:top_k]
        row: dict[str, Any] = {
            "id": case["id"], "question": case["question"], "sku": case.get("sku"),
            "trace_id": result.trace_id, "generation": result.generation,
            "status": result.status, "gold_type": gold_type, "gold_ids": gold,
            "retrieved_ids": retrieved, "metrics": None,
        }
        if gold:
            scores = retrieval_metrics(retrieved, gold, top_k)
            row["metrics"] = scores
            positive.append(scores)
        else:
            unlabelled_count += 1
            unlabelled_empty += int(not retrieved)
        rows.append(row)
    names = ("hit_at_k", "recall_at_k", "mrr_at_k", "ndcg_at_k")
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "parameters": {"tenant_id": access.tenant_id, "subject_id": access.subject_id,
                       "groups": list(access.groups), "generation": pinned, "top_k": top_k,
                       "fixture_path": str(fixture_path)},
        "capabilities": application.capabilities(), "model_calls": 0,
        "case_count": len(rows), "positive_case_count": len(positive),
        "unlabelled_case_count": unlabelled_count,
        "metrics": {name: sum(item[name] for item in positive) / len(positive) if positive else None
                    for name in names},
        "retrieval_empty_rate": unlabelled_empty / unlabelled_count if unlabelled_count else None,
        "empty_rate_basis": "cases_without_gold_only; not refusal_quality",
        "cases": rows,
    }
    if output_path is not None:
        try:
            Path(output_path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            raise ValidationError("Cannot write evaluation output") from exc
    return report
