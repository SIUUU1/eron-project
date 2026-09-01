"""ER:ON RiskService + Explainability v3.

같은 serving/ 디렉터리에 둔다.
- reason_engine_v3.py
- predict_service_with_reason_v3.py

v3.1 DROP-IN FINAL 핵심
-------------
- LightGBM pred_contrib를 model feature 단위로 유지한다.
- 직전 대비 위험 상승 설명은 exact feature Δcontribution으로 생성한다.
- 화면 문구에 쓰는 이전/현재 값도 그 exact feature에서 직접 가져온다.
- clinical-direction gate를 통과한 '임상적 worsening' 변화만 사용자 악화 신호로 노출한다.
- improving/neutral/unknown 변화는 악화 신호에서 제외한다.
- risk가 상승했는데 gate를 통과한 worsening feature가 없으면 현재 위험 신호로 대체하지 않고 빈 악화 사유를 반환한다.
- calibrated risk delta(%p)와 LightGBM raw-score SHAP delta를 명시적으로 구분한다.

주의
----
설명은 인과적 '악화 원인'이 아니라 모델 예측에 기여한 주요 신호다.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import joblib
import lightgbm as lgb
import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import calibrators  # noqa: F401,E402
from reason_engine_v3 import (  # noqa: E402
    CLINICAL_GATE_VERSION,
    build_change_reason,
    build_reason,
    top_delta_contributions,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts"
_WARNED_MISSING: set[tuple[str, ...]] = set()

SERVICE_EXPLAINABILITY_VERSION = "v3.1_dropin_clinical_gate_20260901"

BAND_UI = {
    "red": dict(color="#D7263D", icon="🔴", ko="매우 악화", en="Critical"),
    "amber": dict(color="#F0A202", icon="🟡", ko="악화", en="Deteriorating"),
    "green": dict(color="#2E933C", icon="🟢", ko="저위험", en="Low risk"),
}


class RiskService:
    def __init__(self, art_dir: Path | str = ART, calibrate: bool = True):
        self.dir = Path(art_dir)
        self.b = json.loads((self.dir / "bundle.json").read_text())

        self.models = joblib.load(self.dir / self.b["model"]["pkl"])
        if not isinstance(self.models, list):
            self.models = [self.models]

        self.features = list(self.b["features"])
        self.bands = self.b["risk_bands"]
        self.window_h = self.b["grid"]["label_window_h"]

        cp = self.dir / self.b["calibrator"]["pkl"]
        self.cal = joblib.load(cp) if calibrate and cp.exists() else None
        self.set_operating_point(self.b["default_operating_point"])

    # ------------------------------------------------------------------
    # Operating point / health
    # ------------------------------------------------------------------
    def set_operating_point(self, name: str) -> float:
        ops = self.b["operating_points"]
        if name not in ops:
            raise KeyError(f"운영점 {name} 없음. 가능: {list(ops)}")

        self.op_name = name
        self.threshold = float(ops[name]["threshold"])
        return self.threshold

    def health(self) -> dict:
        need = [
            self.b["model"]["pkl"],
            self.b["calibrator"]["pkl"],
            "bundle.json",
            "feature_spec.json",
        ]
        missing = [n for n in need if not (self.dir / n).exists()]

        probe = np.full(
            (1, len(self.features)),
            np.nan,
            dtype=np.float32,
        )

        try:
            p = float(
                np.mean(
                    [m.predict_proba(probe)[:, 1][0] for m in self.models]
                )
            )
            ok = 0 <= p <= 1
        except Exception as e:
            return {"status": "error", "detail": str(e)}

        return {
            "status": "ok" if ok and not missing else "degraded",
            "missing_files": missing,
            "n_features": len(self.features),
            "model_version": self.b["version"],
            "operating_point": self.op_name,
            "threshold": self.threshold,
            "calibrated": self.cal is not None,
            "explainability": "exact_feature_delta_contribution_clinical_gate_v3",
            "clinical_gate_version": CLINICAL_GATE_VERSION,
            "service_explainability_version": SERVICE_EXPLAINABILITY_VERSION,
            "contribution_space": "lightgbm_raw_score_shap",
        }

    # ------------------------------------------------------------------
    # Feature matrix / prediction
    # ------------------------------------------------------------------
    def _matrix(self, df: pl.DataFrame) -> np.ndarray:
        missing = [c for c in self.features if c not in df.columns]

        if missing:
            df = df.with_columns(
                [
                    pl.lit(None, dtype=pl.Float64).alias(c)
                    for c in missing
                ]
            )

            key = tuple(missing)
            if key not in _WARNED_MISSING:
                _WARNED_MISSING.add(key)
                logger.warning(
                    "Features filled with NULL (%d/%d): %s",
                    len(missing),
                    len(self.features),
                    missing,
                )

        return (
            df.select(self.features)
            .to_numpy()
            .astype(np.float32)
        )

    def _raw_proba(self, df: pl.DataFrame) -> np.ndarray:
        X = self._matrix(df)
        p = np.mean(
            [m.predict_proba(X)[:, 1] for m in self.models],
            axis=0,
        )
        return np.asarray(p, dtype=float)

    def _proba(self, df: pl.DataFrame) -> np.ndarray:
        p = self._raw_proba(df)
        if self.cal is not None:
            p = self.cal.predict(p)
        return np.asarray(p, dtype=float)

    # ------------------------------------------------------------------
    # LightGBM feature contribution
    # ------------------------------------------------------------------
    def _single_model_contrib(
        self,
        model,
        X: np.ndarray,
    ) -> np.ndarray:
        """한 LightGBM 모델의 feature SHAP contribution.

        LightGBM pred_contrib의 마지막 열은 expected value(bias)이므로 제외한다.
        """
        if hasattr(model, "booster_"):
            c = model.booster_.predict(
                X,
                pred_contrib=True,
            )
        elif isinstance(model, lgb.Booster):
            c = model.predict(
                X,
                pred_contrib=True,
            )
        else:
            raise TypeError(
                "LightGBM model expected for explainability. "
                f"Got {type(model)}"
            )

        c = np.asarray(c, dtype=float)

        if c.ndim != 2:
            raise ValueError(
                f"Unexpected pred_contrib shape: {c.shape}"
            )

        if c.shape[1] == len(self.features) + 1:
            c = c[:, :-1]
        elif c.shape[1] != len(self.features):
            raise ValueError(
                "pred_contrib feature count mismatch: "
                f"{c.shape[1]} vs {len(self.features)}"
            )

        return c

    def _feature_contributions(self, df: pl.DataFrame) -> np.ndarray:
        """여러 seed/model의 LightGBM SHAP contribution을 feature별 평균한다.

        주의: 서비스 최종 risk는 probability ensemble + calibration 결과이며,
        아래 contribution은 LightGBM raw-score SHAP 공간이다.
        두 값의 숫자 단위를 직접 비교하면 안 된다.
        """
        X = self._matrix(df)
        all_c = [
            self._single_model_contrib(m, X)
            for m in self.models
        ]
        return np.mean(
            np.stack(all_c, axis=0),
            axis=0,
        )

    # ------------------------------------------------------------------
    # Band / payload
    # ------------------------------------------------------------------
    def _band(self, p: np.ndarray) -> np.ndarray:
        th = self.bands["thresholds"]
        return np.where(
            p >= th["red"],
            "red",
            np.where(
                p >= th["amber"],
                "amber",
                "green",
            ),
        )

    def _band_meta(self, key: str) -> dict:
        for b in self.bands["bands"]:
            if b["key"] == key:
                return b
        return {}

    @staticmethod
    def _natural_frequency(prob: float) -> str:
        if prob >= 0.5:
            return (
                f"비슷한 환자 100명 중 약 "
                f"{round(prob * 100)}명이 악화"
            )
        if prob <= 0:
            return "—"
        return (
            f"비슷한 환자 약 "
            f"{max(round(1 / prob), 2)}명 중 1명이 악화"
        )

    def _payload(self, key: str, prob: float) -> dict:
        ui = BAND_UI[key]
        meta = self._band_meta(key)
        return {
            "risk_pct": round(prob * 100, 1),
            "window_h": self.window_h,
            "band": key,
            "label": meta.get("label"),
            "color": ui["color"],
            "icon": ui["icon"],
            "headline": (
                f"{self.window_h}시간 내 악화 확률 "
                f"{prob * 100:.1f}%"
            ),
            "sub": self._natural_frequency(prob),
            "alarm": bool(prob >= self.threshold),
        }

    # ------------------------------------------------------------------
    # Basic score API
    # ------------------------------------------------------------------
    def score_rows(self, df: pl.DataFrame) -> pl.DataFrame:
        p = self._proba(df)
        keep = [
            c
            for c in (
                "stay_id",
                "ed_stay_id",
                "subject_id",
                "t_idx",
                "t",
                "prediction_time",
            )
            if c in df.columns
        ]

        return df.select(keep).with_columns(
            pl.Series("risk", p),
            pl.Series("risk_pct", np.round(p * 100, 1)),
            pl.Series("band", self._band(p)),
            pl.Series("alarm", p >= self.threshold),
        )

    # ------------------------------------------------------------------
    # Current risk + exact current feature reason
    # ------------------------------------------------------------------
    def score_rows_with_reason(
        self,
        df: pl.DataFrame,
        max_reasons: int = 2,
    ) -> list[dict]:
        """현재 위험도와 exact-feature current contribution을 반환한다."""
        p = self._proba(df)
        c = self._feature_contributions(df)
        rows = df.to_dicts()

        out: list[dict] = []

        for i, row in enumerate(rows):
            prob = float(p[i])
            reasons = build_reason(
                self.features,
                c[i],
                row,
                max_reasons,
            )

            stay = row.get("stay_id", row.get("ed_stay_id"))
            t_value = row.get("t", row.get("prediction_time"))

            out.append(
                {
                    "stay_id": stay,
                    "subject_id": row.get("subject_id"),
                    "t_idx": row.get("t_idx"),
                    "t": str(t_value) if t_value is not None else None,
                    "risk": prob,
                    "risk_pct": round(prob * 100, 1),
                    "band": str(
                        self._band(
                            np.array([prob], dtype=float)
                        )[0]
                    ),
                    "alarm": bool(prob >= self.threshold),
                    "reason": " · ".join(
                        x["text"] for x in reasons
                    ),
                    "reason_title": "현재 예측에 기여한 주요 신호",
                    "reason_detail": reasons,
                    # Backend/UI compatibility: risk_factors is the exact list rendered by UI.
                    # For a single row there is no previous time point, so these are current-risk signals.
                    "risk_factors": [x["text"] for x in reasons],
                    "clinical_worsening_confirmed": None,
                    "service_explainability_version": SERVICE_EXPLAINABILITY_VERSION,
                    "reason_type": "current_risk_signal",
                    "reason_basis": "exact_feature_current_contribution",
                    "reason_notice": (
                        "모델 예측에 기여한 주요 신호이며 "
                        "임상적 인과관계를 의미하지 않습니다."
                    ),
                }
            )

        return out

    # ------------------------------------------------------------------
    # Time series + exact delta-contribution reason
    # ------------------------------------------------------------------
    def score_series_with_reason(
        self,
        df: pl.DataFrame,
        max_reasons: int = 2,
        include_delta_debug: bool = False,
        delta_debug_top_n: int = 10,
    ) -> list[dict]:
        """stay별 시간순 위험도 변화와 임상 방향 gate가 적용된 신호를 반환한다.

        위험도가 상승했을 때 사용자 화면의 change reason은 반드시 아래 조건을 모두 만족한다.
        1) 동일 exact model feature의 SHAP contribution이 증가했다.
        2) 그 exact feature의 실제 값도 직전 시점과 달라졌다.
        3) 정적/latent/workflow proxy feature가 아니다.
        4) 사전 정의된 clinical-direction rule에서 worsening으로 판정됐다.

        중요: risk가 상승했더라도 4)를 만족하는 feature가 없으면 악화 사유를 비워 둔다.
        current-risk feature로 fallback하지 않는다.
        """
        p = self._proba(df)
        c = self._feature_contributions(df)
        rows = df.to_dicts()

        recs = []
        for i, row in enumerate(rows):
            stay = row.get("stay_id", row.get("ed_stay_id"))
            t_value = row.get("t", row.get("prediction_time"))
            recs.append(
                {
                    "row": row,
                    "stay": stay,
                    "t_value": t_value,
                    "risk": float(p[i]),
                    "contrib": c[i],
                }
            )

        def _sort_key(x):
            row = x["row"]
            stay = x["stay"]
            t_idx = row.get("t_idx")
            t_value = x["t_value"]
            return (
                "" if stay is None else str(stay),
                -1 if t_idx is None else int(t_idx),
                "" if t_value is None else str(t_value),
            )

        recs.sort(key=_sort_key)

        previous: dict[Any, dict[str, Any]] = {}
        out: list[dict] = []

        for rec in recs:
            row = rec["row"]
            stay = rec["stay"]
            risk = rec["risk"]
            cur_c = rec["contrib"]

            current = build_reason(
                self.features,
                cur_c,
                row,
                max_reasons,
            )

            prev = previous.get(stay)
            delta_debug = None

            if prev is None:
                delta = None
                change = []
                primary = current
                reason_type = "current_risk_signal"
                reason_title = "현재 예측에 기여한 주요 신호"
                reason_basis = "exact_feature_current_contribution"

            else:
                delta = risk - prev["risk"]

                if delta > 0:
                    change = build_change_reason(
                        feature_names=self.features,
                        previous_contributions=prev["contrib"],
                        current_contributions=cur_c,
                        previous_row=prev["row"],
                        current_row=row,
                        max_reasons=max_reasons,
                    )
                else:
                    change = []

                if include_delta_debug:
                    delta_debug = top_delta_contributions(
                        feature_names=self.features,
                        previous_contributions=prev["contrib"],
                        current_contributions=cur_c,
                        previous_row=prev["row"],
                        current_row=row,
                        top_n=delta_debug_top_n,
                    )

                if delta > 0:
                    if change:
                        primary = change
                        reason_type = "risk_increase_clinical_worsening_signal"
                        reason_title = "직전 예측 대비 임상적 악화 신호"
                        reason_basis = "exact_feature_delta_contribution+clinical_direction_gate"
                    else:
                        # 핵심 안전장치:
                        # 위험도가 상승했더라도 clinical gate를 통과한 worsening 변화가 없으면
                        # current-risk factor를 악화 근거처럼 대신 보여주지 않는다.
                        primary = []
                        reason_type = "risk_increase_without_confirmed_clinical_worsening_signal"
                        reason_title = "직전 대비 확인된 임상적 악화 신호 없음"
                        reason_basis = "clinical_direction_gate_no_pass"
                else:
                    primary = current
                    reason_type = "current_risk_signal"
                    reason_title = "현재 예측에 기여한 주요 신호"
                    reason_basis = "exact_feature_current_contribution"

            # UI/DB detail에 저장할 단일 source of truth.
            # 위험도가 상승한 시점에는 clinical-direction gate를 통과한 worsening 변화만
            # risk_factors로 노출한다. 개선/중립/unknown은 절대 risk_factors에 들어가지 않는다.
            clinical_worsening_confirmed = bool(delta is not None and delta > 0 and change)
            safe_risk_factors = [x["text"] for x in primary]

            payload = {
                "stay_id": stay,
                "subject_id": row.get("subject_id"),
                "t_idx": row.get("t_idx"),
                "t": (
                    str(rec["t_value"])
                    if rec["t_value"] is not None
                    else None
                ),
                "risk": risk,
                "risk_pct": round(risk * 100, 1),
                "risk_delta": delta,
                "risk_delta_pct_point": (
                    None
                    if delta is None
                    else round(delta * 100, 1)
                ),
                "band": str(
                    self._band(
                        np.array([risk], dtype=float)
                    )[0]
                ),
                "alarm": bool(risk >= self.threshold),
                "reason": " · ".join(
                    x["text"] for x in primary
                ),
                "reason_title": reason_title,
                "reason_type": reason_type,
                "reason_basis": reason_basis,
                "reason_detail": primary,
                # IMPORTANT: frontend/backend compatibility field.
                # backend latest_prediction()가 detail.risk_factors를 그대로 화면에 전달하므로
                # 반드시 clinical gate 결과만 넣는다.
                "risk_factors": safe_risk_factors,
                "clinical_worsening_confirmed": clinical_worsening_confirmed,
                "service_explainability_version": SERVICE_EXPLAINABILITY_VERSION,
                "current_reason_detail": current,
                "change_reason_detail": change,
                "reason_notice": (
                    "위험도 변화는 calibrated probability 기준이며, "
                    "Δcontribution은 LightGBM raw-score SHAP 기준입니다. "
                    "직전 대비 악화 신호에는 exact feature 값이 실제로 변했고 "
                    "사전 정의된 clinical-direction gate에서 worsening으로 판정된 항목만 표시합니다. "
                    "개선/중립/방향 미정 변화는 악화 근거에서 제외하며, 임상적 인과관계를 의미하지 않습니다."
                ),
                "clinical_gate_version": CLINICAL_GATE_VERSION,
            }

            if include_delta_debug:
                payload["delta_contribution_debug"] = delta_debug

            out.append(payload)

            previous[stay] = {
                "risk": risk,
                "contrib": cur_c,
                "row": row,
            }

        return out

    # ------------------------------------------------------------------
    # Patient aggregation
    # ------------------------------------------------------------------
    def score_patients(self, df: pl.DataFrame) -> list[dict[str, Any]]:
        r = self.score_rows(df)

        stay_col = "stay_id" if "stay_id" in r.columns else "ed_stay_id"

        agg = [
            pl.col("risk").max().alias("risk_max"),
            pl.col("alarm").any().alias("alarm"),
            pl.col("risk").len().alias("n_pred"),
        ]

        if "t_idx" in r.columns:
            agg.append(
                pl.col("t_idx")
                .filter(pl.col("alarm"))
                .min()
                .alias("first_alarm_t_idx")
            )

        if "t" in r.columns:
            agg.append(
                pl.col("t")
                .filter(pl.col("alarm"))
                .min()
                .alias("first_alarm_t")
            )
        elif "prediction_time" in r.columns:
            agg.append(
                pl.col("prediction_time")
                .filter(pl.col("alarm"))
                .min()
                .alias("first_alarm_t")
            )

        st = (
            r.group_by(stay_col)
            .agg(agg)
            .sort("risk_max", descending=True)
        )

        out = []
        for row in st.iter_rows(named=True):
            key = str(
                self._band(
                    np.array([row["risk_max"]], dtype=float)
                )[0]
            )

            payload = self._payload(
                key,
                row["risk_max"],
            )

            payload.update(
                stay_id=row[stay_col],
                n_predictions=row["n_pred"],
                first_alarm_t_idx=row.get("first_alarm_t_idx"),
                first_alarm_t=(
                    str(row.get("first_alarm_t"))
                    if row.get("first_alarm_t") is not None
                    else None
                ),
            )
            out.append(payload)

        return out

    # ------------------------------------------------------------------
    # Analysis / metadata
    # ------------------------------------------------------------------
    def predict_proba(self, df: pl.DataFrame) -> np.ndarray:
        return self._proba(df)

    @property
    def features_list(self) -> list[str]:
        return self.features

    def band_info(self, key: str) -> dict:
        return self._band_meta(key)

    def info(self) -> dict:
        b = self.b
        return {
            "version": b["version"],
            "task": b["task"],
            "target": b["target"],
            "feature_set": b.get("feature_set"),
            "n_features": b["n_features"],
            "eval_unit": b["eval_unit"],
            "operating_point": self.op_name,
            "threshold": self.threshold,
            "window_h": self.window_h,
            "n_models": len(self.models),
            "calibrator": b["calibrator"]["kind"],
            "created": b["created"],
            "explainability": "exact_feature_delta_contribution_clinical_gate_v3",
            "clinical_gate_version": CLINICAL_GATE_VERSION,
            "service_explainability_version": SERVICE_EXPLAINABILITY_VERSION,
            "contribution_space": "lightgbm_raw_score_shap",
        }

    def validate_input(self, df: pl.DataFrame) -> dict:
        spec = json.loads(
            (self.dir / "feature_spec.json").read_text()
        )
        X = self._matrix(df)

        drift = []
        for i, c in enumerate(self.features):
            s = spec.get(c)
            if not s or s.get("p50") is None:
                continue

            v = X[:, i]
            ok = ~np.isnan(v)
            if ok.sum() < 30:
                continue

            med = float(np.median(v[ok]))
            if med < s["p01"] or med > s["p99"]:
                drift.append(
                    {
                        "feature": c,
                        "input_median": med,
                        "train_p01": s["p01"],
                        "train_p99": s["p99"],
                    }
                )

        return {
            "n_rows": len(X),
            "missing_features": [
                c for c in self.features
                if c not in df.columns
            ],
            "n_drift": len(drift),
            "distribution_warnings": drift[:20],
        }
