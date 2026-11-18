"""One application facade shared by trusted local CLI and authenticated HTTP."""

from dataclasses import asdict
import re

from .config import Settings
from .domain import AccessContext, NotFoundError, PdfIngestResult, ValidationError, identifier, stable_id
from .figures import attach_figures, sniff_media_type
from .ingestion import IngestionService
from .pipeline import QueryPipeline, digest, make_evidence
from .plugins import PluginRegistry, default_registry


class RagApplication:
    def __init__(self, settings: Settings, registry: PluginRegistry | None = None):
        self.settings = settings
        self.registry = registry or default_registry()
        self.descriptions = self.registry.describe(settings)
        # Validate every selection before invoking any factory.
        for role, description in self.descriptions.items():
            if (description["requires_model"] and not settings.allow_models) or (description["uses_network"] and not settings.allow_network):
                from .domain import CapabilityError
                raise CapabilityError(f"model/network plugin forbidden without explicit opt-in: {role}")
        self.components = {role: self.registry.build(role, settings) for role in settings.plugins}
        self.store = self.components["store"]
        self.objects = self.components["objects"]
        self.trace_sink = self.components["trace"]
        fingerprint = stable_id("foundation-v1", {role: self.descriptions[role] for role in ("parser", "cleaner", "chunker")})
        self.ingestion = IngestionService(self.store, self.objects, self.components["parser"],
                                          self.components["cleaner"], self.components["chunker"], fingerprint)
        self.pipeline = QueryPipeline(settings, self.components, self.descriptions)
        self._assets = None

    @property
    def assets(self):
        if self._assets is None:
            from .infrastructure import build_assets
            self._assets = build_assets(self.settings, self.objects)
        return self._assets

    def ingest(self, request):
        return self.ingestion.ingest(request)

    def ingest_pdf(self, path, *, backend="local", pdf_settings=None, pdf_checkpoint=None, **metadata):
        from pathlib import Path
        from hashlib import sha256
        from .domain import IngestRequest
        from .pdf import parse_pdf
        source = Path(path)
        parsed = parse_pdf(source, backend, pdf_settings,
                           **({"checkpoint_path": pdf_checkpoint} if pdf_checkpoint is not None else {}))
        try:
            if source.stat().st_size > 50_000_000:
                raise ValidationError("PDF source changed or exceeds the archive size limit")
            raw = source.read_bytes()
        except OSError:
            raise ValidationError("cannot read PDF source for archival") from None
        if sha256(raw).hexdigest() != parsed.metadata.get("source_sha256"):
            raise ValidationError("PDF source changed during parsing; retry with a stable source")
        raw_key = self.objects.put(raw)
        # Figures are stored before the document, so a stored document never points at a missing picture.
        markdown, figures = attach_figures(parsed.markdown, parsed.figures, self.objects.put)
        result = self.ingest(IngestRequest(content=markdown, media_type="text/markdown",
            source_object_key=raw_key, source_parser=parsed.backend, **metadata))
        return PdfIngestResult(**asdict(result), figures=figures)

    def publish(self, tenant_id, job_id, expected_generation):
        return self.store.publish(tenant_id, job_id, expected_generation)

    def delete_document(self, tenant_id, document_id, expected_generation):
        return self.store.delete_document(tenant_id, document_id, expected_generation)

    def rollback(self, tenant_id, generation, expected_generation):
        return self.store.rollback(tenant_id, generation, expected_generation)

    def set_access(self, tenant_id, document_id, allowed_subjects=None, revoked=None, data_domains=None):
        return self.store.set_access(tenant_id, document_id, allowed_subjects, revoked, data_domains)

    async def query(self, request, observer=None):
        return await self.pipeline.run(request, self.capabilities(), observer)

    def get_job(self, tenant_id, job_id):
        return self.store.get_job(tenant_id, job_id)

    def get_trace(self, tenant_id, trace_id):
        return self.trace_sink.get(tenant_id, trace_id)

    def get_accessible_trace(self, access: AccessContext, trace_id: str):
        """Administrative diagnostics still obey current source permissions.

        A trace can mention retrieval hits absent from the final evidence, and
        can survive a failed query. Check all those references, not only the
        final excerpts. The trusted local CLI retains its existing raw reader.
        """
        trace = self.get_trace(access.tenant_id, trace_id)
        generation = trace.get("generation")
        if generation is None:
            return trace
        snapshot = self.store.open_snapshot(access.tenant_id, generation)
        if snapshot is None:
            raise NotFoundError("trace sources are no longer accessible")
        chunk_ids = set(trace.get("retrieved_chunk_ids", ()))
        for lane in trace.get("retrieval_lanes", ()):
            chunk_ids.update(hit["chunk_id"] for hit in lane.get("hits", ()))
        if chunk_ids:
            allowed = {chunk.chunk_id for chunk in self.store.list_chunks(snapshot, access, trace.get("sku"))}
            if not chunk_ids <= allowed:
                raise NotFoundError("trace sources are no longer accessible")
        parent_ids = set(trace.get("source_parent_ids", ()))
        parent_ids.update(item["parent_id"] for item in trace.get("evidence", ()))
        if parent_ids:
            parents = self.store.get_parents(snapshot, tuple(parent_ids), access)
            if {parent.parent_id for parent in parents} != parent_ids:
                raise NotFoundError("trace sources are no longer accessible")
        return trace

    def get_evidence(self, access: AccessContext, generation: str, evidence_id: str):
        identifier(generation, "generation")
        try:
            parent_id, length, short_hash = evidence_id.rsplit(".", 2)
            identifier(parent_id, "parent_id")
            length = int(length)
            if not 1 <= length <= self.settings.evidence_chars:
                raise ValueError
        except (ValueError, AttributeError) as exc:
            raise ValidationError("invalid evidence identifier") from exc
        snapshot = self.store.open_snapshot(access.tenant_id, generation)
        if snapshot is None:
            raise NotFoundError("snapshot not found")
        parents = self.store.get_parents(snapshot, (parent_id,), access)
        if len(parents) != 1:
            raise NotFoundError("evidence not found or no longer accessible")
        if digest(parents[0].text) != parents[0].content_hash:
            raise ValidationError("source integrity check failed")
        evidence = make_evidence(parents[0], generation, length)
        if evidence.evidence_id != evidence_id:
            raise NotFoundError("evidence identifier integrity check failed")
        return evidence

    def get_evidence_image(self, access: AccessContext, generation: str, evidence_id: str, key: str):
        """Bytes and media type of a figure shown by an evidence the caller may read.

        The key alone opens nothing: it must occur in that evidence, so the
        object store cannot be browsed and a figure follows its document's ACL.
        """
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValidationError("invalid image key")
        evidence = self.get_evidence(access, generation, evidence_id)
        if key not in {image.key for image in evidence.images}:
            raise NotFoundError("image not found in this evidence")
        content = self.objects.get(key)
        media_type = sniff_media_type(content)
        if media_type is None:
            raise ValidationError("stored figure is not a supported image")
        return content, media_type

    def capabilities(self):
        cloud_mode = any(d["requires_model"] for d in self.descriptions.values())
        return {"mode": "cloud" if cloud_mode else "model_free",
                "model_calls": None if cloud_mode else 0, "network_calls": None if cloud_mode else 0,
                "call_counts_basis": "per-request trace" if cloud_mode else "disabled",
                "generation_enabled": self.descriptions["generator"]["enabled"],
                "semantic_support_verification": self.settings.plugins["verifier"] == "cloud",
                "history_resolution": any(self.settings.plugins[role] == "cloud" for role in ("planner", "rewrite")),
                "plugins": self.descriptions}
