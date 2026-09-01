from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class KcdCode(Base):
    """Searchable KCD-9 complete-code master entry."""

    __tablename__ = "kcd_codes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name_ko: Mapped[str] = mapped_column(String(500), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(500), nullable=True)

    __table_args__ = (
        Index("ix_kcd_codes_name_ko", "name_ko"),
        Index("ix_kcd_codes_name_en", "name_en"),
    )
