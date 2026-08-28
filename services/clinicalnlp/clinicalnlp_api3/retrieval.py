from __future__ import annotations

import sqlite3
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


COLLECTIONS = (
    "drug_terms",
    "procedure_terms",
    "anatomy_terms",
    "emergency_terms",
    "kcd9_terms",
)

_ABDOMINAL_PAIN_CONTEXT_RE = re.compile(
    r"배(?:가|로|를|는|도)?(?:\s+[0-9A-Za-z가-힣]+){0,2}\s+아(?:파|프)[0-9A-Za-z가-힣]*"
)
_FEVER_FINDING_CONTEXT_RE = re.compile(
    r"열(?:이요|이|은|는|도)"
    r"(?:\s+(?:조금|좀|많이|계속|약간))?"
    r"(?:\s+(?:있|없|나)[0-9A-Za-z가-힣]*)?"
    r"(?![0-9A-Za-z가-힣])"
)
_PALPATION_CONTEXT_RE = re.compile(
    r"배(?:를|는|도)?\s+(?:(?:한번|좀)\s+)?(?:눌러|만져)[0-9A-Za-z가-힣]*"
)
_DIZZINESS_CONTEXT_RE = re.compile(
    r"(?:좀\s+)?어지(?:러|럽)[0-9A-Za-z가-힣]*"
)
_HEMATOCHEZIA_CONTEXT_RE = re.compile(
    r"(?:변에\s+)?(?:선홍색|빨간)\s*피(?:가|를)?"
)
_STOOL_CONTEXT_RE = re.compile(r"대변|혈변|변에|화장실")
_STROKE_OR_ICH_CONTEXT_RE = re.compile(
    r"스트로(?:크|키?)|스트록|뇌졸중|뇌중풍|뇌출혈|"
    r"브레인\s*해모레지|brain\s*hemorrhage|(?<![A-Za-z])ICH(?![A-Za-z])",
    re.IGNORECASE,
)
_ANTICOAGULANT_STT_SPAN_RE = re.compile(r"항응구제|항구제")
_ANTIHYPERTENSIVE_STT_SPAN_RE = re.compile(r"혈압(?:력|약)")
_MEDICATION_USE_CONTEXT_RE = re.compile(r"먹|복용|투약|약")
_CHILLS_STT_SPAN_RE = re.compile(r"우한")
_CHILLS_SYMPTOM_CONTEXT_RE = re.compile(r"열|기침|감기|몸살|증상|떨|있었|없었")
_APPROVED_EMERGENCY_STT_ALIASES = (
    (re.compile(r"스프텀"), "sputum"),
    (re.compile(r"디스프니아"), "dyspnea"),
    (re.compile(r"하이퍼\s*텐션"), "hypertension"),
    (re.compile(r"얼티케리아"), "urticaria"),
    (re.compile(r"위징|위증"), "wheezing"),
    (re.compile(r"브로콘\s*다일레이터"), "bronchodilator"),
)
_COUGH_STT_ALIAS_RE = re.compile(r"코프")
_APPROVED_DRUG_STT_ALIASES = (
    (re.compile(r"디오트로피움"), 4174),
    (re.compile(r"살부타몰"), 2806),
)


