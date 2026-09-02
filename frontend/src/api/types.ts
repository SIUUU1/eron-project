/**
 * 백엔드 Pydantic 스키마(backend/app/schemas/ed/)에서 도출한 타입.
 * 백엔드 응답 구조가 원천이며, 여기서 임의로 필드를 만들지 않는다.
 */

export interface WhisperDraftSegment {
  id: string | number;
  start: number;
  end: number;
  text: string;
  speaker?: string;
  [key: string]: unknown;
}

export interface WhisperDraftRequest {
  segments: WhisperDraftSegment[];
  [key: string]: unknown;
}

export interface ClinicalDraftField {
  field_id: string;
  value: string;
  ai_original_value: string;
  suggestion_status: "UNCHANGED" | "AUTO_SUGGESTED" | "UNRESOLVED";
  applied_candidates: ClinicalAppliedCandidate[];
  information_status: "PRESENT" | "NONE" | "NOT_ASSESSED" | "UNCERTAIN";
  evidence: ClinicalDraftEvidence[];
}

export interface ClinicalAppliedCandidate {
  collection: string;
  entity_id: string;
  display_value: string;
  source: "RAW_EXACT" | "UMLS";
}

export interface ClinicalDraftEvidence {
  source_segment_id?: string | number;
  segment_id?: string | number;
  [key: string]: unknown;
}

export interface ClinicalCandidateProvenance {
  display_value: string;
  source: "RAW_EXACT" | "UMLS" | "NGRAM_FALLBACK";
  cui?: string | null;
  semantic_types?: string[];
  similarity?: number | null;
}

export interface ClinicalDraftReviewItem {
  id: string;
  type?: string;
  field_id: string;
  segment_id?: string | number;
  source?: string;
  evidence?: string;
  evidence_start?: number;
  evidence_end?: number;
  candidates?: string[];
  candidate_provenance?: ClinicalCandidateProvenance[];
  search_terms_en?: string[];
  needs_review?: boolean;
  [key: string]: unknown;
}

export interface ClinicalApi3Segment {
  id: string | number;
  start?: number;
  end?: number;
  speaker?: string;
  raw_text?: string;
  corrected_text?: string;
  [key: string]: unknown;
}

export interface ClinicalTranslatedSegment {
  segment_id: string | number;
  translated_text_en: string;
}

export interface ClinicalQueryExpansion {
  translated_segments?: ClinicalTranslatedSegment[];
  [key: string]: unknown;
}

export interface ClinicalDraftFields {
  chief_complaint: ClinicalDraftField;
  pain_assessment: ClinicalDraftField;
  history_of_present_illness: ClinicalDraftField;
  past_history: ClinicalDraftField;
  medications: ClinicalDraftField;
  allergy: ClinicalDraftField;
  /** Read compatibility for clinical-workflow payloads created before the allergy key rename. */
  drug_allergy?: ClinicalDraftField;
  social_history: ClinicalDraftField;
  review_of_systems: ClinicalDraftField;
  physical_examination: ClinicalDraftField;
  treatment_plan: ClinicalDraftField;
  impression: ClinicalDraftField;
  outcome: ClinicalDraftField;
}

export interface ClinicalRecordWorkflowResponse {
  schema_version: "clinical-workflow-v2";
  processing_status: "completed" | "partial";
  record_status: "DRAFT";
  workflow_phase: "DRAFT_GENERATION";
  completed_at: null;
  api3: {
    segments: ClinicalApi3Segment[];
    [key: string]: unknown;
  };
  query_expansion?: ClinicalQueryExpansion;
  draft: {
    fields: ClinicalDraftFields;
    review_items: ClinicalDraftReviewItem[];
  };
  errors: unknown[];
}

