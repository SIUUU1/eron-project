-- =====================================================================
-- ER:ON ClinicalNLP PostgreSQL storage — migration 003
--
-- Bind every medical vector row to an immutable VECTOR source release.
-- A vector import is activated only after all rows have been loaded.
-- =====================================================================

BEGIN;

ALTER TABLE clinicalnlp.medical_vectors
    ADD COLUMN IF NOT EXISTS vector_release_id BIGINT;

-- Preserve pre-migration rows without silently treating an unknown index as
-- the active production release. The real importer creates collection-scoped
-- releases and activates them after a complete load.
DO $$
DECLARE
    legacy_release_id BIGINT;
BEGIN
    IF EXISTS (
        SELECT 1
          FROM clinicalnlp.medical_vectors
         WHERE vector_release_id IS NULL
    ) THEN
        INSERT INTO clinicalnlp.source_releases (
            source_kind,
            source_id,
            version,
            content_hash,
            is_active,
            metadata
        )
        VALUES (
            'VECTOR',
            'medical_vector:legacy',
            'legacy-unversioned',
            repeat('0', 64),
            FALSE,
            '{"migration":"003","legacy":true}'::jsonb
        )
        ON CONFLICT (source_kind, source_id, version, content_hash)
        DO NOTHING;

        SELECT release_id
          INTO legacy_release_id
          FROM clinicalnlp.source_releases
         WHERE source_kind = 'VECTOR'
           AND source_id = 'medical_vector:legacy'
           AND version = 'legacy-unversioned'
           AND content_hash = repeat('0', 64);

        UPDATE clinicalnlp.medical_vectors
           SET vector_release_id = legacy_release_id
         WHERE vector_release_id IS NULL;
    END IF;
END $$;

ALTER TABLE clinicalnlp.medical_vectors
    ALTER COLUMN vector_release_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'medical_vectors_vector_release_id_fkey'
           AND conrelid = 'clinicalnlp.medical_vectors'::regclass
    ) THEN
        ALTER TABLE clinicalnlp.medical_vectors
            ADD CONSTRAINT medical_vectors_vector_release_id_fkey
            FOREIGN KEY (vector_release_id)
            REFERENCES clinicalnlp.source_releases(release_id);
    END IF;
END $$;

ALTER TABLE clinicalnlp.medical_vectors
    DROP CONSTRAINT IF EXISTS medical_vectors_concept_pk_source_text_model_version_key;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'uq_clinicalnlp_medical_vector_release_source'
           AND conrelid = 'clinicalnlp.medical_vectors'::regclass
    ) THEN
        ALTER TABLE clinicalnlp.medical_vectors
            ADD CONSTRAINT uq_clinicalnlp_medical_vector_release_source
            UNIQUE (vector_release_id, concept_pk, source_text);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_clinicalnlp_medical_vectors_release
    ON clinicalnlp.medical_vectors (vector_release_id);

INSERT INTO clinicalnlp.schema_migrations(version, description)
VALUES ('003', 'Version medical vector releases independently')
ON CONFLICT (version) DO NOTHING;

COMMIT;
