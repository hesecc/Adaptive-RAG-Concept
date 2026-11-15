"""Deterministic, model-free ingestion with explicit parser and storage seams.

Locators refer to source line ranges and, after chunking, half-open character
ranges in the cleaned section. Raw UTF-8 input is archived separately; retrieval
prefixes are never written into the citable chunk text.
"""

from collections.abc import Sequence
from dataclasses import asdict
from hashlib import sha256
from html.parser import HTMLParser
import re
import unicodedata

from .domain import (
    ChildChunk, IngestRequest, IngestResult, ParentChunk, PreparedDocument,
    Section, ValidationError, stable_id,
)
from .ports import Chunker, Cleaner, KnowledgeStore, ObjectStore, Parser


def _digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


class _SafeHTML(HTMLParser):
    """Read HTML text only. No script execution, URL resolution, or fetching."""

    _blocked = frozenset({
        "script", "style", "svg", "noscript", "head", "template", "iframe", "object",
    })
    _blocks = frozenset({
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
        "figcaption", "figure", "footer", "hr", "li", "main", "ol", "p", "pre",
        "section", "table", "tr", "ul",
    })

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sections: list[Section] = []
        self._parts: list[str] = []
        self._headings: list[tuple[int, str]] = []
        self._heading_parts: list[str] | None = None
        self._heading_level = 0
        self._skip: list[str] = []
        self._start_line: int | None = None

    def _line_break(self) -> None:
        if self._parts and not self._parts[-1].endswith("\n"):
            self._parts.append("\n")

    def _flush(self) -> None:
        text = "".join(self._parts).strip()
        if text:
            self.sections.append(Section(
                heading_path=tuple(title for _, title in self._headings),
                text=text,
                locator=f"lines:{self._start_line or 1}-{self.getpos()[0]}",
            ))
        self._parts = []
        self._start_line = None

    def handle_starttag(self, tag, attrs):
        if tag in self._blocked:
            self._skip.append(tag)
            return
        if self._skip:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self._flush()
            self._heading_level = int(tag[1])
            self._heading_parts = []
        elif tag in self._blocks:
            self._line_break()
        elif tag in {"td", "th"} and self._parts:
            # Keep cells on the same row distinguishable without inventing facts.
            if not self._parts[-1].endswith("\n"):
                self._parts.append(" | ")

    def handle_endtag(self, tag):
        if self._skip:
            if tag in self._skip:
                index = len(self._skip) - 1 - self._skip[::-1].index(tag)
                del self._skip[index:]
            return
        if re.fullmatch(r"h[1-6]", tag) and self._heading_parts is not None:
            title = " ".join("".join(self._heading_parts).split())
            level = self._heading_level
            while self._headings and self._headings[-1][0] >= level:
                self._headings.pop()
            if title:
                self._headings.append((level, title))
            self._heading_parts = None
        elif tag in self._blocks:
            self._line_break()

    def handle_startendtag(self, tag, attrs):
        if tag not in self._blocked:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self._skip:
            return
        if self._heading_parts is not None:
            self._heading_parts.append(data)
            return
        if data:
            if self._start_line is None and data.strip():
                self._start_line = self.getpos()[0]
            self._parts.append(data)

    def finish(self) -> tuple[Section, ...]:
        self.close()
        self._flush()
        return tuple(self.sections)


class StructuredParser:
    """Parse plain text, ATX-heading Markdown, or inert HTML into sections."""

    fingerprint = "structured-parser-v1"

    def parse(self, request: IngestRequest) -> tuple[Section, ...]:
        if request.media_type == "text/html":
            reader = _SafeHTML()
            reader.feed(request.content)
            return reader.finish()
        if request.media_type == "text/plain":
            return (Section((), request.content, f"lines:1-{len(request.content.splitlines()) or 1}"),)
        return self._markdown(request.content)

    @staticmethod
    def _markdown(content: str) -> tuple[Section, ...]:
        result: list[Section] = []
        headings: list[tuple[int, str]] = []
        body: list[str] = []
        start_line = 1
        fence_char: str | None = None
        fence_length = 0

        def flush(end_line: int) -> None:
            text = "\n".join(body).strip()
            if text:
                result.append(Section(
                    tuple(title for _, title in headings), text,
                    f"lines:{start_line}-{max(start_line, end_line)}",
                ))
            body.clear()

        lines = content.splitlines()
        for line_no, line in enumerate(lines, 1):
            fence = re.match(r"^ {0,3}([`~])\1{2,}", line)
            if fence:
                run = fence.group(0).strip()
                if fence_char is None:
                    fence_char, fence_length = run[0], len(run)
                elif run[0] == fence_char and len(run) >= fence_length:
                    fence_char = None
            heading = re.match(r"^ {0,3}(#{1,6})[ \t]+(.+?)\s*$", line) if fence_char is None and not fence else None
            if heading:
                flush(line_no - 1)
                level = len(heading.group(1))
                while headings and headings[-1][0] >= level:
                    headings.pop()
                title = re.sub(r"[ \t]+#+[ \t]*$", "", heading.group(2)).strip()
                headings.append((level, title))
                start_line = line_no + 1
            else:
                if not body:
                    start_line = line_no
                body.append(line)
        flush(len(lines))
        return tuple(result)