export interface PersistedClinicalRecord {
  id: number;
  ed_stay_id: string;
  status: "DRAFT" | "SIGNED";
  record_payload: {
    record: Record<string, string>;
    field_statuses?: Record<string, string> | null;
    field_provenance?: Record<string, unknown>;
    generated?: boolean;
  };
  selected_kcd:
    | Array<{ code: string; name: string; is_rule_out?: boolean }>
    | { code: string; name: string; is_rule_out?: boolean }
    | null;
  clinician_id: string;
  clinician_name: string;
  created_at: string;
  updated_at: string;
  signed_by: string | null;
  signed_at: string | null;
}

export interface KcdCodeItem {
  code: string;
  name: string;
  name_en: string | null;
}

export interface KcdSearchResponse {
  items: KcdCodeItem[];
  total: number;
  query: string;
  limit: number;
}

export interface Meta {
  data_source: string;
  is_demo_timeline: boolean;
  cohort_size: number | null;
  /** false 면 위험도·확률이 모두 null 이다. */
  model_connected: boolean;
}

export interface LatestVital {
  measured_at: string | null;
  heart_rate: number | null;
  resp_rate: number | null;
  sbp: number | null;
  dbp: number | null;
  spo2: number | null;
  temperature_c: number | null;
  /** 항상 null — MIMIC-IV-ED 에 의식수준이 없다. */
  consciousness: string | null;
}

export type RiskLevelApi = "stable" | "watch" | "rising" | "critical";

/**
 * 모델의 3구간(bundle.json risk_bands 실측 경계).
 * green 저위험 · amber 관찰 필요 · red 재평가 필요.
 * risk_level(4단계)은 .env RISK_* 경계이며 이 3구간을 더 잘게 나눈 것이다.
 */
export type RiskBandApi = "green" | "amber" | "red";

export type DischargeType = "icu" | "admitted" | "home" | "expired";

export interface EdStayListItem {
  stay_id: string;
  /** 성씨 + 마스킹 표기(김**). 실명이 아니며 동일 성씨가 중복될 수 있다. */
  display_name: string | null;
  sex: "M" | "F" | null;
  age: number | null;
  arrived_at: string | null;
  /** MIMIC triage.acuity = ESI 1~5. KTAS 와 동일 척도가 아니다. */
  acuity: number | null;
  chief_complaint: string | null;
  chief_complaint_detail: string | null;
  risk_level: RiskLevelApi | null;
  /** 화면 목록의 '현재 위험도'가 쓰는 모델 3구간. 예측이 없으면 null. */
  risk_band: RiskBandApi | null;
  risk_probability: number | null;
  /** 도래한 재검토 필요(red) 알림 수. */
  alert_total: number;
  /** 그중 미확인 수. 0 보다 크면 "의료진 재검토" 버튼이 활성. */
  alert_unread: number;
  /** 재검토 필요 알림이 있고 **전부 확인**된 상태. 목록의 ✓ 조건. */
  reviewed: boolean;
  latest_vital: LatestVital;
  bed_id: string | null;
  record_status: "DRAFT" | "SIGNED" | null;
  /** 퇴실 시각(데모 시간축). 아직 퇴실 전이면 null. */
  departed_at: string | null;
  /** 퇴실 유형. 아직 퇴실 전이면 null → 화면에서는 빈칸. */
  discharge_type: DischargeType | null;
}

export interface EdStayPage {
  items: EdStayListItem[];
  page: number;
  page_size: number;
  total: number;
  meta: Meta;
}

export interface TriageSnapshot {
  heart_rate: number | null;
  resp_rate: number | null;
  sbp: number | null;
  dbp: number | null;
  spo2: number | null;
  temperature_c: number | null;
  pain: string | null;
}

export interface HospitalInfo {
  hadm_id: string | null;
  admitted: boolean;
  icu_transferred: boolean;
}

