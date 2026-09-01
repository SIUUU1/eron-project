import re
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, desc, func, literal, or_, select
from sqlalchemy.orm import Session

from app.api.clinical_records import get_db
from app.core.config import settings
from app.models.kcd_code import KcdCode
from app.schemas.kcd import KcdCodeItem, KcdSearchResponse


router = APIRouter(prefix="/api/kcd", tags=["kcd"])


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


@router.get("/search", response_model=KcdSearchResponse)
def search_kcd_codes(
    q: str = Query(min_length=1, max_length=100),
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    query = q.strip()
    normalized_code = re.sub(r"[^A-Za-z0-9]", "", query).upper()
    aliases = lookup_alias_terms(query)
    terms = []
    for raw_term in re.split(r"\s+", query):
        term = re.sub(r"(의심|추정|증)$", "", raw_term)
        if len(term) >= 2 and term not in terms:
            terms.append(term)
    terms = terms[:5]

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
    conditions.extend(KcdCode.name_ko.ilike(f"%{term}%") for term in terms)

    score = case(
        (func.upper(KcdCode.code) == normalized_code, 0),
        (func.upper(KcdCode.code).like(f"{normalized_code}%"), 1),
        (KcdCode.name_ko == query, 2),
        (KcdCode.name_ko.ilike(f"{query}%"), 3),
        (KcdCode.name_ko.ilike(f"%{query}%"), 4),
        *(
            (or_(KcdCode.name_ko.ilike(f"%{alias}%"), KcdCode.name_en.ilike(f"%{alias}%")), 4)
            for alias in aliases
        ),
        else_=5,
    )
    token_score = sum(
        (case((KcdCode.name_ko.ilike(f"%{term}%"), 1), else_=0) for term in terms),
        start=literal(0),
    )
    where_clause = or_(*conditions)
    total = db.scalar(select(func.count()).select_from(KcdCode).where(where_clause)) or 0
    rows = db.scalars(
        select(KcdCode)
        .where(where_clause)
        .order_by(desc(token_score), score, func.length(KcdCode.name_ko), KcdCode.code)
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
