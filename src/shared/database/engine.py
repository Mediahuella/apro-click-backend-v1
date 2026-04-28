"""SQLAlchemy engine and session factory — optimised for AWS Lambda."""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine as _create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

_engine: Engine | None = None


def get_engine() -> Engine:
    """Return a module-level singleton engine (reused across warm invocations)."""
    global _engine
    if _engine is None:
        url = os.environ.get("DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError(
                "DATABASE_URL is not set or empty. "
                "Ensure the variable is configured in the Lambda environment."
            )
        _engine = _create_engine(
            url,
            pool_size=1,
            max_overflow=0,
            pool_pre_ping=True,
        )
    return _engine


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Yield a session that auto-closes; caller must commit explicitly."""
    session = Session(get_engine())
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