export interface EdStayDetail {
  stay_id: string;
  subject_id_masked: string;
  display_name: string | null;
  sex: "M" | "F" | null;
  age: number | null;
  race: string | null;
  arrived_at: string | null;
  departed_at: string | null;
  arrival_transport: string | null;
  arrival_route: string | null;
  acuity: number | null;
  chief_complaint: string | null;
  chief_complaint_detail: string | null;
  triage: TriageSnapshot;
  disposition: string | null;
  hospital: HospitalInfo;
  risk_level: RiskLevelApi | null;
  /** 화면 배지가 쓰는 모델 3구간. 예측이 없으면 null. */
  risk_band: RiskBandApi | null;
  risk_probability: number | null;
  alert_total: number;
  alert_unread: number;
  reviewed: boolean;
  bed_id: string | null;
  meta: Meta;
}

export interface VitalPoint {
  measured_at: string;
  heart_rate: number | null;
  resp_rate: number | null;
  sbp: number | null;
  dbp: number | null;
  spo2: number | null;
  temperature_c: number | null;
  rhythm: string | null;
  pain: string | null;
  consciousness: string | null;
}

export interface VitalsResponse {
  stay_id: string;
  vitals: VitalPoint[];
  latest: LatestVital;
  count: number;
  meta: {
    outlier_filtered: boolean;
    temperature_unit: string;
    is_demo_timeline: boolean;
    notice: string;
  };
}

export interface PredictionPoint {
  prediction_time: string;
  t_idx: number | null;
  horizon_minutes: number | null;
  risk_probability: number;
  risk_level: RiskLevelApi;
  model_version: string;
}

/**
 * 모델 예측에 기여한 신호 한 줄. 악화의 '원인'이 아니다.
 * v3 부터 model feature 1개 단위다(vital/lab 그룹 합산이 아니다).
 */
export interface RiskSignal {
  /** 모델 feature 명. heart_rate_last, lab_lactate_dt 등 */
  feature: string;
  feature_label: string;
  text: string;
  /** 현재 값 (reason_type = current_risk_signal) */
  value: number | null;
  contribution: number | null;
  /** 직전/현재 값 (reason_type = risk_increase_signal) */
  previous_value: number | null;
  current_value: number | null;
  previous_contribution: number | null;
  current_contribution: number | null;
  delta_contribution: number | null;
  /** 임상 방향 gate 판정. 화면에 노출된 악화 신호는 항상 "worsening". */
  clinical_direction: string | null;
  clinical_rule: string | null;
  clinical_gate_passed: boolean | null;
  /** 기여도의 단위 공간. LightGBM raw-score SHAP 이며 확률 %p 가 아니다. */
  contribution_space: string | null;
}

/**
 * risk_increase_clinical_worsening_signal
 *   = 직전 예측 대비 위험이 오르면서, 임상 방향 gate 에서 '악화'로 확인된 변화
 * risk_increase_without_confirmed_clinical_worsening_signal
 *   = 위험은 올랐지만 악화로 확인된 변화가 없음 (**risk_factors 가 비는 것이 정상**)
 * current_risk_signal
 *   = 현재 위험도에 기여한 신호
 */
export type ReasonType =
  | "risk_increase_clinical_worsening_signal"
  | "risk_increase_without_confirmed_clinical_worsening_signal"
  | "current_risk_signal";

export interface LatestPrediction {
  risk_probability: number | null;
  risk_level: RiskLevelApi | null;
  /** 화면에 그대로 쓰는 신호 문장. reason_notice 와 함께 표시해야 한다. */
  risk_factors: string[];
  risk_signals: RiskSignal[];
  reason_type: ReasonType | null;
  /** 화면 제목. 모델이 만든 문구를 그대로 쓴다. */
  reason_title: string | null;
  reason_basis: string | null;
  /** 위험 상승 시점에서 임상적 악화로 확인된 변화가 있었는가. */
  clinical_worsening_confirmed: boolean | null;
  /** 설명과 함께 반드시 표시해야 하는 문구. 설명이 없으면 null. */
  reason_notice: string | null;
  /** 직전 예측 시점 대비 확률 변화(0~1 스케일). 첫 시점이면 null. */
  risk_delta: number | null;
  /** 항상 빈 배열 — 악화 예측 모델은 권고를 생성하지 않는다. */
  recommendations: string[];
}