class TextCleaner:
    """Conservative whitespace cleaning; preserve case, numbers and negation."""

    fingerprint = "text-cleaner-v1"

    def clean(self, sections: Sequence[Section]) -> tuple[Section, ...]:
        result = []
        for section in sections:
            text = unicodedata.normalize("NFC", section.text)
            text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")
            text = text.replace("\xa0", " ")
            text = "\n".join(line.rstrip() for line in text.split("\n"))
            text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text).strip()
            if text:
                headings = tuple(" ".join(part.split()) for part in section.heading_path if part.strip())
                result.append(Section(headings, text, section.locator))
        return tuple(result)


def _ranges(text: str, limit: int, overlap: int = 0):
    """Yield bounded slices, preferring element/sentence breaks, always advancing."""
    cursor = 0
    while cursor < len(text):
        start = cursor
        while start < len(text) and text[start].isspace():
            start += 1
        if start >= len(text):
            return
        boundary = min(start + limit, len(text))
        if boundary < len(text):
            floor = start + max(1, limit // 3)
            for separators in (("\n\n",), ("\n",), ("。", "！", "？", "；", ". ", "! ", "? ", "; "), (" ", "\t")):
                candidates = [text.rfind(separator, floor, boundary + 1) + len(separator)
                              for separator in separators
                              if text.rfind(separator, floor, boundary + 1) >= floor]
                candidates = [candidate for candidate in candidates if candidate <= boundary]
                if candidates:
                    boundary = max(candidates)
                    break
        end = boundary
        while end > start and text[end - 1].isspace():
            end -= 1
        if end > start:
            yield start, end
        if boundary >= len(text):
            return
        cursor = max(start + 1, end - overlap)


class ParentChildChunker:
    """Create bounded parent/child text while preserving source associations."""

    def __init__(self, parent_chars: int = 1600, child_chars: int = 320, overlap_chars: int = 40):
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (parent_chars, child_chars, overlap_chars)):
            raise ValidationError("chunk sizes must be integers")
        if not 1 <= child_chars <= parent_chars <= 2_000_000 or not 0 <= overlap_chars < child_chars:
            raise ValidationError("require 1 <= child_chars <= parent_chars <= 2000000 and 0 <= overlap_chars < child_chars")
        self.parent_chars = parent_chars
        self.child_chars = child_chars
        self.overlap_chars = overlap_chars

    @property
    def fingerprint(self) -> str:
        return stable_id("parent-child-v1", self.parent_chars, self.child_chars, self.overlap_chars)

    def chunk(self, request: IngestRequest, revision_id: str, sections: Sequence[Section]) -> tuple[tuple[ParentChunk, ...], tuple[ChildChunk, ...]]:
        parents: list[ParentChunk] = []
        children: list[ChildChunk] = []
        for section_no, section in enumerate(sections):
            prefix = " | ".join(dict.fromkeys((request.title, *section.heading_path)))
            for parent_no, (p_start, p_end) in enumerate(_ranges(section.text, self.parent_chars)):
                body = section.text[p_start:p_end]
                locator = f"{section.locator};chars:{p_start}-{p_end}"
                parent_id = stable_id("parent", request.tenant_id, request.document_id, revision_id,
                                      section_no, parent_no, locator, _digest(body))
                parents.append(ParentChunk(
                    request.tenant_id, request.document_id, request.version, revision_id,
                    parent_id, section.heading_path, body, locator, _digest(body),
                ))
                for child_no, (c_start, c_end) in enumerate(_ranges(body, self.child_chars, self.overlap_chars)):
                    text = body[c_start:c_end]
                    child_locator = f"{section.locator};chars:{p_start + c_start}-{p_start + c_end}"
                    chunk_id = stable_id("child", parent_id, child_no, child_locator, _digest(text))
                    children.append(ChildChunk(
                        request.tenant_id, request.document_id, request.version, revision_id,
                        chunk_id, parent_id, text, f"{prefix}\n{text}", child_locator,
                        request.scope, request.sku_ids, request.language,
                    ))
        return tuple(parents), tuple(children)


