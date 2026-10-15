import asyncio

from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from jobfinder import models  # noqa: F401  (registers tables)
from jobfinder.core.config import get_settings
from jobfinder.core.db import Base, database_url

target_metadata = Base.metadata


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_async_engine(database_url(get_settings()))
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=database_url(get_settings()), target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_online())
