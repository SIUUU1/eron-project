# ClinicalNLP service

This directory contains the isolated ER:ON clinical draft service. It accepts
Whisper JSON at `POST /v2/clinical-workflows` and returns a reviewable
`clinical-workflow-v2` draft. It does not persist, complete, or sign records.

## Local configuration

Copy `.env.example` to `.env` and set `OLLAMA_API_KEY`. The real `.env` is
ignored by Git. Do not copy the key into the repository root, Docker image, or
logs.

Build and run the service independently:

```sh
docker build -t eron-clinicalnlp services/clinicalnlp
docker run --rm --env-file services/clinicalnlp/.env -p 8765:8765 \
  -v /path/to/runtime:/runtime:ro \
  -v /path/to/alias-state:/runtime/state \
  eron-clinicalnlp
```

The dictionary, medical-vector, policy-vector, and scispaCy/UMLS assets are
runtime mounts and must not be committed or copied into the image. The alias
database mount is reserved for a future clinician-approved feedback workflow;
the current draft runtime does not read, promote, or apply approved aliases.

Runtime medical-term retrieval translates the dialogue before retrieval, runs
UMLS and bounded translated-term lookup, then applies an official Korean RAW
exact fallback. The RAW fallback is built once in memory from canonical
emergency, procedure, anatomy, and drug-ingredient terms. It excludes product
names, KCD codes, aliases, fuzzy matching, and vector search.

Clinical field routing and terminology retrieval are separate decisions. The
extractor first assigns atomic, conversation-grounded facts to emergency-record
fields. Candidate review items then reuse those evidence assignments and expose
only semantic types allowed by the target field (for example, drug candidates
for medications and disease candidates for impression). A segment may support
multiple fields, but an individual source span is routed to its matching atomic
fact. Filtering never confirms a candidate, rewrites RAW evidence, or invents a
missing clinical fact.

`GET /health` returns HTTP 200 only when required configuration and dictionary
assets are ready. Missing configuration or assets keeps the process available
for diagnostics but returns HTTP 503. Missing optional UMLS assets use the
existing n-gram fallback.

Every draft response includes non-clinical `telemetry` with
`translation_ms`, `translation_calls`, `umls_ms`, `dictionary_ms`, `vector_ms`,
and `clinical_extraction_ms`. These values diagnose latency only; they are not
confidence scores and must not affect candidate ranking or validation results.
Dictionary queries reuse read-only SQLite handles for the duration of one
resolver request. Exact lookups are grouped into batches of at most 64 queries
per collection, and sqlite-vec is loaded at most once per request.

## Docker Compose profile

The repository Compose manifest registers this service under the opt-in
`clinical` profile. Its port `8765` is exposed only to `eron-network`; it is not
published on the host.

Prepare these two host directories before starting the profile:

```text
runtime/clinicalnlp/
├─ medical-dictionaries/
├─ vectors/api3_vectors.sqlite
├─ policy/policy_vectors.sqlite
├─ scispacy/                  # optional Linux-compatible UMLS runtime
└─ state/                     # required empty nested-mount point

runtime/clinicalnlp-state/
└─ alias_feedback.sqlite      # created by the service when needed
```

The runtime directory is mounted read-only. `runtime/clinicalnlp/state/` must
exist so Docker can attach the separate writable state mount below that
read-only mount; it does not hold the writable data itself. The separate
`runtime/clinicalnlp-state/` directory is mounted at `/runtime/state` and must
be writable by container UID/GID `10001`. On Linux, pre-create it with suitable
ownership instead of allowing Docker to create a root-owned directory.

Only developers who start the `clinical` profile need these assets. Normal
Compose usage without that profile does not require them.

### Prepare repository-local assets from WSL

From the repository root, create the default host directories:

```sh
mkdir -p runtime/clinicalnlp/medical-dictionaries
mkdir -p runtime/clinicalnlp/vectors
mkdir -p runtime/clinicalnlp/policy
mkdir -p runtime/clinicalnlp/state
mkdir -p runtime/clinicalnlp-state
```

When migrating assets from the standalone `ClinicalNLP_API3` project, point
`LEGACY_CLINICALNLP_ROOT` at that checkout and copy only the portable
dictionary and SQLite assets:

