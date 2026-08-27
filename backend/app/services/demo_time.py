"""데모 시간축 (D6).

MIMIC timestamp 는 환자별로 무작위 shift 되어 있어 "지금 응급실에 있는 환자"
라는 개념이 성립하지 않는다. app.demo_stay 에는 원본 시간축에서 '현재' 에
대응하는 시점(now_ref)만 저장하고, 오프셋은 조회할 때 now() - now_ref 로
계산한다(app.v_demo_stay). 임상값은 건드리지 않고 시각만 평행이동하며,
상대 간격은 그대로 보존된다.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def shift(moment: datetime | None, offset: timedelta | None) -> datetime | None:
    if moment is None:
        return None
    if offset is None:
        return moment
    return moment + offset


def age_at(anchor_age: int | None, anchor_year: int | None, intime: datetime | None) -> int | None:
    """MIMIC 은 나이를 anchor_age/anchor_year 로 준다. 내원 시점 나이를 계산한다."""
    if anchor_age is None or anchor_year is None or intime is None:
        return None
    age = anchor_age + (intime.year - anchor_year)
    return age if 0 <= age <= 120 else anchor_age
