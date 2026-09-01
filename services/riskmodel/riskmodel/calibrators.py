"""확률 보정기 — 직렬화 안정성을 위해 별도 모듈로 분리.

⚠ pickle 은 클래스의 **모듈 경로**를 저장한다. 학습 스크립트의 `__main__` 안에 정의하면
   다른 프로세스에서 로드할 때 `AttributeError: Can't get attribute '_Platt' on '__main__'`
   가 발생한다. 배포 아티팩트에 들어가는 클래스는 반드시 안정된 모듈에 둔다.
"""
from __future__ import annotations
import numpy as np
from sklearn.linear_model import LogisticRegression


def _logit(x):
    e = 1e-9
    x = np.clip(np.asarray(x, dtype=np.float64), e, 1 - e)
    return np.log(x / (1 - x))


class PlattCalibrator:
    """Platt scaling — logit 공간 로지스틱 보정.

    isotonic 대신 쓰는 이유: isotonic 은 계단함수라 예측값을 소수의 구간으로 뭉갠다
    (실측 23,968 → 76개). 그 결과 목표 운영점(Recall 0.85)을 정확히 집을 수 없어
    F1@R85 가 0.5154 → 0.4854 로 떨어졌다.
    Platt 은 **엄격히 단조**라 순위와 threshold 해상도를 완전히 보존한다.
    """

    def __init__(self, model: LogisticRegression | None = None):
        self.model = model

    def fit(self, p, y) -> "PlattCalibrator":
        lr = LogisticRegression(max_iter=1000)
        lr.fit(_logit(p).reshape(-1, 1), np.asarray(y))
        self.model = lr
        return self

    def predict(self, p) -> np.ndarray:
        return self.model.predict_proba(_logit(p).reshape(-1, 1))[:, 1]


def fit_platt(p, y) -> PlattCalibrator:
    return PlattCalibrator().fit(p, y)
