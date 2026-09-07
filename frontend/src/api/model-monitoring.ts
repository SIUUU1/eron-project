import { apiGet, buildQuery } from "@/api/client";
import type {
  ConfusionMatrixResponse,
  EvaluationStatusResponse,
  ModelMonitoringSummary,
  ThresholdsResponse,
  TrendsResponse,
} from "@/api/types";

/**
 * AI 모델 성능 모니터링.
 *
 * 🔑 시각 파라미터는 **데모 시간축**이다. 화면에 보이는 시각과 같아야 한다.
 * 🔑 서버는 조회 시점에 판정하고 결과를 캐시한다. 별도 배치가 없으므로
 *    호출하면 항상 최신 상태다.
 */

export function getModelSummary(signal?: AbortSignal) {
  return apiGet<ModelMonitoringSummary>("/api/ed/model-monitoring/summary", signal);
}

/** 비우면 서버가 기본 구간을 잡는다(hour = 최근 24시간, day = 최근 30일). */
export function getModelTrends(
  interval: "hour" | "day",
  start?: string,
  end?: string,
  signal?: AbortSignal,
) {
  return apiGet<TrendsResponse>(
    `/api/ed/model-monitoring/trends${buildQuery({ interval, start, end })}`,
    signal,
  );
}

/** threshold 를 주지 않으면 현재 운영값을 쓴다. */
export function getModelConfusionMatrix(threshold?: number, signal?: AbortSignal) {
  return apiGet<ConfusionMatrixResponse>(
    `/api/ed/model-monitoring/confusion-matrix${buildQuery({ threshold })}`,
    signal,
  );
}

export function getModelThresholds(signal?: AbortSignal) {
  return apiGet<ThresholdsResponse>("/api/ed/model-monitoring/thresholds", signal);
}

export function getModelStatus(signal?: AbortSignal) {
  return apiGet<EvaluationStatusResponse>("/api/ed/model-monitoring/status", signal);
}

export const modelMonitoringKeys = {
  root: ["ed", "model-monitoring"] as const,
  summary: ["ed", "model-monitoring", "summary"] as const,
  /**
   * 🔑 range 와 start 를 identity 에 넣는다. interval 만으로 만들면 "최근 7일" 과
   *    "최근 30일" 이 같은 캐시를 공유해 탭을 바꿔도 같은 데이터가 나온다.
   *    start 는 bucket 경계로 내린 값이라 같은 시간(날) 안에서는 고정된다.
   */
  trends: (interval: "hour" | "day", range: string, start?: string) =>
    ["ed", "model-monitoring", "trends", interval, range, start ?? "auto"] as const,
  confusion: (threshold?: number) =>
    ["ed", "model-monitoring", "confusion", threshold ?? "current"] as const,
  thresholds: ["ed", "model-monitoring", "thresholds"] as const,
  status: ["ed", "model-monitoring", "status"] as const,
};
