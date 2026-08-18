from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Vital(Base):
    __tablename__ = "vitals"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    visit_id: Mapped[int] = mapped_column(
        ForeignKey("visits.id"),
        nullable=False,
        index=True,
    )

    measured_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        index=True,
    )

    heart_rate: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    respiratory_rate: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    systolic_bp: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    diastolic_bp: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    temperature: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    spo2: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    consciousness: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )