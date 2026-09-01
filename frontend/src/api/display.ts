/**
 * API 값 → 화면 표기 변환.
 *
 * 백엔드는 원본 코드값(M/F, WALK IN …)을 그대로 주고, 한글 표기는 프론트가 맡는다.
 * UI 디자인은 바꾸지 않는다 — 기존 mock 이 쓰던 표기와 같은 형태로 맞춘다.
 */

import type { DischargeType, RiskBandApi } from "@/api/types";
import type { BedStatus } from "@/lib/mock-data";

const DISCHARGE: Record<DischargeType, string> = {
  icu: "ICU",
  admitted: "입원",
  home: "귀가",
  expired: "사망",
};

/** 아직 퇴실 전이면 빈 문자열 — 임의 값을 채우지 않는다. */
export function dischargeLabel(value: DischargeType | null): string {
  if (!value) return "";
  return DISCHARGE[value] ?? "";
}

export function sexLabel(sex: "M" | "F" | null): "남" | "여" | "-" {
  if (sex === "M") return "남";
  if (sex === "F") return "여";
  return "-";
}

const TRANSPORT: Record<string, string> = {
  "WALK IN": "도보",
  AMBULANCE: "119 구급차",
  HELICOPTER: "헬기",
  OTHER: "기타",
  UNKNOWN: "미상",
};

export function transportLabel(value: string | null): string {
  if (!value) return "미상";
  return TRANSPORT[value] ?? value;
}

const ROUTE: Record<string, string> = {
  "EMERGENCY ROOM": "직접 내원",
  "TRANSFER FROM HOSPITAL": "타병원 전원",
  "TRANSFER FROM SKILLED NURSING FACILITY": "요양시설 전원",
  "PHYSICIAN REFERRAL": "의뢰",
  "WALK-IN/SELF REFERRAL": "직접 내원",
  "CLINIC REFERRAL": "외래 의뢰",
  "PROCEDURE SITE": "시술실 경유",
  "INTERNAL TRANSFER TO OR FROM PSYCH": "원내 전동",
  "AMBULATORY SURGERY TRANSFER": "외래수술 전동",
  "INFORMATION NOT AVAILABLE": "미상",
};

export function routeLabel(value: string | null): string {
  if (!value) return "미상";
  return ROUTE[value] ?? value;
}

/**
 * 환자 목록의 '현재 위험도' 3단계 — 모델 risk_bands 그대로다.
 * 경계값은 backend(bundle.json)가 정하고, 프론트는 라벨과 색만 붙인다.
 */
export const bandMeta: Record<
  RiskBandApi,
  { label: string; dot: string; badge: string; text: string }
> = {
  green: {
    label: "저위험",
    dot: "bg-risk-stable",
    badge: "bg-risk-stable-soft text-risk-stable border-risk-stable/30",
    text: "text-risk-stable",
  },
  amber: {
    label: "관찰 필요",
    dot: "bg-risk-watch",
    badge: "bg-risk-watch-soft text-risk-watch border-risk-watch/40",
    text: "text-risk-watch",
  },
  red: {
    label: "재평가 필요",
    dot: "bg-risk-critical",
    badge: "bg-risk-critical-soft text-risk-critical border-risk-critical/40",
    text: "text-risk-critical",
  },
};

/** 예측이 없으면 null — 임의 구간을 만들지 않는다. */
export function toRiskBand(value: RiskBandApi | null): RiskBandApi | null {
  return value;
}

export function toBedStatus(value: string): BedStatus {
  return value as BedStatus;
}

/** 0.0~1.0 → 0~100. 값이 없으면 null. */
export function toPercent(probability: number | null): number | null {
  if (probability === null || probability === undefined) return null;
  return Math.round(probability * 100);
}

export function formatTime(iso: string | null): string {
  if (!iso) return "--:--";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "--:--";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function formatDateTime(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 숫자 표기. null 이면 대시. 0 으로 채우지 않는다. */
export function num(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined) return "-";
  return value.toFixed(digits);
}
