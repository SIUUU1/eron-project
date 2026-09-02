# ClinicalNLP service

This directory contains the isolated ER:ON clinical draft service. It accepts
Whisper JSON at `POST /v2/clinical-workflows` and returns a reviewable
`clinical-workflow-v2` draft. It does not persist, complete, or sign records.

## Runtime storage contract

The production process is PostgreSQL-only for searchable and mutable data:

- medical terminology and KCD exact lookup
- official Korean RAW exact entries
- medical pgvector search
- policy documents, chunks, FTS, and pgvector search
- versioned clinician-approved aliases

scispaCy models and the UMLS linker cache are large executable/model assets,
not database rows, so they remain a read-only Linux runtime mount. Guardrail and
threshold JSON remain versioned application configuration in the image.

SQLite dictionary and vector files are accepted only by one-time import and
parity tools. The HTTP service has no SQLite backend flag, path setting, or
SQLite bind mount. Legacy SQLite/shadow environment variables are rejected at
startup so an operator cannot mistake a PostgreSQL process for a rollback.

Candidate retrieval uses UMLS semantic types as the primary collection route.
Grounded clinical-field hints narrow compatible routes and are consulted only
as a conditional fallback when the semantic route returns no candidate.

## Local configuration

Copy `.env.example` to `.env` and set `OLLAMA_API_KEY`. The real `.env` is
ignored by Git. Compose injects the repository-root `DATABASE_URL` as
`CLINICALNLP_DATABASE_URL`; set it directly only when running outside Compose.

The runtime translates dialogue before retrieval, runs UMLS and bounded
translated-term lookup, then applies official Korean RAW exact matching. The
RAW matcher is built once at startup from active PostgreSQL canonical emergency,
procedure, anatomy, and drug-ingredient concepts. Product names, KCD codes,
unapproved aliases, fuzzy matching, and vector results are not treated as RAW
exact evidence.

Clinical field routing and terminology retrieval are separate decisions. The
extractor assigns conversation-grounded facts to record fields. Candidate
review items reuse those evidence assignments and expose only semantic types
allowed by the target field. Candidates are never automatically confirmed and
RAW evidence is never rewritten.

Local Compact rollout is controlled by `CLINICALNLP_COMPACT_V3_MODE`.
`legacy` is the default and preserves the established Compact v3 generator.
`lean_shadow` keeps that result authoritative while Compact v3.1 Lean runs only
for local comparison. `lean_primary` uses the sparse Lean contract while
preserving the public `clinical-workflow-v2` and existing UI response. The older
`off`, `compare`, and `primary` values remain available during rollout. Do not
use `lean_shadow` in production because it intentionally adds a second clinical
generation call.

Lean uses one model call for ordinary inputs. Predicted or actual oversized
inputs use bounded segment chunks for atomic Fact extraction followed by one
field-writing call. A failed chunk preserves successful facts, reports the
failed segment IDs as `partial`, and never converts that range to
`NOT_ASSESSED`. The model receives only candidate reference, segment, surface,
canonical term, semantic types, and source; full immutable candidate snapshots
remain in backend memory for deterministic validation.

`GET /health` returns HTTP 200 only when required configuration and active
PostgreSQL releases are ready. Missing optional UMLS assets use the bounded
n-gram fallback. Telemetry values diagnose latency only; they are not confidence
scores and cannot change validation decisions.

## One-time PostgreSQL import

Apply the versioned schema first:

```sh
python3 database/scripts/apply_clinicalnlp_schema.py
```

Import medical dictionary/KCD releases using the database import tool, then
import medical vectors and policy vectors while the legacy read-only assets are
temporarily available:

```sh
python3 database/scripts/import_clinicalnlp_dictionaries.py \
  --dictionary-root "$PWD/runtime/clinicalnlp/medical-dictionaries"

docker compose --profile clinical run --rm --no-deps \
  -v "$PWD/runtime/clinicalnlp/vectors:/runtime/vectors:ro" clinicalnlp \
  python scripts/import_medical_vectors.py \
  --index /runtime/vectors/api3_vectors.sqlite

docker compose --profile clinical run --rm --no-deps \
  -v "$PWD/runtime/clinicalnlp/policy:/runtime/policy:ro" clinicalnlp \
  python scripts/import_policy_index.py \
  --index /runtime/policy/policy_vectors.sqlite
```

Each import is idempotent. A changed source hash creates a new immutable release
instead of overwriting the old one. The policy importer preserves partial dates
such as `2024-06`, document/chunk hashes, page/article traceability, and the
existing 256-dimensional embeddings.

After the import and parity checks pass, the HTTP container needs only
PostgreSQL and the UMLS runtime. Keep legacy SQLite files outside the runtime
mount as recoverable migration archives until the team backup policy permits
removal.

## Docker Compose profile

The service is opt-in under the `clinical` profile. Port `8765` is exposed only
to `eron-network`; it is not published on the host. The only host directory
required by the running container is:

```text
runtime/clinicalnlp/
└─ scispacy/                  # optional Linux-compatible UMLS runtime
   ├─ .venv/
   └─ cache/
```

Override the parent directory in the repository-root `.env` when needed:

```dotenv
CLINICALNLP_RUNTIME_ROOT=/absolute/path/to/clinicalnlp-runtime
```

Compose mounts `${CLINICALNLP_RUNTIME_ROOT}/scispacy` at
`/runtime/scispacy:ro`. There is no writable ClinicalNLP state mount; alias
feedback lives in PostgreSQL.

### Prepare scispaCy/UMLS

Run this from WSL or another Linux `amd64` environment with Docker. Reuse only a
platform-independent UMLS cache; never copy a Windows virtual environment.

```sh
python3 services/clinicalnlp/scripts/setup_scispacy_runtime.py \
  --runtime-root "$PWD/runtime/clinicalnlp/scispacy" \
  --cache-source "/path/to/legacy/runtime/scispacy/cache"

python3 services/clinicalnlp/scripts/verify_scispacy_runtime.py \
  --runtime-root "$PWD/runtime/clinicalnlp/scispacy" \
  --timeout 180
```

The first UMLS load can take several minutes. A healthy HTTP endpoint alone does
not prove that UMLS is active; a synthetic request should emit candidates whose
provenance source is `UMLS`.

Start or rebuild only ClinicalNLP:

```sh
docker compose --profile clinical config
docker compose --profile clinical up -d --build clinicalnlp
docker compose ps clinicalnlp
docker compose logs -f clinicalnlp
```

Starting Compose without `--profile clinical` leaves the existing stack
unchanged. For a temporary smoke test without UMLS, set
`CLINICALNLP_UMLS_ENABLED=false`; PostgreSQL terminology and policy retrieval
remain available.
