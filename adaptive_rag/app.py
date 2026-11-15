"""One application facade shared by trusted local CLI and authenticated HTTP."""

from .config import Settings
from .domain import AccessContext, NotFoundError, ValidationError, identifier, stable_id
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
            if description["requires_model"] or description["uses_network"]:
                from .domain import CapabilityError
                raise CapabilityError(f"model/network plugin forbidden in model-free foundation: {role}")
        self.components = {role: self.registry.build(role, settings) for role in settings.plugins}
        self.store = self.components["store"]
        self.objects = self.components["objects"]
        self.trace_sink = self.components["trace"]
        fingerprint = stable_id("foundation-v1", {role: self.descriptions[role] for role in ("parser", "cleaner", "chunker")})
        self.ingestion = IngestionService(self.store, self.objects, self.components["parser"],
                                          self.components["cleaner"], self.components["chunker"], fingerprint)
        self.pipeline = QueryPipeline(settings, self.components, self.descriptions)

    def ingest(self, request):
        return self.ingestion.ingest(request)

    def publish(self, tenant_id, job_id, expected_generation):
        return self.store.publish(tenant_id, job_id, expected_generation)

    def delete_document(self, tenant_id, document_id, expected_generation):
        return self.store.delete_document(tenant_id, document_id, expected_generation)

    def rollback(self, tenant_id, generation, expected_generation):
        return self.store.rollback(tenant_id, generation, expected_generation)

    def set_access(self, tenant_id, document_id, allowed_subjects, revoked=False):
        return self.store.set_access(tenant_id, document_id, allowed_subjects, revoked)

    async def query(self, request):
        return await self.pipeline.run(request, self.capabilities())

    def get_job(self, tenant_id, job_id):
        return self.store.get_job(tenant_id, job_id)

    def get_trace(self, tenant_id, trace_id):
        return self.trace_sink.get(tenant_id, trace_id)

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

    def capabilities(self):
        return {"mode": "model_free", "model_calls": 0, "network_calls": 0,
                "generation_enabled": self.descriptions["generator"]["enabled"],
                "semantic_support_verification": False, "history_resolution": False,
                "plugins": self.descriptions}
