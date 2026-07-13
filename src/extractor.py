"""Extracts raw text + basic metadata from incoming files.
No interpretation happens here -- just getting text out reliably.
"""
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class ExtractedDoc:
    source_path: str
    text: str
    file_type: str
    metadata: dict = field(default_factory=dict)
    source_url: str | None = None   # if known, lets you repull this later instead of storing the file


def extract(file_path: str) -> ExtractedDoc:
    p = Path(file_path)
    ext = p.suffix.lower()

    if ext == ".pdf":
        return _extract_pdf(p)
    elif ext in (".docx",):
        return _extract_docx(p)
    elif ext in (".html", ".htm", ".webarchive", ".mhtml"):
        return _extract_html(p)
    elif ext in (".txt", ".md"):
        return ExtractedDoc(str(p), p.read_text(encoding="utf-8", errors="ignore"), "text")
    else:
        raise ValueError(f"Unsupported file type: {ext}")


def _extract_pdf(p: Path) -> ExtractedDoc:
    from pypdf import PdfReader
    reader = PdfReader(str(p))
    text_parts = [(page.extract_text() or "") for page in reader.pages]
    meta = dict(reader.metadata) if reader.metadata else {}
    meta["page_count"] = len(reader.pages)
    return ExtractedDoc(str(p), "\n".join(text_parts), "pdf", meta)	

def _extract_docx(p: Path) -> ExtractedDoc:
    import docx
    d = docx.Document(p)
    text = "\n".join(para.text for para in d.paragraphs)
    core = d.core_properties
    meta = {
        "author": core.author,
        "created": str(core.created) if core.created else None,
        "title": core.title,
    }
    return ExtractedDoc(str(p), text, "docx", meta)


def _extract_html(p: Path) -> ExtractedDoc:
    from bs4 import BeautifulSoup
    raw = p.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(raw, "lxml")
    title = soup.title.string if soup.title else None

    # Try to recover a re-pullable URL: canonical link, og:url, then any
    # absolute http(s) link found near the top of the saved page.
    source_url = None
    canonical = soup.find("link", rel="canonical")
    if canonical and canonical.get("href"):
        source_url = canonical["href"]
    if not source_url:
        og_url = soup.find("meta", property="og:url")
        if og_url and og_url.get("content"):
            source_url = og_url["content"]

    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return ExtractedDoc(str(p), text, "html", {"title": title}, source_url=source_url)
