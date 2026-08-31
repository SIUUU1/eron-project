"""Import versioned ClinicalNLP medical dictionaries from SQLite to PostgreSQL."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import unicodedata
from typing import Iterable, Iterator, Sequence


REPO = Path(__file__).resolve().parents[2]
DEFAULT_DICTIONARY_ROOT = REPO / "runtime" / "clinicalnlp" / "medical-dictionaries"
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]+$")
IMPORT_SCHEMA_VERSION = "dictionary-import-v2"


@dataclass(frozen=True)
class Concept:
    collection_name: str
    entity_id: str
    entity_type: str | None
    canonical_ko: str | None
    canonical_en: str | None
    review_status: str
    source_kind: str
    payload: dict[str, object]


@dataclass(frozen=True)
class Term:
    collection_name: str
    entity_id: str
    source_text: str
    normalized_term: str
    language: str
    term_type: str
    review_status: str
    source_kind: str
    source_term_id: str


@dataclass(frozen=True)
class KcdCode:
    code: str
    code_display: str | None
    canonical_ko_name: str | None
    canonical_en_name: str | None
    is_complete: bool
    principal_allowed: bool
    sex_restriction: str | None
    min_age: int | None
    max_age: int | None
    payload: dict[str, object]


@dataclass(frozen=True)
class KcdTerm:
    code: str
    ko_name: str | None
    en_name: str | None
    normalized_term: str
    is_canonical: bool
    source_term_id: str


@dataclass(frozen=True)
class SourceAsset:
    source_kind: str
    source_id: str
    path: Path
    concepts: tuple[Concept, ...] = ()
    terms: tuple[Term, ...] = ()
    kcd_codes: tuple[KcdCode, ...] = ()
    kcd_terms: tuple[KcdTerm, ...] = ()


SOURCE_FILES = (
    ("MEDICAL_DICTIONARY", "drug_dictionary", "ERON_의약품용어_DB_v1.sqlite"),
    (
        "MEDICAL_DICTIONARY",
        "procedure_dictionary",
        "ERON_검사처치시술용어_DB_v1.sqlite",
    ),
    ("MEDICAL_DICTIONARY", "anatomy_dictionary", "ERON_anatomy_terms.sqlite"),
    (
        "MEDICAL_DICTIONARY",
        "emergency_dictionary",
        "ERON_응급의학용어_DB_v1.sqlite",
    ),
    ("KCD", "kcd9", "hira_kcd9.sqlite"),
)


def _dotenv_value(key: str) -> str:
    value = os.environ.get(key)
    if value:
        return value
    dotenv = REPO / ".env"
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    raise SystemExit(f"[FATAL] {key} is required in .env or the environment")


def _validated(value: str, *, label: str) -> str:
    if not value or SAFE_IDENTIFIER.fullmatch(value) is None:
        raise SystemExit(f"[FATAL] invalid {label}: {value!r}")
    return value


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(normalized.split())


def _text(value: object) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _language(value: object, declared: object = None) -> str:
    declared_value = str(declared or "").strip().casefold()
    if declared_value in {"ko", "en", "la", "mixed", "unknown"}:
        return declared_value
    text = str(value or "")
    has_korean = re.search(r"[가-힣]", text) is not None
    has_latin = re.search(r"[A-Za-z]", text) is not None
    if has_korean and has_latin:
        return "mixed"
    if has_korean:
        return "ko"
    if has_latin:
        return "en"
    return "unknown"


def _rows(path: Path, query: str) -> Iterator[sqlite3.Row]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        cursor = connection.execute(query)
        while batch := cursor.fetchmany(2_000):
            yield from batch
    finally:
        connection.close()


def _drug_asset(path: Path) -> SourceAsset:
    concepts = tuple(
        Concept(
            collection_name="drug_terms",
            entity_id=f"drug:{str(row['entity_type']).casefold()}:{row['entity_id']}",
            entity_type=str(row["entity_type"]).casefold(),
            canonical_ko=_text(row["canonical_ko"]),
            canonical_en=_text(row["canonical_en"]),
            review_status=str(row["review_status"] or "official"),
            source_kind="drug_dictionary",
            payload={"source_entity_id": str(row["entity_id"])},
        )
        for row in _rows(
            path,
            """
            SELECT DISTINCT d.entity_type, d.entity_id,
                   COALESCE(i.canonical_ko, p.product_name_ko, d.term) canonical_ko,
                   COALESCE(i.canonical_en, p.product_name_en, '') canonical_en,
                   COALESCE(i.concept_status, p.source_status, d.review_status, 'official') review_status
              FROM drug_terms d
              LEFT JOIN ingredients i
                ON lower(d.entity_type)='ingredient'
               AND CAST(i.ingredient_id AS TEXT)=d.entity_id
              LEFT JOIN products p
                ON lower(d.entity_type)='product' AND p.item_id=d.entity_id
             ORDER BY d.entity_type, d.entity_id
            """,
        )
    )
    terms = [
        Term(
            collection_name="drug_terms",
            entity_id=f"drug:{str(row['entity_type']).casefold()}:{row['entity_id']}",
            source_text=str(row["source_text"]).strip(),
            normalized_term=_normalize(row["normalized_term"] or row["source_text"]),
            language=_language(row["source_text"], row["language"]),
            term_type=str(row["term_type"] or "dictionary"),
            review_status=str(row["review_status"] or ""),
            source_kind=str(row["source_kind"] or "dictionary"),
            source_term_id=str(row["source_term_id"]),
        )
        for row in _rows(
            path,
            """
            SELECT entity_type, entity_id, term source_text, normalized_term,
                   language, term_type, review_status, source_id source_kind,
                   'drug_term:' || term_id source_term_id
              FROM drug_terms
             WHERE length(trim(term)) > 0
            UNION ALL
            SELECT entity_type, entity_id, alias, normalized_alias, NULL,
                   'stt_alias:' || COALESCE(alias_type, ''), review_status,
                   'approved_alias', 'stt_alias:' || alias_id
              FROM stt_aliases
             WHERE length(trim(alias)) > 0
            """,
        )
    ]
    return SourceAsset(
        source_kind="MEDICAL_DICTIONARY",
        source_id="drug_dictionary",
        path=path,
        concepts=concepts,
        terms=tuple(terms),
    )


def _procedure_asset(path: Path) -> SourceAsset:
    concepts = tuple(
        Concept(
            "procedure_terms",
            f"procedure:{row['term_id']}",
            str(row["category"] or "").casefold() or None,
            _text(row["canonical_name_ko"]),
            _text(row["canonical_name_en"]),
            str(row["review_status"] or ""),
            "procedure_dictionary",
            {"category": row["category"]},
        )
        for row in _rows(
            path,
            "SELECT term_id, category, canonical_name_ko, canonical_name_en, review_status FROM clinical_terms",
        )
    )
    terms: list[Term] = []
    for row in _rows(
        path,
        "SELECT term_id, canonical_name_ko, canonical_name_en, review_status, source_id FROM clinical_terms",
    ):
        for value, language, term_type in (
            (row["canonical_name_ko"], "ko", "official_ko"),
            (row["canonical_name_en"], "en", "official_en"),
        ):
            if _text(value):
                terms.append(
                    Term(
                        "procedure_terms", f"procedure:{row['term_id']}",
                        str(value).strip(), _normalize(value), language, term_type,
                        str(row["review_status"] or ""), str(row["source_id"] or "dictionary"),
                        f"clinical_term:{row['term_id']}:{language}",
                    )
                )
    terms.extend(
        Term(
            "procedure_terms", f"procedure:{row['term_id']}",
            str(row["alias"]).strip(), _normalize(row["normalized_alias"] or row["alias"]),
            _language(row["alias"], row["language"]), str(row["alias_type"] or "alias"),
            str(row["review_status"] or ""), str(row["source_id"] or "dictionary"),
            f"term_alias:{row['alias_id']}",
        )
        for row in _rows(
            path,
            "SELECT alias_id, term_id, alias, normalized_alias, language, alias_type, review_status, source_id FROM term_aliases",
        )
        if _text(row["alias"])
    )
    return SourceAsset("MEDICAL_DICTIONARY", "procedure_dictionary", path, concepts, tuple(terms))


def _anatomy_asset(path: Path) -> SourceAsset:
    concepts = tuple(
        Concept(
            "anatomy_terms", f"anatomy:{row['term_id']}",
            str(row["entry_type"] or "").casefold() or None,
            _text(row["korean_name"]), _text(row["english_name"]),
            str(row["verification_status"] or ""), "anatomy_dictionary",
            {"latin_name": row["latin_name"]},
        )
        for row in _rows(
            path,
            "SELECT term_id, korean_name, english_name, latin_name, entry_type, verification_status FROM anatomical_terms",
        )
    )
    terms: list[Term] = []
    for row in _rows(
        path,
        "SELECT term_id, korean_name, english_name, latin_name, verification_status FROM anatomical_terms",
    ):
        for value, language, term_type in (
            (row["korean_name"], "ko", "official_ko"),
            (row["english_name"], "en", "official_en"),
            (row["latin_name"], "la", "official_latin"),
        ):
            if _text(value):
                terms.append(
                    Term(
                        "anatomy_terms", f"anatomy:{row['term_id']}", str(value).strip(),
                        _normalize(value), language, term_type,
                        str(row["verification_status"] or ""), "dictionary",
                        f"anatomical_term:{row['term_id']}:{language}",
                    )
                )
    terms.extend(
        Term(
            "anatomy_terms", f"anatomy:{row['term_id']}", str(row["alias"]).strip(),
            _normalize(row["normalized_alias"] or row["alias"]),
            _language(row["alias"], row["language"]), "alias", "official", "dictionary",
            f"anatomical_alias:{row['alias_id']}",
        )
        for row in _rows(
            path,
            "SELECT alias_id, term_id, language, alias, normalized_alias FROM anatomical_aliases",
        )
        if _text(row["alias"])
    )
    return SourceAsset("MEDICAL_DICTIONARY", "anatomy_dictionary", path, concepts, tuple(terms))


def _emergency_asset(path: Path) -> SourceAsset:
    concepts = tuple(
        Concept(
            "emergency_terms", f"emergency:{row['term_id']}", None,
            _text(row["standard_ko"]), _text(row["standard_en"]),
            str(row["review_status"] or ""), "emergency_dictionary",
            {"provenance": row["provenance"]},
        )
        for row in _rows(
            path,
            "SELECT term_id, standard_ko, standard_en, provenance, review_status FROM terms",
        )
    )
    terms: list[Term] = []
    for row in _rows(
        path,
        "SELECT term_id, standard_ko, standard_en, normalized_ko, normalized_en, provenance, review_status FROM terms",
    ):
        for value, normalized, language, term_type in (
            (row["standard_ko"], row["normalized_ko"], "ko", "official_ko"),
            (row["standard_en"], row["normalized_en"], "en", "official_en"),
        ):
            if _text(value):
                terms.append(
                    Term(
                        "emergency_terms", f"emergency:{row['term_id']}", str(value).strip(),
                        _normalize(normalized or value), language, term_type,
                        str(row["review_status"] or ""), str(row["provenance"] or "dictionary"),
                        f"emergency_term:{row['term_id']}:{language}",
                    )
                )
    terms.extend(
        Term(
            "emergency_terms", f"emergency:{row['term_id']}", str(row["alias"]).strip(),
            _normalize(row["normalized_alias"] or row["alias"]), _language(row["alias"]),
            str(row["alias_type"] or "alias"), str(row["review_status"] or ""),
            str(row["provenance"] or "dictionary"),
            f"emergency_alias:{row['alias_id']}",
        )
        for row in _rows(
            path,
            "SELECT alias_id, term_id, alias, normalized_alias, alias_type, provenance, review_status FROM aliases",
        )
        if _text(row["alias"])
    )
    return SourceAsset("MEDICAL_DICTIONARY", "emergency_dictionary", path, concepts, tuple(terms))


def _kcd_asset(path: Path) -> SourceAsset:
    codes = tuple(
        KcdCode(
            code=str(row["code"]), code_display=_text(row["code_display"]),
            canonical_ko_name=_text(row["canonical_ko_name"]),
            canonical_en_name=_text(row["canonical_en_name"]),
            is_complete=bool(row["is_complete"]),
            principal_allowed=bool(row["principal_allowed"]),
            sex_restriction=_text(row["sex_restriction"]),
            min_age=row["min_age"], max_age=row["max_age"],
            payload={
                "infection_class": row["infection_class"],
                "medicine_type": row["medicine_type"],
                "is_new": bool(row["is_new"]),
                "source_row": row["source_row"],
            },
        )
        for row in _rows(
            path,
            """
            SELECT code, code_display, canonical_ko_name, canonical_en_name,
                   is_complete, principal_allowed, infection_class,
                   sex_restriction, max_age, min_age, medicine_type, is_new, source_row
              FROM kcd_codes
            """,
        )
    )
    terms = tuple(
        KcdTerm(
            code=str(row["code"]), ko_name=_text(row["ko_name"]),
            en_name=_text(row["en_name"]),
            normalized_term=_normalize(row["ko_name"] or row["en_name"]),
            is_canonical=bool(row["is_canonical"]),
            source_term_id=f"kcd_term:{row['term_id']}",
        )
        for row in _rows(
            path,
            "SELECT term_id, code, ko_name, en_name, is_canonical FROM kcd_terms",
        )
    )
    return SourceAsset("KCD", "kcd9", path, kcd_codes=codes, kcd_terms=terms)


LOADERS = {
    "drug_dictionary": _drug_asset,
    "procedure_dictionary": _procedure_asset,
    "anatomy_dictionary": _anatomy_asset,
    "emergency_dictionary": _emergency_asset,
    "kcd9": _kcd_asset,
}


def _copy_rows(stream, table: str, columns: Sequence[str], rows: Iterable[Sequence[object]]) -> None:
    stream.write(
        f"COPY {table} ({', '.join(columns)}) FROM STDIN WITH (FORMAT csv);\n"
    )
    writer = csv.writer(stream, lineterminator="\n")
    for row in rows:
        writer.writerow(["" if value is None else value for value in row])
    stream.write("\\.\n")


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _import_asset(*, asset: SourceAsset, user: str, database: str) -> None:
    content_hash = _hash(asset.path)
    command = [
        "docker", "compose", "exec", "-T", "postgres", "psql",
        "-U", user, "-d", database, "-v", "ON_ERROR_STOP=1", "--no-psqlrc", "-q",
    ]
    process = subprocess.Popen(
        command,
        cwd=REPO,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    stream = process.stdin
    source_kind = _literal(asset.source_kind)
    source_id = _literal(asset.source_id)
    version = _literal(f"{asset.path.stem}:{IMPORT_SCHEMA_VERSION}")
    hash_literal = _literal(content_hash)
    metadata = _literal(json.dumps({"file_name": asset.path.name}, ensure_ascii=False))
    try:
        stream.write("BEGIN;\n")
        stream.write(
            "INSERT INTO clinicalnlp.source_releases"
            "(source_kind, source_id, version, content_hash, is_active, metadata) VALUES "
            f"({source_kind}, {source_id}, {version}, {hash_literal}, FALSE, {metadata}::jsonb) "
            "ON CONFLICT (source_kind, source_id, version, content_hash) DO NOTHING;\n"
        )

        if asset.concepts:
            stream.write(
                "CREATE TEMP TABLE stage_concepts("
                "collection_name text, entity_id text, entity_type text, canonical_ko text, "
                "canonical_en text, review_status text, source_kind text, payload jsonb) ON COMMIT DROP;\n"
            )
            _copy_rows(
                stream,
                "stage_concepts",
                ("collection_name", "entity_id", "entity_type", "canonical_ko", "canonical_en", "review_status", "source_kind", "payload"),
                (
                    (
                        item.collection_name, item.entity_id, item.entity_type,
                        item.canonical_ko, item.canonical_en, item.review_status,
                        item.source_kind, json.dumps(item.payload, ensure_ascii=False),
                    )
                    for item in asset.concepts
                ),
            )
            stream.write(
                "INSERT INTO clinicalnlp.medical_concepts("
                "source_release_id, collection_name, entity_id, entity_type, canonical_ko, "
                "canonical_en, review_status, source_kind, payload) "
                "SELECT r.release_id, s.collection_name, s.entity_id, s.entity_type, "
                "s.canonical_ko, s.canonical_en, s.review_status, s.source_kind, s.payload "
                "FROM stage_concepts s CROSS JOIN clinicalnlp.source_releases r "
                f"WHERE r.source_kind={source_kind} AND r.source_id={source_id} "
                f"AND r.version={version} AND r.content_hash={hash_literal} "
                "ON CONFLICT (source_release_id, collection_name, entity_id) DO NOTHING;\n"
            )
            stream.write(
                "CREATE TEMP TABLE stage_terms("
                "collection_name text, entity_id text, source_text text, normalized_term text, "
                "language text, term_type text, review_status text, source_kind text, "
                "source_term_id text) ON COMMIT DROP;\n"
            )
            _copy_rows(
                stream,
                "stage_terms",
                ("collection_name", "entity_id", "source_text", "normalized_term", "language", "term_type", "review_status", "source_kind", "source_term_id"),
                (
                    (
                        item.collection_name, item.entity_id, item.source_text,
                        item.normalized_term, item.language, item.term_type,
                        item.review_status, item.source_kind, item.source_term_id,
                    )
                    for item in asset.terms
                ),
            )
            stream.write(
                "INSERT INTO clinicalnlp.medical_terms("
                "concept_pk, source_text, normalized_term, language, term_type, review_status, "
                "source_kind, source_term_id) "
                "SELECT c.concept_pk, s.source_text, s.normalized_term, s.language, "
                "s.term_type, s.review_status, s.source_kind, s.source_term_id FROM stage_terms s "
                "JOIN clinicalnlp.source_releases r "
                f"ON r.source_kind={source_kind} AND r.source_id={source_id} "
                f"AND r.version={version} AND r.content_hash={hash_literal} "
                "JOIN clinicalnlp.medical_concepts c ON c.source_release_id=r.release_id "
                "AND c.collection_name=s.collection_name AND c.entity_id=s.entity_id "
                "ON CONFLICT (concept_pk, source_term_id) DO NOTHING;\n"
            )

        if asset.kcd_codes:
            stream.write(
                "CREATE TEMP TABLE stage_kcd_codes("
                "code text, code_display text, canonical_ko_name text, canonical_en_name text, "
                "is_complete boolean, principal_allowed boolean, sex_restriction text, "
                "min_age integer, max_age integer, payload jsonb) ON COMMIT DROP;\n"
            )
            _copy_rows(
                stream,
                "stage_kcd_codes",
                ("code", "code_display", "canonical_ko_name", "canonical_en_name", "is_complete", "principal_allowed", "sex_restriction", "min_age", "max_age", "payload"),
                (
                    (
                        item.code, item.code_display, item.canonical_ko_name,
                        item.canonical_en_name, item.is_complete,
                        item.principal_allowed, item.sex_restriction,
                        item.min_age, item.max_age,
                        json.dumps(item.payload, ensure_ascii=False),
                    )
                    for item in asset.kcd_codes
                ),
            )
            stream.write(
                "INSERT INTO clinicalnlp.kcd_codes("
                "source_release_id, code, code_display, canonical_ko_name, canonical_en_name, "
                "is_complete, principal_allowed, sex_restriction, min_age, max_age, payload) "
                "SELECT r.release_id, s.code, s.code_display, s.canonical_ko_name, "
                "s.canonical_en_name, s.is_complete, s.principal_allowed, s.sex_restriction, "
                "s.min_age, s.max_age, s.payload FROM stage_kcd_codes s "
                "CROSS JOIN clinicalnlp.source_releases r "
                f"WHERE r.source_kind={source_kind} AND r.source_id={source_id} "
                f"AND r.version={version} AND r.content_hash={hash_literal} "
                "ON CONFLICT (source_release_id, code) DO NOTHING;\n"
            )
            stream.write(
                "CREATE TEMP TABLE stage_kcd_terms("
                "code text, ko_name text, en_name text, normalized_term text, "
                "is_canonical boolean, source_term_id text) ON COMMIT DROP;\n"
            )
            _copy_rows(
                stream,
                "stage_kcd_terms",
                ("code", "ko_name", "en_name", "normalized_term", "is_canonical", "source_term_id"),
                (
                    (item.code, item.ko_name, item.en_name, item.normalized_term, item.is_canonical, item.source_term_id)
                    for item in asset.kcd_terms
                ),
            )
            stream.write(
                "INSERT INTO clinicalnlp.kcd_terms("
                "kcd_code_pk, ko_name, en_name, normalized_term, is_canonical, source_term_id) "
                "SELECT c.kcd_code_pk, s.ko_name, s.en_name, s.normalized_term, "
                "s.is_canonical, s.source_term_id "
                "FROM stage_kcd_terms s JOIN clinicalnlp.source_releases r "
                f"ON r.source_kind={source_kind} AND r.source_id={source_id} "
                f"AND r.version={version} AND r.content_hash={hash_literal} "
                "JOIN clinicalnlp.kcd_codes c ON c.source_release_id=r.release_id AND c.code=s.code "
                "ON CONFLICT (kcd_code_pk, source_term_id) DO NOTHING;\n"
            )

        stream.write(
            "UPDATE clinicalnlp.source_releases SET is_active=FALSE "
            f"WHERE source_kind={source_kind} AND source_id={source_id} "
            f"AND (version<>{version} OR content_hash<>{hash_literal});\n"
        )
        stream.write(
            "UPDATE clinicalnlp.source_releases SET is_active=TRUE "
            f"WHERE source_kind={source_kind} AND source_id={source_id} "
            f"AND version={version} AND content_hash={hash_literal};\n"
        )
        stream.write("COMMIT;\n")
        stream.close()
        process.stdin = None
        stdout, stderr = process.communicate()
    except (BrokenPipeError, OSError):
        try:
            stream.close()
        except OSError:
            pass
        process.stdin = None
        stdout, stderr = process.communicate()
    if process.returncode != 0:
        raise SystemExit(
            f"[FATAL] import failed for {asset.path.name}: "
            f"{stderr.strip() or stdout.strip()}"
        )


def _summary(
    *,
    user: str,
    database: str,
    expected: dict[str, int],
) -> dict[str, object]:
    sql = """
    WITH active_releases AS (
        SELECT release_id FROM clinicalnlp.source_releases
         WHERE is_active AND source_kind IN ('MEDICAL_DICTIONARY', 'KCD')
    )
    SELECT json_build_object(
        'source_release_count', (SELECT count(*) FROM active_releases),
        'medical_concept_count', (
            SELECT count(*) FROM clinicalnlp.medical_concepts c
             WHERE c.source_release_id IN (SELECT release_id FROM active_releases)
        ),
        'medical_term_count', (
            SELECT count(*) FROM clinicalnlp.medical_terms t
            JOIN clinicalnlp.medical_concepts c ON c.concept_pk=t.concept_pk
             WHERE c.source_release_id IN (SELECT release_id FROM active_releases)
        ),
        'kcd_code_count', (
            SELECT count(*) FROM clinicalnlp.kcd_codes c
             WHERE c.source_release_id IN (SELECT release_id FROM active_releases)
        ),
        'kcd_term_count', (
            SELECT count(*) FROM clinicalnlp.kcd_terms t
            JOIN clinicalnlp.kcd_codes c ON c.kcd_code_pk=t.kcd_code_pk
             WHERE c.source_release_id IN (SELECT release_id FROM active_releases)
        )
    )::text
    """
    process = subprocess.run(
        [
            "docker", "compose", "exec", "-T", "postgres", "psql",
            "-U", user, "-d", database, "-v", "ON_ERROR_STOP=1", "--no-psqlrc",
            "-t", "-A", "-c", sql,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise SystemExit(f"[FATAL] import verification failed: {process.stderr.strip()}")
    counts = json.loads(process.stdout.strip())
    return {
        "status": "ready" if counts == expected else "not_ready",
        **counts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import ClinicalNLP medical dictionary and KCD SQLite assets."
    )
    parser.add_argument("--database", default=None)
    parser.add_argument("--user", default=None)
    parser.add_argument("--dictionary-root", type=Path, default=DEFAULT_DICTIONARY_ROOT)
    args = parser.parse_args(argv)

    database = _validated(args.database or _dotenv_value("POSTGRES_DB"), label="database")
    user = _validated(args.user or _dotenv_value("POSTGRES_USER"), label="user")
    root = args.dictionary_root.resolve()

    assets: list[SourceAsset] = []
    for source_kind, source_id, filename in SOURCE_FILES:
        path = root / filename
        if not path.is_file():
            raise SystemExit(f"[FATAL] missing dictionary asset: {path}")
        asset = LOADERS[source_id](path)
        if asset.source_kind != source_kind:
            raise SystemExit(f"[FATAL] source kind mismatch: {source_id}")
        assets.append(asset)

    for asset in assets:
        _import_asset(asset=asset, user=user, database=database)

    expected = {
        "source_release_count": len(assets),
        "medical_concept_count": sum(len(asset.concepts) for asset in assets),
        "medical_term_count": sum(len(asset.terms) for asset in assets),
        "kcd_code_count": sum(len(asset.kcd_codes) for asset in assets),
        "kcd_term_count": sum(len(asset.kcd_terms) for asset in assets),
    }
    result = _summary(user=user, database=database, expected=expected)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result.get("status") == "ready" else 1


if __name__ == "__main__":
    sys.exit(main())
