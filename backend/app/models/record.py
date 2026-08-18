from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Record(Base):
    __tablename__ = "records"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    visit_id: Mapped[int] = mapped_column(
        ForeignKey("visits.id"),
        nullable=False,
        index=True,
    )

    record_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    generated_by: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )