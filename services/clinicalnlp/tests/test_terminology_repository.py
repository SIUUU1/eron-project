import sqlite3
import unittest
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from clinicalnlp_api3.terminology_repository import (
    ExactIdentityBatch,
    ShadowTerminologyRepository,
    PostgresTerminologyRepository,
    SqliteTerminologyRepository,
    TerminologyEntity,
    TerminologyIdentity,
)


def _create_dictionary_fixture(root: Path) -> None:
    with sqlite3.connect(root / "ERON_의약품용어_DB_v1.sqlite") as db:
        db.executescript(
            """
            CREATE TABLE ingredients(
                ingredient_id INTEGER, canonical_ko TEXT, canonical_en TEXT,
                concept_status TEXT
            );
            CREATE TABLE products(
                item_id TEXT, product_name_ko TEXT, product_name_en TEXT
            );
            CREATE TABLE drug_terms(
                term_id INTEGER, entity_type TEXT, entity_id TEXT, term TEXT,
                term_type TEXT, review_status TEXT
            );
            INSERT INTO ingredients VALUES(10, '암로디핀', 'amlodipine', 'official');
            INSERT INTO drug_terms VALUES(1, 'ingredient', '10', 'amlodipine', 'en', 'official');
            """
        )
    with sqlite3.connect(root / "ERON_검사처치시술용어_DB_v1.sqlite") as db:
        db.executescript(
            """
            CREATE TABLE clinical_terms(
                term_id INTEGER, category TEXT, canonical_name_ko TEXT,
                canonical_name_en TEXT, review_status TEXT
            );
            CREATE TABLE term_aliases(
                alias_id INTEGER, term_id INTEGER, alias TEXT,
                alias_type TEXT, review_status TEXT
            );
            """
        )
    with sqlite3.connect(root / "ERON_anatomy_terms.sqlite") as db:
        db.executescript(
            """
            CREATE TABLE anatomical_terms(
                term_id INTEGER, korean_name TEXT, english_name TEXT,
                latin_name TEXT, verification_status TEXT
            );
            CREATE TABLE anatomical_aliases(
                alias_id INTEGER, term_id INTEGER, alias TEXT
            );
            """
        )
    with sqlite3.connect(root / "ERON_응급의학용어_DB_v1.sqlite") as db:
        db.executescript(
            """
            CREATE TABLE terms(
                term_id INTEGER, standard_ko TEXT, standard_en TEXT,
                review_status TEXT
            );
            CREATE TABLE aliases(
                alias_id INTEGER, term_id INTEGER, alias TEXT,
                alias_type TEXT, review_status TEXT
            );
            INSERT INTO terms VALUES(1, '기침', 'cough', 'official');
            INSERT INTO aliases VALUES(1, 1, 'Cough', 'english', 'official');
            """
        )
    with sqlite3.connect(root / "hira_kcd9.sqlite") as db:
        db.executescript(
            """
            CREATE TABLE kcd_codes(
                code TEXT, code_display TEXT, canonical_ko_name TEXT,
                canonical_en_name TEXT, is_complete INTEGER,
                principal_allowed INTEGER, sex_restriction TEXT,
                min_age INTEGER, max_age INTEGER
            );
            CREATE TABLE kcd_terms(
                term_id INTEGER, code TEXT, ko_name TEXT, en_name TEXT,
                is_canonical INTEGER
            );
            INSERT INTO kcd_codes VALUES(
                'R05', 'R05', '기침', 'Cough', 1, 1, NULL, NULL, NULL
            );
            INSERT INTO kcd_terms VALUES(1, 'R05', '기침', 'cough', 1);
            """
        )


class _FakeRepository:
    def __init__(self, *, version, entity, identities):
        self.version = version
        self.entity = entity
        self.identities = identities

    @contextmanager
    def request_session(self):
        yield self

    def lookup(self, collection, entity_id):
        return self.entity

    def exact_identities_many(self, requests):
        return ExactIdentityBatch(self.identities, 1)


class _UnavailableRepository:
    version = "postgres:unavailable"

    @contextmanager
    def request_session(self):
        raise RuntimeError("unavailable")
        yield self

    def lookup(self, collection, entity_id):
        raise RuntimeError("unavailable")

    def exact_identities_many(self, requests):
        raise RuntimeError("unavailable")


