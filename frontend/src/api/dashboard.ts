import { apiGet, apiPost, buildQuery } from "@/api/client";
import type {
  AlertAckResult,
  AlertsResponse,
  BedsResponse,
  DashboardSummary,
  IncompleteRecordsResponse,
  ReassessResponse,
  RiskBandApi,
} from "@/api/types";

export function getDashboardSummary(signal?: AbortSignal) {
  return apiGet<DashboardSummary>("/api/ed/dashboard/summary", signal);
}

export function getBeds(signal?: AbortSignal) {
  return apiGet<BedsResponse>("/api/ed/dashboard/beds", signal);
}

export function getIncompleteRecords(limit = 5, signal?: AbortSignal) {
  return apiGet<IncompleteRecordsResponse>(
    `/api/ed/dashboard/incomplete-records${buildQuery({ limit })}`,
    signal,
  );
}

/**
 * band 를 주면 그 구간만 받는다(모델 3구간). 화면에서 거르면 limit 이 먼저 걸려
 * 위험한 경보가 목록 밖으로 밀려난다.
 *
 * `latestOnly` 는 환자당 최신 알림 1건만 받는다 — 실시간 AI 경고 카드가 쓴다.
 * 종 알림 목록은 시점별로 누적된 알림을 받아야 하므로 false 로 둔다.
 * 두 경우 모두 **현재 최신 예측이 재검토 필요인 환자**만 서버가 돌려준다.
 */
export function getAlerts(
  limit = 20,
  band?: RiskBandApi,
  latestOnly?: boolean,
  signal?: AbortSignal,
) {
  return apiGet<AlertsResponse>(
    // buildQuery 는 문자열/숫자만 받는다. 불리언은 문자열로 넘긴다.
    `/api/ed/alerts${buildQuery({ limit, band, latest_only: latestOnly ? "true" : undefined })}`,
    signal,
  );
}

/**
 * 재검토 완료. **어느 예측에 대한 확인인지는 서버가 정한다**(현재 최신 예측).
 * 여러 번 눌러도 한 건이라 종 카운트가 중복 차감되지 않는다.
 */
export function acknowledgeAlert(stayId: string, by?: string) {
  return apiPost<undefined, AlertAckResult>(
    `/api/ed/alerts/${stayId}/acknowledge${buildQuery({ by })}`,
    undefined,
  );
}

export function getReassessQueue(signal?: AbortSignal) {
  return apiGet<ReassessResponse>("/api/ed/reassess-queue", signal);
}

export const dashboardKeys = {
  summary: ["ed", "dashboard", "summary"] as const,
  beds: ["ed", "dashboard", "beds"] as const,
  incompleteRecords: ["ed", "dashboard", "incomplete-records"] as const,
  alerts: (band?: RiskBandApi, latestOnly?: boolean) =>
    ["ed", "alerts", band ?? "all", latestOnly ? "latest" : "all"] as const,
  /** 접두사 무효화용 — band/latestOnly 조합이 여럿이라 루트로 한 번에 건드린다. */
  alertsRoot: ["ed", "alerts"] as const,
  reassess: ["ed", "reassess"] as const,
};
