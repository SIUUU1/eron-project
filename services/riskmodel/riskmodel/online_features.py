"""온라인 feature 빌더 — 원본 EHR 입력에서 실시간으로 운영 feature 를 만든다.

⚠ feature 목록·개수는 `artifacts/bundle.json` 에서 읽는다. 코드에 하드코딩하지 않는다.

⚠ `src/data/build_features.py`(배치)와 **동일한 규칙**을 재현해야 한다.
   특히 시간 규칙을 어기면 미래 정보 누출이 된다:
     · vital : charttime <= t
     · lab   : storetime(결과 보고 시각) <= t   ← charttime(채혈) 아님
     · triage/chiefcomplaint : ED 도착 시점 확정이라 항상 사용 가능

입력 스키마
    patient : dict  — stay_id, age, gender, arrival_transport, ed_intime,
                      admittime, chiefcomplaint, triage_* (7 vital + pain + acuity)
    vitals  : [(charttime, var, value)]   var ∈ heart_rate/resp_rate/spo2/sbp/dbp/mbp/temperature/gcs_*
    labs    : [(available_time, var, value)]  var ∈ bundle.lab_itemids 의 키
    t       : 예측 시각 (datetime)

사용
    fb = OnlineFeatureBuilder()
    row = fb.build(patient, vitals, labs, t)            # 한 시점
    df  = fb.build_series(patient, vitals, labs, t0, n) # 1시간 간격 n 시점
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence
import numpy as np
import polars as pl
import joblib

sys.path.insert(0, str(Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts"

VITAL_VARS = ["heart_rate", "resp_rate", "spo2", "sbp", "dbp", "mbp",
              "temperature", "gcs_eye", "gcs_verbal", "gcs_motor"]
WINDOW_H = 6           # lagging window (build_features.py FC.derived.window_h)
LAB_WINDOW_H = 24

# 절대 임계값 (features.yaml abnormal_vital.criteria)
ABN = {"sbp": (90, None), "spo2": (90, None), "resp_rate": (8, 30),
       "heart_rate": (40, 130), "temperature": (35.5, None)}
# 생리학적 타당 범위 (features.yaml vitals.valid_range)
VALID = {"heart_rate": (10, 300), "resp_rate": (1, 90), "spo2": (30, 100),
         "sbp": (30, 300), "dbp": (10, 220), "mbp": (15, 250),
         "temperature": (25, 45), "gcs_eye": (1, 4), "gcs_verbal": (1, 5),
         "gcs_motor": (1, 6)}


class OnlineFeatureBuilder:
    def __init__(self, art_dir: Path | str = ART):
        self.dir = Path(art_dir)
        self.b = json.loads((self.dir / "bundle.json").read_text())
        self.features: list[str] = self.b["features"]
        tt = joblib.load(self.dir / "text_transformer.pkl")
        self.vec, self.svd = tt["vectorizer"], tt["svd"]
        self.n_svd = tt["n_svd"]

    # ---------- 내부 ----------
    @staticmethod
    def _clip(var: str, v: float) -> float | None:
        lo, hi = VALID.get(var, (-np.inf, np.inf))
        return None if v is None or not (lo <= v <= hi) else float(v)

    @staticmethod
    def _abn_count(vals: dict) -> tuple[int, int]:
        """(이상 개수, 판정 불가 개수)"""
        n = unk = 0
        for var, (lo, hi) in ABN.items():
            v = vals.get(var)
            if v is None:
                unk += 1
                continue
            if (lo is not None and v < lo) or (hi is not None and v >= hi):
                n += 1
        return n, unk

    def _vital_block(self, hist: dict[str, list[tuple[datetime, float]]], t: datetime) -> dict:
        """t 이전 관측만으로 last/min/max/mean/slope/mask/dt/n 산출."""
        out: dict[str, float | None] = {}
        last_vals: dict[str, float] = {}
        for var in VITAL_VARS:
            obs = [(ts, v) for ts, v in hist.get(var, []) if ts <= t]   # 🔑 미래 차단
            if not obs:
                for suf in ("last", "min", "max", "mean", "slope", "dt"):
                    out[f"{var}_{suf}"] = None
                out[f"{var}_mask"] = 0
                out[f"{var}_n"] = 0
                continue
            obs.sort(key=lambda x: x[0])
            ts_last, v_last = obs[-1]
            out[f"{var}_last"] = v_last
            out[f"{var}_dt"] = (t - ts_last).total_seconds() / 3600
            out[f"{var}_mask"] = 1
            last_vals[var] = v_last
            win = [(ts, v) for ts, v in obs if ts > t - timedelta(hours=WINDOW_H)]
            out[f"{var}_n"] = len(win)
            if win:
                vs = np.array([v for _, v in win], dtype=float)
                out[f"{var}_min"] = float(vs.min())
                out[f"{var}_max"] = float(vs.max())
                out[f"{var}_mean"] = float(vs.mean())
                if len(win) > 1:
                    dth = np.array([(t - ts).total_seconds() / 3600 for ts, _ in win])
                    # ⚠ np.cov 는 ddof=1(표본), ndarray.var() 는 ddof=0(모집단)이 기본이다.
                    #   섞으면 slope 가 n/(n-1) 배 부풀려진다. polars 배치와 동일하게 ddof=1 로 맞춘다.
                    var_d = float(dth.var(ddof=1))
                    out[f"{var}_slope"] = float(-np.cov(vs, dth)[0, 1] / var_d) if var_d > 0 else None
                else:
                    out[f"{var}_slope"] = None
            else:
                for suf in ("min", "max", "mean", "slope"):
                    out[f"{var}_{suf}"] = None
        return out, last_vals

    def _lab_block(self, hist: dict[str, list[tuple[datetime, float]]], t: datetime) -> dict:
        out: dict[str, float | None] = {}
        needed = {c.rsplit("_", 1)[0][4:] for c in self.features if c.startswith("lab_")}
        for var in needed:
            obs = [(ts, v) for ts, v in hist.get(var, []) if ts <= t]   # 🔑 storetime <= t
            if not obs:
                out[f"lab_{var}_last"] = None
                out[f"lab_{var}_dt"] = None
                out[f"lab_{var}_mask"] = 0
                continue
            obs.sort(key=lambda x: x[0])
            ts_last, v_last = obs[-1]
            out[f"lab_{var}_last"] = float(v_last)
            out[f"lab_{var}_dt"] = (t - ts_last).total_seconds() / 3600
            out[f"lab_{var}_mask"] = 1
        return out

    def _text_block(self, cc: str) -> dict:
        z = self.svd.transform(self.vec.transform([(cc or "").lower()]))[0]
        return {f"cc_svd_{i:03d}": float(z[i]) for i in range(self.n_svd)}

    @staticmethod
    def _to_hist(rows: Iterable[Sequence]) -> dict[str, list[tuple[datetime, float]]]:
        h: dict[str, list[tuple[datetime, float]]] = {}
        for ts, var, val in rows:
            if val is None:
                continue
            h.setdefault(var, []).append((ts, float(val)))
        return h

    # ---------- 공개 ----------
    def build(self, patient: dict, vitals: Iterable[Sequence],
              labs: Iterable[Sequence], t: datetime,
              prev_abn: list[int] | None = None) -> dict:
        """한 시점의 feature dict. prev_abn 은 delta/persistence 계산용 과거 count 열."""
        vh = self._to_hist((ts, v, self._clip(v, x)) for ts, v, x in vitals)
        lh = self._to_hist(labs)
        row: dict = {}
        vb, last_vals = self._vital_block(vh, t)
        row.update(vb)
        row.update(self._lab_block(lh, t))
        row.update(self._text_block(patient.get("chiefcomplaint", "")))

        # triage (도착 시점 확정 → 항상 사용 가능)
        for k in ("temperature", "heartrate", "resprate", "o2sat", "sbp", "dbp", "pain", "acuity"):
            row[f"triage_{k}"] = patient.get(f"triage_{k}")
        if row.get("triage_heartrate") and row.get("triage_sbp"):
            row["triage_shock_index"] = row["triage_heartrate"] / row["triage_sbp"]

        # 파생
        row["age"] = patient.get("age")
        row["hours_from_ed"] = (t - patient["ed_intime"]).total_seconds() / 3600
        row["hours_from_admit"] = ((t - patient["admittime"]).total_seconds() / 3600
                                   if patient.get("admittime") else None)
        hr, sbp = last_vals.get("heart_rate"), last_vals.get("sbp")
        dbp, mbp = last_vals.get("dbp"), last_vals.get("mbp")
        row["shock_index"] = hr / sbp if hr and sbp else None
        row["map_est"] = mbp if mbp else ((sbp + 2 * dbp) / 3 if sbp and dbp else None)
        row["mod_shock_index"] = hr / row["map_est"] if hr and row.get("map_est") else None
        row["pulse_pressure"] = sbp - dbp if sbp and dbp else None

        n_abn, n_unk = self._abn_count(last_vals)
        row["abnormal_vital_count"] = n_abn
        row["abnormal_vital_n_unknown"] = n_unk
        for var in ABN:
            v = last_vals.get(var)
            lo, hi = ABN[var]
            row[f"abn_{var}"] = (None if v is None else
                                 int((lo is not None and v < lo) or (hi is not None and v >= hi)))
        seq = (prev_abn or []) + [n_abn]
        row["abnormal_vital_delta"] = (n_abn - seq[-1 - WINDOW_H]) if len(seq) > WINDOW_H else None
        row["abnormal_vital_slope"] = (row["abnormal_vital_delta"] / WINDOW_H
                                       if row["abnormal_vital_delta"] is not None else None)
        run = 0
        for x in reversed(seq):
            if x >= 1:
                run += 1
            else:
                break
        row["abnormal_vital_persistence"] = min(run, 12)
        return {k: row.get(k) for k in self.features}

    def build_series(self, patient: dict, vitals, labs,
                     t0: datetime, n_steps: int, step_h: int = 1) -> pl.DataFrame:
        """1시간 간격 n 시점 — 실제 운영 형태(매시간 재예측)."""
        vitals, labs = list(vitals), list(labs)
        rows, abn_hist = [], []
        for k in range(n_steps):
            t = t0 + timedelta(hours=k * step_h)
            r = self.build(patient, vitals, labs, t, prev_abn=abn_hist)
            abn_hist.append(r.get("abnormal_vital_count") or 0)
            r = dict(r); r["stay_id"] = patient["stay_id"]; r["t_idx"] = k; r["t"] = t
            rows.append(r)
        return pl.DataFrame(rows, infer_schema_length=None)
