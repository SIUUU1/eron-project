-- =====================================================================
-- ER:ON ClinicalNLP PostgreSQL storage — migration 002
--
-- Preserve distinct source rows even when their normalized search text is
-- identical. Search normalization is an index concern, not record identity.
-- =====================================================================

BEGIN;

ALTER TABLE clinicalnlp.medical_terms
    ADD COLUMN IF NOT EXISTS source_term_id TEXT;
ALTER TABLE clinicalnlp.kcd_terms
    ADD COLUMN IF NOT EXISTS source_term_id TEXT;

ALTER TABLE clinicalnlp.medical_terms
    DROP CONSTRAINT IF EXISTS medical_terms_concept_pk_normalized_term_language_term_type_key;
ALTER TABLE clinicalnlp.kcd_terms
    DROP CONSTRAINT IF EXISTS kcd_terms_kcd_code_pk_normalized_term_key;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'uq_clinicalnlp_medical_term_source'
           AND conrelid = 'clinicalnlp.medical_terms'::regclass
    ) THEN
        ALTER TABLE clinicalnlp.medical_terms
            ADD CONSTRAINT uq_clinicalnlp_medical_term_source
            UNIQUE (concept_pk, source_term_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'uq_clinicalnlp_kcd_term_source'
           AND conrelid = 'clinicalnlp.kcd_terms'::regclass
    ) THEN
        ALTER TABLE clinicalnlp.kcd_terms
            ADD CONSTRAINT uq_clinicalnlp_kcd_term_source
            UNIQUE (kcd_code_pk, source_term_id);
    END IF;
END $$;

INSERT INTO clinicalnlp.schema_migrations(version, description)
VALUES ('002', 'Preserve medical and KCD source term identities')
ON CONFLICT (version) DO NOTHING;

COMMIT;
