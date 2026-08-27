"""의존성 주입용 DB 세션."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from app.database import SessionLocal


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