export interface PredictionsResponse {
  stay_id: string;
  predictions: PredictionPoint[];
  latest: LatestPrediction;
  count: number;
  meta: { model_connected: boolean; notice: string };
}

/** POST /api/ed/predictions/run 실행 요약. 화면은 로그·토스트에만 쓴다. */
export interface PredictionRunResult {
  /** 코호트 stay 수 */
  stays: number;
  /** 이번 실행에서 계산 대상으로 고른 stay 수 (due + 슬롯 조건) */
  selected: number;
  /** 실행 기준 15분 슬롯(데모 축) */
  slot: string | null;
  scored: number;
  rows: number;
  out_of_scope: number;
  failed: number;
}

export interface DashboardSummary {
  total: number;
  critical: number;
  rising: number;
  watch: number;
  stable: number;
  /** 예측이 없어 위험도를 산출할 수 없는 환자 수 */
  unassessed: number;
  ai_alerts_today: number;
  meta: Meta;
}

/** pending = 환자는 있으나 첫 예측 전(흰색). 위험도 카운트에 포함하지 않는다. */
export type BedStatusApi = "critical" | "moderate" | "low" | "pending" | "empty";

export interface BedItem {
  bed_id: string;
  status: BedStatusApi;
  stay_id: string | null;
  display_name: string | null;
  age: number | null;
  sex: "M" | "F" | null;
  devices: string[];
}

export interface BedsResponse {
  summary: {
    total: number;
    critical: number;
    moderate: number;
    low: number;
    /** 환자는 있으나 아직 첫 예측이 없는 병상 */
    pending: number;
    empty: number;
  };
  zones: { zone: string; beds: BedItem[] }[];
  /** 'none' 이면 아직 어떤 환자도 예측이 도래하지 않은 상태다(대체 색을 쓰지 않는다). */
  meta: { is_demo_assignment: boolean; status_source: "prediction" | "none" };
}

/** 경보가 켜진 시점 1건. app.prediction 에서 파생한다(별도 적재 없음). */
export interface AlertItem {
  /** 근거가 된 예측 행 id */
  id: number;
  stay_id: string;
  display_name: string | null;
  /** 경보가 켜진 예측 시점 (데모 시간축) */
  alert_time: string;
  level: RiskLevelApi;
  /** 화면 배지가 쓰는 모델 3구간. */
  band: RiskBandApi | null;
  /** 그 시점의 보정 확률 (0~1) */
  risk_probability: number | null;
  /** 모델이 만든 기여 신호 문장. 악화의 '원인'이 아니다. */
  message: string;
  reason_type: ReasonType | null;
  /** 재검토 완료 시각. 다음 예측이 생기면 자동으로 null 로 돌아간다. */
  acknowledged_at: string | null;
}

export interface AlertsResponse {
  items: AlertItem[];
  /** 아직 재검토하지 않은 🔴 재평가 필요 환자 수 — 종 아이콘 숫자. */
  unread_count: number;
  meta: Meta;
}

/** POST /api/ed/alerts/{stay_id}/acknowledge 결과. */
export interface AlertAckResult {
  ed_stay_id: string;
  /** 이번에 확인 처리한 알림 수 */
  acknowledged: number;
  /** 처리 후 전체에 남은 미확인 알림 수 */
  unread_count: number;
}

export interface ReassessItem {
  stay_id: string;
  display_name: string | null;
  risk_level: RiskLevelApi | null;
  /** 화면 배지가 쓰는 모델 3구간. 예측이 없으면 null. */
  risk_band: RiskBandApi | null;
  risk_probability: number | null;
  /** 배정 병상(데모 배정). 없으면 null. */
  bed_id: string | null;
  acuity: number | null;
  due_minutes: number;
  due_label: string;
}

export interface ReassessResponse {
  items: ReassessItem[];
  /** 재평가 큐는 예측이 없으면 ESI 중증도 순으로 정렬한다(병상 색과는 별개 기준). */
  meta: { is_demo_assignment: boolean; status_source: "prediction" | "triage_acuity" };
}
