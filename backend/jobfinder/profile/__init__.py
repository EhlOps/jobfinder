"""profile: reading what a user gives us about themselves into plain text.

    formats       the upload file types and the content type each is stored with (no parser imports)
    resume_parse  text from PDF, DOCX, TXT and Markdown files, size- and time-capped, off the event loop
    web_page      a public web page as text, refusing internal addresses on every redirect hop
"""
