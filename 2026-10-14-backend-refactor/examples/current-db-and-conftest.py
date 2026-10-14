# backend/jobfinder/db.py (today): engine is built when the module is imported
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from jobfinder.config import get_settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(get_settings().database_url, pool_pre_ping=True, pool_size=10, max_overflow=10)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session

# backend/tests/conftest.py:24-30 (today): env must be set before any jobfinder import

os.environ["DATABASE_URL"] = TEST_URL
os.environ["SECRET_KEY"] = "test-secret-key-0123456789-abcdefghijklmnop"
# An https value in backend/.env would make the session cookie Secure, which the http test client drops.
os.environ["PUBLIC_BASE_URL"] = "http://test"

from jobfinder.db import Base, get_db
