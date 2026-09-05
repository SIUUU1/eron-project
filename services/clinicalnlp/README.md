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

Draft responses separate Ollama latency for both translation and clinical
generation. The `translation_*` and `clinical_llm_*` metrics include provider
call/retry counts, client-observed `http_ms`, and Ollama-reported `provider_ms`,
`provider_load_ms`, `prompt_eval_ms`, and `token_eval_ms`. The corresponding
`unattributed_http_ms` is the non-negative difference between client HTTP time
and Ollama `total_duration`; it can include transport, remote queueing, response
transfer, and client parsing, so it must not be interpreted as TLS time alone.
Clinical generation telemetry also exposes Fact chunk, field-group fallback,
length fallback, repair, regeneration, and failed-segment counts so an extra
provider call can be attributed before changing prompts or chunk sizes.
`clinical_llm_validation_failure_reasons` records schema paths and property
names that triggered repair without copying clinical values into telemetry.
When a term-like Fact (`MATCHED_TERM` or the model's non-contract `TERM` alias)
omits a usable `candidate_ref` but preserves explicit text and valid evidence,
the Fact contract safely downgrades only that item to `UNMATCHED_TERM`; it never
invents a candidate or clinical text. Other schema-invalid Fact kinds are not
coerced into terms.
`clinical_llm_fact_recovery_count` and
`clinical_llm_fact_recovery_reasons` expose this structural recovery without
copying the fact text into telemetry. When an invalid Fact has valid source
segment IDs but cannot be safely downgraded, valid sibling Facts are preserved
and only those source segments are retried. Malformed roots and Facts without
usable source IDs retain the bounded whole-response recovery behavior. The
`clinical_llm_fact_targeted_retry_count` and
`clinical_llm_fact_preserved_count` metrics distinguish this smaller retry from
a whole-chunk repair.
Field generation separately verifies that every `fact_refs` value exists in the
request Fact Registry. When a sparse field response copies segment IDs instead,
valid sibling fields are preserved and only fields with dangling references are
requested again. `clinical_llm_field_reference_retry_count` and
`clinical_llm_field_preserved_count` expose this path; an unresolved retry is
reported as a field-generation review issue rather than publishing invalid
references.
Chunked Fact extraction runs at most three independent chunks concurrently;
the reported worker count records the concurrency selected for the request.
Fact chunks use a stage-specific extraction contract; the complete field
writing policy is sent only to the final field-writing call instead of being
repeated for every Fact chunk.
Translation batch telemetry also reports the planned batch count, target/context
segment counts, elapsed time, response-error bisections, failed segments, HTTP
429 responses, partial retries, preserved segments, and retry reasons. Retry
reasons distinguish invalid JSON, missing segments, empty translations, output
length exhaustion, and invalid `medical_terms` values. A partially valid model
response keeps valid segment translations and retries only failed segment IDs;
only the remaining failures are bisected if that targeted retry is incomplete.
Planned translation batches are evenly bounded to at most 12 target segments
before the existing token-budget check; this avoids predictable output-length
failures. Two or more planned batches run in ordered waves of at most two
workers. Each worker owns its counters and provider diagnostics, and the caller
merges results and telemetry in source-batch order after the wave completes.
After the provider retry is exhausted, HTTP 429 finishes the current wave but
stops later waves without response-error bisection; successful translations in
the same or earlier wave are preserved as a partial result instead of
multiplying rate-limited requests. `translation_worker_count` records whether
the request selected zero, one, or two workers.

Medical retrieval telemetry is diagnostic and additive. Collection metrics
report vector batch, query, SQL statement, accepted-candidate, empty-query, and
elapsed-time totals. The drug lane also reports `ingredient` and `product` SQL
time and raw result counts separately. Search-stage counters distinguish exact
lookups, vector fallbacks, UMLS surface/canonical queries, semantic fallbacks,
and n-gram fallbacks. These counters observe the current retrieval behavior;
they do not alter ranking, limits, routing, or candidate confirmation.

UMLS telemetry decomposes the client-observed `umls_ms` into worker batches,
fallbacks, input and detected-span counts, mention detection, entity linking,
extractor time, and worker overhead. `umls_model_load_ms` is the worker-reported
process load duration and is recorded as a maximum rather than summed across
batches. `umls_worker_overhead_ms` is the non-negative difference between the
parent's worker-call time and the worker-reported extraction time, so it can
include readiness waits, scheduling, and IPC. Its cold-start subset is reported
only when worker status explicitly showed that the worker was not ready before
the call. These timings may overlap earlier workflow stages because worker
startup is asynchronous and must not be added together as an end-to-end total.

The service exposes separate liveness and readiness checks. `/health` reports
whether the HTTP draft runtime was constructed, while `/ready` returns success
only after the optional UMLS worker is ready (or immediately when UMLS is
disabled). Draft requests received during UMLS warm-up return a bounded 503
instead of occupying the request for the worker timeout. The container health
check uses `/ready` and gives the immutable scispaCy/UMLS assets a four-minute
startup grace period.

For a one-off local audit of the queries that actually reach pgvector, run the
evaluation-only harness with an approved synthetic or de-identified Whisper
JSON file:

```sh
docker compose --profile clinical run --rm --no-deps \
  -v "$PWD/services/clinicalnlp:/app:ro" \
  -v "/absolute/path/to/whisper.json:/evaluation/input.json:ro" \
  clinicalnlp python scripts/evaluate_vector_fallback.py \
  --input /evaluation/input.json
```

The default report includes only query hashes, lengths, routed collections,
candidate counts, and empty-result flags. `--full-trace` additionally exports
query text and candidate IDs and must be used only for an explicitly approved
evaluation export. Neither mode changes retrieval or the generated draft.

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
