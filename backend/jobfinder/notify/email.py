"""Outbound email over plain SMTP: Mailpit in dev, the `smtp` (postfix) service in production."""
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

import aiosmtplib

from jobfinder.config import get_settings


class EmailError(Exception):
    pass


def build_message(
    to: str, subject: str, text: str, html: str | None = None, headers: dict[str, str] | None = None
) -> EmailMessage:
    s = get_settings()
    # Long single-token headers (List-Unsubscribe URLs) must stay plain `<url>`; the default 78-char limit
    # would turn them into RFC 2047 encoded words that mail clients don't treat as a link.
    msg = EmailMessage(policy=policy.default.clone(max_line_length=998))
    name, addr = parseaddr(s.email_from)
    msg["From"] = formataddr((name, addr)) if name else addr
    msg["To"] = to
    msg["Subject"] = subject
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


async def send_email(
    to: str, subject: str, text: str, html: str | None = None, headers: dict[str, str] | None = None
) -> None:
    s = get_settings()
    msg = build_message(to, subject, text, html, headers)
    try:
        await aiosmtplib.send(
            msg,
            hostname=s.smtp_host,
            port=s.smtp_port,
            username=s.smtp_user or None,
            password=s.smtp_password or None,
            start_tls=bool(s.smtp_starttls),
            timeout=30,
        )
    except (aiosmtplib.SMTPException, OSError) as e:
        raise EmailError(f"Could not send email via {s.smtp_host}:{s.smtp_port}: {e}") from e
