-- =====================================================================
-- ER:ON ClinicalNLP PostgreSQL storage — migration 001
--
-- PostgreSQL owns searchable medical/policy data, vectors, and mutable
-- alias feedback. scispaCy models and the UMLS linker cache remain runtime
-- files mounted into the ClinicalNLP container.
--
-- This migration is intentionally idempotent. docker-entrypoint applies it
-- to a fresh volume; apply_clinicalnlp_schema.py applies it to an existing DB.
-- =====================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE SCHEMA IF NOT EXISTS clinicalnlp;

CREATE TABLE IF NOT EXISTS clinicalnlp.schema_migrations (
    version      TEXT PRIMARY KEY,
    description  TEXT        NOT NULL,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A changed source hash is a new immutable release, never an overwrite.
CREATE TABLE IF NOT EXISTS clinicalnlp.source_releases (
    release_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_kind   TEXT        NOT NULL CHECK (
        source_kind IN ('MEDICAL_DICTIONARY', 'KCD', 'POLICY', 'VECTOR')
    ),
    source_id     TEXT        NOT NULL,
    version       TEXT        NOT NULL,
    content_hash  TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    is_active     BOOLEAN     NOT NULL DEFAULT FALSE,
    loaded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (source_kind, source_id, version, content_hash)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_clinicalnlp_active_source_release
    ON clinicalnlp.source_releases (source_kind, source_id)
    WHERE is_active;

CREATE TABLE IF NOT EXISTS clinicalnlp.medical_concepts (
    concept_pk        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_release_id BIGINT      NOT NULL REFERENCES clinicalnlp.source_releases(release_id),
    collection_name   TEXT        NOT NULL CHECK (
        collection_name IN (
            'drug_terms', 'procedure_terms', 'anatomy_terms', 'emergency_terms'
        )
    ),
    entity_id         TEXT        NOT NULL,
    entity_type       TEXT,
    canonical_ko      TEXT,
    canonical_en      TEXT,
    review_status     TEXT        NOT NULL,
    source_kind       TEXT        NOT NULL,
    payload           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_release_id, collection_name, entity_id)
);

CREATE INDEX IF NOT EXISTS ix_clinicalnlp_concepts_collection_entity
    ON clinicalnlp.medical_concepts (collection_name, entity_id);

CREATE TABLE IF NOT EXISTS clinicalnlp.medical_terms (
    term_pk         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    concept_pk      BIGINT      NOT NULL REFERENCES clinicalnlp.medical_concepts(concept_pk) ON DELETE CASCADE,
    source_text     TEXT        NOT NULL,
    normalized_term TEXT        NOT NULL,
    language        TEXT        NOT NULL CHECK (language IN ('ko', 'en', 'la', 'mixed', 'unknown')),
    term_type       TEXT        NOT NULL,
    review_status   TEXT        NOT NULL,
    source_kind     TEXT        NOT NULL,
    search_document TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(source_text, '') || ' ' || coalesce(normalized_term, ''))
    ) STORED,
    UNIQUE (concept_pk, normalized_term, language, term_type)
);

CREATE INDEX IF NOT EXISTS ix_clinicalnlp_terms_exact
    ON clinicalnlp.medical_terms (normalized_term);
