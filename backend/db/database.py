from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import text

_BACKEND_DIR = Path(__file__).resolve().parents[1]
DATABASE_PATH = _BACKEND_DIR / "agentic_tests.db"
DATABASE_URL = f"sqlite+aiosqlite:///{DATABASE_PATH}"

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncSession:
    async with async_session() as session:
        yield session


async def init_db():
    from db.models import Base

    def _ensure_sqlite_additive_schema(sync_conn) -> None:
        if sync_conn.dialect.name != "sqlite":
            return

        additive_columns: dict[str, list[tuple[str, str]]] = {
            "load_test_scenarios": [
                ("query_params_json", "JSON DEFAULT '{}'"),
                ("body_json", "JSON"),
                ("expected_statuses_json", "JSON DEFAULT '[]'"),
            ],
            "load_test_results": [
                ("runner_status", "VARCHAR DEFAULT 'passed'"),
                ("runner_message", "TEXT DEFAULT ''"),
                ("runner_exit_code", "INTEGER"),
                ("runner_stdout_excerpt", "TEXT DEFAULT ''"),
                ("runner_stderr_excerpt", "TEXT DEFAULT ''"),
                ("metric_shape", "VARCHAR"),
                ("request_count_source", "VARCHAR"),
                ("error_rate_source", "VARCHAR"),
                ("parse_warnings_json", "JSON DEFAULT '[]'"),
            ],
        }

        for table_name, columns in additive_columns.items():
            existing_cols = {
                row[1]
                for row in sync_conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
            }
            for col_name, col_sql in columns:
                if col_name not in existing_cols:
                    sync_conn.execute(
                        text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_sql}")
                    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_sqlite_additive_schema)
