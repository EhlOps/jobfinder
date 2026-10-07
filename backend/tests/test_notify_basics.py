import aiosmtplib
import pytest

from jobfinder.config import get_settings
from jobfinder.notify import email as mail
from jobfinder.notify import tokens


def test_token_round_trip_and_purpose_isolation():
    t = tokens.make_token("questions", 42)
    assert tokens.read_token("questions", t) == 42
    with pytest.raises(tokens.InvalidToken):
        tokens.read_token("unsubscribe", t)          # a question link can't unsubscribe anyone


def test_token_tampering_and_garbage_rejected():
    t = tokens.make_token("questions", 42)
    for bad in (t[:-3] + "xyz", "garbage", "", t + "A", "a.b.c"):
        with pytest.raises(tokens.InvalidToken) as e:
            tokens.read_token("questions", bad)
        assert e.value.expired is False


def test_token_signed_with_another_secret_is_rejected(monkeypatch):
    t = tokens.make_token("questions", 7)
    monkeypatch.setattr(get_settings(), "secret_key", "a-different-secret")
    with pytest.raises(tokens.InvalidToken):
        tokens.read_token("questions", t)


def test_expired_token_is_reported_as_expired(monkeypatch):
    t = tokens.make_token("questions", 1)
    monkeypatch.setitem(tokens.MAX_AGE, "questions", -1)
    with pytest.raises(tokens.InvalidToken) as e:
        tokens.read_token("questions", t)
    assert e.value.expired is True


def test_build_message_is_multipart_with_headers(monkeypatch):
    monkeypatch.setattr(get_settings(), "email_from", "JobFinder <jobs@example.com>")
    msg = mail.build_message("me@x.com", "Subject here", "plain body", "<p>html body</p>", {"List-Unsubscribe": "<https://u>"})
    assert msg["To"] == "me@x.com" and msg["Subject"] == "Subject here" and "jobs@example.com" in msg["From"]
    assert msg["List-Unsubscribe"] == "<https://u>"
    assert msg.get_body(("plain",)).get_content().strip() == "plain body"
    assert "html body" in msg.get_body(("html",)).get_content()


async def test_send_email_passes_smtp_settings(monkeypatch):
    seen = {}

    async def fake_send(msg, **kw):
        seen.update(kw, to=msg["To"])

    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    s = get_settings()
    monkeypatch.setattr(s, "smtp_host", "smtp"), monkeypatch.setattr(s, "smtp_port", 587)
    monkeypatch.setattr(s, "smtp_user", ""), monkeypatch.setattr(s, "smtp_starttls", False)
    await mail.send_email("me@x.com", "s", "t")
    assert seen["hostname"] == "smtp" and seen["port"] == 587 and seen["username"] is None and seen["start_tls"] is False
    assert seen["to"] == "me@x.com"


@pytest.mark.parametrize("exc", [aiosmtplib.SMTPException("rejected"), OSError("connection refused")])
async def test_send_failures_become_email_error(monkeypatch, exc):
    async def boom(msg, **kw):
        raise exc

    monkeypatch.setattr(aiosmtplib, "send", boom)
    with pytest.raises(mail.EmailError):
        await mail.send_email("me@x.com", "s", "t")


def test_long_list_unsubscribe_header_stays_a_plain_url():
    url = "https://jobs.example.com/api/unsubscribe/" + "x" * 120
    msg = mail.build_message("me@x.com", "s", "t", "<p>h</p>", {"List-Unsubscribe": f"<{url}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
    raw = msg.as_string()
    assert f"List-Unsubscribe: <{url}>" in raw and "=?utf-8?" not in raw.split("\n\n")[0]
