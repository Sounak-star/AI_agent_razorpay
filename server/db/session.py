"""
Database session factory.

SQLite: WAL journal mode + foreign-key enforcement enabled per-connection.
Postgres: standard psycopg2 pool (no special flags needed).
Switch by changing DATABASE_URL — no code changes required.
"""

from __future__ import annotations

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from server.config import settings
from server.db.models import Base

# ── Engine ────────────────────────────────────────────────────────────────────

_connect_args: dict = {}
if settings.DATABASE_URL.startswith("sqlite"):
    _connect_args["check_same_thread"] = False

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=_connect_args,
    echo=False,
    # Pool settings that work for both SQLite and Postgres
    pool_pre_ping=True,
)

# SQLite pragmas — WAL for better concurrent reads, FK enforcement
if settings.DATABASE_URL.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


# ── Session factory ───────────────────────────────────────────────────────────

SessionLocal = sessionmaker(
    bind=engine,
    class_=Session,
    autocommit=False,
    autoflush=False,
)


def create_tables() -> None:
    """Create all tables (idempotent). Called from FastAPI lifespan."""
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency: yields a DB session and closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