```sh
LEGACY_CLINICALNLP_ROOT=/mnt/c/Users/<windows-user>/ERON/ClinicalNLP_API3

cp -a "$LEGACY_CLINICALNLP_ROOT/local_assets/medical_dictionaries/." \
  runtime/clinicalnlp/medical-dictionaries/
cp "$LEGACY_CLINICALNLP_ROOT/data/api3_vectors.sqlite" \
  runtime/clinicalnlp/vectors/
cp "$LEGACY_CLINICALNLP_ROOT/data/policy_vectors.sqlite" \
  runtime/clinicalnlp/policy/
```

### Prepare the optional scispaCy/UMLS runtime

Run this step from WSL or another Linux `amd64` environment with Docker. Reuse
only the standalone project's platform-independent UMLS cache; never copy its
Windows `.venv` into the container runtime.

```sh
python3 services/clinicalnlp/scripts/setup_scispacy_runtime.py \
  --runtime-root "$PWD/runtime/clinicalnlp/scispacy" \
  --cache-source "$LEGACY_CLINICALNLP_ROOT/runtime/scispacy/cache"
```

The setup command copies the cache, creates a Linux Python 3.12 environment
through the same `python:3.12-slim` base used by ClinicalNLP, installs the pinned
packages from `services/clinicalnlp/scispacy-requirements.txt`, and starts the
real worker once. It succeeds only after the worker loads the local UMLS 2022AB
snapshot and reports `ready`.

Re-run the non-mutating verification independently when troubleshooting:

```sh
python3 services/clinicalnlp/scripts/verify_scispacy_runtime.py \
  --runtime-root "$PWD/runtime/clinicalnlp/scispacy" \
  --timeout 180
```

Restart only ClinicalNLP after preparing or replacing the runtime:

```sh
docker compose --profile clinical restart clinicalnlp
```

The first UMLS load can take several minutes and consumes substantially more
memory than n-gram fallback. Confirm container memory and end-to-end latency on
the deployment host. A healthy HTTP service alone does not prove that optional
UMLS linking is active; verify that a synthetic request emits candidates whose
source is `UMLS`.

Verify the required assets before starting Docker:

```sh
test -d runtime/clinicalnlp/medical-dictionaries
test -s runtime/clinicalnlp/vectors/api3_vectors.sqlite
test -s runtime/clinicalnlp/policy/policy_vectors.sqlite
test -d runtime/clinicalnlp/state
test -d runtime/clinicalnlp-state
```

Do not stage `runtime/`, the SQLite files, dictionaries, or real `.env` files
in Git. Provision the equivalent directories once on each OCI deployment
host or persistent volume.

Override the default host paths from the shell or the repository-root `.env`:

```dotenv
CLINICALNLP_RUNTIME_ROOT=/absolute/path/to/clinicalnlp-runtime
CLINICALNLP_STATE_ROOT=/absolute/path/to/clinicalnlp-state
```

When Docker Compose is invoked from WSL, use WSL paths such as
`/mnt/c/Users/<windows-user>/...`. When it is invoked from Windows PowerShell,
use Windows paths such as `C:/Users/<windows-user>/...`. The directories must
already exist; Compose intentionally does not create missing bind sources.

These host-path variables are separate from `services/clinicalnlp/.env`.
The service-specific file owns `OLLAMA_API_KEY` and container-side settings;
Compose interpolation for bind-mount sources happens before that file is
loaded.

Start only ClinicalNLP:

```sh
docker compose --profile clinical config
docker compose --profile clinical up -d --build clinicalnlp
docker compose ps clinicalnlp
docker compose logs -f clinicalnlp
```

Starting Compose without `--profile clinical` leaves the existing stack
unchanged. The optional `env_file.required` declaration needs Docker Compose
2.24 or newer. OCI must provide a Linux-compatible scispaCy environment;
the existing Windows virtual environment cannot execute in the Linux image.
For an initial container smoke test without a Linux UMLS runtime, set
`CLINICALNLP_UMLS_ENABLED=false` in `services/clinicalnlp/.env`; dictionary and
vector retrieval remain available. Re-enable it only after mounting a
Linux-compatible `runtime/clinicalnlp/scispacy/` environment.
