"""Authenticated local HTTP adapter over the shared application."""

from dataclasses import asdict
import hmac
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .app import RagApplication
from .config import Principal, Settings, load_settings
from .domain import IngestRequest, QueryRequest, RagError, ValidationError


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DocumentInput(Input):
    document_id: str
    version: str
    content: str = Field(min_length=1, max_length=2_000_000)
    media_type: str = "text/markdown"
    title: str = "Untitled"
    scope: str = "store"
    sku_ids: list[str] = Field(default_factory=list, max_length=1000)
    allowed_subjects: list[str] = Field(default_factory=list, max_length=1000)
    language: str = "zh"


class PublishInput(Input):
    job_id: str
    expected_generation: str | None = None


class QueryInput(Input):
    question: str = Field(min_length=1, max_length=4000)
    sku: str | None = None
    generation: str | None = None
    top_k: int = Field(default=5, ge=1, le=20)
    history: list[str] = Field(default_factory=list, max_length=6)


class DeleteInput(Input):
    document_id: str
    expected_generation: str | None = None


class RollbackInput(Input):
    generation: str
    expected_generation: str | None = None


class AccessInput(Input):
    document_id: str
    allowed_subjects: list[str] = Field(default_factory=list, max_length=1000)
    revoked: bool = False


def create_api(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    if not settings.api_tokens:
        raise ValidationError("HTTP requires explicit API tokens in trusted configuration")
    application = RagApplication(settings)
    api = FastAPI(title="Adaptive RAG Foundation", version="0.1.0")
    api.state.application = application

    def authenticate(request: Request) -> Principal:
        header = request.headers.get("authorization", "")
        scheme, separator, token = header.partition(" ")
        found = None
        if separator and scheme.lower() == "bearer" and token and not any(c.isspace() for c in token):
            for configured, principal in settings.api_tokens.items():
                if hmac.compare_digest(configured.encode("utf-8"), token.encode("utf-8")):
                    found = principal
        if found is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Invalid bearer token", headers={"WWW-Authenticate": "Bearer"})
        return found

    def administrator(principal: Annotated[Principal, Depends(authenticate)]) -> Principal:
        if not principal.admin:
            from .domain import ForbiddenError
            raise ForbiddenError("Administrator permission required")
        return principal

    User = Annotated[Principal, Depends(authenticate)]
    Admin = Annotated[Principal, Depends(administrator)]

    @api.exception_handler(RagError)
    async def rag_error(_request: Request, exc: RagError):
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": str(exc)}})

    @api.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        # Omit submitted input values, which may contain private source text.
        details = [{"location": list(item["loc"]), "type": item["type"]} for item in exc.errors()]
        return JSONResponse(status_code=422, content={"error": {"code": "validation_error", "message": "Invalid request", "details": details}})

    from starlette.exceptions import HTTPException

    @api.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code, headers=exc.headers, content={"error": {"code": "unauthorized" if exc.status_code == 401 else "http_error", "message": str(exc.detail)}})

    @api.exception_handler(Exception)
    async def internal_error(_request: Request, _exc: Exception):
        return JSONResponse(status_code=500, content={"error": {"code": "internal_error", "message": "Internal application error"}})

    @api.get("/health")
    def health():
        return {"status": "ok", "mode": "model_free"}

    @api.get("/capabilities")
    def capabilities(principal: User):
        return application.capabilities()

    @api.post("/documents", status_code=201)
    def ingest(body: DocumentInput, principal: Admin):
        values = body.model_dump()
        values["sku_ids"] = tuple(values["sku_ids"])
        values["allowed_subjects"] = tuple(values["allowed_subjects"])
        return asdict(application.ingest(IngestRequest(tenant_id=principal.tenant_id, **values)))

    @api.post("/publish")
    def publish(body: PublishInput, principal: Admin):
        return asdict(application.publish(principal.tenant_id, body.job_id, body.expected_generation))

    @api.post("/query")
    async def query(body: QueryInput, principal: User):
        values = body.model_dump()
        values["history"] = tuple(values["history"])
        return asdict(await application.query(QueryRequest(access=principal.access, **values)))

    @api.post("/documents/delete")
    def delete_document(body: DeleteInput, principal: Admin):
        return asdict(application.delete_document(principal.tenant_id, body.document_id, body.expected_generation))

    @api.post("/rollback")
    def rollback(body: RollbackInput, principal: Admin):
        return asdict(application.rollback(principal.tenant_id, body.generation, body.expected_generation))

    @api.post("/access")
    def access(body: AccessInput, principal: Admin):
        application.set_access(principal.tenant_id, body.document_id, tuple(body.allowed_subjects), body.revoked)
        return {"status": "updated"}

    @api.get("/jobs/{job_id}")
    def job(job_id: str, principal: Admin):
        return application.get_job(principal.tenant_id, job_id)

    @api.get("/traces/{trace_id}")
    def trace(trace_id: str, principal: Admin):
        return application.get_trace(principal.tenant_id, trace_id)

    @api.get("/evidence/{generation}/{evidence_id}")
    def evidence(generation: str, evidence_id: str, principal: User):
        return asdict(application.get_evidence(principal.access, generation, evidence_id))

    return api