class TerminologyRepositoryTests(unittest.TestCase):
    def test_sqlite_repository_returns_exact_identity_and_verified_entity(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _create_dictionary_fixture(root)
            repository = SqliteTerminologyRepository(root)

            with repository.request_session():
                batch = repository.exact_identities_many(
                    (("  COUGH  ", frozenset({"emergency_terms"})),)
                )
                entity = repository.lookup("emergency_terms", "emergency:1")

        self.assertEqual(
            batch.identities,
            ((TerminologyIdentity("emergency_terms", "emergency:1"),),),
        )
        self.assertEqual(batch.statement_count, 1)
        self.assertEqual(
            entity,
            TerminologyEntity(
                collection="emergency_terms",
                entity_id="emergency:1",
                canonical_ko="기침",
                canonical_en="cough",
                review_status="official",
            ),
        )

    def test_sqlite_repository_includes_kcd_in_explicit_exact_search(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _create_dictionary_fixture(root)
            repository = SqliteTerminologyRepository(root)

            batch = repository.exact_identities_many(
                (("cough", frozenset({"kcd9_terms"})),)
            )

        self.assertEqual(
            batch.identities,
            ((TerminologyIdentity("kcd9_terms", "kcd:R05"),),),
        )

    def test_shadow_repository_returns_primary_results_on_mismatch(self):
        primary_entity = TerminologyEntity(
            "emergency_terms", "emergency:1", "기침", "cough", "official"
        )
        primary_identity = TerminologyIdentity("emergency_terms", "emergency:1")
        secondary_identity = TerminologyIdentity("emergency_terms", "emergency:2")
        repository = ShadowTerminologyRepository(
            _FakeRepository(
                version="sqlite:v1",
                entity=primary_entity,
                identities=((primary_identity,),),
            ),
            _FakeRepository(
                version="postgres:v1",
                entity=None,
                identities=((secondary_identity,),),
            ),
        )

        with repository.request_session():
            batch = repository.exact_identities_many(
                (("cough", frozenset({"emergency_terms"})),)
            )
            entity = repository.lookup("emergency_terms", "emergency:1")

        self.assertEqual(batch.identities, ((primary_identity,),))
        self.assertEqual(entity, primary_entity)
        self.assertEqual(repository.mismatch_count, 2)

    def test_shadow_repository_keeps_primary_available_when_postgres_is_down(self):
        primary_identity = TerminologyIdentity("emergency_terms", "emergency:1")
        repository = ShadowTerminologyRepository(
            _FakeRepository(
                version="sqlite:v1",
                entity=None,
                identities=((primary_identity,),),
            ),
            _UnavailableRepository(),
        )

        with repository.request_session():
            batch = repository.exact_identities_many(
                (("cough", frozenset({"emergency_terms"})),)
            )

        self.assertEqual(batch.identities, ((primary_identity,),))
        self.assertEqual(repository.mismatch_count, 2)

    @unittest.skipUnless(
        os.environ.get("CLINICALNLP_REPOSITORY_PARITY") == "1",
        "requires provisioned SQLite and PostgreSQL terminology releases",
    )
    def test_provisioned_postgres_matches_representative_sqlite_exact_results(self):
        root = Path(os.environ["CLINICALNLP_API3_DB_ROOT"])
        database_url = os.environ.get(
            "CLINICALNLP_DATABASE_URL",
            os.environ["DATABASE_URL"],
        )
        sqlite_repository = SqliteTerminologyRepository(root)
        postgres_repository = PostgresTerminologyRepository(database_url)
        with sqlite3.connect(root / "ERON_응급의학용어_DB_v1.sqlite") as db:
            emergency_query = db.execute(
                "SELECT standard_en FROM terms "
                "WHERE trim(coalesce(standard_en, '')) <> '' LIMIT 1"
            ).fetchone()[0]
        with sqlite3.connect(root / "ERON_의약품용어_DB_v1.sqlite") as db:
            drug_query = db.execute(
                "SELECT term FROM drug_terms "
                "WHERE trim(coalesce(term, '')) <> '' LIMIT 1"
            ).fetchone()[0]
        requests = (
            (str(emergency_query), frozenset({"emergency_terms"})),
            (str(drug_query), frozenset({"drug_terms"})),
        )

        sqlite_batch = sqlite_repository.exact_identities_many(requests)
        postgres_batch = postgres_repository.exact_identities_many(requests)
        shadow_repository = ShadowTerminologyRepository(
            sqlite_repository,
            postgres_repository,
        )
        with shadow_repository.request_session():
            shadow_batch = shadow_repository.exact_identities_many(requests)

        self.assertEqual(postgres_batch.identities, sqlite_batch.identities)
        self.assertEqual(shadow_batch.identities, sqlite_batch.identities)
        self.assertGreater(sum(map(len, sqlite_batch.identities)), 0)
        for identities in sqlite_batch.identities:
            for identity in identities:
                self.assertEqual(
                    postgres_repository.lookup(
                        identity.collection,
                        identity.entity_id,
                    ),
                    sqlite_repository.lookup(
                        identity.collection,
                        identity.entity_id,
                    ),
                )
                with shadow_repository.request_session():
                    self.assertEqual(
                        shadow_repository.lookup(
                            identity.collection,
                            identity.entity_id,
                        ),
                        sqlite_repository.lookup(
                            identity.collection,
                            identity.entity_id,
                        ),
                    )
        self.assertEqual(shadow_repository.mismatch_count, 0)


if __name__ == "__main__":
    unittest.main()
