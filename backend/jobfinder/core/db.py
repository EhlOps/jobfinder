"""Database primitives: the ORM base, the engine and session factory, and the request session.

One transaction per request: `get_db` commits when the handler returns and rolls back when it raises.
A handler that commits itself still works (the final commit then has nothing to do); those commits go
away area by area in M4. Use `side_session` for a write that must survive the caller's rollback.
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Protocol
from urllib.parse import quote

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

Sessions = async_sessionmaker[AsyncSession]


class Base(DeclarativeBase):
    pass


class DbSettings(Protocol):
    database_url: str
    postgres_host: str
    postgres_port: int
    postgres_user: str
    postgres_password: str
    postgres_db: str


def database_url(s: DbSettings) -> str:
    """DATABASE_URL when set, else a URL built from the POSTGRES_* parts (user and password encoded,
    because k8s passes parts: the password comes from its own Secret and may hold @ / %)."""
    if s.database_url:
        return s.database_url
    auth = quote(s.postgres_user, safe="") + ":" + quote(s.postgres_password, safe="")
    return f"postgresql+asyncpg://{auth}@{s.postgres_host}:{s.postgres_port}/{s.postgres_db}"


def make_engine(url: str) -> AsyncEngine:
    """Connects lazily: building an engine does no I/O."""
    return create_async_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10)


def make_sessions(engine: AsyncEngine) -> Sessions:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """The request's session, from the app's context (api/app.py)."""
    sessions: Sessions = request.app.state.ctx.sessions
    async with sessions() as session:
        yield session
        await session.commit()


# scope="function": the commit runs when the handler returns, before the response is sent, so a
# failed commit is an error response, never a success for data that was then lost.
DbSession = Annotated[AsyncSession, Depends(get_db, scope="function")]


@asynccontextmanager
async def side_session(sessions: Sessions) -> AsyncIterator[AsyncSession]:
    """A short separate transaction, committed on exit (rate events, AI call records, task failures)."""
    async with sessions() as session, session.begin():
        yield session
