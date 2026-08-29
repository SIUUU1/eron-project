from __future__ import annotations

from collections import deque
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable

from .retrieval import DictionaryPaths


OFFICIAL_REVIEW_STATUSES = frozenset(
    {
        "official",
        "approved",
        "verified",
        "official_coded",
        "official_source",
        "manually_verified",
        "source_imported",
    }
)
_HANGUL_RE = re.compile(r"[가-힣]")
_MAX_MATCHES_PER_SEGMENT = 50


def _read_rows(path: Path, query: str) -> list[sqlite3.Row]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return list(connection.execute(query))
    finally:
        connection.close()


def _official(status: object) -> bool:
    return str(status or "").strip().casefold() in OFFICIAL_REVIEW_STATUSES


class OfficialRawExactRetriever:
    """Match official Korean canonical terms with one in-memory text scan."""

    def __init__(self, db_root: Path) -> None:
        paths = DictionaryPaths.discover(Path(db_root))
        entries = list(self._canonical_entries(paths))
        self._transitions: list[dict[str, int]] = [{}]
        self._failures: list[int] = [0]
        self._outputs: list[list[dict[str, Any]]] = [[]]
        for entry in entries:
            self._insert(entry)
        self._build_failures()

    @staticmethod
    def _canonical_entries(paths: DictionaryPaths) -> Iterable[dict[str, Any]]:
        for row in _read_rows(
            paths.drug,
            """
            SELECT ingredient_id, canonical_ko, canonical_en, concept_status
              FROM ingredients
             WHERE canonical_ko IS NOT NULL AND length(trim(canonical_ko)) >= 2
            """,
        ):
            term = str(row["canonical_ko"] or "").strip()
            if term and _HANGUL_RE.search(term) and _official(row["concept_status"]):
                yield {
                    "term": term,
                    "collection": "drug_terms",
                    "entity_id": f"drug:ingredient:{row['ingredient_id']}",
                    "canonical_ko": term,
                    "canonical_en": str(row["canonical_en"] or ""),
                    "entity_type": "ingredient",
                }

        for row in _read_rows(
            paths.procedure,
            """
            SELECT term_id, canonical_name_ko, canonical_name_en, review_status
              FROM clinical_terms
             WHERE canonical_name_ko IS NOT NULL
               AND length(trim(canonical_name_ko)) >= 2
            """,
        ):
            term = str(row["canonical_name_ko"] or "").strip()
            if term and _HANGUL_RE.search(term) and _official(row["review_status"]):
                yield {
                    "term": term,
                    "collection": "procedure_terms",
                    "entity_id": f"procedure:{row['term_id']}",
                    "canonical_ko": term,
                    "canonical_en": str(row["canonical_name_en"] or ""),
                }

        for row in _read_rows(
            paths.anatomy,
            """
            SELECT term_id, korean_name, english_name, verification_status
              FROM anatomical_terms
             WHERE korean_name IS NOT NULL AND length(trim(korean_name)) >= 2
            """,
        ):
            term = str(row["korean_name"] or "").strip()
            if term and _HANGUL_RE.search(term) and _official(row["verification_status"]):
                yield {
                    "term": term,
                    "collection": "anatomy_terms",
                    "entity_id": f"anatomy:{row['term_id']}",
                    "canonical_ko": term,
                    "canonical_en": str(row["english_name"] or ""),
                }

        for row in _read_rows(
            paths.emergency,
            """
            SELECT term_id, standard_ko, standard_en, review_status
              FROM terms
             WHERE standard_ko IS NOT NULL AND length(trim(standard_ko)) >= 2
            """,
        ):
            term = str(row["standard_ko"] or "").strip()
            if term and _HANGUL_RE.search(term) and _official(row["review_status"]):
                yield {
                    "term": term,
                    "collection": "emergency_terms",
                    "entity_id": f"emergency:{row['term_id']}",
                    "canonical_ko": term,
                    "canonical_en": str(row["standard_en"] or ""),
                }

    def _insert(self, entry: dict[str, Any]) -> None:
        state = 0
        for character in entry["term"]:
            next_state = self._transitions[state].get(character)
            if next_state is None:
                next_state = len(self._transitions)
                self._transitions[state][character] = next_state
                self._transitions.append({})
                self._failures.append(0)
                self._outputs.append([])
            state = next_state
        self._outputs[state].append(entry)

    def _build_failures(self) -> None:
        queue: deque[int] = deque(self._transitions[0].values())
        while queue:
            state = queue.popleft()
            for character, next_state in self._transitions[state].items():
                queue.append(next_state)
                failure = self._failures[state]
                while failure and character not in self._transitions[failure]:
                    failure = self._failures[failure]
                self._failures[next_state] = self._transitions[failure].get(
                    character, 0
                )
                self._outputs[next_state].extend(
                    self._outputs[self._failures[next_state]]
                )

    def retrieve(
        self, *, raw_text: str, context: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        del context
        state = 0
        matches: dict[tuple[int, int, str, str], dict[str, Any]] = {}
        for end_index, character in enumerate(raw_text, start=1):
            while state and character not in self._transitions[state]:
                state = self._failures[state]
            state = self._transitions[state].get(character, 0)
            for entry in self._outputs[state]:
                source_text = entry["term"]
                start_index = end_index - len(source_text)
                candidate = {
                    "source_text": source_text,
                    "start_char": start_index,
                    "end_char": end_index,
                    "collection": entry["collection"],
                    "entity_id": entry["entity_id"],
                    "canonical_ko": entry["canonical_ko"],
                    "canonical_en": entry["canonical_en"],
                    "match_type": "official_exact",
                    "review_status": "official",
                    "retrieval_score": 1.0,
                }
                if entry.get("entity_type"):
                    candidate["entity_type"] = entry["entity_type"]
                key = (
                    start_index,
                    end_index,
                    entry["collection"],
                    entry["entity_id"],
                )
                matches.setdefault(key, candidate)
        return sorted(
            matches.values(),
            key=lambda item: (
                item["start_char"],
                -(item["end_char"] - item["start_char"]),
                item["collection"],
                item["entity_id"],
            ),
        )[:_MAX_MATCHES_PER_SEGMENT]
