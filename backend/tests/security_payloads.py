"""Hostile inputs shared by tests: anything a user pastes or a scraped page contains must stay inert."""

SQLI = [
    "acme';DROP TABLE companies;--",
    "x' OR '1'='1",
    "1;DELETE FROM job_links",
    "\\'; SELECT pg_sleep(5);--",
    "acme\" UNION SELECT password_hash FROM users--",
]

# Must never reach an href or be followed as a link.
SCRIPT_URLS = [
    "javascript:alert(1)",
    "JaVaScRiPt:alert(document.cookie)",
    " javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "//evil.example/x",
]

XSS_TEXT = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "\"><svg/onload=alert(1)>",
]

# Must never be fetched (SSRF). Hostnames here resolve to non-public addresses.
PRIVATE_URLS = [
    "http://127.0.0.1/",
    "http://localhost:8000/",
    "http://10.0.0.5/admin",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/",
    "http://0.0.0.0/",
]
