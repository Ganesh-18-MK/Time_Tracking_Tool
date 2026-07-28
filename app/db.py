"""Database setup.

Default is a local SQLite file so the POC runs with zero setup; production
per PRD §9 is Postgres — set DATABASE_URL=postgresql+psycopg://... and the
same code runs unchanged (SQLAlchemy abstracts the dialect).
"""
import os

from sqlalchemy import create_engine
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
