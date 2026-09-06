/**
 * 모델 성능 화면이 공통으로 쓰는 표기 규칙과 상수.
 *
 * 🔑 값이 없으면 `N/A` 다. 0 으로 채우지 않는다 — 0.000 은 "성능이 0" 으로 읽힌다.
 */

/** 지표 표기. null 이면 N/A — 숫자를 지어내지 않는다. */
export function metric(value: number | null | undefined, digits = 3): string {
  if (value === null || value === undefined) return "N/A";
  return value.toFixed(digits);
}

/** 비율을 퍼센트로. null 이면 N/A. */
export function percent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return "N/A";
  return `${(value * 100).toFixed(digits)}%`;
}

export type TrendRange = "24h" | "7d" | "30d";

/**
 * 추이 조회 구간.
 *
 * 🔑 `days` 가 없으면 7일과 30일이 서버 기본값(day interval = 30일)으로 똑같이 조회된다.
 *    탭 라벨과 실제 범위가 어긋나므로 기간을 명시한다.
 */
export const TREND_RANGES: Record<
  TrendRange,
  { label: string; interval: "hour" | "day"; days: number }
> = {
  "24h": { label: "최근 24시간", interval: "hour", days: 1 },
  "7d": { label: "최근 7일", interval: "day", days: 7 },
  "30d": { label: "최근 30일", interval: "day", days: 30 },
};

/**
 * 데모 시각을 bucket 경계로 내린다. 서버 `bucket_of()` 와 같은 규칙이다
 * (day = 자정, hour = 정시).
 */
function floorToBucket(moment: Date, interval: "hour" | "day"): Date {
  const floored = new Date(moment);
  floored.setMinutes(0, 0, 0);
  if (interval === "day") floored.setHours(0, 0, 0, 0);
  return floored;
}

/**
 * 타임존 표기 없는 ISO 문자열.
 *
 * ⚠ `toISOString()` 을 쓰면 안 된다. 끝에 `Z` 가 붙어 서버가 aware datetime 으로 파싱하고,
 *   naive 인 demo_prediction_time 과 비교하다 TypeError 로 죽는다.
 *   이 프로젝트의 데모 시각은 전부 naive 이며 화면도 로컬 시각으로 읽는다(api/display.ts).
 */
function toNaiveIso(value: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}` +
    `T${pad(value.getHours())}:${pad(value.getMinutes())}:${pad(value.getSeconds())}`
  );
}

/**
 * 데모 시각 기준 추이 조회 범위.
 *
 * 🔑 `demo_now` 를 그대로 쓰면 30초마다 값이 바뀌어 조회 범위가 계속 흔들린다.
 *    bucket 경계로 내린 뒤 한 칸 뒤를 끝으로 잡아, 같은 시간(또는 같은 날) 안에서는
 *    범위가 고정되게 한다. 현재 진행 중인 bucket 도 포함된다.
 *
 * demo_now 를 아직 못 받았으면 빈 객체를 돌려주고 서버 기본값에 맡긴다.
 */
export function trendWindow(
  demoNow: string | null | undefined,
  range: TrendRange,
): { start?: string; end?: string } {
  if (!demoNow) return {};
  const now = new Date(demoNow);
  if (Number.isNaN(now.getTime())) return {};

  const { interval, days } = TREND_RANGES[range];
  const anchor = floorToBucket(now, interval);

  const end = new Date(anchor);
  if (interval === "day") end.setDate(end.getDate() + 1);
  else end.setHours(end.getHours() + 1);

  const start = new Date(end);
  start.setDate(start.getDate() - days);

  return { start: toNaiveIso(start), end: toNaiveIso(end) };
}