def _component_fingerprint(component) -> tuple[str, str]:
    value = getattr(component, "fingerprint", "explicit-config-in-processing-fingerprint")
    if not isinstance(value, str):
        raise ValidationError("ingestion plugin fingerprint must be a string")
    return (f"{type(component).__module__}.{type(component).__qualname__}", value)


class IngestionService:
    """Prepare immutable documents and stage them; publication is separate."""

    def __init__(self, store: KnowledgeStore, objects: ObjectStore, parser: Parser,
                 cleaner: Cleaner, chunker: Chunker, processing_fingerprint: str):
        if not isinstance(processing_fingerprint, str) or not processing_fingerprint.strip():
            raise ValidationError("processing_fingerprint must be a non-empty string")
        self.store = store
        self.objects = objects
        self.parser = parser
        self.cleaner = cleaner
        self.chunker = chunker
        self.processing_fingerprint = stable_id(
            processing_fingerprint, _component_fingerprint(parser),
            _component_fingerprint(cleaner), _component_fingerprint(chunker),
        )

    def ingest(self, request: IngestRequest) -> IngestResult:
        raw = request.content.encode("utf-8")
        content_hash = sha256(raw).hexdigest()
        metadata = asdict(request)
        metadata.pop("content")
        metadata["sku_ids"] = sorted(set(request.sku_ids))
        metadata["allowed_subjects"] = sorted(set(request.allowed_subjects))
        revision_id = stable_id("revision-v1", metadata, content_hash, self.processing_fingerprint)
        raw_key = self.objects.put(raw)
        sections = self.cleaner.clean(self.parser.parse(request))
        if not sections or not any(section.text.strip() for section in sections):
            raise ValidationError("document contains no readable text after parsing and cleaning")
        parents, children = self.chunker.chunk(request, revision_id, sections)
        self._validate_chunks(request, revision_id, sections, parents, children)
        return self.store.stage(PreparedDocument(
            request=request, revision_id=revision_id,
            processing_fingerprint=self.processing_fingerprint, content_hash=content_hash,
            raw_key=raw_key, parents=tuple(parents), children=tuple(children),
        ))

    @staticmethod
    def _validate_chunks(request, revision_id, sections, parents, children):
        if not parents or not children:
            raise ValidationError("chunker must return non-empty parents and children")
        parent_ids = {parent.parent_id for parent in parents}
        if len(parent_ids) != len(parents) or len({child.chunk_id for child in children}) != len(children):
            raise ValidationError("chunker returned duplicate chunk identifiers")
        for chunk in (*parents, *children):
            if (chunk.tenant_id, chunk.document_id, chunk.version, chunk.revision_id) != (
                request.tenant_id, request.document_id, request.version, revision_id,
            ):
                raise ValidationError("chunk identity must match document tenant and revision")
            if not chunk.text.strip() or not chunk.locator:
                raise ValidationError("chunks must contain text and a source locator")
        for parent in parents:
            if parent.content_hash != _digest(parent.text):
                raise ValidationError("parent content hash does not match text")
            if not any(parent.text in section.text and parent.heading_path == section.heading_path
                       for section in sections):
                raise ValidationError("parent text must come from a parsed section with the same headings")
        parents_by_id = {parent.parent_id: parent for parent in parents}
        for child in children:
            if child.parent_id not in parent_ids:
                raise ValidationError("child must reference a parent in the same document")
            if child.text not in parents_by_id[child.parent_id].text:
                raise ValidationError("child text must come from its referenced parent")
            if not child.retrieval_text or child.text not in child.retrieval_text:
                raise ValidationError("retrieval text must contain the citable child text")
            if (child.scope, child.sku_ids, child.language) != (request.scope, request.sku_ids, request.language):
                raise ValidationError("child scope, SKU and language must match document metadata")
