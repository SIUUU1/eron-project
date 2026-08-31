-- Preserve source precision for policy dates such as YYYY-MM and YYYY.
BEGIN;

ALTER TABLE clinicalnlp.policy_documents
    ALTER COLUMN published_at TYPE TEXT
    USING published_at::TEXT;

INSERT INTO clinicalnlp.schema_migrations(version, description)
VALUES ('004', 'Preserve partial policy publication dates')
ON CONFLICT (version) DO NOTHING;

COMMIT;
