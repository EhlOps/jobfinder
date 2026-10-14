"""companies: one-time import of the retired seed YAML; the table is now the only source of truth

Until 0021 every ingest run upserted `ingest/companies.yaml` + `ingest/companies.d/*.yaml` into
`companies` (`service.sync_companies`). Those files are gone. `data/0021_seed_companies.json` is a
frozen snapshot of exactly what that sync wrote: it was produced once from the YAML with the old
`service.load_seed()` (files in load order, first `(ats, slug)` wins, rows missing name/ats/slug or
with an ATS outside `ATS_FETCHERS` skipped, slug cast to str), keeping name, ats, slug,
prestige_tier (YAML `prestige`, default 3), size (default "mid"), industry (default "") and
sponsors_visas (null when absent).
Do not regenerate or edit it; add boards through the app or `jobfinder.cli add-board`.

Fresh databases get every seed board here. Existing ones already hold these rows from the old
sync, so conflicts on (ats, slug) are left alone: admin edits are never clobbered and only rows
that are missing (e.g. deleted) are added back.

Revision ID: 0021
Revises: 0020
"""
import json
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from alembic import op

revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None

DATA_FILE = Path(__file__).parent / "data" / "0021_seed_companies.json"
CHUNK = 200

# A frozen view of the columns this import writes, so later model changes can't alter it.
companies = sa.table(
    'companies',
    sa.column('name', sa.String), sa.column('ats', sa.String), sa.column('slug', sa.String),
    sa.column('prestige_tier', sa.Integer), sa.column('size', sa.String),
    sa.column('industry', sa.String), sa.column('enabled', sa.Boolean),
    sa.column('sponsors_visas', sa.Boolean), sa.column('origin', sa.String),
)


def load_rows() -> list[dict]:
    return json.loads(DATA_FILE.read_text())


def import_rows(conn: sa.engine.Connection, rows: list[dict]) -> int:
    """Insert seed rows that aren't there yet. Returns how many were inserted."""
    values = [
        {"name": r["name"], "ats": r["ats"], "slug": r["slug"], "prestige_tier": r["prestige_tier"],
         "size": r["size"], "industry": r["industry"], "enabled": True,
         "sponsors_visas": r["sponsors_visas"], "origin": "seed"}
        for r in rows
    ]
    inserted = 0
    for i in range(0, len(values), CHUNK):
        stmt = insert(companies).values(values[i:i + CHUNK])
        res = conn.execute(stmt.on_conflict_do_nothing(constraint='uq_company_ats_slug'))
        inserted += res.rowcount if res is not None else 0  # None in offline (--sql) mode
    return inserted


def upgrade() -> None:
    import_rows(op.get_bind(), load_rows())


def downgrade() -> None:
    # Intentionally a no-op: the schema is unchanged, and the rows can't be told apart from boards
    # the old YAML sync wrote (or admins have since edited), so removing them would lose real data.
    pass
