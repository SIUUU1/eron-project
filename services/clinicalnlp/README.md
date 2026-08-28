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
docker run --rm --env-file services/clinicalnlp/.env -p 8000:8000 \
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
