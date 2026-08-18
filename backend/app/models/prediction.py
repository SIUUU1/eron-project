from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Prediction(Base):
    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    visit_id: Mapped[int] = mapped_column(
        ForeignKey("visits.id"),
        nullable=False,
        index=True,
    )

    predicted_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    risk_score: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )

    risk_level: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    prediction_horizon: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    risk_factors: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )