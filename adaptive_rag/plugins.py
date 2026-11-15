"""Explicit role registry and safe defaults. Never imports a name from input."""

from dataclasses import dataclass
from typing import Any, Callable

from .config import DEFAULT_PLUGINS, Settings
from .domain import CapabilityError, ValidationError


METHODS = {
    "parser": "parse", "cleaner": "clean", "chunker": "chunk", "store": "stage",
    "objects": "put", "trace": "save", "decision": "decide", "planner": "plan",
    "rewrite": "transform", "multi_query": "transform", "hyde": "transform",
    "dense": "search", "lexical": "search", "fusion": "fuse", "reranker": "rank",
    "judge": "assess", "gap": "transform", "generator": "generate", "verifier": "verify",
}
OPTIONAL = frozenset(("rewrite", "multi_query", "hyde", "dense", "gap", "generator"))
REQUIRED_METHODS = {role: (method,) for role, method in METHODS.items()} | {
    "store": ("stage", "get_job", "publish", "open_snapshot", "list_chunks", "get_parents",
              "delete_document", "rollback", "set_access"),
    "objects": ("put", "get"),
    "trace": ("save", "get"),
}


@dataclass(frozen=True)
class PluginSpec:
    role: str
    name: str
    factory: Callable[[Settings], Any]
    version: str = "1"
    requires_model: bool = False
    uses_network: bool = False
    enabled: bool = True


class PluginRegistry:
    def __init__(self):
        self._entries: dict[tuple[str, str], PluginSpec] = {}

    def register(self, role: str, name: str, factory: Callable[[Settings], Any], *, version: str = "1", requires_model: bool = False, uses_network: bool = False, enabled: bool = True) -> None:
        if role not in METHODS or not name or not callable(factory):
            raise ValidationError("invalid plugin role, name or factory")
        key = (role, name)
        if key in self._entries:
            raise ValidationError(f"plugin already registered: {role}/{name}")
        if not enabled and role not in OPTIONAL:
            raise ValidationError(f"required role cannot be disabled: {role}")
        self._entries[key] = PluginSpec(role, name, factory, version, requires_model, uses_network, enabled)

    def spec(self, role: str, name: str) -> PluginSpec:
        try:
            return self._entries[role, name]
        except KeyError as exc:
            raise ValidationError(f"unknown plugin: {role}/{name}") from exc

    def build(self, role: str, settings: Settings) -> Any:
        spec = self.spec(role, settings.plugins[role])
        if spec.requires_model or spec.uses_network:
            raise CapabilityError(f"model/network plugin forbidden in model-free foundation: {role}/{spec.name}")
        value = spec.factory(settings)
        if spec.enabled:
            missing = [method for method in REQUIRED_METHODS[role] if not callable(getattr(value, method, None))]
            if missing:
                raise ValidationError(f"plugin does not implement {role}: {', '.join(missing)}")
        return value

    def describe(self, settings: Settings) -> dict[str, Any]:
        return {
            role: {"plugin": (spec := self.spec(role, name)).name, "version": spec.version,
                   "enabled": spec.enabled, "requires_model": spec.requires_model, "uses_network": spec.uses_network}
            for role, name in settings.plugins.items()
        }


def default_registry() -> PluginRegistry:
    from .ingestion import ParentChildChunker, StructuredParser, TextCleaner
    from .pipeline import CitationVerifier, DisabledGenerator, LexicalCoverageJudge, RuleDecisionPolicy, RulePlanner
    from .retrieval import BM25Retriever, PassthroughReranker, ReciprocalRankFusion
    from .storage import FileObjectStore, SQLiteKnowledgeStore, SQLiteTraceSink

    registry = PluginRegistry()
    factories = {
        "parser": lambda s: StructuredParser(), "cleaner": lambda s: TextCleaner(),
        "chunker": lambda s: ParentChildChunker(s.parent_chars, s.child_chars, s.overlap_chars),
        "store": lambda s: SQLiteKnowledgeStore(s.data_dir), "objects": lambda s: FileObjectStore(s.data_dir),
        "trace": lambda s: SQLiteTraceSink(s.data_dir), "decision": lambda s: RuleDecisionPolicy(),
        "planner": lambda s: RulePlanner(s.max_subqueries), "lexical": lambda s: BM25Retriever(),
        "fusion": lambda s: ReciprocalRankFusion(), "reranker": lambda s: PassthroughReranker(),
        "judge": lambda s: LexicalCoverageJudge(), "generator": lambda s: DisabledGenerator(),
        "verifier": lambda s: CitationVerifier(),
    }
    for role, name in DEFAULT_PLUGINS.items():
        registry.register(role, name, factories.get(role, lambda s: None), enabled=role not in OPTIONAL)
    return registry