CREATE INDEX IF NOT EXISTS ix_clinicalnlp_terms_trgm
    ON clinicalnlp.medical_terms USING gin (normalized_term gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_clinicalnlp_terms_fts
    ON clinicalnlp.medical_terms USING gin (search_document);

CREATE TABLE IF NOT EXISTS clinicalnlp.medical_vectors (
    vector_pk     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    concept_pk    BIGINT      NOT NULL REFERENCES clinicalnlp.medical_concepts(concept_pk) ON DELETE CASCADE,
    source_text   TEXT        NOT NULL,
    embedding     VECTOR(256) NOT NULL,
    model_version TEXT        NOT NULL,
    payload       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (concept_pk, source_text, model_version)
);

CREATE TABLE IF NOT EXISTS clinicalnlp.kcd_codes (
    kcd_code_pk       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_release_id BIGINT  NOT NULL REFERENCES clinicalnlp.source_releases(release_id),
    code              TEXT    NOT NULL,
    code_display      TEXT,
    canonical_ko_name TEXT,
    canonical_en_name TEXT,
    is_complete       BOOLEAN NOT NULL DEFAULT FALSE,
    principal_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    sex_restriction   TEXT,
    min_age           INTEGER,
    max_age           INTEGER,
    payload           JSONB   NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (source_release_id, code),
    CHECK (min_age IS NULL OR min_age >= 0),
    CHECK (max_age IS NULL OR max_age >= 0),
    CHECK (min_age IS NULL OR max_age IS NULL OR min_age <= max_age)
);

CREATE INDEX IF NOT EXISTS ix_clinicalnlp_kcd_codes_code
    ON clinicalnlp.kcd_codes (code);

CREATE TABLE IF NOT EXISTS clinicalnlp.kcd_terms (
    kcd_term_pk    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kcd_code_pk    BIGINT  NOT NULL REFERENCES clinicalnlp.kcd_codes(kcd_code_pk) ON DELETE CASCADE,
    ko_name        TEXT,
    en_name        TEXT,
    normalized_term TEXT   NOT NULL,
    is_canonical   BOOLEAN NOT NULL DEFAULT FALSE,
    search_document TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(ko_name, '') || ' ' || coalesce(en_name, ''))
    ) STORED,
    UNIQUE (kcd_code_pk, normalized_term)
);

CREATE INDEX IF NOT EXISTS ix_clinicalnlp_kcd_terms_exact
    ON clinicalnlp.kcd_terms (normalized_term);
