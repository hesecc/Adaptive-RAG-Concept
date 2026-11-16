"""Authenticated local HTTP adapter over the shared application."""

from dataclasses import asdict
import asyncio
from contextlib import suppress
import hmac
import json
from pathlib import Path
from typing import Annotated

from anyio import CancelScope
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .app import RagApplication
from .auth import AccountStore, AuthenticationError, COOKIE_NAME, SESSION_SECONDS
from .config import Principal, Settings, load_settings
from .domain import IngestRequest, QueryRequest, RagError, safe_error


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


class LoginInput(Input):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class AccountInput(Input):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=6, max_length=128)


def create_api(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    application = RagApplication(settings)
    admin_tenants = {principal.tenant_id for principal in settings.api_tokens.values() if principal.admin}
    browser_tenant = settings.browser_tenant or (next(iter(admin_tenants)) if len(admin_tenants) == 1 else "demo")
    accounts = AccountStore(settings.data_dir, browser_tenant)
    api = FastAPI(title="Adaptive RAG Foundation", version="0.1.0")
    api.state.application = application
    api.state.accounts = accounts

    @api.middleware("http")
    async def browser_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    static = Path(__file__).resolve().parent / "static"
    api.mount("/static", StaticFiles(directory=static), name="static")

    @api.get("/", include_in_schema=False)
    def workbench():
        return FileResponse(static / "index.html")

    def authenticate(request: Request) -> Principal:
        header = request.headers.get("authorization", "")
        scheme, separator, token = header.partition(" ")
        found = None
        if separator and scheme.lower() == "bearer" and token and not any(c.isspace() for c in token):
            for configured, principal in settings.api_tokens.items():
                if hmac.compare_digest(configured.encode("utf-8"), token.encode("utf-8")):
                    found = principal
                    request.state.username = principal.subject_id
        if not header:
            account = accounts.session(request.cookies.get(COOKIE_NAME))
            if account is not None:
                found = account.principal
                request.state.username = account.username
        if found is None:
            raise AuthenticationError("请先登录工作台。")
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
        return {"status": "ok", "mode": application.capabilities()["mode"]}

    @api.get("/capabilities")
    def capabilities(principal: User):
        return application.capabilities()

    @api.get("/console/session")
    def console_session(request: Request, principal: User):
        return {"tenant_id": principal.tenant_id, "subject_id": principal.subject_id,
                "username": request.state.username, "admin": principal.admin,
                "capabilities": application.capabilities()}

    @api.post("/auth/login")
    def login(body: LoginInput, request: Request):
        token, account = accounts.login(body.username, body.password)
        principal = account.principal
        response = JSONResponse(content={"tenant_id": principal.tenant_id, "subject_id": principal.subject_id,
            "username": account.username, "admin": principal.admin, "capabilities": application.capabilities()})
        response.set_cookie(COOKIE_NAME, token, max_age=SESSION_SECONDS, httponly=True,
                            secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
                            samesite="lax", path="/")
        return response

    @api.post("/auth/logout", status_code=204)
    def logout(request: Request):
        accounts.logout(request.cookies.get(COOKIE_NAME))
        response = Response(status_code=204)
        response.delete_cookie(COOKIE_NAME, httponly=True, samesite="lax", path="/")
        return response

    @api.get("/admin/users")
    def users(principal: Admin):
        return {"users": accounts.users(principal)}

    @api.post("/admin/users", status_code=201)
    def create_user(body: AccountInput, principal: Admin):
        return accounts.create(principal, body.username, body.password)

    @api.delete("/admin/users/{username}", status_code=204)
    def delete_user(username: str, principal: Admin):
        accounts.delete(principal, username)
        return Response(status_code=204)

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

    @api.post("/query/stream")
    async def query_stream(body: QueryInput, principal: User):
        values = body.model_dump()
        values["history"] = tuple(values["history"])
        query_request = QueryRequest(access=principal.access, **values)

        async def events():
            # One queue belongs to one request; no global run registry or
            # pollable in-progress source data is created.
            queue = asyncio.Queue(maxsize=256)
            trace_id = None

            def observe(event, value):
                nonlocal trace_id
                if event == "trace":
                    trace_id = value["trace_id"]
                queue.put_nowait((event, value))

            async def run_query():
                try:
                    value = await application.query(query_request, observer=observe)
                    trace = await asyncio.to_thread(application.get_accessible_trace,
                                                    principal.access, value.trace_id)
                    queue.put_nowait(("result", {"result": asdict(value), "trace": trace}))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Plugins and transports may put credentials in exception
                    # text. Only a stable public code and generic text cross
                    # this streaming transport.
                    code = exc.code if isinstance(exc, RagError) else "internal_error"
                    diagnostic = safe_error(exc)
                    value = {"code": code, "message": diagnostic["message"],
                             "error": diagnostic, "trace_id": trace_id}
                    # A failed query can still have useful validated outputs.
                    # Recheck every source against current permissions before
                    # returning them, exactly as for a successful terminal.
                    if trace_id:
                        try:
                            value["trace"] = await asyncio.to_thread(
                                application.get_accessible_trace, principal.access, trace_id)
                        except Exception:
                            pass
                    queue.put_nowait(("error", value))

            task = asyncio.create_task(run_query())
            try:
                while True:
                    event, value = await queue.get()
                    yield f"event: {event}\ndata: {json.dumps(value, ensure_ascii=False, allow_nan=False)}\n\n"
                    if event in {"result", "error"}:
                        break
            finally:
                if not task.done():
                    task.cancel()
                # Starlette cancels its stream task group on disconnect. Shield
                # cleanup from repeated level cancellation while the pipeline
                # writes its bounded cancellation trace.
                with CancelScope(shield=True):
                    with suppress(asyncio.CancelledError):
                        await task

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no"})

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
    def trace(trace_id: str, principal: User):
        return application.get_accessible_trace(principal.access, trace_id)

    @api.get("/evidence/{generation}/{evidence_id}")
    def evidence(generation: str, evidence_id: str, principal: User):
        return asdict(application.get_evidence(principal.access, generation, evidence_id))

    return api
