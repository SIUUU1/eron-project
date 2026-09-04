import re
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, desc, func, literal, or_, select
from sqlalchemy.orm import Session

from app.api.clinical_records import get_db
from app.core.config import settings
from app.models.kcd_code import KcdCode
from app.schemas.kcd import KcdCodeItem, KcdSearchResponse


router = APIRouter(prefix="/api/kcd", tags=["kcd"])


def search_token_variants(query: str) -> tuple[tuple[str, ...], ...]:
    """Return searchable token variants without relying on diagnosis-specific rules."""
    token_groups: list[tuple[str, ...]] = []
    for raw_token in re.findall(r"[A-Za-z0-9]+|[가-힣]+", query):
        token = re.sub(r"(의심|추정|증)$", "", raw_token)
        if len(token) < 2:
            continue

        variants = [token]
        lower_token = token.lower()
        if re.fullmatch(r"[a-z]+", lower_token):
            if lower_token.endswith("ies") and len(lower_token) > 3:
                variants.append(f"{token[:-3]}y")
            elif lower_token.endswith("es") and len(lower_token) > 3:
                variants.extend((token[:-2], token[:-1]))
            elif lower_token.endswith("s") and not lower_token.endswith("ss"):
                variants.append(token[:-1])

        group = tuple(dict.fromkeys(variants))
        if group not in token_groups:
            token_groups.append(group)
        if len(token_groups) == 5:
            break
    return tuple(token_groups)


def display_code(code: str) -> str:
    normalized = code.replace(".", "").upper()
    return f"{normalized[:3]}.{normalized[3:]}" if len(normalized) > 3 else normalized


def lookup_alias_terms(query: str) -> tuple[str, ...]:
    if not settings.kcd_alias_db_path:
        return ()

    alias_db = Path(settings.kcd_alias_db_path)
    if not alias_db.is_file():
        return ()

    normalized_aliases = {
        re.sub(r"[^A-Za-z0-9가-힣]", "", token).lower()
        for token in re.split(r"\s+", query)
    }
    normalized_aliases.add(re.sub(r"[^A-Za-z0-9가-힣]", "", query).lower())
    normalized_aliases.discard("")
    if not normalized_aliases:
        return ()

    placeholders = ", ".join("?" for _ in normalized_aliases)
    sql = f"""
        SELECT DISTINCT t.standard_ko, t.standard_en
        FROM aliases a
        JOIN terms t ON t.term_id = a.term_id
        WHERE lower(a.normalized_alias) IN ({placeholders})
          AND a.review_status = 'SOURCE_IMPORTED'
    """
    try:
        with sqlite3.connect(f"file:{alias_db}?mode=ro", uri=True) as connection:
            rows = connection.execute(sql, tuple(normalized_aliases)).fetchall()
    except (OSError, sqlite3.Error):
        return ()

    return tuple(dict.fromkeys(value for row in rows for value in row if value))


# Colloquial organ names whose KCD master wording differs from a plain
# "{organ}의 악성 신생물" substitution (verified against hira_kcd9.sqlite).
_CANCER_ORGAN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "대장": ("결장", "직장"),
    "자궁": ("자궁경부", "자궁체부"),
    "간": ("간", "간 및 간내 담관"),
    "코": ("비강",),
}


def expand_common_kcd_terms(query: str) -> tuple[str, ...]:
    """Expand common cancer names to the wording used by the KCD master."""
    normalized = " ".join(query.strip().split())
    expanded: list[str] = []

    korean_cancer = re.fullmatch(r"([가-힣]{1,30})암", normalized)
    if korean_cancer:
        organ = korean_cancer.group(1)
        for official_organ in _CANCER_ORGAN_SYNONYMS.get(organ, (organ,)):
            expanded.append(f"{official_organ}의 악성 신생물")

    english_cancer = re.fullmatch(r"(.+?)\s+cancer", normalized, flags=re.IGNORECASE)
    if english_cancer:
        organ = english_cancer.group(1).strip()
        expanded.append(f"malignant neoplasm of {organ}")

    return tuple(expanded)


