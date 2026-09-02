"""Database engine, session factory and shared column conventions.

Synchronous SQLAlchemy throughout. FastAPI runs synchronous handlers in a
threadpool, which is entirely adequate here and removes a whole class of async
session-lifecycle bugs from a codebase whose correctness story is transactional.

``row_version`` (P0 §6) is a BIGINT drawn from **one shared PostgreSQL sequence**
across every versioned table, so it doubles as a globally monotonic cursor for
the P5 sync change feed.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from sqlalchemy import BigInteger, Date, DateTime, MetaData, Numeric, String, create_engine, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Session, mapped_column, sessionmaker

from app.core.config import get_settings

__all__ = [
    "Base",
    "ROW_VERSION_SEQUENCE",
    "next_row_version",
    "uuid_pk",
    "uuid_fk",
    "uuid_nullable",
    "money_minor",
    "quantity",
    "utc_timestamp",
    "business_day",
    "get_engine",
    "get_sessionmaker",
    "session_scope",
]

ROW_VERSION_SEQUENCE = "row_version_seq"

# Predictable constraint/index names so the schema-assertion test can look them
# up by name and a later migration cannot quietly rename one.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# --- shared column types ----------------------------------------------------

uuid_pk = Annotated[
    uuid.UUID, mapped_column(PGUUID(as_uuid=True), primary_key=True)
]
uuid_fk = Annotated[uuid.UUID, mapped_column(PGUUID(as_uuid=True), nullable=False)]
uuid_nullable = Annotated[
    uuid.UUID | None, mapped_column(PGUUID(as_uuid=True), nullable=True)
]
# FIN-1: money is BIGINT minor units. There is no Numeric money column anywhere.
money_minor = Annotated[int, mapped_column(BigInteger, nullable=False)]
# FIN-2: quantity is NUMERIC(12,3), never an integer type.
quantity = Annotated[Decimal, mapped_column(Numeric(12, 3), nullable=False)]
utc_timestamp = Annotated[datetime, mapped_column(DateTime(timezone=True), nullable=False)]
business_day = Annotated[date, mapped_column(Date, nullable=False)]
short_text = Annotated[str, mapped_column(String(64), nullable=False)]


def next_row_version(session: Session) -> int:
    """Draw the next value from the shared sequence.

    Called explicitly whenever a versioned row is inserted or updated. P1 has
    few write paths, so an explicit bump is clearer than a trigger; if the number
    of write paths grows, move this into a ``BEFORE UPDATE`` trigger so it cannot
    be forgotten.
    """
    return session.execute(text(f"SELECT nextval('{ROW_VERSION_SEQUENCE}')")).scalar_one()


# --- engine / session -------------------------------------------------------

_engine = None
_sessionmaker = None


def get_engine(url: str | None = None):
    global _engine
    if url is not None:
        return create_engine(url, future=True, pool_pre_ping=True)
    if _engine is None:
        _engine = create_engine(
            get_settings().require_database_url(), future=True, pool_pre_ping=True
        )
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = sessionmaker(
            bind=get_engine(), class_=Session, expire_on_commit=False, future=True
        )
    return _sessionmaker


def session_scope() -> Session:
    return get_sessionmaker()()
