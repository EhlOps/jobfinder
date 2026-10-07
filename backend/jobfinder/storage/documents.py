"""File store for documents (uploads and fetched link snapshots), kept on a docker volume.

Layout, one directory per document:
    {root}/{user_id}/{doc_id}/meta.json        source of truth (id, kind, tags, hashes, ...)
    {root}/{user_id}/{doc_id}/original.<ext>   the uploaded file (uploads only)
    {root}/{user_id}/{doc_id}/text.txt         extracted / fetched text

IDs are generated here (`doc_` + ms-timestamp hex + random hex, so they sort by creation time);
paths are built only from the integer user id and a validated ID, never from client input.
Swapping this for S3/MinIO later means re-implementing this class; callers don't change.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from jobfinder.config import get_settings

ID_RE = re.compile(r"^doc_[0-9a-f]{22}$")
TAG_KEY_RE = re.compile(r"^[a-z0-9_.-]{1,40}$")
MAX_TAG_VALUE = 200
# Tags the store sets itself; callers can't overwrite them through set_tags().
RESERVED_TAGS = {"kind", "ext", "source", "url", "link_kind"}


class StoreError(Exception):
    pass


class NotFound(StoreError):
    pass


@dataclass
class DocumentMeta:
    id: str
    user_id: int
    kind: str  # resume|portfolio|other|link
    filename: str
    content_type: str
    size: int
    sha256: str
    created_at: str
    chars: int = 0
    tags: dict[str, str] = field(default_factory=dict)
    has_original: bool = False
    fetch_error: str | None = None
    fetched_at: str | None = None


def new_id() -> str:
    return f"doc_{int(time.time() * 1000):012x}{secrets.token_hex(5)}"


def validate_tags(tags: dict[str, str]) -> dict[str, str]:
    out = {}
    for k, v in tags.items():
        if not TAG_KEY_RE.match(k):
            raise StoreError(f"Invalid tag name {k!r} (use a-z 0-9 _ . - up to 40 chars)")
        if k in RESERVED_TAGS:
            raise StoreError(f"Tag {k!r} is reserved")
        out[k] = str(v)[:MAX_TAG_VALUE]
    return out


class DocumentStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    # ── paths ─────────────────────────────────────────────────────────────
    def _user_dir(self, user_id: int) -> Path:
        return self.root / str(int(user_id))

    def _doc_dir(self, user_id: int, doc_id: str) -> Path:
        if not ID_RE.match(doc_id):
            raise NotFound("Invalid document id")
        return self._user_dir(user_id) / doc_id

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, path)

    # ── write ─────────────────────────────────────────────────────────────
    def put(
        self,
        user_id: int,
        kind: str,
        filename: str,
        *,
        text: str,
        data: bytes | None = None,
        ext: str = "",
        content_type: str = "application/octet-stream",
        tags: dict[str, str] | None = None,
        fetch_error: str | None = None,
        fetched: bool = False,
    ) -> tuple[DocumentMeta, bool]:
        """Store a document. Returns (meta, created). Uploading identical bytes twice returns
        the existing document (created=False) instead of storing a duplicate."""
        sha = hashlib.sha256(data if data is not None else text.encode()).hexdigest()
        if data is not None:
            for existing in self.list(user_id):
                if existing.has_original and existing.sha256 == sha:
                    return existing, False

        doc_id = new_id()
        now = datetime.now(UTC).isoformat()
        auto = {"kind": kind}
        if ext:
            auto["ext"] = ext.lstrip(".")
        meta = DocumentMeta(
            id=doc_id,
            user_id=int(user_id),
            kind=kind,
            filename=filename[:255],
            content_type=content_type,
            size=len(data) if data is not None else len(text.encode()),
            sha256=sha,
            created_at=now,
            chars=len(text),
            tags={**(tags or {}), **auto},
            has_original=data is not None,
            fetch_error=fetch_error,
            fetched_at=now if fetched else None,
        )
        user_dir = self._user_dir(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        tmp = user_dir / f".tmp-{doc_id}"  # invisible to list(); renamed into place when complete
        tmp.mkdir()
        try:
            if data is not None:
                (tmp / f"original{ext.lower()}").write_bytes(data)
            (tmp / "text.txt").write_text(text)
            (tmp / "meta.json").write_text(json.dumps(asdict(meta), indent=1))
            os.rename(tmp, user_dir / doc_id)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return meta, True

    def set_tags(self, user_id: int, doc_id: str, tags: dict[str, str]) -> DocumentMeta:
        """Replace the custom tags (automatic ones are kept)."""
        meta = self.get(user_id, doc_id)
        custom = validate_tags(tags)
        auto = {k: v for k, v in meta.tags.items() if k in RESERVED_TAGS}
        meta.tags = {**custom, **auto}
        self._write_json(self._doc_dir(user_id, doc_id) / "meta.json", asdict(meta))
        return meta

    def replace_text(
        self, user_id: int, doc_id: str, text: str, *, fetch_error: str | None = None, fetched: bool = True
    ) -> DocumentMeta:
        meta = self.get(user_id, doc_id)
        d = self._doc_dir(user_id, doc_id)
        (d / "text.txt").write_text(text)
        meta.chars = len(text)
        meta.fetch_error = fetch_error
        if fetched:
            meta.fetched_at = datetime.now(UTC).isoformat()
        self._write_json(d / "meta.json", asdict(meta))
        return meta

    def delete(self, user_id: int, doc_id: str) -> None:
        d = self._doc_dir(user_id, doc_id)
        if not d.is_dir():
            raise NotFound("Document not found")
        shutil.rmtree(d)

    # ── read ──────────────────────────────────────────────────────────────
    def get(self, user_id: int, doc_id: str) -> DocumentMeta:
        path = self._doc_dir(user_id, doc_id) / "meta.json"
        try:
            return DocumentMeta(**json.loads(path.read_text()))
        except FileNotFoundError:
            raise NotFound("Document not found") from None

    def read_text(self, user_id: int, doc_id: str) -> str:
        try:
            return (self._doc_dir(user_id, doc_id) / "text.txt").read_text()
        except FileNotFoundError:
            raise NotFound("Document not found") from None

    def original_path(self, user_id: int, doc_id: str) -> Path | None:
        d = self._doc_dir(user_id, doc_id)
        if not d.is_dir():
            raise NotFound("Document not found")
        return next(d.glob("original*"), None)

    def list(
        self, user_id: int, *, kind: str | None = None, tags: dict[str, str] | None = None
    ) -> list[DocumentMeta]:
        user_dir = self._user_dir(user_id)
        if not user_dir.is_dir():
            return []
        out = []
        for d in sorted(user_dir.iterdir()):
            if not ID_RE.match(d.name):
                continue
            try:
                meta = DocumentMeta(**json.loads((d / "meta.json").read_text()))
            except (OSError, ValueError, TypeError):
                continue  # half-written or corrupt entry: skip rather than fail the listing
            if kind and meta.kind != kind:
                continue
            if tags and any(meta.tags.get(k) != v for k, v in tags.items()):
                continue
            out.append(meta)
        return out


def get_store() -> DocumentStore:
    return DocumentStore(Path(get_settings().store_dir))
