import asyncio

from alembic import context
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from jonex_wiki.storage.db import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    from jonex_wiki.infra.settings import Settings
    settings = Settings()
    context.configure(url=settings.database_url,
                      target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    from jonex_wiki.infra.settings import Settings
    from jonex_wiki.storage.db import db_connect_args
    settings = Settings()
    engine = create_async_engine(settings.database_url, connect_args=db_connect_args(settings))
    async with engine.connect() as connection:
        # 时序关键:search_path 已指向 DB_SCHEMA,但 alembic 要在迁移之前先建 alembic_version 表——
        # schema 若不存在会报 "no schema has been selected to create in"。这里提前确保 schema 存在
        # (迁移 0001 内的 CREATE SCHEMA IF NOT EXISTS 保留,幂等兜底)
        if connection.dialect.name == "postgresql":
            schema = settings.db_schema
            quoted = schema.replace('"', '""')
            await connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{quoted}"'))
            await connection.commit()
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