@router.get("/search", response_model=KcdSearchResponse)
def search_kcd_codes(
    q: str = Query(min_length=1, max_length=100),
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    query = q.strip()
    normalized_code = re.sub(r"[^A-Za-z0-9]", "", query).upper()
    aliases = tuple(
        dict.fromkeys((*lookup_alias_terms(query), *expand_common_kcd_terms(query)))
    )
    token_groups = search_token_variants(query)

    def token_condition(variants: tuple[str, ...]):
        return or_(
            *(
                or_(
                    KcdCode.name_ko.ilike(f"%{variant}%"),
                    KcdCode.name_en.ilike(f"%{variant}%"),
                )
                for variant in variants
            )
        )

    conditions = [
        KcdCode.name_ko.ilike(f"%{query}%"),
        KcdCode.name_en.ilike(f"%{query}%"),
    ]
    conditions.extend(
        or_(
            KcdCode.name_ko.ilike(f"%{alias}%"),
            KcdCode.name_en.ilike(f"%{alias}%"),
        )
        for alias in aliases
    )
    if normalized_code:
        conditions.append(KcdCode.code.ilike(f"%{normalized_code}%"))
    conditions.extend(token_condition(group) for group in token_groups)

    code_score_conditions = (
        (
            (func.upper(KcdCode.code) == normalized_code, 0),
            (func.upper(KcdCode.code).like(f"{normalized_code}%"), 1),
        )
        if normalized_code
        else ()
    )
    score = case(
        *code_score_conditions,
        (KcdCode.name_ko == query, 2),
        (func.lower(KcdCode.name_en) == query.lower(), 2),
        (KcdCode.name_ko.ilike(f"{query}%"), 3),
        (KcdCode.name_en.ilike(f"{query}%"), 3),
        (KcdCode.name_ko.ilike(f"%{query}%"), 4),
        (KcdCode.name_en.ilike(f"%{query}%"), 4),
        *(
            (or_(KcdCode.name_ko.ilike(f"%{alias}%"), KcdCode.name_en.ilike(f"%{alias}%")), 4)
            for alias in aliases
        ),
        else_=5,
    )
    token_score = sum(
        (case((token_condition(group), 1), else_=0) for group in token_groups),
        start=literal(0),
    )
    exact_priority = case((score <= 2, score), else_=3)
    generality_score = (
        case(
            (
                and_(
                    score > 2,
                    or_(
                        KcdCode.name_ko.ilike("%합병증을 동반하지 않은%"),
                        KcdCode.name_en.ilike("%without complications%"),
                    ),
                ),
                0,
            ),
            (
                and_(
                    score > 2,
                    or_(
                        KcdCode.name_ko.ilike("상세불명의 %"),
                        KcdCode.name_ko.ilike("기타 및 상세불명의 %"),
                        KcdCode.name_en.ilike("unspecified %"),
                        KcdCode.name_en.ilike("other and unspecified %"),
                        KcdCode.name_en.ilike("%, unspecified"),
                    ),
                ),
                1,
            ),
            else_=2,
        )
        if len(token_groups) == 1
        else literal(2)
    )
    where_clause = or_(*conditions)
    total = db.scalar(select(func.count()).select_from(KcdCode).where(where_clause)) or 0
    rows = db.scalars(
        select(KcdCode)
        .where(where_clause)
        .order_by(
            exact_priority,
            generality_score,
            score,
            desc(token_score),
            func.length(KcdCode.name_ko),
            KcdCode.code,
        )
        .limit(limit)
    ).all()
    return KcdSearchResponse(
        items=[
            KcdCodeItem(code=display_code(row.code), name=row.name_ko, name_en=row.name_en)
            for row in rows
        ],
        total=total,
        query=query,
        limit=limit,
    )
