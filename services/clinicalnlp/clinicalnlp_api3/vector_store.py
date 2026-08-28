from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import unicodedata
import zlib
from difflib import SequenceMatcher
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

import numpy as np

from .retrieval import COLLECTIONS, DictionaryPaths


VECTOR_DIMENSIONS = 256
VECTOR_INDEX_SCHEMA_VERSION = "medical-vector-index-v2"
MAX_VECTOR_CANDIDATES = 8
DEFAULT_VECTOR_INDEX = Path(__file__).parents[1] / "data" / "api3_vectors.sqlite"
_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")
_MEDICATION_CONTEXT_RE = re.compile(
    r"(?:^|[\s,])약(?:은|을|이|도|만|물)?(?=$|[\s,?.])|"
    r"복용|투여|처방|사용\s*중|정\b|캡슐|시럽|주입|mg|mcg|mL|인슐린|항생제",
    re.IGNORECASE,
)
_PROCEDURE_CONTEXT_RE = re.compile(
    r"검사|시술|수술|촬영|채혈|삽관|인투베이션|CT|MRI|PET|ECG|EKG|CBC|ABGA|"
    r"INR|APTT|초음파|엑스레이|X[- ]?ray",
    re.IGNORECASE,
)
_DIAGNOSIS_CONTEXT_RE = re.compile(
    r"염|암|골절|경색|질환|증후군|부전|의심|의증|진단|병변"
)
_COLLOQUIAL_SYMPTOM_RE = re.compile(
    r"아파|아프|숨.{0,2}차|쓰러|두근|어지러|토하|토했|피가|붓|저리|마비|답답"
)
_CARDIAC_RHYTHM_CONTEXT_RE = re.compile(r"ECG|EKG|심전도|리듬", re.IGNORECASE)
_RHYTHM_SOURCE_RE = re.compile(r"사이너스|시너스|탁키|타키|브래디")
_LOANWORD_SOURCE_RE = re.compile(
    r"(?:이션|에이션|레이션|디아|리아|미아|페니아|콥|코프|로크|레인|"
    r"그래피|스코피|토미|플라스티|테라피|로지|마이신|실린|프릴|피린|졸|딘|핀)$"
)
_KOREAN_PARTICLE_SUFFIXES = (
    "으로부터", "에서부터", "에게서", "이라고", "이라는", "부터", "까지",
    "에서", "에게", "으로", "이나", "이며", "이고", "이면", "인데", "이요",
    "예요", "하고", "은", "는", "이", "가", "을", "를", "도", "만", "과",
    "와", "에", "의", "로", "고",
)
_VECTOR_BLOCK_RE = re.compile(
    r"없|아니|모르|같|언제|왜|어디|주세요|오셨어요|있나요|계세요|기억|현재|가능성|나요|"
    r"증상|천천히|말씀|복용|봉투|가지고|검사|많이|됩니다|통증|심장박동|모니터링|"
    r"위험|선생님|우선|그러면|그러시|이상한|일단|볼게|그래|병이나|거\s*좀|"
    r"팔다리|오케이|나오면|결과|평가|예전에|될\s*것|해요|적은"
)
_FUZZY_THRESHOLDS = {
    "drug_terms": 0.72,
    "procedure_terms": 0.68,
    "anatomy_terms": 0.76,
    "emergency_terms": 0.64,
    "kcd9_terms": 0.72,
}
_DRUG_ENGLISH_TOKEN_BLOCKLIST = {
    "anhydrous",
    "besylate",
    "bromide",
    "calcium",
    "capsule",
    "capsules",
    "dihydrate",
    "hydrate",
    "hydrochloride",
    "injection",
    "maleate",
    "mesylate",
    "monohydrate",
    "nicotinate",
    "potassium",
    "sodium",
    "sulfate",
    "tablet",
    "tablets",
    "trihydrate",
}
_DRUG_PRODUCT_FORM_RE = re.compile(
    r"^(?P<brand>.+?)(?:점안액|흡입액|구강붕해정|서방정|주사액|"
    r"캡슐|시럽|크림|연고|과립|패취|정|주|액|겔|산)(?:\d|$)"
)

_INITIALS = (
    "g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s",
    "ss", "", "j", "jj", "ch", "k", "t", "p", "h",
)
_VOWELS = (
    "a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa",
    "wae", "oe", "yo", "u", "wo", "we", "wi", "yu", "eu", "ui", "i",
)
_FINALS = (
    "", "k", "k", "ks", "n", "nj", "nh", "t", "l", "lk", "lm", "lb",
    "ls", "lt", "lp", "lh", "m", "p", "ps", "t", "t", "ng", "t", "t",
    "k", "t", "p", "h",
)


@dataclass(frozen=True)
class VectorRecord:
    source_text: str
    entity_id: str
    canonical_ko: str
    canonical_en: str
    review_status: str
    source_kind: str
    payload: dict[str, Any]


def _require_sqlite_vec():
    try:
        import sqlite_vec
    except ImportError as error:  # pragma: no cover - deployment failure path
        raise RuntimeError(
            "sqlite-vec is required; install the project dependencies first"
        ) from error
    return sqlite_vec


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    sqlite_vec = _require_sqlite_vec()
    target = f"file:{path}?mode=ro" if read_only else str(path)
    connection = sqlite3.connect(target, uri=read_only)
    connection.row_factory = sqlite3.Row
    connection.enable_load_extension(True)
    sqlite_vec.load(connection)
    connection.enable_load_extension(False)
    return connection


