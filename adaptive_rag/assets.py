"""Image assets indexed by their text descriptions, never by image embeddings.

Asset metadata is authoritative. The vector index is derived and every hit is
hydrated against current metadata and ACLs. Uploads do not run paid models;
explicit indexing consumes the durable metadata outbox outside DB transactions.
"""

import asyncio
from collections import Counter
from hashlib import sha256
import io
import json
import math
from pathlib import Path
from time import monotonic, time
from uuid import uuid4
import warnings

from .assets_persistence import AssetDatabase, encode
from .domain import (AccessContext, CapabilityError, ConflictError, NotFoundError,
                     ValidationError, identifier, nonempty, safe_error, stable_id)
from .retrieval import tokenize
from .data_access import domains_allowed, normalize_data_domains


MAX_IMAGE_BYTES = 10_000_000


def _text(value, name, limit):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ValidationError(f"{name} must contain at most {limit} characters without NUL")
    return value.strip()


def _metadata(title, description, ocr_text, tags, allowed_subjects, data_domains):
    title = nonempty(title, "title", 300).strip()
    description = _text(description, "description", 16_000)
    ocr_text = _text(ocr_text, "ocr_text", 100_000)
    if not isinstance(tags, tuple) or len(tags) > 100:
        raise ValidationError("tags must be a tuple of at most 100 strings")
    tags = tuple(dict.fromkeys(nonempty(tag, "tag", 100).strip() for tag in tags))
    if not isinstance(allowed_subjects, tuple) or len(allowed_subjects) > 1000:
        raise ValidationError("allowed_subjects must be a tuple of at most 1000 identifiers")
    subjects = tuple(dict.fromkeys(identifier(subject, "allowed_subject") for subject in allowed_subjects))
    return {"title": title, "description": description, "ocr_text": ocr_text,
            "tags": list(tags), "allowed_subjects": list(subjects),
            "data_domains": list(normalize_data_domains(data_domains))}


