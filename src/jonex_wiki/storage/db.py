from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def db_connect_args(settings) -> dict:
    """按方言生成连接参数:PostgreSQL 经 search_path 指定目标 schema(表都建在该 schema)。

    实现选择 search_path 而非模型/迁移里的 schema 声明:表名不带 schema 前缀,
    连接级 search_path 决定落点——模型、迁移、仓储代码零改动;SQLite 无 schema 概念,
    返回空参不受影响。
    """
    if settings.database_url.startswith("postgresql"):
        return {"server_settings": {"search_path": settings.db_schema}}
    return {}
