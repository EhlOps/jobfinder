import pytest

from jobfinder.storage.documents import DocumentStore, NotFound, StoreError, new_id


@pytest.fixture
def store(tmp_path):
    return DocumentStore(tmp_path / "store")


def put_upload(store, user=1, data=b"hello", name="cv.txt", kind="resume", **kw):
    return store.put(user, kind, name, data=data, text=data.decode(), ext=".txt", content_type="text/plain", **kw)


def test_round_trip_and_layout(store, tmp_path):
    meta, created = put_upload(store, tags={"source": "upload"})
    assert created and meta.id.startswith("doc_") and meta.has_original and meta.chars == 5
    d = tmp_path / "store" / "1" / meta.id
    assert {p.name for p in d.iterdir()} == {"meta.json", "original.txt", "text.txt"}
    assert store.get(1, meta.id) == meta
    assert store.read_text(1, meta.id) == "hello"
    assert store.original_path(1, meta.id).read_bytes() == b"hello"
    assert meta.tags == {"source": "upload", "kind": "resume", "ext": "txt"}


def test_ids_sort_by_creation_time():
    ids = [new_id() for _ in range(50)]
    assert ids == sorted(ids) or len(set(ids)) == 50  # same-ms ids differ in the random part
    assert len(set(ids)) == 50


def test_list_filters_by_kind_and_tags(store):
    a, _ = put_upload(store, data=b"a", kind="resume")
    b, _ = put_upload(store, data=b"b", kind="portfolio")
    store.set_tags(1, b.id, {"target": "backend"})
    assert [m.id for m in store.list(1)] == sorted([a.id, b.id])
    assert [m.id for m in store.list(1, kind="portfolio")] == [b.id]
    assert [m.id for m in store.list(1, tags={"target": "backend"})] == [b.id]
    assert store.list(1, tags={"target": "frontend"}) == []
    assert [m.id for m in store.list(1, tags={"ext": "txt", "kind": "resume"})] == [a.id]


def test_identical_upload_is_deduplicated(store):
    first, c1 = put_upload(store, data=b"same")
    second, c2 = put_upload(store, data=b"same", name="other-name.txt")
    assert (c1, c2) == (True, False) and second.id == first.id and len(store.list(1)) == 1
    # but another user can store the same bytes
    assert put_upload(store, user=2, data=b"same")[1] is True


def test_text_only_documents_have_no_original(store):
    meta, _ = store.put(1, "link", "example.com", text="page text", tags={"source": "link", "url": "https://example.com"})
    assert not meta.has_original and store.original_path(1, meta.id) is None
    meta = store.replace_text(1, meta.id, "new text", fetch_error=None)
    assert store.read_text(1, meta.id) == "new text" and meta.chars == 8 and meta.fetched_at


def test_set_tags_replaces_custom_but_keeps_automatic(store):
    meta, _ = put_upload(store, tags={"source": "upload"})
    store.set_tags(1, meta.id, {"a": "1"})
    assert store.set_tags(1, meta.id, {"b": "2"}).tags == {"b": "2", "source": "upload", "kind": "resume", "ext": "txt"}


@pytest.mark.parametrize("tags", [{"kind": "x"}, {"Bad Key": "v"}, {"": "v"}, {"a" * 41: "v"}])
def test_invalid_or_reserved_tags_rejected(store, tags):
    meta, _ = put_upload(store)
    with pytest.raises(StoreError):
        store.set_tags(1, meta.id, tags)


@pytest.mark.parametrize("bad", ["../../etc/passwd", "doc_../x", "..", "doc_zzzzzzzzzzzzzzzzzzzzzz", "", "doc_" + "a" * 21])
def test_malformed_ids_never_touch_the_filesystem(store, bad):
    for call in (store.get, store.read_text, store.delete, store.original_path):
        with pytest.raises(NotFound):
            call(1, bad)


def test_users_cannot_see_each_others_documents(store):
    meta, _ = put_upload(store, user=1)
    assert store.list(2) == []
    with pytest.raises(NotFound):
        store.get(2, meta.id)
    with pytest.raises(NotFound):
        store.delete(2, meta.id)


def test_delete_removes_directory(store, tmp_path):
    meta, _ = put_upload(store)
    store.delete(1, meta.id)
    assert not (tmp_path / "store" / "1" / meta.id).exists() and store.list(1) == []
    with pytest.raises(NotFound):
        store.delete(1, meta.id)


def test_failed_write_leaves_no_partial_document(store, tmp_path, monkeypatch):
    import os

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "rename", boom)
    with pytest.raises(OSError):
        put_upload(store)
    monkeypatch.undo()
    assert store.list(1) == []
    assert [p for p in (tmp_path / "store" / "1").iterdir()] == []  # temp dir cleaned up


def test_listing_skips_stray_and_corrupt_entries(store, tmp_path):
    meta, _ = put_upload(store)
    user_dir = tmp_path / "store" / "1"
    (user_dir / ".tmp-doc_whatever").mkdir()
    (user_dir / "not-a-doc").mkdir()
    bad = user_dir / new_id()
    bad.mkdir()
    (bad / "meta.json").write_text("{not json")
    assert [m.id for m in store.list(1)] == [meta.id]