def _image(content):
    if not isinstance(content, bytes) or not 1 <= len(content) <= MAX_IMAGE_BYTES:
        raise ValidationError("image content must contain 1-10000000 bytes")
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        raise CapabilityError("image uploads require the Pillow dependency") from None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as probe:
                fmt = probe.format
                width, height = probe.size
                if fmt not in ("PNG", "JPEG", "WEBP") or width * height > 25_000_000 or max(width, height) > 16_384:
                    raise ValidationError("image must be PNG, JPEG or WEBP with at most 25 million pixels")
                if getattr(probe, "n_frames", 1) != 1:
                    raise ValidationError("animated images are not supported")
                probe.verify()
            with Image.open(io.BytesIO(content)) as decoded:
                decoded.load()
                decoded.thumbnail((480, 480))
                preview = io.BytesIO()
                decoded.convert("RGB").save(preview, format="JPEG", quality=85)
        return {"media_type": {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[fmt],
                "width": width, "height": height, "size_bytes": len(content)}, preview.getvalue()
    except ValidationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValidationError("image cannot be decoded as a supported static image") from None


def _allowed(asset, access):
    subjects = asset["allowed_subjects"]
    return (asset["tenant_id"] == access.tenant_id
            and domains_allowed(asset.get("data_domains", ()), access.data_domains)
            and (not subjects or bool(set(subjects) & access.subjects)))


def _representations(asset):
    # OCR is split independently so an excessively long transcript does not
    # drown the short description. Each immutable representation owns an ID.
    summary = "\n".join(filter(None, (asset["title"], asset["description"], " ".join(asset["tags"]))))
    pieces = [summary[i:i + 3000] for i in range(0, len(summary), 3000)]
    ocr = asset["ocr_text"]
    pieces.extend(ocr[i:i + 3000] for i in range(0, len(ocr), 3000))
    return [(stable_id("image-text-v1", asset["tenant_id"], asset["asset_id"], asset["revision_id"], i, text), text)
            for i, text in enumerate(pieces) if text.strip()]


class AssetService:
    def __init__(self, data_dir: Path, objects, index, embedder=None,
                 embedding_profile="image-descriptions-v1", postgres_dsn=None):
        identifier(embedding_profile, "embedding_profile")
        self.db = AssetDatabase(data_dir, postgres_dsn)
        self.objects, self.index, self.embedder = objects, index, embedder
        self.profile = embedding_profile

    def _public(self, asset):
        value = dict(asset)
        value.setdefault("data_domains", [])
        indexed_profile = value.get("index_profile") or value.get("index_target_profile")
        if indexed_profile is not None and indexed_profile != self.profile:
            value["indexing_status"] = "outdated"
        value["content_url"] = f"/assets/{asset['asset_id']}/content"
        value["thumbnail_url"] = f"/assets/{asset['asset_id']}/thumbnail"
        value.pop("object_key", None)
        value.pop("thumbnail_key", None)
        return value

    def _current(self, connection, access, asset_id):
        identifier(asset_id, "asset_id")
        result = self.db.current(connection, access.tenant_id, asset_id)
        if not result or result[1] or not _allowed(result[0], access):
            raise NotFoundError("image asset not found or no longer accessible")
        return result[0]

    def create(self, access: AccessContext, content: bytes, *, asset_id: str,
               title: str, description="", ocr_text="", tags=(), allowed_subjects=(),
               source_document_id=None, source_page=None, data_domains=()):
        identifier(asset_id, "asset_id")
        metadata = _metadata(title, description, ocr_text, tags, allowed_subjects, data_domains)
        if source_document_id is not None:
            identifier(source_document_id, "source_document_id")
        if source_page is not None and (type(source_page) is not int or not 1 <= source_page <= 100_000):
            raise ValidationError("source_page must be a positive page number")
        image, thumbnail = _image(content)
        content_hash = sha256(content).hexdigest()
        revision = stable_id("image-revision-v1", content_hash, metadata, source_document_id, source_page)
        # Validation precedes object writes. A failure after either put can
        # leave an unreferenced content-addressed object, never a broken asset.
        object_key = self.objects.put(content)
        thumbnail_key = self.objects.put(thumbnail)
        asset = {"tenant_id": access.tenant_id, "asset_id": asset_id,
                 "revision_id": revision, "object_key": object_key,
                 "thumbnail_key": thumbnail_key, "sha256": content_hash,
                 "source_document_id": source_document_id, "source_page": source_page,
                 "source": "document" if source_document_id else "upload",
                 "created_by": access.subject_id, "created_at": time(),
                 "indexing_status": "pending", "index_profile": None,
                 "index_target_profile": self.profile,
                 "index_error": None, "index_stages": [],
                 **image, **metadata}
        with self.db.transaction(access.tenant_id + ":" + asset_id) as connection:
            current = self.db.current(connection, access.tenant_id, asset_id)
            if current:
                if not current[1] and current[0]["revision_id"] == revision and _allowed(current[0], access):
                    return self._public(current[0])
                raise ConflictError("asset_id already exists; update metadata using expected_revision")
            self.db.write_asset(connection, asset)
            self.db.enqueue(connection, asset, self.profile)
        return self._public(asset)

    def list(self, access):
        with self.db.transaction() as connection:
            rows = connection.execute("SELECT body FROM image_assets WHERE tenant_id=? AND deleted=0 ORDER BY asset_id", (access.tenant_id,)).fetchall()
        return [self._public(asset) for asset in (json.loads(row["body"]) for row in rows) if _allowed(asset, access)]

    def get(self, access, asset_id):
        with self.db.transaction() as connection:
            return self._public(self._current(connection, access, asset_id))

    def update(self, access, asset_id, *, title, description, ocr_text, tags,
               allowed_subjects, expected_revision, data_domains=None):
        identifier(expected_revision, "expected_revision")
        with self.db.transaction(access.tenant_id + ":" + asset_id) as connection:
            asset = self._current(connection, access, asset_id)
            metadata = _metadata(title, description, ocr_text, tags, allowed_subjects,
                tuple(asset.get("data_domains", ())) if data_domains is None else data_domains)
            if asset["revision_id"] != expected_revision:
                raise ConflictError("image asset changed; reload its current revision")
            revision = stable_id("image-revision-v1", asset["sha256"], metadata,
                                 asset["source_document_id"], asset["source_page"])
            if revision == expected_revision:
                return self._public(asset)
            asset = asset | metadata | {"revision_id": revision, "indexing_status": "pending",
                                        "index_profile": None, "index_target_profile": self.profile,
                                        "index_error": None, "index_stages": []}
            self.db.write_asset(connection, asset)
            self.db.enqueue(connection, asset, self.profile, requeue=True)
        return self._public(asset)

    def delete(self, access, asset_id, expected_revision=None):
        with self.db.transaction(access.tenant_id + ":" + asset_id) as connection:
            asset = self._current(connection, access, asset_id)
            if expected_revision is not None and expected_revision != asset["revision_id"]:
                raise ConflictError("image asset changed; reload its current revision")
            asset["indexing_status"] = "deleted"
            connection.execute("UPDATE image_assets SET deleted=1,body=? WHERE tenant_id=? AND asset_id=?", (encode(asset), access.tenant_id, asset_id))
            # Cleanup remains an outbox effect; deleting metadata succeeds even
            # when Milvus is temporarily unavailable. Files retain historical
            # references until a future object retention policy removes them.
            connection.execute("UPDATE image_index_jobs SET state='cancelled',owner=NULL WHERE tenant_id=? AND asset_id=? AND state IN ('pending','failed','running')", (access.tenant_id, asset_id))
            cleanup = {"kind": "delete", "asset_id": asset_id, "revision_id": asset["revision_id"], "profile": self.profile, "vectors": None, "stages": []}
            connection.execute("INSERT INTO image_index_jobs(tenant_id,job_id,asset_id,revision_id,state,body) VALUES(?,?,?,?,?,?) ON CONFLICT(tenant_id,job_id) DO NOTHING", (access.tenant_id, stable_id("image-delete", access.tenant_id, asset_id), asset_id, asset["revision_id"], "pending", encode(cleanup)))

    def content(self, access, asset_id, *, thumbnail=False):
        with self.db.transaction() as connection:
            asset = self._current(connection, access, asset_id)
        key = asset["thumbnail_key"] if thumbnail else asset["object_key"]
        content = self.objects.get(key)
        # Object storage can be remote and slow. Recheck access and version
        # after that effect so an intervening delete/revocation is respected.
        with self.db.transaction() as connection:
            current = self._current(connection, access, asset_id)
            if current["revision_id"] != asset["revision_id"]:
                raise ConflictError("image changed while loading; retry with the current revision")
        return content, "image/jpeg" if thumbnail else asset["media_type"]

    def retry(self, access, asset_id):
        """Explicit retry: unknown paid calls are never retried by a poll loop."""
        with self.db.transaction(access.tenant_id + ":" + asset_id) as connection:
            asset = self._current(connection, access, asset_id)
            job_id = self.db.enqueue(connection, asset, self.profile)
            row = connection.execute("SELECT state,body FROM image_index_jobs WHERE tenant_id=? AND job_id=?", (access.tenant_id, job_id)).fetchone()
            if row["state"] == "running":
                raise ConflictError("indexing is running; wait before retrying")
            # Explicit reindex must also rebuild a lost derived index after a
            # previously successful job. Keep cached vectors to avoid another
            # embedding request when only the external index needs recovery.
            connection.execute("UPDATE image_index_jobs SET state='pending',owner=NULL WHERE tenant_id=? AND job_id=?", (access.tenant_id, job_id))
            asset["indexing_status"] = "pending"
            asset["index_profile"] = None
            asset["index_target_profile"] = self.profile
            asset["index_error"] = None
            self.db.write_current(connection, asset)
        return self._public(asset)

    @staticmethod
    def _stage(stages, name, started, output, status="ok"):
        stages.append({"name": name, "status": status,
                       "duration_ms": round((monotonic() - started) * 1000, 3), "output": output})

    async def process_pending(self, tenant_id, limit=10):
        identifier(tenant_id, "tenant_id")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValidationError("index batch limit must be between 1 and 100")
        result = {"processed": 0, "ready": 0, "deleted": 0, "failed": 0, "cancelled": 0,
                  "model_calls": 0, "embedding_requests": 0, "jobs": [],
                  "embedding_enabled": self.embedder is not None}
        for _ in range(limit):
            claimed = await asyncio.to_thread(self._claim, tenant_id, self.embedder is not None)
            if claimed is None:
                break
            job_id, owner, job, asset = claimed
            if job.get("kind") != "delete" and self.embedder is None:
                # Keep outbox durable while cloud models are disabled.
                await asyncio.to_thread(self._release, tenant_id, job_id, owner)
                break
            stages = []
            state = "done"
            try:
                if job.get("kind") == "delete":
                    started = monotonic()
                    await asyncio.to_thread(self.index.delete, tenant_id, job["asset_id"])
                    self._stage(stages, "vector_delete", started, {"asset_id": job["asset_id"]})
                elif asset is None:
                    state = "cancelled"
                else:
                    representations = _representations(asset)
                    if not representations:
                        state = "awaiting_description"
                    else:
                        if job.get("vectors") is None:
                            started = monotonic()
                            result["embedding_requests"] += 1
                            usage = {}
                            try:
                                vectors = await self._embed(tuple(text for _, text in representations), usage)
                            except BaseException:
                                self._stage(stages, "description_embedding", started,
                                            {"usage": usage}, "error")
                                raise
                            finally:
                                result["model_calls"] += usage.get("model_calls", 0)
                                job["usage"] = usage
                            if len(vectors) != len(representations):
                                raise CapabilityError("image description embedding returned the wrong count")
                            from .vector_store import validate_vector
                            job["vectors"] = [list(validate_vector(vector)) for vector in vectors]
                            if len({len(vector) for vector in job["vectors"]}) != 1:
                                raise CapabilityError("image description embeddings have inconsistent dimensions")
                            self._stage(stages, "description_embedding", started, {"representations": len(vectors), "usage": usage})
                            await asyncio.to_thread(self._save_job, tenant_id, job_id, owner, job)
                        from .vector_store import VectorRecord
                        records = tuple(VectorRecord(record_id, tenant_id, asset["asset_id"], asset["revision_id"], self.profile, tuple(vector))
                                        for (record_id, _), vector in zip(representations, job["vectors"]))
                        started = monotonic()
                        await asyncio.to_thread(self.index.upsert, records)
                        self._stage(stages, "vector_upsert", started, {"representations": len(records)})
            except asyncio.CancelledError:
                job["error"] = {"code": "index_interrupted", "message": "Indexing was interrupted; inspect the task before explicit retry."}
                await asyncio.shield(asyncio.to_thread(self._finish, tenant_id, job_id, owner, job, "failed", stages))
                raise
            except Exception as exc:
                state = "failed"
                job["error"] = safe_error(exc)
                self._stage(stages, "index_error", monotonic(), job["error"], "error")
            state = await asyncio.to_thread(self._finish, tenant_id, job_id, owner, job, state, stages)
            result["processed"] += 1
            counter = ("deleted" if job.get("kind") == "delete" else "ready") if state == "done" else "failed" if state == "failed" else "cancelled"
            result[counter] += 1
            result["jobs"].append({"job_id": job_id, "asset_id": job["asset_id"], "state": state,
                                   "stages": stages, "error": job.get("error")})
        return result

    def _claim(self, tenant_id, indexing_enabled=True):
        with self.db.transaction(tenant_id + ":image-worker") as connection:
            # A process crash may leave running jobs. Do not infer that a paid
            # request was never accepted; surface explicit retry after a lease.
            stale = connection.execute("SELECT job_id,asset_id,revision_id,body FROM image_index_jobs WHERE tenant_id=? AND state='running' AND started_at<?", (tenant_id, time() - 900)).fetchall()
            for row in stale:
                job = json.loads(row["body"])
                if job.get("kind") != "delete" and job["profile"] != self.profile:
                    continue
                job["error"] = {"code": "index_interrupted", "message": "Indexing was interrupted; inspect provider usage before explicit retry."}
                connection.execute("UPDATE image_index_jobs SET state='failed',owner=NULL,body=? WHERE tenant_id=? AND job_id=?", (encode(job), tenant_id, row["job_id"]))
                current = self.db.current(connection, tenant_id, row["asset_id"])
                if (current and not current[1] and current[0]["revision_id"] == row["revision_id"]
                        and current[0].get("index_target_profile", self.profile) == self.profile):
                    asset = current[0] | {"indexing_status": "failed", "index_error": job["error"]}
                    self.db.write_current(connection, asset)
            candidates = connection.execute("SELECT job_id,body FROM image_index_jobs WHERE tenant_id=? AND state='pending' ORDER BY job_id", (tenant_id,)).fetchall()
            row = next((candidate for candidate in candidates
                        if json.loads(candidate["body"]).get("kind") == "delete"
                        or (indexing_enabled and json.loads(candidate["body"])["profile"] == self.profile)), None)
            if row is None:
                return None
            owner = uuid4().hex
            job = json.loads(row["body"])
            connection.execute("UPDATE image_index_jobs SET state='running',owner=?,started_at=? WHERE tenant_id=? AND job_id=?", (owner, time(), tenant_id, row["job_id"]))
            current = self.db.current(connection, tenant_id, job["asset_id"])
            asset = current[0] if (current and not current[1]
                                  and current[0]["revision_id"] == job["revision_id"]
                                  and current[0].get("index_target_profile", self.profile) == self.profile) else None
            return row["job_id"], owner, job, asset

    async def _embed(self, texts, usage):
        # CloudTransport owns actual provider attempts, including batch and
        # retry calls. A deterministic test adapter must never be counted as a
        # real provider request merely because its embed method was invoked.
        from .cloud import reset_usage, start_usage, usage_snapshot
        token = start_usage()
        try:
            return await self.embedder.embed(texts)
        finally:
            usage.update(usage_snapshot())
            reset_usage(token)

    def _release(self, tenant_id, job_id, owner):
        with self.db.transaction() as connection:
            connection.execute("UPDATE image_index_jobs SET state='pending',owner=NULL WHERE tenant_id=? AND job_id=? AND owner=?", (tenant_id, job_id, owner))

    def _save_job(self, tenant_id, job_id, owner, job):
        with self.db.transaction() as connection:
            cursor = connection.execute("UPDATE image_index_jobs SET body=? WHERE tenant_id=? AND job_id=? AND state='running' AND owner=?", (encode(job), tenant_id, job_id, owner))
            if cursor.rowcount != 1:
                raise ConflictError("index task was superseded")

    def _finish(self, tenant_id, job_id, owner, job, state, stages):
        with self.db.transaction(tenant_id + ":" + job["asset_id"]) as connection:
            current = self.db.current(connection, tenant_id, job["asset_id"])
            active = (current and not current[1] and current[0]["revision_id"] == job["revision_id"]
                      and current[0].get("index_target_profile", self.profile) == self.profile)
            row = connection.execute("SELECT state,owner FROM image_index_jobs WHERE tenant_id=? AND job_id=?", (tenant_id, job_id)).fetchone()
            if not row or row["owner"] != owner or row["state"] != "running":
                if current and current[1] and job.get("kind") != "delete":
                    # An old external upsert may finish after deletion cleanup.
                    # Requeue cleanup after this observed late completion.
                    connection.execute("UPDATE image_index_jobs SET state='pending',owner=NULL WHERE tenant_id=? AND job_id=?", (tenant_id, stable_id("image-delete", tenant_id, job["asset_id"])))
                return "cancelled"
            if not active and job.get("kind") != "delete":
                state = "cancelled"
            job["stages"] = stages
            connection.execute("UPDATE image_index_jobs SET state=?,owner=NULL,body=? WHERE tenant_id=? AND job_id=?", (state, encode(job), tenant_id, job_id))
            if active and job.get("kind") != "delete":
                asset = current[0] | {"indexing_status": "ready" if state == "done" else state,
                                      "index_profile": self.profile if state == "done" else None,
                                      "index_error": job.get("error") if state == "failed" else None,
                                      "index_stages": stages}
                self.db.write_current(connection, asset)
            return state

    async def search(self, access, question, top_k=5):
        nonempty(question, "question", 4000)
        if type(top_k) is not int or not 1 <= top_k <= 20:
            raise ValidationError("top_k must be between 1 and 20")
        trace = {"trace_id": uuid4().hex, "tenant_id": access.tenant_id,
                 "subject_id": access.subject_id, "question": question,
                 "search_mode": "semantic" if self.embedder else "lexical",
                 "stages": [], "hits": [], "model_calls": 0,
                 "embedding_requests": 0, "status": "running"}
        started = monotonic()
        try:
            if self.embedder:
                tick = monotonic()
                trace["embedding_requests"] += 1
                usage = {}
                try:
                    vectors = await self._embed((question,), usage)
                except BaseException:
                    self._stage(trace["stages"], "query_embedding", tick, {"usage": usage}, "error")
                    raise
                finally:
                    trace["model_calls"] += usage.get("model_calls", 0)
                    trace["usage"] = usage
                if len(vectors) != 1:
                    raise CapabilityError("query embedding returned the wrong count")
                self._stage(trace["stages"], "query_embedding", tick, {"dimensions": len(vectors[0])})
                tick = monotonic()
                matches = await asyncio.to_thread(self.index.search, access.tenant_id, self.profile, vectors[0], 1000)
                self._stage(trace["stages"], "vector_search", tick, {"candidate_count": len(matches)})
                tick = monotonic()
                hits = await asyncio.to_thread(self._hydrate, access, matches, top_k)
                self._stage(trace["stages"], "asset_hydrate", tick,
                            {"authorized_image_count": len(hits), "requested_count": top_k,
                             "candidate_limit_reached": len(matches) == 1000})
            else:
                tick = monotonic()
                hits = await asyncio.to_thread(self._lexical, access, question, top_k)
                self._stage(trace["stages"], "description_lexical_search", tick, {"image_count": len(hits)})
            trace["hits"] = hits
            trace["status"] = "ok" if hits else "empty"
        except asyncio.CancelledError:
            trace["status"] = "cancelled"
            trace["error"] = safe_error(asyncio.CancelledError())
            raise
        except Exception as exc:
            trace["status"] = "error"
            trace["error"] = safe_error(exc)
            self._stage(trace["stages"], "search_error", monotonic(), trace["error"], "error")
        finally:
            trace["duration_ms"] = round((monotonic() - started) * 1000, 3)
            # If persistence fails, callers receive an explicit storage error,
            # never a trace ID that cannot be replayed.
            await asyncio.shield(asyncio.to_thread(self._save_trace, trace))
        # Persistence may be remote. Re-read using the same current-permission
        # gate as historical replay before delivering any saved image metadata.
        return await asyncio.to_thread(self.get_trace, access, trace["trace_id"])

    def _hydrate(self, access, matches, limit):
        hits, seen = [], set()
        with self.db.transaction() as connection:
            for match in matches:
                if match.tenant_id != access.tenant_id or match.profile != self.profile or match.asset_id in seen:
                    continue
                current = self.db.current(connection, access.tenant_id, match.asset_id)
                if not current or current[1]:
                    continue
                asset = current[0]
                if not _allowed(asset, access) or asset["revision_id"] != match.revision_id or asset["indexing_status"] != "ready" or asset["index_profile"] != self.profile:
                    continue
                representations = dict(_representations(asset))
                if match.record_id not in representations:
                    continue
                if not isinstance(match.score, (int, float)) or not math.isfinite(match.score):
                    raise CapabilityError("vector index returned an invalid score")
                seen.add(match.asset_id)
                hits.append({"asset": self._public(asset), "score": float(match.score),
                             "matched_text": representations[match.record_id], "representation_id": match.record_id})
                if len(hits) >= limit:
                    break
        return hits

    def _lexical(self, access, question, limit):
        query = set(tokenize(question))
        scored = []
        for asset in self.list(access):
            best = None
            for record_id, text in _representations(asset):
                counts = Counter(tokenize(text))
                overlap = sum(min(counts[word], 3) for word in query)
                if overlap:
                    score = overlap / (1 + math.log1p(sum(counts.values())))
                    if best is None or score > best[0]:
                        best = score, record_id, text
            if best:
                scored.append({"asset": asset, "score": best[0], "representation_id": best[1], "matched_text": best[2]})
        scored.sort(key=lambda hit: (-hit["score"], hit["asset"]["asset_id"]))
        return scored[:limit]

    def _save_trace(self, trace):
        with self.db.transaction() as connection:
            connection.execute("INSERT INTO image_traces(tenant_id,trace_id,body) VALUES(?,?,?)", (trace["tenant_id"], trace["trace_id"], encode(trace)))

    def get_trace(self, access, trace_id):
        identifier(trace_id, "trace_id")
        with self.db.transaction() as connection:
            row = connection.execute("SELECT body FROM image_traces WHERE tenant_id=? AND trace_id=?", (access.tenant_id, trace_id)).fetchone()
            if not row:
                raise NotFoundError("image trace not found")
            trace = json.loads(row["body"])
            if trace["subject_id"] != access.subject_id:
                raise NotFoundError("image trace not found")
            for hit in trace["hits"]:
                current = self._current(connection, access, hit["asset"]["asset_id"])
                if current["revision_id"] != hit["asset"]["revision_id"]:
                    raise NotFoundError("image trace sources changed or are no longer accessible")
        return {key: value for key, value in trace.items() if key not in ("tenant_id", "subject_id")}
