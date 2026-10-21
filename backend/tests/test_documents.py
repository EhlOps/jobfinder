import io

import docx
import pytest

from jobfinder.core.config import get_settings
from jobfinder.models import Task
from jobfinder.tasks import runtime


def _docx_bytes(text: str) -> bytes:
    d = docx.Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


async def _post(authed, name: str, data: bytes, ctype: str = "text/plain", **form):
    return await authed.post("/api/documents", data=form or None, files={"file": (name, data, ctype)})


async def _run(maker, task_id: int) -> Task:
    """Deliver the task the way worker-io does (the ledger runtime), and return its row."""
    async with maker() as db:
        kind = (await db.get(Task, task_id)).kind
    await runtime.execute(kind, runtime.REGISTRY[kind], task_id, payload_model=runtime.PAYLOADS[kind], wants_ai=False)
    async with maker() as db:
        return await db.get(Task, task_id)


async def upload(authed, maker, name: str, data: bytes, ctype: str = "text/plain", **form) -> dict:
    """Upload, let the worker read it, and return the document as the API then shows it."""
    r = await _post(authed, name, data, ctype, **form)
    assert r.status_code == 202, r.text
    if r.json()["task_id"]:
        await _run(maker, r.json()["task_id"])
    return (await authed.get(f"/api/documents/{r.json()['document']['id']}")).json()


@pytest.fixture
def sync_uploads(monkeypatch):
    monkeypatch.setattr(get_settings(), "sync_uploads", True)


# ── async upload: the api stores, worker-io reads (#51) ────────────────
async def test_upload_is_processing_until_the_worker_reads_it(authed, maker):
    r = await _post(authed, "resume.txt", b"Python engineer")
    assert r.status_code == 202
    doc, task_id = r.json()["document"], r.json()["task_id"]
    assert doc["status"] == "processing" and doc["chars"] == 0 and doc["duplicate"] is False
    assert doc["tags"] == {"source": "upload", "kind": "resume", "ext": "txt"}
    assert [d["status"] for d in (await authed.get("/api/documents")).json()] == ["processing"]
    async with maker() as db:
        task = await db.get(Task, task_id)
    me = (await authed.get("/api/auth/me")).json()["id"]
    assert (task.kind, task.queue, task.user_id, task.payload) == ("parse_document", "io", me, {"doc_id": doc["id"]})

    done = await _run(maker, task_id)
    assert done.status == "done" and done.result["status"] == "ready"
    assert (await authed.get(f"/api/tasks/{task_id}")).json()["status"] == "done"
    doc = (await authed.get(f"/api/documents/{doc['id']}")).json()
    assert doc["status"] == "ready" and doc["chars"] == len("Python engineer")
    assert (await authed.get(f"/api/documents/{doc['id']}/text")).text == "Python engineer"


async def test_upload_txt_and_docx(authed, maker):
    doc = await upload(authed, maker, "cv.docx", _docx_bytes("Built things at Acme"), "application/octet-stream",
                       kind="portfolio")
    assert doc["kind"] == "portfolio" and doc["chars"] > 0 and doc["status"] == "ready"
    await upload(authed, maker, "resume.txt", b"Python engineer")
    assert len((await authed.get("/api/documents")).json()) == 2


async def test_upload_is_published_on_io_under_the_ledger(authed, published):
    r = await _post(authed, "a.txt", b"hi")
    await published.settle()
    assert published == [("io", "parse_document", r.json()["task_id"])]


async def test_unreadable_file_marks_the_document_error_and_the_task_still_finishes(authed, maker):
    r = await _post(authed, "cv.pdf", b"%PDF-1.4 not really", "application/pdf")
    assert r.status_code == 202
    task = await _run(maker, r.json()["task_id"])
    assert task.status == "done" and task.result["status"] == "error"
    assert task.result["error"] == "Could not read that file"
    doc = (await authed.get(f"/api/documents/{r.json()['document']['id']}")).json()
    assert doc["status"] == "error" and doc["chars"] == 0


async def test_slow_parse_in_the_worker_times_out_as_unreadable(authed, maker, monkeypatch):
    import time

    from jobfinder.profile import resume_parse

    monkeypatch.setattr(resume_parse, "PARSE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(resume_parse, "extract_text", lambda data, ext: time.sleep(0.6) or "late")
    r = await _post(authed, "a.txt", b"hi")
    task = await _run(maker, r.json()["task_id"])
    assert task.status == "done" and task.result["status"] == "error"
    await _parser_idle()


async def test_duplicate_upload_returns_existing(authed, maker):
    first = (await _post(authed, "a.txt", b"same bytes")).json()
    again = (await _post(authed, "b.txt", b"same bytes")).json()  # still processing: the same task
    assert again["document"]["id"] == first["document"]["id"] and again["document"]["duplicate"] is True
    assert again["task_id"] == first["task_id"]
    await _run(maker, first["task_id"])
    third = (await _post(authed, "c.txt", b"same bytes")).json()  # already read: nothing to wait for
    assert third["document"]["status"] == "ready" and third["document"]["duplicate"] and third["task_id"] is None
    assert len((await authed.get("/api/documents")).json()) == 1


async def test_a_document_deleted_before_its_parse_is_skipped(authed, maker):
    r = (await _post(authed, "a.txt", b"bye")).json()
    assert (await authed.delete(f"/api/documents/{r['document']['id']}")).status_code == 204
    task = await _run(maker, r["task_id"])
    assert task.status == "done" and task.result["skipped"] == "deleted"


async def test_the_profile_extract_waits_for_documents_still_processing(authed, maker):
    r = (await _post(authed, "a.txt", b"not read yet")).json()
    resp = await authed.post("/api/onboarding/extract")
    assert resp.status_code == 409 and resp.json()["code"] == "documents_processing"
    await _run(maker, r["task_id"])
    assert (await authed.post("/api/onboarding/extract")).status_code == 202


async def test_an_unreadable_file_uploaded_again_is_read_again(authed, maker, monkeypatch):
    from jobfinder.profile import resume_parse

    monkeypatch.setattr(resume_parse, "extract_text", lambda data, ext: 1 / 0)
    first = (await _post(authed, "a.txt", b"flaky")).json()
    await _run(maker, first["task_id"])
    monkeypatch.setattr(resume_parse, "extract_text", lambda data, ext: data.decode())
    again = (await _post(authed, "a.txt", b"flaky")).json()
    assert again["document"]["status"] == "processing" and again["task_id"] not in (None, first["task_id"])
    await _run(maker, again["task_id"])
    assert (await authed.get(f"/api/documents/{first['document']['id']}")).json()["status"] == "ready"


# ── unchanged behaviour ────────────────────────────────────────────────
async def test_list_hides_tailored_resumes(authed, maker):
    from jobfinder.resumes.service import match_tags
    from jobfinder.storage.documents import get_store

    up = await upload(authed, maker, "a.txt", b"aaa")
    uid = (await authed.get("/api/auth/me")).json()["id"]
    get_store().put(uid, "resume", "Tailored.json", text="{}", content_type="application/json", tags=match_tags(3))
    for params in ({}, {"kind": "resume"}):
        assert [d["id"] for d in (await authed.get("/api/documents", params=params)).json()] == [up["id"]]
    by_tag = (await authed.get("/api/documents", params={"tag": "match:3"})).json()
    assert [d["filename"] for d in by_tag] == ["Tailored.json"] and by_tag[0]["status"] == "ready"
    assert (await authed.put(f"/api/documents/{up['id']}/tags", json={"match": "3"})).status_code == 422
    tid = by_tag[0]["id"]
    assert (await authed.put(f"/api/documents/{tid}/tags", json={})).json()["tags"]["match"] == "3"


async def test_retrieval_endpoints(authed, maker):
    doc = await upload(authed, maker, "my resume.txt", b"Go and SQL")
    assert (await authed.get(f"/api/documents/{doc['id']}")).json()["filename"] == "my resume.txt"
    assert (await authed.get(f"/api/documents/{doc['id']}/text")).text == "Go and SQL"
    dl = await authed.get(f"/api/documents/{doc['id']}/download")
    assert dl.content == b"Go and SQL" and "my%20resume.txt" in dl.headers["content-disposition"]


async def test_tags_and_filtering(authed, maker):
    a = await upload(authed, maker, "a.txt", b"aaa")
    b = await upload(authed, maker, "b.txt", b"bbb")
    r = await authed.put(f"/api/documents/{b['id']}/tags", json={"target": "backend"})
    assert r.status_code == 200 and r.json()["tags"]["target"] == "backend"
    assert [d["id"] for d in (await authed.get("/api/documents", params={"tag": "target:backend"})).json()] == [b["id"]]
    assert {d["id"] for d in (await authed.get("/api/documents", params={"tag": "source:upload"})).json()} == {a["id"], b["id"]}
    assert (await authed.get("/api/documents", params={"tag": "nocolon"})).status_code == 422
    assert (await authed.put(f"/api/documents/{b['id']}/tags", json={"kind": "x"})).status_code == 422


async def test_upload_rejects_bad_input(authed):
    assert (await _post(authed, "x.exe", b"MZ", "application/octet-stream")).status_code == 415
    assert (await _post(authed, "a.txt", b"hi", kind="nope")).status_code == 422
    assert (await authed.get("/api/documents")).json() == []  # nothing stored for rejected uploads


async def test_upload_size_limit(authed, monkeypatch):
    monkeypatch.setattr(get_settings(), "upload_max_bytes", 10)
    assert (await _post(authed, "a.txt", b"x" * 11)).status_code == 413
    assert (await authed.get("/api/documents")).json() == []


async def test_other_users_documents_are_invisible(authed, client, make_account, maker):
    r = (await _post(authed, "a.txt", b"hi")).json()
    doc = r["document"]
    await client.post("/api/auth/logout")
    await make_account("o@x.com", "password-oooo")
    for path in ("", "/download", "/text"):
        assert (await client.get(f"/api/documents/{doc['id']}{path}")).status_code == 404
    assert (await client.delete(f"/api/documents/{doc['id']}")).status_code == 404
    assert (await client.get(f"/api/tasks/{r['task_id']}")).status_code == 404  # the parse task is the owner's
    assert (await client.get("/api/documents/../../etc/passwd")).status_code in (404, 405)
    assert (await client.get("/api/documents")).json() == []


async def test_delete_document(authed, maker):
    doc = await upload(authed, maker, "a.txt", b"hi")
    assert (await authed.delete(f"/api/documents/{doc['id']}")).status_code == 204
    assert (await authed.get(f"/api/documents/{doc['id']}")).status_code == 404


def test_meta_json_keeps_its_old_shape_for_ready_documents(isolated_store):
    """Ready documents are written without `status` (the previous release can't read the key) and read
    back as ready, like every file written before #51."""
    import json

    def raw(doc_id):
        return json.loads((isolated_store.root / "1" / doc_id / "meta.json").read_text())

    ready, _ = isolated_store.put(1, "resume", "old.txt", text="old", data=b"old", ext=".txt")
    busy, _ = isolated_store.put(1, "resume", "new.txt", text="", data=b"new", ext=".txt", status="processing")
    assert "status" not in raw(ready.id) and raw(busy.id)["status"] == "processing"
    assert isolated_store.get(1, ready.id).status == "ready"
    assert {m.id: m.status for m in isolated_store.list(1)} == {ready.id: "ready", busy.id: "processing"}
    isolated_store.replace_text(1, busy.id, "read", fetched=False)
    assert "status" not in raw(busy.id)


# ── real PDFs, page cap ────────────────────────────────────────────────
async def test_real_pdf_is_parsed(authed, maker):
    from pdfs import make_pdf

    doc = await upload(authed, maker, "cv.pdf", make_pdf(["Jane Doe Backend Engineer"]), "application/pdf")
    assert doc["status"] == "ready"
    assert "Jane Doe Backend Engineer" in (await authed.get(f"/api/documents/{doc['id']}/text")).text


def test_pdf_page_cap(monkeypatch):
    from pdfs import make_pdf

    from jobfinder.profile import resume_parse

    monkeypatch.setattr(resume_parse, "MAX_PDF_PAGES", 3)
    text = resume_parse.extract_text(make_pdf([f"page{i}" for i in range(10)]), ".pdf")
    assert "page2" in text and "page3" not in text


def test_the_api_does_not_load_the_parsers_or_the_fetcher():
    """#51: parsing and link fetching run on worker-io, so the api process never imports them (docx stays:
    the cover letter export builds .docx files)."""
    import subprocess
    import sys

    code = (
        "import sys; from jobfinder.api.app import create_app; create_app(); "
        "print(sorted(m for m in ('jobfinder.profile.resume_parse', 'jobfinder.profile.web_page', 'pdfplumber', "
        "'trafilatura') if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]", out.stdout + out.stderr


# ── SYNC_UPLOADS=true: read in the request, as before #51 ──────────────
async def test_sync_upload_is_ready_at_once(authed, sync_uploads):
    r = await _post(authed, "resume.txt", b"Python engineer")
    assert r.status_code == 202 and r.json()["task_id"] is None
    doc = r.json()["document"]
    assert doc["status"] == "ready" and doc["chars"] == len("Python engineer")
    again = (await _post(authed, "b.txt", b"Python engineer")).json()
    assert again["document"]["id"] == doc["id"] and again["document"]["duplicate"] and again["task_id"] is None


async def test_sync_corrupt_pdf_is_a_422(authed, sync_uploads):
    assert (await _post(authed, "cv.pdf", b"%PDF-1.4 not really", "application/pdf")).status_code == 422
    assert (await authed.get("/api/documents")).json() == []


async def _parser_idle():
    """Parses that timed out keep running to the end; wait until their slots are back so tests don't leak into each other."""
    import asyncio

    from jobfinder.profile import resume_parse

    for _ in range(100):
        if resume_parse._slots._value == resume_parse.MAX_PARSES:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("parser slots never came back")


@pytest.mark.real_commit
async def test_sync_slow_parse_times_out_without_blocking_the_api(authed, monkeypatch, sync_uploads):
    import asyncio
    import time

    from jobfinder.profile import resume_parse

    monkeypatch.setattr(resume_parse, "PARSE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(resume_parse, "extract_text", lambda data, ext: time.sleep(1) or "late")
    started = asyncio.get_running_loop().time()
    upload = asyncio.create_task(_post(authed, "a.txt", b"hi"))
    await asyncio.sleep(0.05)
    assert (await authed.get("/api/auth/me")).status_code == 200  # still answering while the parse runs
    r = await upload
    assert r.status_code == 422 and asyncio.get_running_loop().time() - started < 0.9
    await _parser_idle()


async def test_sync_stuck_parses_keep_their_slots_so_uploads_get_503_not_more_threads(authed, monkeypatch, sync_uploads):
    import threading

    from jobfinder.profile import resume_parse

    await _parser_idle()
    release = threading.Event()
    monkeypatch.setattr(resume_parse, "PARSE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(resume_parse, "extract_text", lambda data, ext: release.wait(5) and "late")
    try:
        for i in range(resume_parse.MAX_PARSES):  # each times out for the client but its thread is still running
            assert (await _post(authed, f"{i}.txt", b"hi")).status_code == 422
        r = await _post(authed, "third.txt", b"hi")
        assert r.status_code == 503 and "try again" in r.json()["detail"]
        assert (await authed.get("/api/auth/me")).status_code == 200
    finally:
        release.set()
    await _parser_idle()  # slots come back once the threads really finish
    monkeypatch.undo()
    assert (await _post(authed, "ok.txt", b"hello")).status_code == 202
