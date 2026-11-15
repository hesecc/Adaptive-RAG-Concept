"""Trusted local management CLI. HTTP authentication is a separate adapter."""

import argparse
import asyncio
from dataclasses import asdict, is_dataclass, replace
import json
from pathlib import Path
import sys

from .app import RagApplication
from .config import load_settings
from .domain import AccessContext, IngestRequest, QueryRequest, RagError, ValidationError
from .evaluation import run_evaluation


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValidationError(message)


def parser() -> argparse.ArgumentParser:
    root = JsonArgumentParser(prog="adaptive-rag", description="Local model-free RAG foundation")
    root.add_argument("--data-dir", type=Path)
    root.add_argument("--config", type=Path)
    commands = root.add_subparsers(dest="command", required=True)

    def tenant_command(name, **kwargs):
        command = commands.add_parser(name, **kwargs)
        command.add_argument("--tenant", required=True)
        return command

    command = tenant_command("ingest")
    command.add_argument("--document", required=True)
    command.add_argument("--version", required=True)
    command.add_argument("--file", required=True, type=Path)
    command.add_argument("--title")
    command.add_argument("--sku", action="append", default=[])
    command.add_argument("--allow", action="append", default=[])
    command.add_argument("--language", default="zh")

    command = tenant_command("publish")
    command.add_argument("--job", required=True)
    command.add_argument("--expected-generation")

    command = tenant_command("query")
    command.add_argument("--question", required=True)
    command.add_argument("--sku")
    command.add_argument("--generation")
    command.add_argument("--top-k", type=int, default=5)
    command.add_argument("--subject", default="local")
    command.add_argument("--group", action="append", default=[])

    commands.add_parser("capabilities")
    for name in ("job", "trace"):
        command = tenant_command(name)
        command.add_argument("--id", required=True)
    command = tenant_command("evidence")
    command.add_argument("--generation", required=True)
    command.add_argument("--id", required=True)
    command.add_argument("--subject", default="local")
    command.add_argument("--group", action="append", default=[])

    command = tenant_command("delete")
    command.add_argument("--document", required=True)
    command.add_argument("--expected-generation")
    command = tenant_command("rollback")
    command.add_argument("--generation", required=True)
    command.add_argument("--expected-generation")
    command = tenant_command("access")
    command.add_argument("--document", required=True)
    command.add_argument("--allow", action="append", default=[])
    command.add_argument("--revoked", action="store_true")

    command = tenant_command("eval")
    command.add_argument("--fixture", required=True, type=Path)
    command.add_argument("--output", required=True, type=Path)
    command.add_argument("--generation")
    command.add_argument("--top-k", type=int, default=5)
    command.add_argument("--subject", default="local")
    command.add_argument("--group", action="append", default=[])
    commands.add_parser("serve")
    return root


def _access(args) -> AccessContext:
    return AccessContext(args.tenant, args.subject, tuple(args.group))


def dispatch(args):
    settings = load_settings(args.config)
    if args.data_dir is not None:
        settings = replace(settings, data_dir=args.data_dir)
    if args.command == "serve":
        from .api import create_api
        import uvicorn
        api = create_api(settings)
        uvicorn.run(api, host=settings.host, port=settings.port, access_log=False)
        return {"status": "stopped"}
    application = RagApplication(settings)
    if args.command == "ingest":
        media_type = {".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain",
                      ".html": "text/html", ".htm": "text/html"}.get(args.file.suffix.lower())
        if media_type is None:
            raise ValidationError("Supported source extensions: .md, .markdown, .txt, .html, .htm")
        try:
            if args.file.stat().st_size > 8_000_000:
                raise ValidationError("Source file exceeds the 8 MB input limit")
            content = args.file.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValidationError("Cannot read source file as UTF-8") from exc
        return application.ingest(IngestRequest(
            tenant_id=args.tenant, document_id=args.document, version=args.version, content=content,
            media_type=media_type, title=args.title or args.file.stem,
            scope="sku" if args.sku else "store", sku_ids=tuple(args.sku),
            allowed_subjects=tuple(args.allow), language=args.language,
        ))
    if args.command == "publish":
        return application.publish(args.tenant, args.job, args.expected_generation)
    if args.command == "query":
        return asyncio.run(application.query(QueryRequest(_access(args), args.question, sku=args.sku, generation=args.generation, top_k=args.top_k)))
    if args.command == "capabilities":
        return application.capabilities()
    if args.command == "job":
        return application.get_job(args.tenant, args.id)
    if args.command == "trace":
        return application.get_trace(args.tenant, args.id)
    if args.command == "evidence":
        return application.get_evidence(_access(args), args.generation, args.id)
    if args.command == "delete":
        return application.delete_document(args.tenant, args.document, args.expected_generation)
    if args.command == "rollback":
        return application.rollback(args.tenant, args.generation, args.expected_generation)
    if args.command == "access":
        application.set_access(args.tenant, args.document, tuple(args.allow), args.revoked)
        return {"status": "updated"}
    if args.command == "eval":
        return asyncio.run(run_evaluation(application, args.fixture, _access(args), generation=args.generation, top_k=args.top_k, output_path=args.output))
    raise ValidationError("Unknown command")


def main(argv: list[str] | None = None) -> int:
    try:
        result = dispatch(parser().parse_args(argv))
        print(json.dumps(asdict(result) if is_dataclass(result) else result, ensure_ascii=False))
        return 0
    except RagError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception:
        print(json.dumps({"error": {"code": "internal_error", "message": "Internal application error"}}), file=sys.stderr)
        return 1
