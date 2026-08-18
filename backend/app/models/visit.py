from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Visit(Base):
    __tablename__ = "visits"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id"),
        nullable=False,
        index=True,
    )

    arrival_time: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
    )

    triage_level: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    chief_complaint: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(30),
        default="active",
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )