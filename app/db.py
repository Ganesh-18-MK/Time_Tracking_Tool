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

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
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
