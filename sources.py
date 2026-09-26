"""Turn a link or an uploaded document into input for the text model."""

import base64
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlparse

MAX_SOURCE_CHARS = 15_000  # enough for an article, keeps the request cheap
MAX_DOWNLOAD_BYTES = 3_000_000
DOCUMENT_TYPES = ["pdf", "txt", "md"]


class _ArticleText(HTMLParser):
    """Collects the readable text of a web page (title, headings, paragraphs, lists)."""

    READABLE = {"title", "h1", "h2", "h3", "p", "li", "blockquote"}
    SKIPPED = {"script", "style", "nav", "footer", "header", "aside", "form", "noscript"}

    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[str] = []
        self._readable_depth = 0
        self._skipped_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIPPED:
            self._skipped_depth += 1
        elif tag in self.READABLE:
            self._readable_depth += 1
            self.blocks.append("")

    def handle_endtag(self, tag):
        if tag in self.SKIPPED and self._skipped_depth:
            self._skipped_depth -= 1
        elif tag in self.READABLE and self._readable_depth:
            self._readable_depth -= 1

    def handle_data(self, data):
        if self._readable_depth and not self._skipped_depth and self.blocks:
            self.blocks[-1] += data

    def text(self) -> str:
        lines = [" ".join(block.split()) for block in self.blocks]
        return "\n".join(line for line in lines if len(line) > 1)


def is_valid_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def fetch_article(url: str) -> str:
    """Download a web page and return its readable text. Raises ValueError (pt-PT) on failure."""
    request = urllib.request.Request(
        url.strip(),
        headers={"User-Agent": "Mozilla/5.0 (PostMind; +https://postmind.local)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read(MAX_DOWNLOAD_BYTES)
            charset = response.headers.get_content_charset() or "utf-8"
    except Exception as error:
        raise ValueError(f"Não consegui abrir o link ({error}).") from error

    parser = _ArticleText()
    parser.feed(raw.decode(charset, errors="replace"))
    text = parser.text()
    if len(text) < 200:
        raise ValueError(
            "Não consegui ler o texto desta página (pode exigir login ou bloquear leitores). "
            "Experimenta copiar o texto para o campo do tema."
        )
    return text[:MAX_SOURCE_CHARS]


def document_input(name: str, data: bytes) -> dict:
    """Content block for the model: PDFs are sent as files, text files as text."""
    if name.lower().endswith(".pdf"):
        encoded = base64.b64encode(data).decode("ascii")
        return {
            "type": "input_file",
            "filename": name,
            "file_data": f"data:application/pdf;base64,{encoded}",
        }
    text = data.decode("utf-8", errors="replace")[:MAX_SOURCE_CHARS]
    return {"type": "input_text", "text": f"Documento «{name}»:\n{text}"}
