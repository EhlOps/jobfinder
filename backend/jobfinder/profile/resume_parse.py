"""Extract plain text from uploaded resume/portfolio files."""
import asyncio
import io
from itertools import islice

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}
MAX_CHARS = 60_000
MAX_PDF_PAGES = 30  # a resume is a few pages; anything longer is cut off, not parsed to the end
PARSE_TIMEOUT_S = 20.0
_slots = asyncio.Semaphore(2)  # at most two uploads being parsed at once, so a burst can't swamp the threads


def extract_text(data: bytes, ext: str) -> str:
    ext = ext.lower()
    if ext == ".pdf":
        import pdfplumber

        parts, total = [], 0
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in islice(pdf.pages, MAX_PDF_PAGES):
                parts.append(page.extract_text() or "")
                total += len(parts[-1])
                if total > MAX_CHARS:
                    break
        text = "\n\n".join(parts)
    elif ext == ".docx":
        import docx

        d = docx.Document(io.BytesIO(data))
        parts = [p.text for p in d.paragraphs]
        for table in d.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
        text = "\n".join(parts)
    elif ext in {".txt", ".md"}:
        text = data.decode("utf-8", errors="replace")
    else:
        raise ValueError(f"Unsupported file type: {ext}")
    return text.strip()[:MAX_CHARS]


async def extract_text_async(data: bytes, ext: str) -> str:
    """extract_text in a worker thread with a time limit, so a heavy or crafted file can't stall the API.
    (A thread can't be killed: on timeout the request is answered and the parse finishes in the background,
    bounded by the page cap above.)"""
    async with _slots:
        return await asyncio.wait_for(asyncio.to_thread(extract_text, data, ext), PARSE_TIMEOUT_S)
