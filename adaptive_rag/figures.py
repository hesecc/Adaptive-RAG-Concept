"""Figures inside documents: the text marker, the evidence view and the stored bytes.

A figure travels with its document as one Markdown image line,

    ![Figure 1: Transformer 模型架构。](ragfig:<sha256> "第 3 页")

so the caption is ordinary chunk text (searched, embedded, cited) and the figure
stays with the passage around it. The image bytes sit in the content-addressed
object store under the same sha256. Nothing else records the link: the document
revision is the source of truth, and the evidence built from it lists its figures.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import re
import unicodedata

from .domain import EvidenceImage, ValidationError

MAX_CAPTION = 160

MARKER = re.compile(
    r'!\[(?P<caption>[^\[\]\n]*)\]\(ragfig:(?P<key>[0-9a-f]{64})(?: "第 (?P<page>[0-9]{1,5}) 页")?\)')
# How MinerU's full.md points at the pictures it exported next to it.
ARCHIVE_REFERENCE = re.compile(r"!\[[^\[\]\n]*\]\((?P<path>images/[^()\s]+)\)")


@dataclass(frozen=True)
class Figure:
    """One picture a parser found in a document, with what the parser knows about it."""
    path: str                              # how the parsed Markdown refers to it
    content: bytes = field(repr=False)
    caption: str = ""
    page: int | None = None                # 1-based page of the source PDF


def clean_caption(value: str) -> str:
    """One line without brackets, so the caption cannot end the marker early."""
    text = unicodedata.normalize("NFC", value)
    text = re.sub(r"[\x00-\x1f\x7f\\]+", " ", text).replace("[", "(").replace("]", ")")
    text = " ".join(text.split())
    return text if len(text) <= MAX_CAPTION else text[:MAX_CAPTION - 1].rstrip() + "…"


def marker(key: str, caption: str = "", page: int | None = None) -> str:
    title = f' "第 {page} 页"' if page else ""
    return f"![{clean_caption(caption)}](ragfig:{key}{title})"


def find_figures(text: str) -> tuple[EvidenceImage, ...]:
    """The figures a passage shows, in reading order, one entry per image."""
    found: dict[str, EvidenceImage] = {}
    for match in MARKER.finditer(text):
        found.setdefault(match["key"], EvidenceImage(
            match["key"], match["caption"].strip(), int(match["page"]) if match["page"] else None))
    return tuple(found.values())


def cut_before_figure(text: str, length: int) -> int:
    """`length`, moved back to the start of the figure line it would split.

    A passage cut inside a marker would show a stray `![...](ragfig:ab12` and
    lose the figure. A marker at the very start is cut as asked, so the result
    is never zero for a positive `length`.
    """
    for match in MARKER.finditer(text):
        if match.start() < length < match.end():
            return match.start() or length
        if match.start() >= length:
            break
    return length


def readable_text(text: str) -> str:
    """Wording for a model: the caption and page of each figure, never the storage key."""
    def say(match):
        page = f"第 {match['page']} 页" if match["page"] else ""
        detail = "，".join(part for part in (match["caption"].strip(), page) if part)
        return f"［插图：{detail}］" if detail else "［插图］"
    return MARKER.sub(say, text)


def plain_text(text: str) -> str:
    """Text for word matching: figure lines reduced to their captions."""
    return MARKER.sub(lambda match: match["caption"], text)


def sniff_media_type(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def attach_figures(markdown: str, figures: Sequence[Figure],
                   store: Callable[[bytes], str]) -> tuple[str, dict[str, int]]:
    """Store the figures the Markdown points at and rewrite each reference into a marker.

    A reference to a figure that is missing, too large or not a static PNG, JPEG or
    WEBP is removed; the text around it, caption included, stays.
    """
    from .assets import image_info
    by_path = {figure.path: figure for figure in figures}
    keys: dict[str, str | None] = {}

    def replace(match):
        path = match["path"]
        if path not in keys:
            figure = by_path.get(path)
            keys[path] = None
            if figure is not None:
                try:
                    image_info(figure.content)
                except ValidationError:
                    pass
                else:
                    keys[path] = store(figure.content)
        key = keys[path]
        return marker(key, by_path[path].caption, by_path[path].page) if key else ""

    rewritten = ARCHIVE_REFERENCE.sub(replace, markdown)
    stored = sum(key is not None for key in keys.values())
    return rewritten, {"referenced": len(keys), "stored": stored, "skipped": len(keys) - stored}
