"""Stable storage contract shared by medical vector builders and adapters."""

VECTOR_DIMENSIONS = 256
VECTOR_INDEX_SCHEMA_VERSION = "medical-vector-index-v2"
MEDICAL_VECTOR_COLLECTIONS = (
    "drug_terms",
    "procedure_terms",
    "anatomy_terms",
    "emergency_terms",
)
MEDICAL_VECTOR_MODEL_VERSION = "medical-hash-embedding-v1"
