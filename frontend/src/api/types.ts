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
  suggestion_status: "UNCHANGED" | "AUTO_SUGGESTED" | "UNRESOLVED";
  information_status: "PRESENT" | "NONE" | "NOT_ASSESSED" | "UNCERTAIN";
}

export interface ClinicalDraftFields {
  chief_complaint: ClinicalDraftField;
  pain_assessment: ClinicalDraftField;
  history_of_present_illness: ClinicalDraftField;
  past_history: ClinicalDraftField;
  medications: ClinicalDraftField;
  drug_allergy: ClinicalDraftField;
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
  draft: {
    fields: ClinicalDraftFields;
    review_items: unknown[];
  };
  errors: unknown[];
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
  risk_probability: number | null;
  latest_vital: LatestVital;
  bed_id: string | null;
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
  risk_probability: number | null;
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

export interface PredictionsResponse {
  stay_id: string;
  predictions: PredictionPoint[];
  latest: {
    risk_probability: number | null;
    risk_level: RiskLevelApi | null;
    risk_factors: string[];
    recommendations: string[];
  };
  count: number;
  meta: { model_connected: boolean; notice: string };
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

export type BedStatusApi = "critical" | "moderate" | "low" | "empty";

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
  summary: { total: number; critical: number; moderate: number; low: number; empty: number };
  zones: { zone: string; beds: BedItem[] }[];
  /** status_source 가 'triage_acuity' 면 예측이 아니라 ESI 중증도로 색을 정한 것이다. */
  meta: { is_demo_assignment: boolean; status_source: "prediction" | "triage_acuity" };
}

export interface AlertItem {
  id: number;
  stay_id: string;
  display_name: string | null;
  alert_time: string;
  level: RiskLevelApi;
  message: string;
  acknowledged_at: string | null;
}

export interface AlertsResponse {
  items: AlertItem[];
  meta: Meta;
}

export interface ReassessItem {
  stay_id: string;
  display_name: string | null;
  risk_level: RiskLevelApi | null;
  risk_probability: number | null;
  acuity: number | null;
  due_minutes: number;
  due_label: string;
}

export interface ReassessResponse {
  items: ReassessItem[];
  meta: { is_demo_assignment: boolean; status_source: "prediction" | "triage_acuity" };
}
