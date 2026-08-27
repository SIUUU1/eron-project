from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class DemoClock(BaseModel):
    """데모 시계 상태.

    virtual_now 가 화면 전체의 '현재' 다. speed 가 1 이고 offset_seconds 가 0 이면
    실제 시각과 동일하게 동작한다(평상시).
    """

    virtual_now: datetime = Field(..., description="데모 기준 현재 시각")
    real_now: datetime = Field(..., description="서버 실제 시각")
    speed: float = Field(..., description="0=정지, 1=실시간, 3600=1초에 1시간")
    offset_seconds: int = Field(..., description="virtual_now - real_now (초)")
    elapsed_seconds: int = Field(
        0, description="시나리오 시작점 이후 경과 시간(초). 0 이면 더 되감을 수 없다."
    )
    can_rewind: bool = Field(False, description="되감기 가능 여부")
    is_shifted: bool = Field(..., description="실제 시각과 다르게 흐르고 있는지")
