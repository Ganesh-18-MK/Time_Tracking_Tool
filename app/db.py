"""Database setup.

Default is a local SQLite file so the POC runs with zero setup; production
per PRD §9 is Postgres — set DATABASE_URL=postgresql+psycopg://... and the
same code runs unchanged (SQLAlchemy abstracts the dialect).
"""
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tms.db")
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DB_PATH}")
_is_sqlite = DATABASE_URL.startswith("sqlite")

connect_args = {"check_same_thread": False} if _is_sqlite else {}
# Pool sizing (Ganesh, 2026-08-21 — performance pass for "will 100
# concurrent users work"): SQLAlchemy's own defaults are pool_size=5,
# max_overflow=10 (15 connections total) per process, which is plenty at
# ~45 staff spread across a workday but is the kind of thing that quietly
# becomes the bottleneck once a lot of requests land at once — every route
# in this app is a plain `def` (not `async def`), so FastAPI/Starlette
# runs each one in a worker thread (AnyIO's thread limiter defaults to 40
# concurrent), and each of those threads wants its own DB connection for
# the life of the request. 15 < 40 means requests 16-40 would queue for a
# free connection even though there's thread capacity to run them. Bumped
# to comfortably cover that, env-overridable so it can be tuned against
# real load-test numbers (see load_test/README.md) without a code change.
# SQLite has no equivalent pool concept worth sizing (single-writer file
# lock regardless) — only applied on the Postgres path.
pool_kwargs = {} if _is_sqlite else {
    "pool_size": int(os.environ.get("DB_POOL_SIZE", "20")),
    "max_overflow": int(os.environ.get("DB_MAX_OVERFLOW", "20")),
    "pool_pre_ping": True,  # drop/replace a connection Postgres has silently closed, instead of erroring the request
}
engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True, **pool_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401  (register tables)

    Base.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Minimal, additive-only migration.

    This project has no migration tool by design (see CLAUDE.md): schema
    changes are normally `rm tms.db` + re-import. That stops being safe the
    moment real employees have submitted real days, so instead of nuking
    production data on every model tweak, add whatever columns the ORM
    declares that the live table doesn't have yet. Additive-only — it will
    never drop, rename, or backfill a column; that still needs a deliberate
    one-off script.
    """
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                col_type = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type}'))
