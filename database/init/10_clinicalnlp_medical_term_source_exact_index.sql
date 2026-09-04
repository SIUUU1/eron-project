-- ER:ON ClinicalNLP PostgreSQL storage — migration 006
--
-- Exact terminology lookup intentionally falls back to source_text because
-- some imported dictionaries use source-specific normalized forms that omit
-- spaces or punctuation.  Index the fallback expression so PostgreSQL can
-- satisfy both sides of the exact-match predicate without scanning terms.
BEGIN;

CREATE INDEX IF NOT EXISTS ix_clinicalnlp_medical_terms_source_exact
    ON clinicalnlp.medical_terms (lower(trim(source_text)));

INSERT INTO clinicalnlp.schema_migrations(version, description)
VALUES ('006', 'Index exact medical source terminology text')
ON CONFLICT (version) DO NOTHING;

COMMIT;
