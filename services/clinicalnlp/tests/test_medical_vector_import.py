from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from clinicalnlp_api3.medical_vector_import import (
    VectorIndexMetadata,
    build_release_descriptor,
    validate_vector_metadata,
)


class MedicalVectorImportContractTests(unittest.TestCase):
    def test_release_identity_is_collection_scoped_and_content_addressed(self) -> None:
        metadata = VectorIndexMetadata(
            collection="drug_terms",
            source_sha256="a" * 64,
            schema_version="medical-vector-index-v2",
            dimensions=256,
            row_count=3,
        )
        with tempfile.TemporaryDirectory() as directory:
            index_path = Path(directory) / "vectors.sqlite"
            index_path.write_bytes(b"first")
            first = build_release_descriptor(index_path, metadata)
            second = build_release_descriptor(index_path, metadata)
            index_path.write_bytes(b"second")
            changed = build_release_descriptor(index_path, metadata)

        self.assertEqual(first, second)
        self.assertEqual(first.source_id, "medical_vector:drug_terms")
        self.assertEqual(len(first.content_hash), 64)
        self.assertNotEqual(first.content_hash, changed.content_hash)

    def test_only_runtime_medical_collections_are_accepted(self) -> None:
        valid = VectorIndexMetadata(
            collection="emergency_terms",
            source_sha256="b" * 64,
            schema_version="medical-vector-index-v2",
            dimensions=256,
            row_count=1,
        )
        validate_vector_metadata(valid)

        with self.assertRaisesRegex(ValueError, "unsupported medical vector collection"):
            validate_vector_metadata(
                VectorIndexMetadata(
                    collection="kcd9_terms",
                    source_sha256="b" * 64,
                    schema_version="medical-vector-index-v2",
                    dimensions=256,
                    row_count=1,
                )
            )

    def test_schema_and_dimension_mismatch_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "schema version"):
            validate_vector_metadata(
                VectorIndexMetadata(
                    collection="anatomy_terms",
                    source_sha256="c" * 64,
                    schema_version="legacy",
                    dimensions=256,
                    row_count=1,
                )
            )
        with self.assertRaisesRegex(ValueError, "dimensions"):
            validate_vector_metadata(
                VectorIndexMetadata(
                    collection="anatomy_terms",
                    source_sha256="c" * 64,
                    schema_version="medical-vector-index-v2",
                    dimensions=128,
                    row_count=1,
                )
            )


if __name__ == "__main__":
    unittest.main()
