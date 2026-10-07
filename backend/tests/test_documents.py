import io

import docx


def _docx_bytes(text: str) -> bytes:
    d = docx.Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


async def test_upload_txt_and_docx(authed):
    r = await authed.post("/api/documents", files={"file": ("resume.txt", b"Python engineer", "text/plain")})
    assert r.status_code == 201
    body = r.json()
    assert body["id"].startswith("doc_") and body["chars"] == len("Python engineer")
    assert body["tags"] == {"source": "upload", "kind": "resume", "ext": "txt"}

    r = await authed.post(
        "/api/documents",
        data={"kind": "portfolio"},
        files={"file": ("cv.docx", _docx_bytes("Built things at Acme"), "application/octet-stream")},
    )
    assert r.status_code == 201 and r.json()["kind"] == "portfolio" and r.json()["chars"] > 0
    assert len((await authed.get("/api/documents")).json()) == 2


async def test_retrieval_endpoints(authed):
    doc = (await authed.post("/api/documents", files={"file": ("my resume.txt", b"Go and SQL", "text/plain")})).json()
    assert (await authed.get(f"/api/documents/{doc['id']}")).json()["filename"] == "my resume.txt"
    assert (await authed.get(f"/api/documents/{doc['id']}/text")).text == "Go and SQL"
    dl = await authed.get(f"/api/documents/{doc['id']}/download")
    assert dl.content == b"Go and SQL" and "my%20resume.txt" in dl.headers["content-disposition"]


async def test_tags_and_filtering(authed):
    a = (await authed.post("/api/documents", files={"file": ("a.txt", b"aaa", "text/plain")})).json()
    b = (await authed.post("/api/documents", files={"file": ("b.txt", b"bbb", "text/plain")})).json()
    r = await authed.put(f"/api/documents/{b['id']}/tags", json={"target": "backend"})
    assert r.status_code == 200 and r.json()["tags"]["target"] == "backend"
    assert [d["id"] for d in (await authed.get("/api/documents", params={"tag": "target:backend"})).json()] == [b["id"]]
    assert {d["id"] for d in (await authed.get("/api/documents", params={"tag": "source:upload"})).json()} == {a["id"], b["id"]}
    assert (await authed.get("/api/documents", params={"tag": "nocolon"})).status_code == 422
    assert (await authed.put(f"/api/documents/{b['id']}/tags", json={"kind": "x"})).status_code == 422


async def test_duplicate_upload_returns_existing(authed):
    first = (await authed.post("/api/documents", files={"file": ("a.txt", b"same bytes", "text/plain")})).json()
    second = (await authed.post("/api/documents", files={"file": ("b.txt", b"same bytes", "text/plain")})).json()
    assert second["id"] == first["id"] and second["duplicate"] is True
    assert len((await authed.get("/api/documents")).json()) == 1


async def test_upload_rejects_bad_input(authed):
    r = await authed.post("/api/documents", files={"file": ("x.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 415
    r = await authed.post("/api/documents", data={"kind": "nope"}, files={"file": ("a.txt", b"hi", "text/plain")})
    assert r.status_code == 422
    r = await authed.post("/api/documents", files={"file": ("bad.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 422
    assert (await authed.get("/api/documents")).json() == []  # nothing stored for rejected uploads


async def test_other_users_documents_are_invisible(authed, client, make_account):
    doc = (await authed.post("/api/documents", files={"file": ("a.txt", b"hi", "text/plain")})).json()
    await client.post("/api/auth/logout")
    await make_account("o@x.com", "password-oooo")
    for path in ("", "/download", "/text"):
        assert (await client.get(f"/api/documents/{doc['id']}{path}")).status_code == 404
    assert (await client.delete(f"/api/documents/{doc['id']}")).status_code == 404
    assert (await client.get("/api/documents/../../etc/passwd")).status_code in (404, 405)
    assert (await client.get("/api/documents")).json() == []


async def test_delete_document(authed):
    doc = (await authed.post("/api/documents", files={"file": ("a.txt", b"hi", "text/plain")})).json()
    assert (await authed.delete(f"/api/documents/{doc['id']}")).status_code == 204
    assert (await authed.get(f"/api/documents/{doc['id']}")).status_code == 404


# ── real PDFs, page cap, timeout (CR-06 / GAP-01) ───────────────────────
async def test_real_pdf_is_parsed(authed):
    from pdfs import make_pdf

    r = await authed.post("/api/documents", files={"file": ("cv.pdf", make_pdf(["Jane Doe Backend Engineer"]), "application/pdf")})
    assert r.status_code == 201
    text = (await authed.get(f"/api/documents/{r.json()['id']}/text")).text
    assert "Jane Doe Backend Engineer" in text


def test_pdf_page_cap(monkeypatch):
    from pdfs import make_pdf

    from jobfinder.profile import resume_parse

    monkeypatch.setattr(resume_parse, "MAX_PDF_PAGES", 3)
    text = resume_parse.extract_text(make_pdf([f"page{i}" for i in range(10)]), ".pdf")
    assert "page2" in text and "page3" not in text


async def test_corrupt_pdf_is_a_422(authed):
    r = await authed.post("/api/documents", files={"file": ("cv.pdf", b"%PDF-1.4 not really", "application/pdf")})
    assert r.status_code == 422


async def _parser_idle():
    """Parses that timed out keep running to the end; wait until their slots are back so tests don't leak into each other."""
    import asyncio

    from jobfinder.profile import resume_parse

    for _ in range(100):
        if resume_parse._slots._value == resume_parse.MAX_PARSES:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("parser slots never came back")


async def test_slow_parse_times_out_without_blocking_the_api(authed, monkeypatch):
    import asyncio
    import time

    from jobfinder.profile import resume_parse

    monkeypatch.setattr(resume_parse, "PARSE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(resume_parse, "extract_text", lambda data, ext: time.sleep(1) or "late")
    started = asyncio.get_running_loop().time()
    upload = asyncio.create_task(authed.post("/api/documents", files={"file": ("a.txt", b"hi", "text/plain")}))
    await asyncio.sleep(0.05)
    assert (await authed.get("/api/auth/me")).status_code == 200  # still answering while the parse runs
    r = await upload
    assert r.status_code == 422 and asyncio.get_running_loop().time() - started < 0.9
    await _parser_idle()


async def test_stuck_parses_keep_their_slots_so_uploads_get_503_not_more_threads(authed, monkeypatch):
    import threading

    from jobfinder.profile import resume_parse

    await _parser_idle()
    release = threading.Event()
    monkeypatch.setattr(resume_parse, "PARSE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(resume_parse, "extract_text", lambda data, ext: release.wait(5) and "late")
    try:
        for i in range(resume_parse.MAX_PARSES):  # each times out for the client but its thread is still running
            assert (await authed.post("/api/documents", files={"file": (f"{i}.txt", b"hi", "text/plain")})).status_code == 422
        r = await authed.post("/api/documents", files={"file": ("third.txt", b"hi", "text/plain")})
        assert r.status_code == 503 and "try again" in r.json()["detail"]
        assert (await authed.get("/api/auth/me")).status_code == 200
    finally:
        release.set()
    await _parser_idle()  # slots come back once the threads really finish
    monkeypatch.undo()
    assert (await authed.post("/api/documents", files={"file": ("ok.txt", b"hello", "text/plain")})).status_code == 201
