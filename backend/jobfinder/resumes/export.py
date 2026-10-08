"""ATS-safe resume export: single column, standard headings, no tables/images/text boxes.

The renderers take any object shaped like the TailoredResume schema (attribute access):
  contact {name, email, phone, location, links[]}, summary, skills[],
  experience[{company, role, start, end, location, bullets[]}], projects[{name, bullets[], link}],
  education[{school, degree, dates, details[]}].
All field access goes through `_sections`, so adjusting names to the real schema happens there.
"""

import io
from xml.sax.saxutils import escape

from docx.shared import Pt
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer

from jobfinder.letters.docx_export import new_document, to_bytes

HEADINGS = ("Summary", "Skills", "Experience", "Projects", "Education")


def _join(parts, sep=" | ") -> str:
    return sep.join(str(p).strip() for p in parts if p and str(p).strip())


def _sections(resume) -> tuple[str, list[str], list[tuple[str, list[tuple[str, list[str]]]]]]:
    """Normalise to (name, contact lines, [(heading, [(entry line, bullets)])]); empty sections are dropped."""
    c = resume.contact
    contact = [_join([c.email, c.phone, c.location]), _join(c.links or [])]
    sections: list[tuple[str, list[tuple[str, list[str]]]]] = []
    if resume.summary:
        sections.append(("Summary", [(resume.summary, [])]))
    if resume.skills:
        sections.append(("Skills", [(", ".join(resume.skills), [])]))
    if resume.experience:
        sections.append(
            (
                "Experience",
                [
                    (
                        _join([e.role, e.company, e.location, _join([e.start, e.end], " - ")]),
                        list(e.bullets or []),
                    )
                    for e in resume.experience
                ],
            )
        )
    if resume.projects:
        sections.append(
            (
                "Projects",
                [(_join([p.name, p.link]), list(p.bullets or [])) for p in resume.projects],
            )
        )
    if resume.education:
        sections.append(
            (
                "Education",
                [
                    (_join([e.degree, e.school, e.dates]), list(e.details or []))
                    for e in resume.education
                ],
            )
        )
    return c.name, [x for x in contact if x], sections


def resume_to_text(resume) -> str:
    name, contact, sections = _sections(resume)
    lines = [name, *contact]
    for heading, entries in sections:
        lines += ["", heading]
        for line, bullets in entries:
            lines.append(line)
            lines += [f"- {b}" for b in bullets]
    return "\n".join(lines) + "\n"


def render_docx(resume) -> bytes:
    name, contact, sections = _sections(resume)
    d = new_document()
    title = d.add_paragraph()
    run = title.add_run(name)
    run.bold = True
    run.font.size = Pt(16)
    title.paragraph_format.space_after = Pt(2)
    for line in contact:
        d.add_paragraph(line).paragraph_format.space_after = Pt(0)
    for heading, entries in sections:
        d.add_heading(heading, level=1)
        for line, bullets in entries:
            para = d.add_paragraph(line)
            para.paragraph_format.space_after = Pt(2)
            if bullets:
                para.runs[0].bold = True
            for b in bullets:
                d.add_paragraph(b, style="List Bullet").paragraph_format.space_after = Pt(0)
    # Heading 1 defaults to a colored theme font; keep fonts plain and black for parsers
    st = d.styles["Heading 1"]
    st.font.name = "Calibri"
    st.font.size = Pt(13)
    st.font.bold = True
    st.font.color.rgb = None
    return to_bytes(d)


def render_pdf(resume) -> bytes:
    name, contact, sections = _sections(resume)
    base = ParagraphStyle("base", fontName="Helvetica", fontSize=10.5, leading=13, spaceAfter=2)
    title = ParagraphStyle("title", parent=base, fontName="Helvetica-Bold", fontSize=16, leading=19)
    head = ParagraphStyle(
        "head", parent=base, fontName="Helvetica-Bold", fontSize=12.5, leading=15, spaceBefore=8
    )
    entry = ParagraphStyle("entry", parent=base, fontName="Helvetica-Bold")
    story = [Paragraph(escape(name), title), *(Paragraph(escape(line), base) for line in contact)]
    for heading, entries in sections:
        story.append(Paragraph(heading, head))
        for line, bullets in entries:
            story.append(Paragraph(escape(line), entry if bullets else base))
            if bullets:
                story.append(
                    ListFlowable(
                        [ListItem(Paragraph(escape(b), base)) for b in bullets],
                        bulletType="bullet",
                        start="-",
                        leftIndent=14,
                    )
                )
        story.append(Spacer(1, 2))
    buf = io.BytesIO()
    SimpleDocTemplate(
        buf,
        pagesize=LETTER,
        leftMargin=inch,
        rightMargin=inch,
        topMargin=inch,
        bottomMargin=inch,
        title=name,
    ).build(story)
    return buf.getvalue()