class Retriever(Protocol):
    def retrieve(self, *, raw_text: str, context: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ...


@dataclass(frozen=True)
class DictionaryPaths:
    drug: Path
    procedure: Path
    anatomy: Path
    emergency: Path
    kcd9: Path

    @classmethod
    def discover(cls, root: Path) -> "DictionaryPaths":
        patterns = {
            "drug": "ERON_의약품용어_DB_v1.sqlite",
            "procedure": "ERON_검사처치시술용어_DB_v1.sqlite",
            "anatomy": "ERON_anatomy_terms.sqlite",
            "emergency": "ERON_응급의학용어_DB_v1.sqlite",
            "kcd9": "hira_kcd9.sqlite",
        }
        found: dict[str, Path] = {}
        for key, filename in patterns.items():
            matches = list(root.rglob(filename))
            if len(matches) != 1:
                raise ValueError(f"expected one {key} dictionary under {root}, found {len(matches)}")
            found[key] = matches[0]
        return cls(**found)


class SqliteDictionaryRetriever:
    """Lexical half of hybrid retrieval over the five source dictionaries."""

    def __init__(self, db_root: Path):
        self.paths = DictionaryPaths.discover(Path(db_root))

    @staticmethod
    def _read(path: Path, query: str, parameters: Any) -> list[sqlite3.Row]:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            values = parameters if isinstance(parameters, tuple) else (parameters,)
            return list(connection.execute(query, values))
        finally:
            connection.close()

    @staticmethod
    def _find_source(raw_text: str, source_text: str) -> int:
        start = raw_text.find(source_text)
        while start >= 0:
            if start == 0 or not raw_text[start - 1].isalnum() or not source_text[0].isalnum():
                return start
            start = raw_text.find(source_text, start + 1)
        return -1

    @staticmethod
    def _candidate(
        raw_text: str,
        source_text: str,
        *,
        collection: str,
        entity_id: str,
        canonical_ko: str,
        canonical_en: str | None,
        match_type: str,
        review_status: str | None,
        **extra: Any,
    ) -> dict[str, Any] | None:
        start = SqliteDictionaryRetriever._find_source(raw_text, source_text)
        if start < 0:
            return None
        return {
            "source_text": source_text,
            "start_char": start,
            "end_char": start + len(source_text),
            "collection": collection,
            "entity_id": entity_id,
            "canonical_ko": canonical_ko,
            "canonical_en": canonical_en or "",
            "match_type": match_type,
            "review_status": review_status,
            "retrieval_score": 1.0,
            **extra,
        }

    def _emergency(
        self, raw_text: str, context: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        rows = self._read(
            self.paths.emergency,
            """
            SELECT * FROM (
                SELECT a.alias AS source_text, a.term_id, a.alias_type,
                       a.review_status, t.standard_ko AS canonical_ko,
                       t.standard_en AS canonical_en,
                       'alias_exact' AS match_type
                  FROM aliases a JOIN terms t USING(term_id)
                 WHERE length(a.alias) >= 2 AND instr(?, a.alias) > 0
                UNION ALL
                SELECT t.standard_ko AS source_text, t.term_id,
                       'OFFICIAL' AS alias_type, t.review_status,
                       t.standard_ko AS canonical_ko,
                       t.standard_en AS canonical_en,
                       'official_exact' AS match_type
                  FROM terms t
                 WHERE length(t.standard_ko) >= 2 AND instr(?, t.standard_ko) > 0
            ) ORDER BY length(source_text) DESC LIMIT 30
            """,
            (raw_text, raw_text),
        )
        exact_candidates = [
            candidate
            for row in rows
            if (candidate := self._candidate(
                raw_text,
                row["source_text"],
                collection="emergency_terms",
                entity_id=f"emergency:{row['term_id']}",
                canonical_ko=row["canonical_ko"],
                canonical_en=row["canonical_en"],
                match_type=row["match_type"],
                review_status=row["review_status"],
                alias_type=row["alias_type"],
            ))
        ]
        contextual_candidates: list[dict[str, Any]] = []
        dialogue_text = "\n".join(
            str(segment.get("text") or "") for segment in context
        )
        hematochezia_matches = list(_HEMATOCHEZIA_CONTEXT_RE.finditer(raw_text))
        if hematochezia_matches and _STOOL_CONTEXT_RE.search(dialogue_text):
            hematochezia = self._read(
                self.paths.emergency,
                """
                SELECT term_id, standard_ko AS canonical_ko,
                       standard_en AS canonical_en
                  FROM terms
                 WHERE lower(standard_en)='hematochezia'
                 ORDER BY term_id LIMIT 1
                """,
                (),
            )
            if hematochezia:
                row = hematochezia[0]
                for match in hematochezia_matches:
                    candidate = self._candidate(
                        raw_text,
                        match.group(0),
                        collection="emergency_terms",
                        entity_id=f"emergency:{row['term_id']}",
                        canonical_ko=row["canonical_ko"],
                        canonical_en=row["canonical_en"],
                        match_type="contextual_phrase",
                        review_status="NEEDS_CLINICAL_REVIEW",
                        retrieval_score=0.90,
                        source_kind="full_dialogue_context",
                        evidence_text="선홍색 혈변",
                    )
                    if candidate is not None:
                        contextual_candidates.append(candidate)
        dizziness_matches = list(_DIZZINESS_CONTEXT_RE.finditer(raw_text))
        if dizziness_matches:
            dizziness = self._read(
                self.paths.emergency,
                """
                SELECT term_id, standard_ko AS canonical_ko,
                       standard_en AS canonical_en
                  FROM terms
                 WHERE lower(standard_en)='vertigo'
                 ORDER BY term_id LIMIT 1
                """,
                (),
            )
            if dizziness:
                row = dizziness[0]
                for match in dizziness_matches:
                    candidate = self._candidate(
                        raw_text,
                        match.group(0),
                        collection="emergency_terms",
                        entity_id=f"emergency:{row['term_id']}",
                        canonical_ko=row["canonical_ko"],
                        canonical_en=row["canonical_en"],
                        match_type="contextual_phrase",
                        review_status="NEEDS_CLINICAL_REVIEW",
                        retrieval_score=0.88,
                        source_kind="derived_context_expression",
                        evidence_text="어지럼",
                    )
                    if candidate is not None:
                        contextual_candidates.append(candidate)
        abdominal_matches = list(_ABDOMINAL_PAIN_CONTEXT_RE.finditer(raw_text))
        if abdominal_matches:
            abdominal_pain = self._read(
                self.paths.emergency,
                """
                SELECT term_id, standard_ko AS canonical_ko,
                       standard_en AS canonical_en
                  FROM terms
                 WHERE lower(standard_en)='abdominal pain'
                 ORDER BY term_id LIMIT 1
                """,
                (),
            )
            if abdominal_pain:
                row = abdominal_pain[0]
                for match in abdominal_matches:
                    candidate = self._candidate(
                        raw_text,
                        match.group(0),
                        collection="emergency_terms",
                        entity_id=f"emergency:{row['term_id']}",
                        canonical_ko=row["canonical_ko"],
                        canonical_en=row["canonical_en"],
                        match_type="contextual_phrase",
                        review_status="NEEDS_CLINICAL_REVIEW",
                        retrieval_score=0.82,
                        source_kind="derived_context_expression",
                        evidence_text="배가 아파요",
                    )
                    if candidate is not None:
                        contextual_candidates.append(candidate)
        fever_matches = list(_FEVER_FINDING_CONTEXT_RE.finditer(raw_text))
        if fever_matches:
            fever = self._read(
                self.paths.emergency,
                """
                SELECT term_id, standard_ko AS canonical_ko,
                       standard_en AS canonical_en
                  FROM terms
                 WHERE lower(standard_en)='fever'
                 ORDER BY term_id LIMIT 1
                """,
                (),
            )
            if fever:
                row = fever[0]
                for match in fever_matches:
                    candidate = self._candidate(
                        raw_text,
                        match.group(0),
                        collection="emergency_terms",
                        entity_id=f"emergency:{row['term_id']}",
                        canonical_ko=row["canonical_ko"],
                        canonical_en=row["canonical_en"],
                        match_type="contextual_phrase",
                        review_status="NEEDS_CLINICAL_REVIEW",
                        retrieval_score=0.86,
                        source_kind="derived_context_expression",
                        evidence_text="열이 나요",
                    )
                    if candidate is not None:
                        contextual_candidates.append(candidate)
        palpation_matches = list(_PALPATION_CONTEXT_RE.finditer(raw_text))
        if palpation_matches:
            palpation = self._read(
                self.paths.emergency,
                """
                SELECT term_id, standard_ko AS canonical_ko,
                       standard_en AS canonical_en
                  FROM terms
                 WHERE lower(standard_en)='palpation'
                 ORDER BY term_id LIMIT 1
                """,
                (),
            )
            if palpation:
                row = palpation[0]
                for match in palpation_matches:
                    candidate = self._candidate(
                        raw_text,
                        match.group(0),
                        collection="emergency_terms",
                        entity_id=f"emergency:{row['term_id']}",
                        canonical_ko=row["canonical_ko"],
                        canonical_en=row["canonical_en"],
                        match_type="contextual_phrase",
                        review_status="NEEDS_CLINICAL_REVIEW",
                        retrieval_score=0.80,
                        source_kind="derived_context_expression",
                        evidence_text="palpation",
                    )
                    if candidate is not None:
                        contextual_candidates.append(candidate)
        if _ANTICOAGULANT_STT_SPAN_RE.search(raw_text):
            anticoagulant_rows = self._read(
                self.paths.emergency,
                """
                SELECT term_id, standard_ko AS canonical_ko,
                       standard_en AS canonical_en
                  FROM terms
                 WHERE standard_ko='항응고제'
                 ORDER BY term_id LIMIT 1
                """,
                (),
            )
            if anticoagulant_rows:
                row = anticoagulant_rows[0]
                for match in _ANTICOAGULANT_STT_SPAN_RE.finditer(raw_text):
                    candidate = self._candidate(
                        raw_text,
                        match.group(0),
                        collection="emergency_terms",
                        entity_id=f"emergency:{row['term_id']}",
                        canonical_ko=row["canonical_ko"],
                        canonical_en=row["canonical_en"],
                        match_type="contextual_alternative",
                        review_status="NEEDS_CLINICAL_REVIEW",
                        retrieval_score=0.84,
                        source_kind="stt_phonetic_expression",
                        evidence_text="항응고제",
                    )
                    if candidate is not None:
                        contextual_candidates.append(candidate)
        if (
            _ANTIHYPERTENSIVE_STT_SPAN_RE.search(raw_text)
            and _MEDICATION_USE_CONTEXT_RE.search(raw_text)
        ):
            antihypertensive_rows = self._read(
                self.paths.emergency,
                """
                SELECT term_id, standard_ko AS canonical_ko,
                       standard_en AS canonical_en
                  FROM terms
                 WHERE lower(standard_en)='antihypertensive agent'
                 ORDER BY term_id LIMIT 1
                """,
                (),
            )
            if antihypertensive_rows:
                row = antihypertensive_rows[0]
                for match in _ANTIHYPERTENSIVE_STT_SPAN_RE.finditer(raw_text):
                    candidate = self._candidate(
                        raw_text,
                        match.group(0),
                        collection="emergency_terms",
                        entity_id=f"emergency:{row['term_id']}",
                        canonical_ko=row["canonical_ko"],
                        canonical_en=row["canonical_en"],
                        match_type="contextual_alternative",
                        review_status="NEEDS_CLINICAL_REVIEW",
                        retrieval_score=0.83,
                        source_kind="medication_context",
                        evidence_text="고혈압약",
                    )
                    if candidate is not None:
                        contextual_candidates.append(candidate)
        if (
            _ANTICOAGULANT_STT_SPAN_RE.search(raw_text)
            and _MEDICATION_USE_CONTEXT_RE.search(raw_text)
            and _STROKE_OR_ICH_CONTEXT_RE.search(dialogue_text)
        ):
            anticoagulant_rows = self._read(
                self.paths.emergency,
                """
                SELECT term_id, standard_ko AS canonical_ko,
                       standard_en AS canonical_en
                  FROM terms
                 WHERE standard_ko='항응고제'
                 ORDER BY term_id LIMIT 1
                """,
                (),
            )
            if anticoagulant_rows:
                row = anticoagulant_rows[0]
                for match in _ANTICOAGULANT_STT_SPAN_RE.finditer(raw_text):
                    candidate = self._candidate(
                        raw_text,
                        match.group(0),
                        collection="emergency_terms",
                        entity_id=f"emergency:{row['term_id']}",
                        canonical_ko=row["canonical_ko"],
                        canonical_en=row["canonical_en"],
                        match_type="contextual_alternative",
                        review_status="NEEDS_CLINICAL_REVIEW",
                        retrieval_score=0.62,
                        source_kind="full_dialogue_context",
                        evidence_text="stroke_or_intracranial_hemorrhage_context",
                    )
                    if candidate is not None:
                        contextual_candidates.append(candidate)
        return exact_candidates + contextual_candidates

    def _approved_emergency_stt_aliases(
        self, raw_text: str
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for pattern, canonical_en in _APPROVED_EMERGENCY_STT_ALIASES:
            matches = list(pattern.finditer(raw_text))
            if not matches:
                continue
            rows = self._read(
                self.paths.emergency,
                """
                SELECT term_id, standard_ko AS canonical_ko,
                       standard_en AS canonical_en
                  FROM terms
                 WHERE lower(standard_en)=?
                 ORDER BY term_id LIMIT 1
                """,
                canonical_en,
            )
            if not rows:
                continue
            row = rows[0]
            for match in matches:
                candidates.append(
                    {
                        "source_text": match.group(0),
                        "start_char": match.start(),
                        "end_char": match.end(),
                        "collection": "emergency_terms",
                        "entity_id": f"emergency:{row['term_id']}",
                        "canonical_ko": row["canonical_ko"],
                        "canonical_en": row["canonical_en"],
                        "match_type": "stt_alias_exact",
                        "review_status": "NEEDS_CLINICAL_REVIEW",
                        "retrieval_score": 0.99,
                        "source_kind": "approved_stt_loanword",
                    }
                )
        return candidates

    def _approved_kcd_stt_aliases(self, raw_text: str) -> list[dict[str, Any]]:
        matches = list(_COUGH_STT_ALIAS_RE.finditer(raw_text))
        if not matches:
            return []
        rows = self._read(
            self.paths.kcd9,
            """
            SELECT c.code, c.code_display,
                   c.canonical_ko_name AS canonical_ko,
                   c.canonical_en_name AS canonical_en,
                   c.principal_allowed, c.sex_restriction,
                   c.min_age, c.max_age
              FROM kcd_codes c
             WHERE c.code='R05' AND c.is_complete=1
             LIMIT 1
            """,
            (),
        )
        if not rows:
            return []
        row = rows[0]
        return [
            {
                "source_text": match.group(0),
                "start_char": match.start(),
                "end_char": match.end(),
                "collection": "kcd9_terms",
                "entity_id": f"kcd:{row['code']}",
                "canonical_ko": row["canonical_ko"],
                "canonical_en": row["canonical_en"],
                "match_type": "stt_alias_exact",
                "review_status": "NEEDS_CLINICAL_REVIEW",
                "retrieval_score": 0.99,
                "source_kind": "approved_stt_loanword",
                "code": row["code"],
                "code_display": row["code_display"],
                "is_complete": True,
                "principal_allowed": bool(row["principal_allowed"]),
                "sex_restriction": row["sex_restriction"],
                "min_age": row["min_age"],
                "max_age": row["max_age"],
            }
            for match in matches
        ]

    def _approved_drug_stt_aliases(self, raw_text: str) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for pattern, ingredient_id in _APPROVED_DRUG_STT_ALIASES:
            matches = list(pattern.finditer(raw_text))
            if not matches:
                continue
            rows = self._read(
                self.paths.drug,
                """
                SELECT ingredient_id, canonical_ko, canonical_en
                  FROM ingredients
                 WHERE ingredient_id=?
                 LIMIT 1
                """,
                ingredient_id,
            )
            if not rows:
                continue
            row = rows[0]
            for match in matches:
                candidates.append(
                    {
                        "source_text": match.group(0),
                        "start_char": match.start(),
                        "end_char": match.end(),
                        "collection": "drug_terms",
                        "entity_id": f"drug:ingredient:{row['ingredient_id']}",
                        "canonical_ko": row["canonical_ko"],
                        "canonical_en": row["canonical_en"],
                        "match_type": "stt_alias_exact",
                        "review_status": "NEEDS_CLINICAL_REVIEW",
                        "retrieval_score": 0.99,
                        "source_kind": "approved_stt_loanword",
                    }
                )
        return candidates

    def _anatomy(self, raw_text: str) -> list[dict[str, Any]]:
        rows = self._read(
            self.paths.anatomy,
            """
            SELECT a.alias AS source_text, a.term_id,
                   t.korean_name AS canonical_ko, t.english_name AS canonical_en,
                   t.verification_status AS review_status
              FROM anatomical_aliases a JOIN anatomical_terms t USING(term_id)
             WHERE length(a.alias) >= 2 AND instr(?, a.alias) > 0
             ORDER BY length(a.alias) DESC LIMIT 30
            """,
            raw_text,
        )
        return [
            candidate
            for row in rows
            if (candidate := self._candidate(
                raw_text,
                row["source_text"],
                collection="anatomy_terms",
                entity_id=f"anatomy:{row['term_id']}",
                canonical_ko=row["canonical_ko"],
                canonical_en=row["canonical_en"],
                match_type="alias_exact",
                review_status=row["review_status"],
            ))
        ]

    def _procedure(self, raw_text: str) -> list[dict[str, Any]]:
        rows = self._read(
            self.paths.procedure,
            """
            SELECT a.alias AS source_text, a.term_id, a.alias_type, a.review_status,
                   t.canonical_name_ko AS canonical_ko, t.canonical_name_en AS canonical_en,
                   t.category
              FROM term_aliases a JOIN clinical_terms t USING(term_id)
             WHERE length(a.alias) >= 2 AND instr(?, a.alias) > 0
             ORDER BY length(a.alias) DESC LIMIT 30
            """,
            raw_text,
        )
        candidates = [
            candidate
            for row in rows
            if (candidate := self._candidate(
                raw_text,
                row["source_text"],
                collection="procedure_terms",
                entity_id=f"procedure:{row['term_id']}",
                canonical_ko=row["canonical_ko"],
                canonical_en=row["canonical_en"],
                match_type="alias_exact",
                review_status=row["review_status"],
                alias_type=row["alias_type"],
                category=row["category"],
            ))
        ]
        if candidates:
            return candidates
        phrase = re.search(
            r"([가-힣]{2,10})\s*(CT|MRI|PET|ECG|EKG|X[- ]?ray)",
            raw_text,
            re.IGNORECASE,
        )
        if phrase is None:
            return []
        source_text = phrase.group(0)
        fts_query = f"{phrase.group(1)} AND {phrase.group(2)}"
        rows = self._read(
            self.paths.procedure,
            """
            SELECT s.term_id, s.name_ko AS canonical_ko, s.name_en AS canonical_en,
                   t.category, s.rank
              FROM term_search s JOIN clinical_terms t USING(term_id)
             WHERE term_search MATCH ?
             ORDER BY s.rank LIMIT 8
            """,
            (fts_query,),
        )
        return [
            candidate
            for row in rows
            if (candidate := self._candidate(
                raw_text,
                source_text,
                collection="procedure_terms",
                entity_id=f"procedure:{row['term_id']}",
                canonical_ko=row["canonical_ko"],
                canonical_en=row["canonical_en"],
                match_type="fts",
                review_status="OFFICIAL",
                retrieval_score=0.55,
                category=row["category"],
            ))
        ]

    def _drug(self, raw_text: str) -> list[dict[str, Any]]:
        rows = self._read(
            self.paths.drug,
            """
            SELECT d.term AS source_text, d.entity_type, d.entity_id, d.term_type,
                   d.review_status,
                   COALESCE(i.canonical_ko, p.product_name_ko, d.term) AS canonical_ko,
                   COALESCE(i.canonical_en, p.product_name_en, '') AS canonical_en
              FROM drug_terms d
              LEFT JOIN ingredients i
                ON d.entity_type='INGREDIENT' AND i.ingredient_id=CAST(d.entity_id AS INTEGER)
              LEFT JOIN products p
                ON d.entity_type='PRODUCT' AND p.item_id=d.entity_id
             WHERE length(d.term) >= 2 AND instr(?, d.term) > 0
             ORDER BY length(d.term) DESC LIMIT 30
            """,
            raw_text,
        )
        return [
            candidate
            for row in rows
            if (candidate := self._candidate(
                raw_text,
                row["source_text"],
                collection="drug_terms",
                entity_id=f"drug:{row['entity_type'].lower()}:{row['entity_id']}",
                canonical_ko=row["canonical_ko"],
                canonical_en=row["canonical_en"],
                match_type="official_exact",
                review_status=row["review_status"],
                entity_type=row["entity_type"].lower(),
                term_type=row["term_type"],
            ))
        ]

    def _kcd9(self, raw_text: str) -> list[dict[str, Any]]:
        contextual_candidates: list[dict[str, Any]] = []
        chills_matches = list(_CHILLS_STT_SPAN_RE.finditer(raw_text))
        if chills_matches and _CHILLS_SYMPTOM_CONTEXT_RE.search(raw_text):
            rows = self._read(
                self.paths.kcd9,
                """
                SELECT t.code, t.ko_name AS canonical_ko,
                       t.en_name AS canonical_en, c.code_display,
                       c.principal_allowed, c.sex_restriction,
                       c.min_age, c.max_age
                  FROM kcd_terms t JOIN kcd_codes c USING(code)
                 WHERE t.code IN ('R508', 'R688')
                   AND instr(t.ko_name, '오한') > 0
                   AND c.is_complete=1
                 ORDER BY t.code, t.is_canonical DESC, t.term_id
                """,
                (),
            )
            unique_rows: dict[str, sqlite3.Row] = {}
            for row in rows:
                unique_rows.setdefault(row["code"], row)
            for match in chills_matches:
                for row in unique_rows.values():
                    candidate = self._candidate(
                        raw_text,
                        match.group(0),
                        collection="kcd9_terms",
                        entity_id=f"kcd:{row['code']}",
                        canonical_ko=row["canonical_ko"],
                        canonical_en=row["canonical_en"],
                        match_type="contextual_alternative",
                        review_status="official",
                        retrieval_score=0.86,
                        source_kind="stt_phonetic_expression",
                        evidence_text="오한",
                        code=row["code"],
                        code_display=row["code_display"],
                        is_complete=True,
                        principal_allowed=bool(row["principal_allowed"]),
                        sex_restriction=row["sex_restriction"],
                        min_age=row["min_age"],
                        max_age=row["max_age"],
                    )
                    if candidate is not None:
                        contextual_candidates.append(candidate)
        rows = self._read(
            self.paths.kcd9,
            """
            SELECT t.ko_name AS source_text, t.code, c.code_display,
                   c.canonical_ko_name AS canonical_ko,
                   c.canonical_en_name AS canonical_en,
                   c.principal_allowed, c.sex_restriction, c.min_age, c.max_age
              FROM kcd_terms t JOIN kcd_codes c USING(code)
             WHERE c.is_complete=1 AND length(t.ko_name) >= 2 AND instr(?, t.ko_name) > 0
             ORDER BY length(t.ko_name) DESC, t.is_canonical DESC LIMIT 30
            """,
            raw_text,
        )
        candidates = [
            candidate
            for row in rows
            if (candidate := self._candidate(
                raw_text,
                row["source_text"],
                collection="kcd9_terms",
                entity_id=f"kcd:{row['code']}",
                canonical_ko=row["canonical_ko"],
                canonical_en=row["canonical_en"],
                match_type="official_exact",
                review_status="official",
                code=row["code"],
                code_display=row["code_display"],
                is_complete=True,
                principal_allowed=bool(row["principal_allowed"]),
                sex_restriction=row["sex_restriction"],
                min_age=row["min_age"],
                max_age=row["max_age"],
            ))
        ]
        if candidates or contextual_candidates:
            return candidates + contextual_candidates

        diagnosis_markers = ("염", "암", "골절", "경색", "질환", "증후군", "부전", "의심", "의증", "진단")
        if not any(marker in raw_text for marker in diagnosis_markers):
            return []
        cleaned = re.sub(
            r"\s*(?:의심입니다|의심|의증입니다|의증|추정입니다|추정|진단입니다|진단)\s*[.!?]?\s*$",
            "",
            raw_text.strip(),
        )
        tokens = [token for token in re.findall(r"[0-9A-Za-z가-힣]+", cleaned) if len(token) >= 2]
        if not tokens:
            return []
        source_text = " ".join(tokens)
        start = raw_text.find(source_text)
        if start < 0:
            return []
        fts_query = " AND ".join(tokens)
        rows = self._read(
            self.paths.kcd9,
            """
            SELECT f.code, f.ko_name, f.rank, c.code_display,
                   c.canonical_ko_name AS canonical_ko,
                   c.canonical_en_name AS canonical_en,
                   c.principal_allowed, c.sex_restriction, c.min_age, c.max_age
              FROM kcd_terms_fts f JOIN kcd_codes c USING(code)
             WHERE kcd_terms_fts MATCH ? AND c.is_complete=1 AND c.principal_allowed=1
             ORDER BY f.rank LIMIT 8
            """,
            (fts_query,),
        )
        return [
            candidate
            for row in rows
            if (candidate := self._candidate(
                raw_text,
                source_text,
                collection="kcd9_terms",
                entity_id=f"kcd:{row['code']}",
                canonical_ko=row["canonical_ko"],
                canonical_en=row["canonical_en"],
                match_type="fts",
                review_status="official",
                retrieval_score=0.55,
                code=row["code"],
                code_display=row["code_display"],
                is_complete=True,
                principal_allowed=True,
                sex_restriction=row["sex_restriction"],
                min_age=row["min_age"],
                max_age=row["max_age"],
            ))
        ]

    def retrieve(self, *, raw_text: str, context: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates = (
            self._drug(raw_text)
            + self._procedure(raw_text)
            + self._anatomy(raw_text)
            + self._emergency(raw_text, context)
            + self._kcd9(raw_text)
            + self._approved_emergency_stt_aliases(raw_text)
            + self._approved_kcd_stt_aliases(raw_text)
            + self._approved_drug_stt_aliases(raw_text)
        )
        deduplicated: dict[tuple[Any, ...], dict[str, Any]] = {}
        for candidate in candidates:
            key = (
                candidate["start_char"],
                candidate["end_char"],
                candidate["collection"],
                candidate["entity_id"],
            )
            deduplicated.setdefault(key, candidate)
        return sorted(
            deduplicated.values(),
            key=lambda item: (
                item["start_char"],
                -(item["end_char"] - item["start_char"]),
                item["collection"],
            ),
        )[:50]


class HybridRetriever:
    """Merge SQLite lexical candidates with independently updated vector collections."""

    def __init__(self, lexical: Retriever, vector: Retriever | None = None):
        self.lexical = lexical
        self.vector = vector

    def retrieve(self, *, raw_text: str, context: list[dict[str, Any]]) -> list[dict[str, Any]]:
        lexical_candidates = self.lexical.retrieve(raw_text=raw_text, context=context)
        candidates = list(lexical_candidates)
        if self.vector is not None:
            vector_candidates = self.vector.retrieve(raw_text=raw_text, context=context)
            for candidate in vector_candidates:
                start = candidate.get("start_char")
                end = candidate.get("end_char")
                overlaps_lexical_evidence = any(
                    isinstance(start, int)
                    and isinstance(end, int)
                    and start < lexical.get("end_char", -1)
                    and end > lexical.get("start_char", -1)
                    and (
                        lexical.get("match_type") != "contextual_alternative"
                        or (
                            candidate.get("collection") == lexical.get("collection")
                            and candidate.get("entity_id") == lexical.get("entity_id")
                        )
                    )
                    for lexical in lexical_candidates
                )
                if not overlaps_lexical_evidence:
                    candidates.append(candidate)
        merged: dict[tuple[Any, ...], dict[str, Any]] = {}
        for candidate in candidates:
            key = (
                candidate.get("start_char"),
                candidate.get("end_char"),
                candidate.get("collection"),
                candidate.get("entity_id"),
            )
            current = merged.get(key)
            if current is None or candidate.get("retrieval_score", 0) > current.get("retrieval_score", 0):
                merged[key] = candidate
        return list(merged.values())

