import { apiGet, apiPost, buildQuery } from "@/api/client";
import type {
  EdStayDetail,
  EdStayPage,
  PredictionRunResult,
  PredictionsResponse,
  VitalsResponse,
} from "@/api/types";

export interface EdStayQuery {
  page?: number;
  pageSize?: number;
  riskLevel?: string | null;
  acuity?: number | null;
  search?: string | null;
  /** acuity_mix = KTAS 균형 정렬 (각 등급 내 최신 내원 순) */
  sort?: "risk" | "arrival" | "acuity_mix";
}

export function getEdStays(q: EdStayQuery = {}, signal?: AbortSignal) {
  const qs = buildQuery({
    page: q.page ?? 1,
    page_size: q.pageSize ?? 20,
    risk_level: q.riskLevel,
    acuity: q.acuity,
    search: q.search,
    sort: q.sort ?? "risk",
  });
  return apiGet<EdStayPage>(`/api/ed/stays${qs}`, signal);
}

export function getEdStay(stayId: string, signal?: AbortSignal) {
  return apiGet<EdStayDetail>(`/api/ed/stays/${stayId}`, signal);
}

export function getEdStayVitals(stayId: string, signal?: AbortSignal) {
  return apiGet<VitalsResponse>(`/api/ed/stays/${stayId}/vitals?order=asc&limit=100`, signal);
}

export function getEdStayPredictions(stayId: string, signal?: AbortSignal) {
  return apiGet<PredictionsResponse>(`/api/ed/stays/${stayId}/predictions`, signal);
}

/**
 * 예측 갱신을 즉시 한 번 돌린다.
 *
 * ⚠ 어떤 환자를 계산할지는 **백엔드가 정한다**(next_prediction_at · due · 15분 슬롯).
 *   프론트는 "지금 시각이니 이 환자들" 같은 판단을 하지 않는다.
 *   데모 시계를 앞으로 옮긴 직후에만 쓰며, 평소에는 스케줄러가 같은 일을 한다.
 */
export function runPredictions(signal?: AbortSignal) {
  return apiPost<undefined, PredictionRunResult>("/api/ed/predictions/run", undefined, signal);
}

export const edStayKeys = {
  list: (q: EdStayQuery) => ["ed", "stays", q] as const,
  detail: (stayId: string) => ["ed", "stay", stayId] as const,
  vitals: (stayId: string) => ["ed", "stay", stayId, "vitals"] as const,
  predictions: (stayId: string) => ["ed", "stay", stayId, "predictions"] as const,
};