def _source_paths(paths: DictionaryPaths) -> dict[str, Path]:
    return {
        "drug_terms": paths.drug,
        "procedure_terms": paths.procedure,
        "anatomy_terms": paths.anatomy,
        "emergency_terms": paths.emergency,
        "kcd9_terms": paths.kcd9,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def dictionary_source_hashes(
    paths: DictionaryPaths,
    *,
    collections: Iterable[str] | None = None,
) -> dict[str, str]:
    """Return content hashes used to bind vector rows to source assets."""
    source_paths = _source_paths(paths)
    selected = tuple(collections or COLLECTIONS)
    unknown = set(selected) - set(source_paths)
    if unknown:
        raise ValueError(f"unknown vector collections: {sorted(unknown)}")
    return {
        collection: _file_sha256(source_paths[collection])
        for collection in selected
    }


def _clean(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return " ".join(_TOKEN_RE.findall(normalized))


def _short_native_surface_similarity(
    source_text: str, retrieved_text: str
) -> float | None:
    source = _clean(source_text).replace(" ", "")
    retrieved = _clean(retrieved_text).replace(" ", "")
    if (
        not source
        or not retrieved
        or max(len(source), len(retrieved)) > 4
        or re.fullmatch(r"[가-힣]+", source) is None
        or re.fullmatch(r"[가-힣]+", retrieved) is None
    ):
        return None
    return SequenceMatcher(None, source, retrieved).ratio()


def _surface_forms(text: str) -> set[str]:
    compact = _clean(text).replace(" ", "")
    forms = {compact} if compact else set()
    for suffix in _KOREAN_PARTICLE_SUFFIXES:
        if compact.endswith(suffix) and len(compact) > len(suffix):
            forms.add(compact[: -len(suffix)])
    return forms


def _loanword_particle_stem(text: str) -> str | None:
    compact = _clean(text).replace(" ", "")
    for suffix in _KOREAN_PARTICLE_SUFFIXES:
        if not compact.endswith(suffix) or len(compact) <= len(suffix) + 1:
            continue
        stem = compact[: -len(suffix)]
        if _LOANWORD_SOURCE_RE.search(stem):
            return stem
    return None


def _is_surface_equivalent(source_text: str, *dictionary_terms: str) -> bool:
    source_forms = _surface_forms(source_text)
    return any(source_forms & _surface_forms(term) for term in dictionary_terms if term)


def _romanize_hangul(text: str) -> str:
    output: list[str] = []
    for character in text:
        code = ord(character)
        if 0xAC00 <= code <= 0xD7A3:
            offset = code - 0xAC00
            initial = offset // 588
            vowel = (offset % 588) // 28
            final = offset % 28
            output.append(_INITIALS[initial] + _VOWELS[vowel] + _FINALS[final])
        elif character.isascii() and character.isalnum():
            output.append(character.casefold())
        elif character.isspace():
            output.append(" ")
    return "".join(output)


def _phonetic_key(text: str) -> str:
    value = _romanize_hangul(_clean(text)).replace(" ", "")
    for source, replacement in (
        ("eu", ""),
        ("eo", "o"),
        ("yeo", "yo"),
        ("ei", "ai"),
    ):
        value = value.replace(source, replacement)
    return value


def _phonetic_skeleton(text: str) -> str:
    value = _phonetic_key(text)
    for source, replacement in (
        ("syeon", "tion"),
        ("syon", "tion"),
        ("jyeon", "tion"),
        ("jyon", "tion"),
        ("shon", "tion"),
    ):
        value = value.replace(source, replacement)
    skeleton = "".join(
        character
        for character in value
        if character.isascii()
        and character.isalnum()
        and character not in "aeiouy"
    )
    skeleton = skeleton.translate(
        str.maketrans(
            {
                "b": "p",
                "c": "k",
                "d": "t",
                "f": "p",
                "g": "k",
                "l": "r",
                "q": "k",
                "v": "p",
            }
        )
    )
    return re.sub(r"(.)\1+", r"\1", skeleton)


def _loanword_skeleton(text: str) -> str:
    value = _phonetic_key(text)
    for source, replacement in (
        ("syeon", "tion"),
        ("syon", "tion"),
        ("jyeon", "tion"),
        ("jyon", "tion"),
        ("shon", "tion"),
    ):
        value = value.replace(source, replacement)
    skeleton = "".join(
        character
        for character in value
        if character.isascii()
        and character.isalnum()
        and character not in "aeiouy"
    )
    skeleton = skeleton.translate(
        str.maketrans({"c": "k", "d": "t", "g": "k", "l": "r", "q": "k"})
    )
    return re.sub(r"(.)\1+", r"\1", skeleton)


def _phonetic_forms(text: str) -> set[str]:
    """Return conservative pronunciation forms for mixed Korean/English STT."""
    cleaned = _clean(text).replace(" ", "")
    base = _phonetic_key(cleaned)
    forms = {base} if base else set()

    loanword_stem = _loanword_particle_stem(cleaned)
    if loanword_stem:
        forms.add(_phonetic_key(loanword_stem))

    # Medical English followed by the Korean alternative particle is often
    # joined into one Whisper token: "stroke이나" -> "스트로키나".
    if cleaned.endswith("이나") and base.endswith("ina"):
        particle_stripped = base[:-3]
        forms.add(particle_stripped)
    elif cleaned.endswith("나") and base.endswith("na"):
        particle_stripped = base[:-2]
        forms.add(particle_stripped)
        if particle_stripped.endswith("i"):
            forms.add(particle_stripped[:-1])

    # Silent final e should not separate an English dictionary spelling from
    # its Korean pronunciation (for example, stroke -> 스트로크).
    if cleaned.isascii() and base.endswith("e") and len(base) > 3:
        forms.add(base[:-1])
    return {form for form in forms if form}


def _loanword_skeleton_pair(
    source_text: str, retrieved_text: str
) -> tuple[str, str] | None:
    source_clean = _clean(source_text).replace(" ", "")
    retrieved_clean = _clean(retrieved_text).replace(" ", "")
    if not any("가" <= character <= "힣" for character in source_clean):
        return None
    if not _LOANWORD_SOURCE_RE.search(source_clean):
        return None
    if not retrieved_clean.isascii() or not retrieved_clean.isalnum():
        return None
    source_skeleton = _loanword_skeleton(source_clean)
    retrieved_skeleton = _loanword_skeleton(retrieved_clean)
    if len(source_skeleton) < 4 or len(retrieved_skeleton) < 4:
        return None
    return source_skeleton, retrieved_skeleton


class MedicalHashEmbedder:
    """Deterministic typo-oriented vectors without a second neural model."""

    def __init__(self, dimensions: int = VECTOR_DIMENSIONS):
        self.dimensions = dimensions

    def _add_ngrams(
        self,
        vector: np.ndarray,
        value: str,
        *,
        namespace: str,
        minimum: int,
        maximum: int,
        weight: float,
    ) -> None:
        compact = value.replace(" ", "")
        for size in range(minimum, min(maximum, len(compact)) + 1):
            for position in range(len(compact) - size + 1):
                feature = f"{namespace}:{compact[position:position + size]}".encode("utf-8")
                hashed = zlib.crc32(feature)
                index = hashed % self.dimensions
                vector[index] += weight if hashed & 0x80000000 == 0 else -weight

    def embed(self, text: str) -> np.ndarray:
        cleaned = _clean(text)
        vector = np.zeros(self.dimensions, dtype=np.float32)
        if not cleaned:
            return vector

        self._add_ngrams(
            vector,
            cleaned,
            namespace="char",
            minimum=2,
            maximum=4,
            weight=1.0,
        )
        decomposed = unicodedata.normalize("NFKD", cleaned)
        self._add_ngrams(
            vector,
            decomposed,
            namespace="jamo",
            minimum=1,
            maximum=4,
            weight=0.7,
        )
        romanized = _romanize_hangul(cleaned)
        self._add_ngrams(
            vector,
            romanized,
            namespace="latin",
            minimum=2,
            maximum=5,
            weight=0.85,
        )
        self._add_ngrams(
            vector,
            _phonetic_key(cleaned),
            namespace="phonetic",
            minimum=2,
            maximum=5,
            weight=1.2,
        )
        self._add_ngrams(
            vector,
            _phonetic_skeleton(cleaned),
            namespace="skeleton",
            minimum=2,
            maximum=5,
            weight=2.0,
        )
        magnitude = float(np.linalg.norm(vector))
        return vector / magnitude if magnitude else vector


def _source_rows(path: Path, query: str) -> Iterator[sqlite3.Row]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        cursor = connection.execute(query)
        while rows := cursor.fetchmany(2_000):
            yield from rows
    finally:
        connection.close()


def _records_for_drugs(paths: DictionaryPaths) -> Iterator[VectorRecord]:
    query = """
        SELECT d.term AS source_text, d.entity_type, d.entity_id,
               d.term_type AS source_kind, d.review_status,
               COALESCE(i.canonical_ko, p.product_name_ko, d.term) AS canonical_ko,
               COALESCE(i.canonical_en, p.product_name_en, '') AS canonical_en
          FROM drug_terms d
          LEFT JOIN ingredients i
            ON d.entity_type='INGREDIENT' AND i.ingredient_id=CAST(d.entity_id AS INTEGER)
          LEFT JOIN products p
            ON d.entity_type='PRODUCT' AND p.item_id=d.entity_id
         WHERE length(trim(d.term)) >= 2
        UNION ALL
        SELECT s.alias, s.entity_type, s.entity_id,
               'stt_alias:' || COALESCE(s.alias_type, ''), s.review_status,
               COALESCE(i.canonical_ko, p.product_name_ko, s.alias),
               COALESCE(i.canonical_en, p.product_name_en, '')
          FROM stt_aliases s
          LEFT JOIN ingredients i
            ON s.entity_type='INGREDIENT' AND i.ingredient_id=CAST(s.entity_id AS INTEGER)
          LEFT JOIN products p
            ON s.entity_type='PRODUCT' AND p.item_id=s.entity_id
         WHERE length(trim(s.alias)) >= 2
    """
    for row in _source_rows(paths.drug, query):
        entity_type = str(row["entity_type"]).casefold()
        record = VectorRecord(
            source_text=row["source_text"],
            entity_id=f"drug:{entity_type}:{row['entity_id']}",
            canonical_ko=row["canonical_ko"] or row["source_text"],
            canonical_en=row["canonical_en"] or "",
            review_status=row["review_status"] or "",
            source_kind=row["source_kind"] or "dictionary",
            payload={"entity_type": entity_type},
        )
        yield record
        if entity_type == "product":
            product_name = str(record.canonical_ko).split("(", 1)[0].strip()
            product_match = _DRUG_PRODUCT_FORM_RE.match(product_name)
            product_brand = (
                product_match.group("brand").strip() if product_match else ""
            )
            if len(_clean(product_brand).replace(" ", "")) >= 2:
                yield VectorRecord(
                    source_text=product_brand,
                    entity_id=record.entity_id,
                    canonical_ko=record.canonical_ko,
                    canonical_en=record.canonical_en,
                    review_status=record.review_status,
                    source_kind="derived_product_ko_brand",
                    payload={
                        "entity_type": entity_type,
                        "parent_source_text": record.source_text,
                    },
                )
        for token in _TOKEN_RE.findall(record.canonical_en):
            normalized_token = token.casefold()
            if (
                len(normalized_token) < 4
                or not normalized_token.isascii()
                or not normalized_token.isalpha()
                or normalized_token in _DRUG_ENGLISH_TOKEN_BLOCKLIST
            ):
                continue
            yield VectorRecord(
                source_text=normalized_token,
                entity_id=record.entity_id,
                canonical_ko=record.canonical_ko,
                canonical_en=record.canonical_en,
                review_status=record.review_status,
                source_kind=f"derived_{entity_type}_en_token",
                payload={
                    "entity_type": entity_type,
                    "parent_source_text": record.source_text,
                },
            )


def _records_for_procedures(paths: DictionaryPaths) -> Iterator[VectorRecord]:
    query = """
        SELECT a.alias AS source_text, t.term_id, t.canonical_name_ko AS canonical_ko,
               COALESCE(t.canonical_name_en, '') AS canonical_en,
               COALESCE(a.review_status, t.review_status, '') AS review_status,
               'alias:' || COALESCE(a.alias_type, '') AS source_kind, t.category
          FROM term_aliases a JOIN clinical_terms t ON a.term_id=t.term_id
         WHERE length(trim(a.alias)) >= 2
        UNION ALL
        SELECT t.canonical_name_ko, t.term_id, t.canonical_name_ko,
               COALESCE(t.canonical_name_en, ''), COALESCE(t.review_status, ''),
               'official_ko', t.category
          FROM clinical_terms t WHERE length(trim(t.canonical_name_ko)) >= 2
        UNION ALL
        SELECT t.canonical_name_en, t.term_id, t.canonical_name_ko,
               COALESCE(t.canonical_name_en, ''), COALESCE(t.review_status, ''),
               'official_en', t.category
          FROM clinical_terms t WHERE length(trim(t.canonical_name_en)) >= 2
    """
    for row in _source_rows(paths.procedure, query):
        yield VectorRecord(
            source_text=row["source_text"],
            entity_id=f"procedure:{row['term_id']}",
            canonical_ko=row["canonical_ko"],
            canonical_en=row["canonical_en"],
            review_status=row["review_status"],
            source_kind=row["source_kind"],
            payload={"category": row["category"]},
        )


def _records_for_anatomy(paths: DictionaryPaths) -> Iterator[VectorRecord]:
    query = """
        SELECT a.alias AS source_text, t.term_id, t.korean_name AS canonical_ko,
               COALESCE(t.english_name, '') AS canonical_en,
               COALESCE(t.verification_status, '') AS review_status,
               'alias' AS source_kind, COALESCE(t.latin_name, '') AS latin_name
          FROM anatomical_aliases a JOIN anatomical_terms t ON a.term_id=t.term_id
         WHERE length(trim(a.alias)) >= 2
        UNION ALL
        SELECT t.korean_name, t.term_id, t.korean_name, COALESCE(t.english_name, ''),
               COALESCE(t.verification_status, ''), 'official_ko', COALESCE(t.latin_name, '')
          FROM anatomical_terms t WHERE length(trim(t.korean_name)) >= 2
        UNION ALL
        SELECT t.english_name, t.term_id, t.korean_name, COALESCE(t.english_name, ''),
               COALESCE(t.verification_status, ''), 'official_en', COALESCE(t.latin_name, '')
          FROM anatomical_terms t WHERE length(trim(t.english_name)) >= 2
        UNION ALL
        SELECT t.latin_name, t.term_id, t.korean_name, COALESCE(t.english_name, ''),
               COALESCE(t.verification_status, ''), 'official_latin', COALESCE(t.latin_name, '')
          FROM anatomical_terms t WHERE length(trim(t.latin_name)) >= 2
    """
    for row in _source_rows(paths.anatomy, query):
        yield VectorRecord(
            source_text=row["source_text"],
            entity_id=f"anatomy:{row['term_id']}",
            canonical_ko=row["canonical_ko"],
            canonical_en=row["canonical_en"],
            review_status=row["review_status"],
            source_kind=row["source_kind"],
            payload={"latin_name": row["latin_name"]},
        )


def _records_for_emergency(paths: DictionaryPaths) -> Iterator[VectorRecord]:
    query = """
        SELECT a.alias AS source_text, t.term_id, t.standard_ko AS canonical_ko,
               COALESCE(t.standard_en, '') AS canonical_en,
               COALESCE(a.review_status, t.review_status, '') AS review_status,
               'alias:' || COALESCE(a.alias_type, '') AS source_kind
          FROM aliases a JOIN terms t ON a.term_id=t.term_id
         WHERE length(trim(a.alias)) >= 2
        UNION ALL
        SELECT t.standard_ko, t.term_id, t.standard_ko, COALESCE(t.standard_en, ''),
               COALESCE(t.review_status, ''), 'official_ko'
          FROM terms t WHERE length(trim(t.standard_ko)) >= 2
        UNION ALL
        SELECT t.standard_en, t.term_id, t.standard_ko, COALESCE(t.standard_en, ''),
               COALESCE(t.review_status, ''), 'official_en'
          FROM terms t WHERE length(trim(t.standard_en)) >= 2
        UNION ALL
        SELECT w.observed_text, t.term_id, t.standard_ko, COALESCE(t.standard_en, ''),
               COALESCE(w.review_status, ''), 'stt_error'
          FROM whisper_errors w JOIN terms t ON w.intended_term_id=t.term_id
         WHERE length(trim(w.observed_text)) >= 2
    """
    for row in _source_rows(paths.emergency, query):
        record = VectorRecord(
            source_text=row["source_text"],
            entity_id=f"emergency:{row['term_id']}",
            canonical_ko=row["canonical_ko"],
            canonical_en=row["canonical_en"],
            review_status=row["review_status"],
            source_kind=row["source_kind"],
            payload={},
        )
        yield record
        if row["source_kind"] == "official_en":
            full_source = _clean(row["source_text"])
            for token in _TOKEN_RE.findall(row["source_text"]):
                token = token.casefold()
                if len(token) < 5 or not token.isascii():
                    continue
                is_full_term = token == full_source.replace(" ", "")
                if not is_full_term:
                    yield VectorRecord(
                        source_text=token,
                        entity_id=record.entity_id,
                        canonical_ko=record.canonical_ko,
                        canonical_en=record.canonical_en,
                        review_status=record.review_status,
                        source_kind="derived_official_en_token",
                        payload={"parent_source_text": row["source_text"]},
                    )
                skeleton = _loanword_skeleton(token)
                if len(skeleton) >= 3 and skeleton != token:
                    yield VectorRecord(
                        source_text=skeleton,
                        entity_id=record.entity_id,
                        canonical_ko=record.canonical_ko,
                        canonical_en=record.canonical_en,
                        review_status=record.review_status,
                        source_kind=(
                            "derived_official_en_phonetic_full"
                            if is_full_term
                            else "derived_official_en_phonetic_component"
                        ),
                        payload={"parent_source_text": row["source_text"]},
                    )


def _records_for_kcd9(paths: DictionaryPaths) -> Iterator[VectorRecord]:
    query = """
        SELECT t.ko_name AS source_text, c.code, c.code_display,
               c.canonical_ko_name AS canonical_ko,
               COALESCE(c.canonical_en_name, '') AS canonical_en,
               'official_ko' AS source_kind, c.principal_allowed,
               c.sex_restriction, c.min_age, c.max_age
          FROM kcd_terms t JOIN kcd_codes c ON t.code=c.code
         WHERE c.is_complete=1 AND length(trim(t.ko_name)) >= 2
        UNION ALL
        SELECT t.en_name, c.code, c.code_display, c.canonical_ko_name,
               COALESCE(c.canonical_en_name, ''), 'official_en', c.principal_allowed,
               c.sex_restriction, c.min_age, c.max_age
          FROM kcd_terms t JOIN kcd_codes c ON t.code=c.code
         WHERE c.is_complete=1 AND length(trim(t.en_name)) >= 2
    """
    for row in _source_rows(paths.kcd9, query):
        yield VectorRecord(
            source_text=row["source_text"],
            entity_id=f"kcd:{row['code']}",
            canonical_ko=row["canonical_ko"],
            canonical_en=row["canonical_en"],
            review_status="official",
            source_kind=row["source_kind"],
            payload={
                "code": row["code"],
                "code_display": row["code_display"],
                "is_complete": True,
                "principal_allowed": bool(row["principal_allowed"]),
                "sex_restriction": row["sex_restriction"],
                "min_age": row["min_age"],
                "max_age": row["max_age"],
            },
        )


_RECORD_FACTORIES = {
    "drug_terms": _records_for_drugs,
    "procedure_terms": _records_for_procedures,
    "anatomy_terms": _records_for_anatomy,
    "emergency_terms": _records_for_emergency,
    "kcd9_terms": _records_for_kcd9,
}


def _deduplicate(records: Iterable[VectorRecord]) -> Iterator[VectorRecord]:
    seen: set[tuple[str, str]] = set()
    for record in records:
        source_text = str(record.source_text or "").strip()
        key = (_clean(source_text), record.entity_id)
        if not key[0] or key in seen:
            continue
        seen.add(key)
        yield VectorRecord(
            source_text=source_text,
            entity_id=record.entity_id,
            canonical_ko=record.canonical_ko,
            canonical_en=record.canonical_en,
            review_status=record.review_status,
            source_kind=record.source_kind,
            payload=record.payload,
        )


def _create_collection(connection: sqlite3.Connection, collection: str) -> None:
    if collection not in COLLECTIONS:
        raise ValueError(f"unknown vector collection: {collection}")
    connection.execute(f'DROP TABLE IF EXISTS "{collection}"')
    partition_column = (
        "entity_type text partition key," if collection == "drug_terms" else ""
    )
    connection.execute(
        f'''CREATE VIRTUAL TABLE "{collection}" USING vec0(
            embedding float[{VECTOR_DIMENSIONS}] distance_metric=cosine,
            {partition_column}
            +source_text text,
            +entity_id text,
            +canonical_ko text,
            +canonical_en text,
            +review_status text,
            +source_kind text,
            +payload text
        )'''
    )


def _insert_vector_batch(
    connection: sqlite3.Connection,
    collection: str,
    batch: list[tuple[Any, ...]],
) -> None:
    if collection == "drug_terms":
        connection.executemany(
            f'''INSERT INTO "{collection}"(
                rowid, embedding, entity_type, source_text, entity_id,
                canonical_ko, canonical_en, review_status, source_kind, payload
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            batch,
        )
        return
    connection.executemany(
        f'''INSERT INTO "{collection}"(
            rowid, embedding, source_text, entity_id, canonical_ko,
            canonical_en, review_status, source_kind, payload
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        batch,
    )


def build_vector_indexes(
    db_root: Path,
    index_path: Path = DEFAULT_VECTOR_INDEX,
    *,
    collections: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build selected collections atomically from the five source dictionaries."""
    paths = DictionaryPaths.discover(Path(db_root))
    selected = tuple(collections or COLLECTIONS)
    unknown = set(selected) - set(COLLECTIONS)
    if unknown:
        raise ValueError(f"unknown vector collections: {sorted(unknown)}")

    index_path = Path(index_path)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    building_path = index_path.with_name(index_path.name + ".building")
    if building_path.exists():
        building_path.unlink()
    if index_path.exists() and set(selected) != set(COLLECTIONS):
        shutil.copy2(index_path, building_path)

    embedder = MedicalHashEmbedder()
    counts: dict[str, int] = {}
    source_hashes = dictionary_source_hashes(paths, collections=selected)
    connection = _connect(building_path)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS vector_index_metadata(
                collection TEXT PRIMARY KEY,
                source_sha256 TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                dimensions INTEGER NOT NULL
            )
            """
        )
        for collection in selected:
            _create_collection(connection, collection)
            batch: list[tuple[Any, ...]] = []
            count = 0
            records = _deduplicate(_RECORD_FACTORIES[collection](paths))
            for rowid, record in enumerate(records, start=1):
                payload = json.dumps(
                    record.payload, ensure_ascii=False, separators=(",", ":")
                )
                base_values = (
                    rowid,
                    embedder.embed(record.source_text),
                    record.source_text,
                    record.entity_id,
                    record.canonical_ko,
                    record.canonical_en,
                    record.review_status,
                    record.source_kind,
                    payload,
                )
                batch.append(
                    (
                        rowid,
                        base_values[1],
                        str(record.payload.get("entity_type") or ""),
                        *base_values[2:],
                    )
                    if collection == "drug_terms"
                    else base_values
                )
                if len(batch) >= 1_000:
                    _insert_vector_batch(connection, collection, batch)
                    count += len(batch)
                    batch.clear()
            if batch:
                _insert_vector_batch(connection, collection, batch)
                count += len(batch)
            connection.execute(
                """
                INSERT INTO vector_index_metadata(
                    collection, source_sha256, schema_version, dimensions
                ) VALUES(?, ?, ?, ?)
                ON CONFLICT(collection) DO UPDATE SET
                    source_sha256=excluded.source_sha256,
                    schema_version=excluded.schema_version,
                    dimensions=excluded.dimensions
                """,
                (
                    collection,
                    source_hashes[collection],
                    VECTOR_INDEX_SCHEMA_VERSION,
                    VECTOR_DIMENSIONS,
                ),
            )
            connection.commit()
            counts[collection] = count
        connection.execute("PRAGMA optimize")
        connection.commit()
    except Exception:
        connection.close()
        if building_path.exists():
            building_path.unlink()
        raise
    else:
        connection.close()

    os.replace(building_path, index_path)
    return {
        "index_path": str(index_path),
        "dimensions": VECTOR_DIMENSIONS,
        "collections": counts,
        "source_versions": source_hashes,
    }


def vector_index_counts(index_path: Path = DEFAULT_VECTOR_INDEX) -> dict[str, int]:
    """Return collection counts from a readable five-collection vector index."""
    path = Path(index_path)
    if not path.is_file():
        raise ValueError(f"vector index is missing: {path}")
    connection = _connect(path, read_only=True)
    try:
        return {
            collection: int(
                connection.execute(f'SELECT count(*) FROM "{collection}"').fetchone()[0]
            )
            for collection in COLLECTIONS
        }
    finally:
        connection.close()


def audit_vector_index(index_path: Path = DEFAULT_VECTOR_INDEX) -> list[str]:
    """Report deterministic integrity failures without modifying the index."""
    path = Path(index_path)
    if not path.is_file():
        return [f"vector index is missing: {path}"]
    try:
        counts = vector_index_counts(path)
    except Exception as error:
        return [f"vector index cannot be read: {error}"]
    empty = [name for name, count in counts.items() if count <= 0]
    errors = [f"empty vector collection: {name}" for name in empty]
    try:
        connection = _connect(path, read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT collection, source_sha256, schema_version, dimensions
                  FROM vector_index_metadata
                """
            ).fetchall()
        finally:
            connection.close()
    except Exception as error:
        errors.append(f"vector metadata cannot be read: {error}")
        return errors
    metadata = {str(row["collection"]): row for row in rows}
    for collection in COLLECTIONS:
        row = metadata.get(collection)
        if row is None:
            errors.append(f"missing vector metadata: {collection}")
            continue
        source_sha256 = str(row["source_sha256"] or "")
        if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
            errors.append(f"invalid vector source hash: {collection}")
        if str(row["schema_version"] or "") != VECTOR_INDEX_SCHEMA_VERSION:
            errors.append(f"invalid vector schema version: {collection}")
        if row["dimensions"] != VECTOR_DIMENSIONS:
            errors.append(f"invalid vector dimensions: {collection}")
    return errors


def _candidate_spans(raw_text: str, embedder: MedicalHashEmbedder) -> list[tuple[int, int, str, np.ndarray]]:
    tokens = list(_TOKEN_RE.finditer(raw_text))
    spans: list[tuple[int, int, str, np.ndarray]] = []
    seen: set[tuple[int, int]] = set()
    for size in range(1, min(5, len(tokens)) + 1):
        for position in range(len(tokens) - size + 1):
            start = tokens[position].start()
            end = tokens[position + size - 1].end()
            value = raw_text[start:end]
            if len(_clean(value).replace(" ", "")) < 2 or (start, end) in seen:
                continue
            seen.add((start, end))
            spans.append((start, end, value, embedder.embed(value)))
            if size == 1:
                loanword_stem = _loanword_particle_stem(value)
                if loanword_stem:
                    stem_end = start + len(loanword_stem)
                    if (start, stem_end) not in seen:
                        seen.add((start, stem_end))
                        spans.append(
                            (
                                start,
                                stem_end,
                                raw_text[start:stem_end],
                                embedder.embed(loanword_stem),
                            )
                        )
    if not spans and raw_text:
        spans.append((0, len(raw_text), raw_text, embedder.embed(raw_text)))
    return spans


class SqliteVectorRetriever:
    """KNN retrieval across five independently rebuildable sqlite-vec collections."""

    def __init__(
        self,
        index_path: Path = DEFAULT_VECTOR_INDEX,
        *,
        top_k: int = 12,
        minimum_similarity: float = 0.38,
    ):
        self.index_path = Path(index_path)
        self.top_k = top_k
        self.minimum_similarity = minimum_similarity
        self.embedder = MedicalHashEmbedder()
        if not self.index_path.is_file():
            raise ValueError(f"vector index not found: {self.index_path}")

    def _collection_threshold(self, collection: str, raw_text: str) -> float | None:
        if collection == "kcd9_terms" and not _DIAGNOSIS_CONTEXT_RE.search(raw_text):
            return None
        if collection == "drug_terms" and not _MEDICATION_CONTEXT_RE.search(raw_text):
            return None
        if collection == "procedure_terms" and not _PROCEDURE_CONTEXT_RE.search(raw_text):
            return None
        if collection == "drug_terms":
            return max(self.minimum_similarity, 0.70)
        if collection == "procedure_terms":
            return max(self.minimum_similarity, 0.65)
        if collection == "anatomy_terms":
            return max(self.minimum_similarity, 0.68)
        if collection == "emergency_terms":
            return max(self.minimum_similarity, 0.68)
        return max(self.minimum_similarity, 0.70)

    @staticmethod
    def _fuzzy_similarity(source_text: str, retrieved_text: str) -> float:
        source = _clean(source_text).replace(" ", "")
        retrieved = _clean(retrieved_text).replace(" ", "")
        if not source or not retrieved:
            return 0.0
        character_score = SequenceMatcher(None, source, retrieved).ratio()
        pronunciation_score = SequenceMatcher(
            None,
            _romanize_hangul(source),
            _romanize_hangul(retrieved),
        ).ratio()
        phonetic_score = max(
            (
                SequenceMatcher(None, left, right).ratio()
                for left in _phonetic_forms(source)
                for right in _phonetic_forms(retrieved)
            ),
            default=0.0,
        )
        loanword_pair = _loanword_skeleton_pair(source_text, retrieved_text)
        loanword_score = (
            SequenceMatcher(None, *loanword_pair).ratio()
            if loanword_pair is not None
            else 0.0
        )
        return max(
            character_score,
            pronunciation_score,
            phonetic_score,
            loanword_score,
        )

    @staticmethod
    def _edit_similarity(source_text: str, retrieved_text: str) -> float:
        def similarity(left: str, right: str) -> float:
            if not left or not right:
                return 0.0
            previous = list(range(len(right) + 1))
            for left_index, left_character in enumerate(left, start=1):
                current = [left_index]
                for right_index, right_character in enumerate(right, start=1):
                    current.append(
                        min(
                            current[-1] + 1,
                            previous[right_index] + 1,
                            previous[right_index - 1]
                            + (left_character != right_character),
                        )
                    )
                previous = current
            return 1.0 - (previous[-1] / max(len(left), len(right)))

        source = _clean(source_text).replace(" ", "")
        retrieved = _clean(retrieved_text).replace(" ", "")
        phonetic_score = max(
            (
                similarity(left, right)
                for left in _phonetic_forms(source)
                for right in _phonetic_forms(retrieved)
            ),
            default=0.0,
        )
        loanword_pair = _loanword_skeleton_pair(source_text, retrieved_text)
        loanword_score = (
            similarity(*loanword_pair) if loanword_pair is not None else 0.0
        )
        return max(
            similarity(source, retrieved),
            similarity(_romanize_hangul(source), _romanize_hangul(retrieved)),
            phonetic_score,
            loanword_score,
        )

    @staticmethod
    def _exact_phonetic_match(source_text: str, retrieved_text: str) -> bool:
        if _phonetic_forms(source_text) & _phonetic_forms(retrieved_text):
            return True
        loanword_pair = _loanword_skeleton_pair(source_text, retrieved_text)
        return loanword_pair is not None and loanword_pair[0] == loanword_pair[1]

    def retrieve(
        self, *, raw_text: str, context: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        del context
        query = self.embedder.embed(raw_text)
        if not np.any(query):
            return []
        spans = _candidate_spans(raw_text, self.embedder)
        symptom_phrase_spans = [
            span
            for span in spans
            if len(_TOKEN_RE.findall(span[2])) == 2
            and _COLLOQUIAL_SYMPTOM_RE.search(span[2])
        ][:4]
        connection = _connect(self.index_path, read_only=True)
        candidates: list[dict[str, Any]] = []
        protected_spans: set[tuple[int, int]] = set()
        try:
            for collection in COLLECTIONS:
                collection_threshold = self._collection_threshold(collection, raw_text)
                if collection_threshold is None:
                    continue
                query_vectors = [(query, self.top_k)]
                if collection in {"drug_terms", "anatomy_terms", "emergency_terms"}:
                    for token in list(_TOKEN_RE.finditer(raw_text))[:12]:
                        token_text = token.group(0)
                        token_length = len(_clean(token_text).replace(" ", ""))
                        loanword_stem = _loanword_particle_stem(token_text)
                        loanword_source = loanword_stem or token_text
                        is_loanword = bool(
                            _LOANWORD_SOURCE_RE.search(loanword_source)
                        )
                        if token_length < 3 and not is_loanword:
                            continue
                        query_vectors.append(
                            (
                                self.embedder.embed(token_text),
                                24 if collection == "drug_terms" else 6,
                            )
                        )
                        base_phonetic = _phonetic_key(token_text)
                        for derived_form in _phonetic_forms(token_text) - {base_phonetic}:
                            query_vectors.append((self.embedder.embed(derived_form), 24))
                        if collection == "emergency_terms":
                            skeleton = _loanword_skeleton(loanword_source)
                            if (
                                len(skeleton) >= 3
                                and is_loanword
                            ):
                                query_vectors.append((self.embedder.embed(skeleton), 24))
                if collection == "emergency_terms":
                    for _, _, span_text, span_vector in symptom_phrase_spans:
                        query_vectors.append((span_vector, 3))
                row_map: dict[tuple[str, str], sqlite3.Row] = {}
                for query_vector, query_limit in query_vectors:
                    entity_types: tuple[str | None, ...] = (
                        ("ingredient", "product")
                        if collection == "drug_terms"
                        else (None,)
                    )
                    for entity_type in entity_types:
                        partition_clause = (
                            " AND entity_type = ?" if entity_type is not None else ""
                        )
                        parameters: tuple[Any, ...] = (
                            (query_vector, query_limit, entity_type)
                            if entity_type is not None
                            else (query_vector, query_limit)
                        )
                        rows = connection.execute(
                            f'''SELECT embedding, source_text, entity_id, canonical_ko,
                                       canonical_en, review_status, source_kind, payload, distance
                                  FROM "{collection}"
                                 WHERE embedding MATCH ? AND k = ?{partition_clause}''',
                            parameters,
                        ).fetchall()
                        for row in rows:
                            key = (row["source_text"], row["entity_id"])
                            current = row_map.get(key)
                            if current is None or row["distance"] < current["distance"]:
                                row_map[key] = row
                collection_candidates: list[dict[str, Any]] = []
                for row in row_map.values():
                    stored = np.frombuffer(row["embedding"], dtype=np.float32)
                    scored_spans = [
                        (float(np.dot(item[3], stored)), item) for item in spans
                    ]
                    exact_phonetic_spans = [
                        (score, item)
                        for score, item in scored_spans
                        if self._exact_phonetic_match(item[2], row["source_text"])
                    ]
                    if exact_phonetic_spans:
                        similarity, (start, end, source_text, span_vector) = min(
                            exact_phonetic_spans,
                            key=lambda scored: scored[1][1] - scored[1][0],
                        )
                    else:
                        strong_phrase_spans = []
                        if collection == "emergency_terms":
                            for item in symptom_phrase_spans:
                                fuzzy = self._fuzzy_similarity(
                                    item[2], row["source_text"]
                                )
                                edit = self._edit_similarity(
                                    item[2], row["source_text"]
                                )
                                if fuzzy >= 0.74 and edit >= 0.62:
                                    score = float(np.dot(item[3], stored))
                                    strong_phrase_spans.append(
                                        ((fuzzy + edit) / 2, score, item)
                                    )
                        if strong_phrase_spans:
                            _, similarity, (
                                start,
                                end,
                                source_text,
                                span_vector,
                            ) = max(
                                strong_phrase_spans,
                                key=lambda scored: (
                                    scored[0],
                                    scored[1],
                                    -(scored[2][1] - scored[2][0]),
                                ),
                            )
                        else:
                            best_score = max(score for score, _ in scored_spans)
                            similarity, (start, end, source_text, span_vector) = min(
                                (
                                    (score, item)
                                    for score, item in scored_spans
                                    if score >= best_score - 0.015
                                ),
                                key=lambda scored: scored[1][1] - scored[1][0],
                            )
                    if _VECTOR_BLOCK_RE.search(source_text):
                        continue
                    if (
                        collection == "anatomy_terms"
                        and _CARDIAC_RHYTHM_CONTEXT_RE.search(raw_text)
                        and _RHYTHM_SOURCE_RE.search(source_text)
                    ):
                        continue
                    is_derived_drug_search_key = (
                        collection == "drug_terms"
                        and row["source_kind"].startswith(
                            ("derived_ingredient_", "derived_product_")
                        )
                    )
                    if not is_derived_drug_search_key and _is_surface_equivalent(
                        source_text,
                        row["source_text"],
                        row["canonical_ko"],
                        row["canonical_en"],
                    ):
                        protected_spans.add((start, end))
                        continue
                    fuzzy_similarity = self._fuzzy_similarity(
                        source_text, row["source_text"]
                    )
                    edit_similarity = self._edit_similarity(
                        source_text, row["source_text"]
                    )
                    exact_phonetic_match = self._exact_phonetic_match(
                        source_text, row["source_text"]
                    )
                    short_native_similarity = _short_native_surface_similarity(
                        source_text, row["source_text"]
                    )
                    if (
                        short_native_similarity is not None
                        and short_native_similarity < 0.50
                        and not exact_phonetic_match
                    ):
                        continue
                    is_derived_phonetic_key = row["source_kind"].startswith(
                        "derived_official_en_phonetic"
                    )
                    if is_derived_phonetic_key and not exact_phonetic_match:
                        continue
                    verified_colloquial_match = (
                        row["source_kind"] == "alias:colloquial"
                        and bool(_COLLOQUIAL_SYMPTOM_RE.search(source_text))
                        and fuzzy_similarity >= 0.74
                        and edit_similarity >= 0.60
                    )
                    strong_string_match = (
                        fuzzy_similarity >= 0.74 and edit_similarity >= 0.62
                    ) or verified_colloquial_match
                    if (
                        similarity < collection_threshold
                        and not exact_phonetic_match
                        and (
                            not strong_string_match
                            or collection == "anatomy_terms"
                        )
                    ):
                        continue
                    if (
                        fuzzy_similarity < _FUZZY_THRESHOLDS[collection]
                        and not exact_phonetic_match
                    ):
                        continue
                    strong_vector_match = (
                        similarity >= 0.72 and fuzzy_similarity >= 0.78
                    )
                    if (
                        edit_similarity < 0.60
                        and not strong_vector_match
                        and not exact_phonetic_match
                    ):
                        continue
                    if exact_phonetic_match:
                        ranking_score = {
                            "derived_official_en_phonetic_component": 0.92,
                            "derived_official_en_token": 0.94,
                            "derived_official_en_phonetic_full": 0.98,
                        }.get(row["source_kind"], 1.0)
                        if row["source_kind"] == "derived_ingredient_en_token":
                            source_compact = _clean(source_text).replace(" ", "")
                            canonical_ko_compact = _clean(
                                row["canonical_ko"]
                            ).replace(" ", "")
                            if source_compact not in canonical_ko_compact:
                                ranking_score -= 0.08
                            if "/" in str(row["canonical_en"] or ""):
                                ranking_score -= 0.04
                    elif strong_string_match:
                        string_evidence = (fuzzy_similarity + edit_similarity) / 2
                        ranking_score = max(
                            similarity,
                            (string_evidence + similarity) / 2,
                        )
                    else:
                        ranking_score = similarity
                    payload = json.loads(row["payload"] or "{}")
                    collection_candidates.append(
                        {
                            "source_text": source_text,
                            "start_char": start,
                            "end_char": end,
                            "collection": collection,
                            "entity_id": row["entity_id"],
                            "canonical_ko": row["canonical_ko"],
                            "canonical_en": row["canonical_en"],
                            "match_type": "vector_ngram",
                            "review_status": row["review_status"],
                            "retrieval_score": round(similarity, 6),
                            "string_similarity": round(fuzzy_similarity, 6),
                            "edit_similarity": round(edit_similarity, 6),
                            "vector_distance": round(float(row["distance"]), 6),
                            "retrieved_text": payload.get(
                                "parent_source_text", row["source_text"]
                            ),
                            "source_kind": row["source_kind"],
                            "_ranking_score": ranking_score,
                            **payload,
                        }
                    )
                collection_candidates.sort(
                    key=lambda item: item["_ranking_score"], reverse=True
                )
                collection_candidates = [
                    candidate
                    for candidate in collection_candidates
                    if (candidate["start_char"], candidate["end_char"])
                    not in protected_spans
                ]
                candidates_by_span: dict[tuple[int, int], list[dict[str, Any]]] = {}
                for candidate in collection_candidates:
                    span = (candidate["start_char"], candidate["end_char"])
                    candidates_by_span.setdefault(span, []).append(candidate)
                selected: list[dict[str, Any]] = []
                for span_candidates in candidates_by_span.values():
                    if collection != "drug_terms":
                        selected.append(span_candidates[0])
                        continue
                    strongest_by_entity_type: dict[str, dict[str, Any]] = {}
                    for candidate in span_candidates:
                        entity_type = str(candidate.get("entity_type") or "")
                        if entity_type in {"ingredient", "product"}:
                            strongest_by_entity_type.setdefault(entity_type, candidate)
                    type_candidates = list(strongest_by_entity_type.values())
                    if len(type_candidates) == 2:
                        span_score = span_candidates[0]["_ranking_score"]
                        for candidate in type_candidates:
                            candidate["_selection_score"] = span_score
                        selected.extend(type_candidates)
                    else:
                        selected.extend(type_candidates or span_candidates[:1])
                selected.sort(
                    key=lambda item: item.get(
                        "_selection_score", item["_ranking_score"]
                    ),
                    reverse=True,
                )
                selected = selected[:MAX_VECTOR_CANDIDATES]
                if len(selected) < MAX_VECTOR_CANDIDATES:
                    selected_ids = {id(candidate) for candidate in selected}
                    selected.extend(
                        candidate
                        for candidate in collection_candidates
                        if id(candidate) not in selected_ids
                    )
                candidates.extend(selected[:MAX_VECTOR_CANDIDATES])
        finally:
            connection.close()

        deduplicated: dict[tuple[Any, ...], dict[str, Any]] = {}
        for candidate in candidates:
            if (candidate["start_char"], candidate["end_char"]) in protected_spans:
                continue
            key = (
                candidate["start_char"],
                candidate["end_char"],
                candidate["collection"],
                candidate["entity_id"],
            )
            current = deduplicated.get(key)
            if current is None or candidate["_ranking_score"] > current["_ranking_score"]:
                deduplicated[key] = candidate
        ranked = sorted(
            deduplicated.values(),
            key=lambda item: (
                -item.get("_selection_score", item["_ranking_score"]),
                item["start_char"],
            ),
        )
        strongest_by_span: dict[tuple[int, int], float] = {}
        for candidate in ranked:
            span = (candidate["start_char"], candidate["end_char"])
            strongest_by_span.setdefault(
                span,
                candidate.get("_selection_score", candidate["_ranking_score"]),
            )
        selected = [
            candidate
            for candidate in ranked
            if candidate.get("_selection_score", candidate["_ranking_score"])
            >= strongest_by_span[(candidate["start_char"], candidate["end_char"])] - 0.03
        ][:MAX_VECTOR_CANDIDATES]
        return [
            {
                key: value
                for key, value in candidate.items()
                if key not in {"_ranking_score", "_selection_score"}
            }
            for candidate in selected
        ]

