from __future__ import annotations

from pydantic import BaseModel, Field


class Meta(BaseModel):
    """응답의 출처를 밝힌다.

    프론트엔드에서 mock 과 live 데이터가 섞이지 않도록 하는 장치다.
    """

    data_source: str = Field("mimic-iv-ed", description="원천 데이터셋")
    is_demo_timeline: bool = Field(
        True,
        description="시각이 데모 시간축으로 평행이동된 값인지 여부 (D6). 임상값은 원본 그대로다.",
    )
    cohort_size: int | None = Field(None, description="적재된 ED stay 코호트 규모")
    model_connected: bool = Field(
        False,
        description="악화 예측 모델 연동 여부. false 면 위험도·확률이 모두 null 이다.",
    )
