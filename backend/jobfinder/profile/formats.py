"""The file types a user may upload, and the content type each is stored with. Kept apart from
resume_parse so the api can check an upload without importing the parsers (#51)."""

CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
}
ALLOWED_EXTENSIONS = set(CONTENT_TYPES)
