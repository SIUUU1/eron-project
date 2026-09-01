from datetime import datetime

from sqlalchemy import DateTime, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ClinicalRecord(Base):
    """Persisted emergency-record draft/signature for one ED stay."""

    __tablename__ = "clinical_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ed_stay_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT", nullable=False)
    record_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    selected_kcd: Mapped[list | dict | None] = mapped_column(JSON, nullable=True)
    clinician_id: Mapped[str] = mapped_column(String(50), nullable=False)
    clinician_name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    signed_by: Mapped[str | None] = mapped_column(String(50), nullable=True)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
