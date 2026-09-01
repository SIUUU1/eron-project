import { apiGet, buildQuery } from "@/api/client";

/**
 * 데모 시계.
 *
 * 화면의 모든 시각이 이 시계에서 파생된다. 1시간 단위 악화 예측 시연을 위해
 * 가상 시각을 앞당기면 목록·상세·차트·병상·퇴실 판정이 한꺼번에 따라온다.
 */
export interface DemoClock {
  virtual_now: string;
  real_now: string;
  /** 0=정지, 1=실시간, 3600=1초에 1시간 */
  speed: number;
  offset_seconds: number;
  /** 시나리오 시작점 이후 경과 시간(초) */
  elapsed_seconds: number;
  /** 되감기 가능 여부. 시작점에서는 false */
  can_rewind: boolean;
  is_shifted: boolean;
}

const BASE = import.meta.env["VITE_API_BASE_URL"] ?? "";

async function post(path: string): Promise<DemoClock> {
  const res = await fetch(`${BASE}${path}`, { method: "POST" });
  if (!res.ok) {
    const detail = await res
      .json()
      .then((b: { detail?: unknown }) => (typeof b.detail === "string" ? b.detail : res.statusText))
      .catch(() => res.statusText);
    throw new Error(detail);
  }
  return (await res.json()) as DemoClock;
}

export function getDemoClock(signal?: AbortSignal) {
  return apiGet<DemoClock>("/api/ed/demo/clock", signal);
}

export function advanceDemoClock(hours: number) {
  return post(`/api/ed/demo/advance${buildQuery({ hours })}`);
}

export function setDemoSpeed(value: number) {
  return post(`/api/ed/demo/speed${buildQuery({ value })}`);
}

export function resetDemoClock() {
  return post("/api/ed/demo/reset");
}

export const demoClockKeys = { clock: ["ed", "demo", "clock"] as const };

/**
 * 시계 이동 단위.
 * 15분은 예측 스케줄러의 실행 슬롯(00/15/30/45)과 같은 폭이라,
 * +15분 한 번이 "예측 슬롯 하나 진행" 과 맞아떨어진다.
 */
export const DEMO_STEP_HOURS = { hour: 1, quarter: 0.25 } as const;

/** 경과 시간 표기 — +45m · +1h 15m. 시(hour)로만 반올림하면 15분 시연이 +0h 로 보인다. */
export function elapsedLabel(seconds: number): string {
  const total = Math.max(0, Math.round(seconds / 60));
  const h = Math.floor(total / 60);
  const m = total % 60;
  if (h === 0) return `+${m}m`;
  return m === 0 ? `+${h}h` : `+${h}h ${m}m`;
}

/** 배속 순환: 실시간 → 1초=1분 → 1초=1시간 → 정지 → 실시간 */
export const SPEED_CYCLE = [1, 60, 3600, 0] as const;

export function speedLabel(speed: number): string {
  if (speed === 0) return "정지";
  if (speed === 1) return "실시간";
  if (speed === 60) return "60배";
  if (speed === 3600) return "1초=1시간";
  return `${speed}배`;
}
