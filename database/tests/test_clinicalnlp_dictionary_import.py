from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
import uuid


REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = tuple(
    sorted((REPO / "database" / "init").glob("[0-9][0-9]_clinicalnlp*.sql"))
)
IMPORTER = REPO / "database" / "scripts" / "import_clinicalnlp_dictionaries.py"


def _dotenv_value(key: str) -> str:
    value = os.environ.get(key)
    if value:
        return value
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"{key} is required")


def _create_dictionary_fixture(root: Path) -> None:
    with closing(sqlite3.connect(root / "ERON_의약품용어_DB_v1.sqlite")) as db:
        db.executescript(
            """
            CREATE TABLE ingredients(
                ingredient_id INTEGER PRIMARY KEY, ingredient_code TEXT,
                canonical_ko TEXT, canonical_en TEXT, concept_status TEXT
            );
            CREATE TABLE products(
                item_id TEXT PRIMARY KEY, product_name_ko TEXT,
                product_name_en TEXT, source_status TEXT
            );
            CREATE TABLE drug_terms(
                term_id INTEGER PRIMARY KEY, entity_type TEXT, entity_id TEXT,
                term TEXT, normalized_term TEXT, language TEXT, term_type TEXT,
                source_id TEXT, review_status TEXT
            );
            CREATE TABLE stt_aliases(
                alias_id INTEGER PRIMARY KEY, alias TEXT, normalized_alias TEXT,
                entity_type TEXT, entity_id TEXT, alias_type TEXT,
                review_status TEXT
            );
            INSERT INTO ingredients VALUES(1, 'I001', '암로디핀', 'amlodipine', 'OFFICIAL_CODED');
            INSERT INTO products VALUES('P001', 'M-lodipine Tab.', 'M-lodipine', 'official');
            INSERT INTO drug_terms VALUES
                (1, 'INGREDIENT', '1', '암로디핀', '암로디핀', 'ko', 'canonical', 'S1', 'official'),
                (2, 'INGREDIENT', '1', 'amlodipine', 'amlodipine', 'en', 'canonical', 'S1', 'official'),
                (3, 'PRODUCT', 'P001', 'M-lodipine', 'm-lodipine', 'en', 'product', 'S1', 'official');
            """
        )

    with closing(sqlite3.connect(root / "ERON_검사처치시술용어_DB_v1.sqlite")) as db:
        db.executescript(
            """
            CREATE TABLE clinical_terms(
                term_id INTEGER PRIMARY KEY, category TEXT,
                canonical_name_ko TEXT, canonical_name_en TEXT,
                review_status TEXT, source_id TEXT
            );
            CREATE TABLE term_aliases(
                alias_id INTEGER PRIMARY KEY, term_id INTEGER, alias TEXT,
                normalized_alias TEXT, language TEXT, alias_type TEXT,
                source_id TEXT, review_status TEXT
            );
            INSERT INTO clinical_terms VALUES(1, 'TEST', '심전도', 'electrocardiography', 'official', 'S2');
            INSERT INTO term_aliases VALUES(1, 1, 'ECG', 'ecg', 'en', 'ABBREVIATION', 'S2', 'official');
            """
        )

    with closing(sqlite3.connect(root / "ERON_anatomy_terms.sqlite")) as db:
        db.executescript(
            """
            CREATE TABLE anatomical_terms(
                term_id INTEGER PRIMARY KEY, korean_name TEXT,
                english_name TEXT, latin_name TEXT, entry_type TEXT,
                verification_status TEXT
            );
            CREATE TABLE anatomical_aliases(
                alias_id INTEGER PRIMARY KEY, term_id INTEGER,
                language TEXT, alias TEXT, normalized_alias TEXT
            );
            INSERT INTO anatomical_terms VALUES(1, '안와', 'orbit', 'orbita', 'TERM', 'official');
            INSERT INTO anatomical_aliases VALUES(1, 1, 'ko', '눈확', '눈확');
            """
        )

    with closing(sqlite3.connect(root / "ERON_응급의학용어_DB_v1.sqlite")) as db:
        db.executescript(
            """
            CREATE TABLE terms(
                term_id INTEGER PRIMARY KEY, standard_en TEXT,
                standard_ko TEXT, normalized_en TEXT, normalized_ko TEXT,
                provenance TEXT, review_status TEXT
            );
            CREATE TABLE aliases(
                alias_id INTEGER PRIMARY KEY, term_id INTEGER, alias TEXT,
                normalized_alias TEXT, alias_type TEXT, provenance TEXT,
                review_status TEXT
            );
            INSERT INTO terms VALUES(1, 'dyspnea', '호흡곤란', 'dyspnea', '호흡곤란', 'official', 'official');
            INSERT INTO aliases VALUES
                (1, 1, 'shortness of breath', 'shortness of breath', 'SYNONYM', 'official', 'official'),
                (2, 1, 'Shortness Of Breath', 'shortness of breath', 'SYNONYM', 'official', 'official');
            """
        )

    with closing(sqlite3.connect(root / "hira_kcd9.sqlite")) as db:
        db.executescript(
            """
            CREATE TABLE kcd_codes(
                code TEXT PRIMARY KEY, code_display TEXT,
                canonical_ko_name TEXT, canonical_en_name TEXT,
                is_complete INTEGER, principal_allowed INTEGER,
                infection_class TEXT, sex_restriction TEXT,
                max_age INTEGER, min_age INTEGER,
                medicine_type TEXT, is_new INTEGER, source_row INTEGER
            );
            CREATE TABLE kcd_terms(
                term_id INTEGER PRIMARY KEY, code TEXT, ko_name TEXT,
                en_name TEXT, is_canonical INTEGER, source_row INTEGER
            );
            INSERT INTO kcd_codes VALUES('J44.9', 'J44.9', '상세불명의 만성 폐색성 폐질환', 'COPD, unspecified', 1, 1, NULL, NULL, NULL, NULL, NULL, 0, 1);
            INSERT INTO kcd_terms VALUES
                (1, 'J44.9', '상세불명의 만성 폐색성 폐질환', 'COPD, unspecified', 1, 1),
                (2, 'J44.9', '만성 폐쇄성 폐질환', 'chronic obstructive pulmonary disease', 0, 2),
                (3, 'J44.9', '만성 폐쇄성 폐질환', 'COPD', 0, 3);
            """
        )


class ClinicalNlpDictionaryImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pg_user = _dotenv_value("POSTGRES_USER")
        cls.database = f"eron_clinicalnlp_import_{uuid.uuid4().hex[:12]}"
        cls._postgres("createdb", "-U", cls.pg_user, cls.database)
        for migration in MIGRATIONS:
            cls._postgres(
                "psql", "-U", cls.pg_user, "-d", cls.database,
                "-v", "ON_ERROR_STOP=1", "--no-psqlrc", "-f", "-",
                input_text=migration.read_text(encoding="utf-8"),
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._postgres("dropdb", "-U", cls.pg_user, "--if-exists", cls.database)

    @classmethod
    def _postgres(cls, *arguments: str, input_text: str | None = None) -> str:
        process = subprocess.run(
            ["docker", "compose", "exec", "-T", "postgres", *arguments],
            cwd=REPO,
            input=input_text,
            capture_output=True,
            text=True,
        )
        if process.returncode != 0:
            raise AssertionError(process.stderr.strip() or process.stdout.strip())
        return process.stdout.strip()

    def test_import_is_idempotent_and_preserves_dictionary_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            drug_hash = hashlib.sha256(
                (root / "ERON_의약품용어_DB_v1.sqlite").read_bytes()
            ).hexdigest()
            self._postgres(
                "psql", "-U", self.pg_user, "-d", self.database,
                "-v", "ON_ERROR_STOP=1", "--no-psqlrc", "-c",
                "INSERT INTO clinicalnlp.source_releases("
                "source_kind, source_id, version, content_hash, is_active) VALUES ("
                f"'MEDICAL_DICTIONARY', 'drug_dictionary', 'legacy-v1', '{drug_hash}', TRUE)",
            )
            command = [
                "python3",
                str(IMPORTER),
                "--database",
                self.database,
                "--dictionary-root",
                str(root),
            ]
            results = []
            for _ in range(2):
                process = subprocess.run(
                    command,
                    cwd=REPO,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(process.returncode, 0, process.stderr)
                results.append(json.loads(process.stdout))

        expected = {
            "status": "ready",
            "source_release_count": 5,
            "medical_concept_count": 5,
            "medical_term_count": 14,
            "kcd_code_count": 1,
            "kcd_term_count": 3,
        }
        self.assertEqual(results[0], expected)
        self.assertEqual(results[1], expected)


if __name__ == "__main__":
    unittest.main()
