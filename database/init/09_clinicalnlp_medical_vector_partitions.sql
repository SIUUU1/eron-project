-- Scope pgvector ANN indexes to the active runtime search partitions.
--
-- Collection/entity metadata is duplicated intentionally: pgvector cannot use
-- predicates that live behind the medical_concepts join when choosing an ANN
-- index.  The importer keeps these immutable search keys in sync with the
-- referenced concept and switches is_active atomically with source_releases.
BEGIN;

ALTER TABLE clinicalnlp.medical_vectors
    ADD COLUMN IF NOT EXISTS collection_name TEXT,
    ADD COLUMN IF NOT EXISTS entity_type TEXT,
    ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE clinicalnlp.medical_vectors v
   SET collection_name = c.collection_name,
       entity_type = c.entity_type,
       is_active = vr.is_active
  FROM clinicalnlp.medical_concepts c,
       clinicalnlp.source_releases vr
 WHERE c.concept_pk = v.concept_pk
   AND vr.release_id = v.vector_release_id
   AND (
       v.collection_name IS DISTINCT FROM c.collection_name
       OR v.entity_type IS DISTINCT FROM c.entity_type
       OR v.is_active IS DISTINCT FROM vr.is_active
   );

ALTER TABLE clinicalnlp.medical_vectors
    ALTER COLUMN collection_name SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'medical_vectors_collection_name_check'
           AND conrelid = 'clinicalnlp.medical_vectors'::regclass
    ) THEN
        ALTER TABLE clinicalnlp.medical_vectors
            ADD CONSTRAINT medical_vectors_collection_name_check
            CHECK (collection_name IN (
                'drug_terms',
                'procedure_terms',
                'anatomy_terms',
                'emergency_terms'
            ));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_clinicalnlp_vectors_drug_ingredient_hnsw
    ON clinicalnlp.medical_vectors
    USING hnsw (embedding vector_cosine_ops)
    WHERE is_active
      AND collection_name = 'drug_terms'
      AND entity_type = 'ingredient';

CREATE INDEX IF NOT EXISTS ix_clinicalnlp_vectors_drug_product_hnsw
    ON clinicalnlp.medical_vectors
    USING hnsw (embedding vector_cosine_ops)
    WHERE is_active
      AND collection_name = 'drug_terms'
      AND entity_type = 'product';

CREATE INDEX IF NOT EXISTS ix_clinicalnlp_vectors_procedure_hnsw
    ON clinicalnlp.medical_vectors
    USING hnsw (embedding vector_cosine_ops)
    WHERE is_active
      AND collection_name = 'procedure_terms';

CREATE INDEX IF NOT EXISTS ix_clinicalnlp_vectors_anatomy_hnsw
    ON clinicalnlp.medical_vectors
    USING hnsw (embedding vector_cosine_ops)
    WHERE is_active
      AND collection_name = 'anatomy_terms';

CREATE INDEX IF NOT EXISTS ix_clinicalnlp_vectors_emergency_hnsw
    ON clinicalnlp.medical_vectors
    USING hnsw (embedding vector_cosine_ops)
    WHERE is_active
      AND collection_name = 'emergency_terms';

DROP INDEX IF EXISTS clinicalnlp.ix_clinicalnlp_medical_vectors_hnsw;

INSERT INTO clinicalnlp.schema_migrations(version, description)
VALUES ('005', 'Partition active medical vector ANN indexes')
ON CONFLICT (version) DO NOTHING;

COMMIT;
