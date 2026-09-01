"""Migrate stored clinical-record JSON from drug_allergy to allergy."""

from sqlalchemy import select

from app.api.clinical_records import migrate_legacy_allergy_key
from app.database import SessionLocal
from app.models.clinical_record import ClinicalRecord


def main() -> None:
    migrated_count = 0
    with SessionLocal() as db:
        records = db.scalars(select(ClinicalRecord)).all()
        for record in records:
            migrated_payload = migrate_legacy_allergy_key(record.record_payload)
            if migrated_payload == record.record_payload:
                continue
            record.record_payload = migrated_payload
            migrated_count += 1
        db.commit()
    print(f"Migrated {migrated_count} clinical record(s).")


if __name__ == "__main__":
    main()
