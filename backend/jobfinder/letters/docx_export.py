import io
import re

import docx
from docx.shared import Inches, Pt


def build_docx(content: str) -> bytes:
    """A plain, ATS-friendly Word letter: Calibri 11, 1-inch margins, blank-line separated paragraphs."""
    d = docx.Document()
    for section in d.sections:
        section.top_margin = section.bottom_margin = section.left_margin = section.right_margin = Inches(1)
    style = d.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = block.strip().split("\n")
        para = d.add_paragraph()
        para.paragraph_format.space_after = Pt(10)
        for i, line in enumerate(lines):
            run = para.add_run(line.strip())
            if i < len(lines) - 1:
                run.add_break()  # keep "Sincerely,\nName" on adjacent lines
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def filename(company: str, title: str) -> str:
    base = re.sub(r"[^A-Za-z0-9 ._-]+", "", f"Cover Letter - {company} - {title}").strip()
    base = re.sub(r"\s+", " ", base)[:120]
    return f"{base}.docx"
