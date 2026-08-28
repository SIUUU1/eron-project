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
database is the only writable runtime artifact.

`GET /health` returns HTTP 200 only when required configuration and dictionary
assets are ready. Missing configuration or assets keeps the process available
for diagnostics but returns HTTP 503. Missing optional UMLS assets use the
existing n-gram fallback.

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
