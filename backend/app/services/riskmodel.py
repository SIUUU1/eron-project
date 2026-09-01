"""내부 악화 예측 서비스(services/riskmodel)를 호출하는 HTTP 어댑터.

`clinicalnlp.py` · `whisper.py` 와 같은 패턴이다. 여기서는 HTTP 만 담당하고
feature 계산·등급 매핑은 하지 않는다.

🔑 왜 원본 관측을 그대로 보내는가
    feature 100개를 만드는 규칙은 riskmodel 안 OnlineFeatureBuilder 한 곳에만 둔다.
    backend 가 feature 를 만들면 그 규칙이 두 곳에 생기고, 어긋나면 에러 없이
    성능만 떨어진다. backend 는 DB 에서 꺼낸 원본만 넘긴다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx2


class RiskModelUnavailableError(RuntimeError):
    """예측 서비스에 연결할 수 없거나 5xx 를 돌려주었다."""


class InvalidRiskModelResponseError(RuntimeError):
    """예측 서비스가 공개 계약을 어긴 응답을 돌려주었다."""


def _isoformat(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _encode(payload: dict[str, Any]) -> dict[str, Any]:
    """datetime 을 ISO 문자열로 바꾼다. 관측 튜플 안의 시각도 포함된다."""
    return {
        "patient": {k: _isoformat(v) for k, v in payload["patient"].items()},
        "vitals": [[_isoformat(ts), var, val] for ts, var, val in payload["vitals"]],
        "labs": [[_isoformat(ts), var, val] for ts, var, val in payload["labs"]],
        "t_end": _isoformat(payload["t_end"]),
    }


class RiskModelClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        http_client: httpx2.AsyncClient,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client

    async def health(self) -> dict[str, Any]:
        try:
            response = await self._http_client.get(
                f"{self._base_url}/health", timeout=self._timeout_seconds
            )
        except httpx2.HTTPError as exc:
            raise RiskModelUnavailableError from exc
        if response.status_code != 200:
            raise RiskModelUnavailableError
        try:
            return response.json()
        except ValueError as exc:
            raise InvalidRiskModelResponseError from exc

    async def predict(self, payload: dict[str, Any]) -> dict[str, Any]:
        """원본 관측 → 1시간 간격 위험도.

        응답은 `in_scope` 가 false 면 predictions 가 비어 있다(적용 범위 밖).
        그 경우 확률을 지어내지 않고 그대로 돌려준다.
        """
        try:
            response = await self._http_client.post(
                f"{self._base_url}/predict",
                json=_encode(payload),
                timeout=self._timeout_seconds,
            )
        except httpx2.HTTPError as exc:
            raise RiskModelUnavailableError from exc

        if response.status_code == 422:
            raise InvalidRiskModelResponseError
        if response.status_code != 200:
            raise RiskModelUnavailableError

        try:
            result = response.json()
        except ValueError as exc:
            raise InvalidRiskModelResponseError from exc

        if not isinstance(result, dict) or "predictions" not in result:
            raise InvalidRiskModelResponseError
        if not result.get("model_version"):
            # 버전 없이 저장하면 어느 모델이 낸 값인지 되짚을 수 없다.
            raise InvalidRiskModelResponseError
        return result