CREATE INDEX IF NOT EXISTS ix_clinicalnlp_kcd_terms_trgm
    ON clinicalnlp.kcd_terms USING gin (normalized_term gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_clinicalnlp_kcd_terms_fts
    ON clinicalnlp.kcd_terms USING gin (search_document);

CREATE TABLE IF NOT EXISTS clinicalnlp.policy_documents (
    document_pk          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_release_id    BIGINT      NOT NULL REFERENCES clinicalnlp.source_releases(release_id),
    source_id            TEXT        NOT NULL,
    source_family_id     TEXT        NOT NULL,
    title                TEXT        NOT NULL,
    document_type        TEXT        NOT NULL,
    usage_scope          TEXT        NOT NULL,
    jurisdiction         TEXT        NOT NULL,
    published_at         DATE,
    snapshot_at          TIMESTAMPTZ NOT NULL,
    source_path          TEXT        NOT NULL,
    source_url           TEXT,
    document_hash        TEXT        NOT NULL CHECK (document_hash ~ '^[0-9a-f]{64}$'),
    basis_type           TEXT        NOT NULL,
    rule_ids             TEXT[]      NOT NULL DEFAULT '{}',
    supersedes_source_id TEXT,
    is_active            BOOLEAN     NOT NULL DEFAULT FALSE,
    extraction_status    TEXT        NOT NULL,
    chunk_count          INTEGER     NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
    UNIQUE (source_release_id, source_id)
);

CREATE INDEX IF NOT EXISTS ix_clinicalnlp_policy_scope_active
    ON clinicalnlp.policy_documents (usage_scope, is_active);
CREATE INDEX IF NOT EXISTS ix_clinicalnlp_policy_rule_ids
    ON clinicalnlp.policy_documents USING gin (rule_ids);

CREATE TABLE IF NOT EXISTS clinicalnlp.policy_chunks (
    chunk_pk      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_pk   BIGINT  NOT NULL REFERENCES clinicalnlp.policy_documents(document_pk) ON DELETE CASCADE,
    chunk_id      TEXT    NOT NULL UNIQUE,
    ordinal       INTEGER NOT NULL CHECK (ordinal >= 0),
    section       TEXT,
    page          INTEGER CHECK (page IS NULL OR page > 0),
    article       TEXT,
    chunk_text    TEXT    NOT NULL,
    rule_ids      TEXT[]  NOT NULL DEFAULT '{}',
    source_path   TEXT    NOT NULL,
    content_hash  TEXT    NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    search_document TSVECTOR GENERATED ALWAYS AS (
        to_tsvector(
            'simple',
            coalesce(section, '') || ' ' || coalesce(article, '') || ' ' || chunk_text
        )
    ) STORED
);

CREATE INDEX IF NOT EXISTS ix_clinicalnlp_policy_chunks_document
    ON clinicalnlp.policy_chunks (document_pk, ordinal);
CREATE INDEX IF NOT EXISTS ix_clinicalnlp_policy_chunks_rule_ids
    ON clinicalnlp.policy_chunks USING gin (rule_ids);
CREATE INDEX IF NOT EXISTS ix_clinicalnlp_policy_chunks_fts
    ON clinicalnlp.policy_chunks USING gin (search_document);

CREATE TABLE IF NOT EXISTS clinicalnlp.policy_vectors (
    chunk_pk      BIGINT      PRIMARY KEY REFERENCES clinicalnlp.policy_chunks(chunk_pk) ON DELETE CASCADE,
    embedding     VECTOR(256) NOT NULL,
    model_version TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_clinicalnlp_policy_vectors_hnsw
    ON clinicalnlp.policy_vectors USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS clinicalnlp.alias_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clinicalnlp.alias_candidates (
    candidate_id       TEXT PRIMARY KEY,
    source_alias       TEXT        NOT NULL,
    normalized_alias   TEXT        NOT NULL,
    collection_name    TEXT        NOT NULL,
    entity_id          TEXT        NOT NULL,
    canonical_ko       TEXT,
    canonical_en       TEXT,
    entity_type        TEXT,
    source_entity_type TEXT,
    status             TEXT        NOT NULL CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED')),
    created_at         TIMESTAMPTZ NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL,
    promoted_version   INTEGER
);

CREATE INDEX IF NOT EXISTS ix_clinicalnlp_alias_candidate_status
    ON clinicalnlp.alias_candidates (status, updated_at);
CREATE INDEX IF NOT EXISTS ix_clinicalnlp_alias_candidate_normalized
    ON clinicalnlp.alias_candidates (normalized_alias);

CREATE TABLE IF NOT EXISTS clinicalnlp.alias_confirmations (
    candidate_id      TEXT        NOT NULL REFERENCES clinicalnlp.alias_candidates(candidate_id) ON DELETE CASCADE,
    actor_hash        TEXT        NOT NULL,
    identity_verified BOOLEAN     NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (candidate_id, actor_hash)
);

CREATE TABLE IF NOT EXISTS clinicalnlp.alias_versions (
    version          INTEGER     PRIMARY KEY,
    created_at       TIMESTAMPTZ NOT NULL,
    promotion_reason TEXT        NOT NULL,
    actor_hash       TEXT,
    manifest_hash    TEXT        NOT NULL CHECK (manifest_hash ~ '^[0-9a-f]{64}$')
);

CREATE TABLE IF NOT EXISTS clinicalnlp.alias_release_entries (
    version            INTEGER NOT NULL REFERENCES clinicalnlp.alias_versions(version) ON DELETE CASCADE,
    candidate_id       TEXT    NOT NULL,
    source_alias       TEXT    NOT NULL,
    normalized_alias   TEXT    NOT NULL,
    collection_name    TEXT    NOT NULL,
    entity_id          TEXT    NOT NULL,
    canonical_ko       TEXT,
    canonical_en       TEXT,
    entity_type        TEXT,
    source_entity_type TEXT,
    PRIMARY KEY (version, candidate_id)
);

INSERT INTO clinicalnlp.schema_migrations(version, description)
VALUES ('001', 'ClinicalNLP PostgreSQL storage schema')
ON CONFLICT (version) DO NOTHING;

COMMIT;
