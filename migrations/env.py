import asyncio
import os
from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
from app.storage import Base


def migrate(connection):
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run():
    url = os.environ["DATABASE_URL"]
    if not url.startswith("postgresql+asyncpg://"):
        raise ValueError("PostgreSQL asyncpg URL required")
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline migration is not supported in this release")
asyncio.run(run())
