// ⚠ 화면 배지는 모델 3구간(api/display.ts 의 bandMeta)을 쓴다.
// 아래 4단계는 mock 픽스처 정렬에만 남아 있다.
export type RiskLevel = "stable" | "watch" | "rising" | "critical";

export const riskOrder: Record<RiskLevel, number> = {
  critical: 0,
  rising: 1,
  watch: 2,
  stable: 3,
};

export type RecordStatus = "미작성" | "작성 중" | "검토 대기" | "의사 인증 완료";

export interface Patient {
  id: string;
  name: string;
  sex: "남" | "여";
  age: number;
  arrivedAt: string;
  arrivalRoute: string;
  arrivalMeans: string;
  ktas: 1 | 2 | 3 | 4 | 5;
  chiefComplaint: string;
  chiefComplaintDetail: string;
  risk: RiskLevel;
  deteriorationProbability: number;
  bed: string;
  recordStatus: RecordStatus;
  vitals: {
    hr: number;
    rr: number;
    sbp: number;
    dbp: number;
    bt: number;
    spo2: number;
    mental: string;
  };
  trend: VitalPoint[];
  riskFactors: string[];
  recommendations: string[];
}

export interface VitalPoint {
  time: string;
  hr: number;
  sbp: number;
  dbp: number;
  spo2: number;
  bt: number;
  probability: number;
}

export const patients: Patient[] = [
  {
    id: "P-20260812-001",
    name: "김민수",
    sex: "남",
    age: 72,
    arrivedAt: "2026-08-12 08:35",
    arrivalRoute: "직접 내원",
    arrivalMeans: "119 구급차",
    ktas: 2,
    chiefComplaint: "흉통",
    chiefComplaintDetail: "1시간 전부터 발생한 흉통",
    risk: "critical",
    deteriorationProbability: 87,
    bed: "A01",
    recordStatus: "작성 중",
    vitals: { hr: 118, rr: 26, sbp: 88, dbp: 56, bt: 37.8, spo2: 91, mental: "Alert" },
    trend: [
      { time: "09:00", hr: 92, sbp: 132, dbp: 84, spo2: 97, bt: 36.8, probability: 42 },
      { time: "10:00", hr: 98, sbp: 124, dbp: 78, spo2: 96, bt: 37.0, probability: 51 },
      { time: "11:00", hr: 106, sbp: 112, dbp: 70, spo2: 94, bt: 37.3, probability: 65 },
      { time: "12:00", hr: 112, sbp: 98, dbp: 62, spo2: 93, bt: 37.6, probability: 76 },
      { time: "현재", hr: 118, sbp: 88, dbp: 56, spo2: 91, bt: 37.8, probability: 87 },
    ],
    riskFactors: ["혈압 지속 감소", "심박수 증가", "산소포화도 감소", "고령 (72세)", "흉통 지속"],
    recommendations: [
      "의료진 즉시 재평가",
      "심전도 및 심근효소검사 결과 확인",
      "지속적인 혈압·산소포화도 모니터링",
      "필요 시 응급처치 준비",
    ],
  },
  {
    id: "P-20260812-002",
    name: "이영희",
    sex: "여",
    age: 64,
    arrivedAt: "2026-08-12 09:10",
    arrivalRoute: "타병원 전원",
    arrivalMeans: "사설 구급차",
    ktas: 2,
    chiefComplaint: "호흡곤란",
    chiefComplaintDetail: "3시간 전부터 악화된 호흡곤란",
    risk: "rising",
    deteriorationProbability: 68,
    bed: "A03",
    recordStatus: "작성 중",
    vitals: { hr: 104, rr: 24, sbp: 132, dbp: 82, bt: 37.4, spo2: 93, mental: "Alert" },
    trend: [
      { time: "09:00", hr: 88, sbp: 144, dbp: 88, spo2: 96, bt: 36.9, probability: 31 },
      { time: "10:00", hr: 92, sbp: 140, dbp: 86, spo2: 95, bt: 37.0, probability: 38 },
      { time: "11:00", hr: 96, sbp: 138, dbp: 85, spo2: 94, bt: 37.2, probability: 49 },
      { time: "12:00", hr: 100, sbp: 135, dbp: 83, spo2: 94, bt: 37.3, probability: 59 },
      { time: "현재", hr: 104, sbp: 132, dbp: 82, spo2: 93, bt: 37.4, probability: 68 },
    ],
    riskFactors: ["산소포화도 감소", "호흡수 증가", "기저 만성폐질환"],
    recommendations: [
      "산소 투여 상태 재확인",
      "흉부 영상검사 결과 확인",
      "호흡수·산소포화도 집중 모니터링",
    ],
  },
  {
    id: "P-20260812-003",
    name: "박준호",
    sex: "남",
    age: 35,
    arrivedAt: "2026-08-12 10:05",
    arrivalRoute: "직접 내원",
    arrivalMeans: "도보",
    ktas: 3,
    chiefComplaint: "복통",
    chiefComplaintDetail: "우하복부 통증",
    risk: "watch",
    deteriorationProbability: 34,
    bed: "C02",
    recordStatus: "미작성",
    vitals: { hr: 92, rr: 18, sbp: 128, dbp: 78, bt: 37.9, spo2: 98, mental: "Alert" },
    trend: [
      { time: "09:00", hr: 84, sbp: 130, dbp: 80, spo2: 99, bt: 37.2, probability: 18 },
      { time: "10:00", hr: 86, sbp: 129, dbp: 79, spo2: 99, bt: 37.4, probability: 22 },
      { time: "11:00", hr: 88, sbp: 128, dbp: 78, spo2: 98, bt: 37.6, probability: 27 },
      { time: "12:00", hr: 90, sbp: 128, dbp: 78, spo2: 98, bt: 37.8, probability: 31 },
      { time: "현재", hr: 92, sbp: 128, dbp: 78, spo2: 98, bt: 37.9, probability: 34 },
    ],
    riskFactors: ["발열 지속", "복통 지속"],
    recommendations: ["복부 CT 결과 확인", "외과 협진 고려"],
  },
  {
    id: "P-20260812-004",
    name: "최수진",
    sex: "여",
    age: 28,
    arrivedAt: "2026-08-12 11:20",
    arrivalRoute: "직접 내원",
    arrivalMeans: "자가용",
    ktas: 4,
    chiefComplaint: "발열",
    chiefComplaintDetail: "2일간의 발열 및 인후통",
    risk: "stable",
    deteriorationProbability: 12,
    bed: "E03",
    recordStatus: "검토 대기",
    vitals: { hr: 84, rr: 16, sbp: 118, dbp: 74, bt: 38.1, spo2: 99, mental: "Alert" },
    trend: [
      { time: "09:00", hr: 80, sbp: 116, dbp: 72, spo2: 99, bt: 38.4, probability: 14 },
      { time: "10:00", hr: 82, sbp: 117, dbp: 73, spo2: 99, bt: 38.3, probability: 13 },
      { time: "11:00", hr: 82, sbp: 118, dbp: 74, spo2: 99, bt: 38.2, probability: 13 },
      { time: "12:00", hr: 84, sbp: 118, dbp: 74, spo2: 99, bt: 38.1, probability: 12 },
      { time: "현재", hr: 84, sbp: 118, dbp: 74, spo2: 99, bt: 38.1, probability: 12 },
    ],
    riskFactors: ["발열"],
    recommendations: ["해열제 투여 후 경과 관찰"],
  },
];

export const sortedPatients = [...patients].sort(
  (a, b) =>
    riskOrder[a.risk] - riskOrder[b.risk] ||
    b.deteriorationProbability - a.deteriorationProbability,
);

export function getPatient(id: string) {
  return patients.find((p) => p.id === id);
}

export const summary = {
  total: 24,
  critical: 2,
  rising: 4,
  aiAlertsToday: 8,
};

/* ---------------- 병상 현황판 ---------------- */

export type BedStatus = "critical" | "moderate" | "low" | "pending" | "empty";

export interface Bed {
  id: string;
  status: BedStatus;
  name?: string | undefined;
  age?: number | undefined;
  sex?: "M" | "F" | undefined;
  devices?: ("E" | "V" | "C")[] | undefined;
  patientId?: string | undefined;
}

export const bedStatusMeta: Record<BedStatus, { label: string; card: string; text: string }> = {
  critical: {
    label: "재평가 필요",
    card: "bg-risk-critical-soft border-risk-critical/35",
    text: "text-risk-critical",
  },
  moderate: {
    label: "관찰 필요",
    card: "bg-risk-watch-soft border-risk-watch/45",
    text: "text-risk-watch",
  },
  low: {
    label: "저위험",
    card: "bg-risk-stable-soft border-risk-stable/35",
    text: "text-risk-stable",
  },
  // 환자는 있지만 아직 첫 예측 전 — 흰색. 위험도 카운트에는 들어가지 않는다.
  pending: { label: "예측 대기", card: "bg-card border-border", text: "text-muted-foreground" },
  empty: { label: "빈 병상", card: "bg-muted border-border", text: "text-muted-foreground" },
};

const b = (
  id: string,
  status: BedStatus,
  name?: string,
  age?: number,
  sex?: "M" | "F",
  devices?: ("E" | "V" | "C")[],
  patientId?: string,
): Bed => ({ id, status, name, age, sex, devices, patientId });

export const bedZones: { zone: string; beds: Bed[] }[] = [
  {
    zone: "A 구역 (Resus)",
    beds: [
      b("A01", "critical", "김민수", 72, "M", ["E", "V", "C"], "P-20260812-001"),
      b("A02", "critical", "이정환", 62, "M", ["V"]),
      b("A03", "moderate", "이영희", 64, "F", ["V"], "P-20260812-002"),
      b("A04", "moderate", "최우진", 34, "M"),
      b("A05", "low", "정다은", 28, "F"),
      b("A06", "empty"),
    ],
  },
  {
    zone: "B 구역",
    beds: [
      b("B01", "critical", "강현우", 55, "M", ["E", "V"]),
      b("B02", "moderate", "오세훈", 47, "M", ["C"]),
      b("B03", "moderate", "한지민", 66, "F", ["V"]),
      b("B04", "low", "이서연", 23, "F"),
      b("B05", "low", "박준형", 31, "M"),
      b("B06", "empty"),
    ],
  },
  {
    zone: "C 구역",
    beds: [
      b("C01", "critical", "조영호", 69, "M", ["E", "C"]),
      b("C02", "moderate", "박준호", 35, "M", [], "P-20260812-003"),
      b("C03", "moderate", "문태성", 50, "M", ["C"]),
      b("C04", "low", "김수빈", 19, "F"),
      b("C05", "low", "이민준", 27, "M"),
      b("C06", "empty"),
    ],
  },
  {
    zone: "D 구역",
    beds: [
      b("D01", "critical", "서지훈", 74, "M", ["E", "V"]),
      b("D02", "moderate", "김연우", 64, "F", ["V", "C"]),
      b("D03", "moderate", "장예진", 38, "F"),
      b("D04", "low", "최민석", 29, "M"),
      b("D05", "empty"),
      b("D06", "empty"),
    ],
  },
  {
    zone: "E 구역",
    beds: [
      b("E01", "moderate", "임재원", 57, "M", ["C"]),
      b("E02", "moderate", "배지현", 63, "F", ["V"]),
      b("E03", "low", "최수진", 28, "F", [], "P-20260812-004"),
      b("E04", "low", "안유진", 24, "F"),
      b("E05", "empty"),
      b("E06", "empty"),
    ],
  },
  {
    zone: "F 구역",
    beds: [
      b("F01", "low", "양도현", 26, "M"),
      b("F02", "low", "김채영", 21, "F"),
      b("F03", "moderate", "허민호", 48, "M", ["V"]),
      b("F04", "empty"),
      b("F05", "empty"),
      b("F06", "empty"),
    ],
  },
];

export const bedSummary = {
  total: 36,
  critical: bedZones.flatMap((z) => z.beds).filter((x) => x.status === "critical").length,
  moderate: bedZones.flatMap((z) => z.beds).filter((x) => x.status === "moderate").length,
  low: bedZones.flatMap((z) => z.beds).filter((x) => x.status === "low").length,
  empty: bedZones.flatMap((z) => z.beds).filter((x) => x.status === "empty").length,
};

/* ---------------- AI 경고 / 알림 ---------------- */

export const aiAlerts = [
  {
    time: "12:58",
    patient: "김민수",
    patientId: "P-20260812-001",
    level: "critical" as RiskLevel,
    message: "악화 확률 87%로 상승 · 즉시 재평가 권고",
  },
  {
    time: "12:41",
    patient: "이영희",
    patientId: "P-20260812-002",
    level: "rising" as RiskLevel,
    message: "산소포화도 93% 하강 추세",
  },
  {
    time: "12:20",
    patient: "서지훈",
    patientId: null,
    level: "rising" as RiskLevel,
    message: "심박수 20분간 지속 상승",
  },
  {
    time: "11:52",
    patient: "박준호",
    patientId: "P-20260812-003",
    level: "watch" as RiskLevel,
    message: "발열 지속 · 재측정 필요",
  },
];

export const reassessQueue = [
  { patient: "김민수", patientId: "P-20260812-001", due: "즉시", risk: "critical" as RiskLevel },
  { patient: "이영희", patientId: "P-20260812-002", due: "10분 내", risk: "rising" as RiskLevel },
  { patient: "서지훈", patientId: null, due: "15분 내", risk: "rising" as RiskLevel },
  { patient: "박준호", patientId: "P-20260812-003", due: "30분 내", risk: "watch" as RiskLevel },
];

/* ---------------- 샘플 대화 / 기록 ---------------- */

export interface DialogueTurn {
  speaker: "의료진" | "환자";
  text: string;
}

export const sampleDialogue: DialogueTurn[] = [
  { speaker: "의료진", text: "어디가 불편해서 오셨나요?" },
  { speaker: "환자", text: "한 시간 전부터 가슴이 쥐어짜듯이 아파요." },
  { speaker: "의료진", text: "통증이 다른 곳으로 퍼지나요?" },
  { speaker: "환자", text: "왼쪽 팔과 턱까지 아픈 것 같아요." },
  { speaker: "의료진", text: "통증이 0점에서 10점이면 몇 점 정도인가요?" },
  { speaker: "환자", text: "한 8점 정도 되는 것 같아요." },
  { speaker: "의료진", text: "숨이 차거나 식은땀이 나나요?" },
  { speaker: "환자", text: "숨이 조금 차고 식은땀도 났어요." },
  { speaker: "의료진", text: "이전에도 이런 증상이 있었나요?" },
  { speaker: "환자", text: "이렇게 심한 건 처음입니다." },
  { speaker: "의료진", text: "평소 앓고 있는 병이 있으세요?" },
  { speaker: "환자", text: "고혈압이랑 당뇨가 있어요." },
  { speaker: "의료진", text: "복용 중인 약이 있나요?" },
  { speaker: "환자", text: "혈압약과 당뇨약을 먹고 있습니다." },
  { speaker: "의료진", text: "알레르기는 있나요?" },
  { speaker: "환자", text: "잘 모르겠습니다." },
];

export type RecordFieldKey =
  | "chiefComplaint"
  | "painAssessment"
  | "presentIllness"
  | "pastHistory"
  | "medication"
  | "allergy"
  | "socialHistory"
  | "systemReview"
  | "physicalExam"
  | "treatmentPlan"
  | "impression"
  | "outcome";

export const recordFieldLabels: Record<RecordFieldKey, string> = {
  chiefComplaint: "주호소",
  painAssessment: "통증평가",
  presentIllness: "현병력",
  pastHistory: "과거력",
  medication: "복용약",
  allergy: "알레르기",
  socialHistory: "사회력 (흡연·음주)",
  systemReview: "계통문진",
  physicalExam: "신체검진",
  treatmentPlan: "진료계획",
  impression: "추정진단",
  outcome: "응급진료결과",
};

export type EmergencyRecord = Record<RecordFieldKey, string>;

export const emptyRecord: EmergencyRecord = {
  chiefComplaint: "",
  painAssessment: "",
  presentIllness: "",
  pastHistory: "",
  medication: "",
  allergy: "",
  socialHistory: "",
  systemReview: "",
  physicalExam: "",
  treatmentPlan: "",
  impression: "",
  outcome: "",
};

/** 대화에서 확인된 내용만 반영한 AI 초안 (미언급 항목은 공란/미확인) */
export const aiDraftRecord: EmergencyRecord = {
  chiefComplaint: "흉통",
  painAssessment: "NRS 8, 쥐어짜는 양상의 흉통, 좌측 팔 및 턱으로 방사",
  presentIllness:
    "내원 약 1시간 전부터 발생한 쥐어짜는 양상의 흉통으로 내원함. 좌측 팔과 턱으로 방사되며 호흡곤란 및 발한 동반함. 과거 동일 정도의 흉통 경험은 없음.",
  pastHistory: "HTN, DM",
  medication: "혈압약, 당뇨약 복용 중 (정확한 약제명 확인 필요)",
  allergy: "확인 필요 (환자 인지 못함)",
  socialHistory: "",
  systemReview: "Chest pain (+), Dyspnea (+), Sweating (+)",
  physicalExam: "미확인",
  treatmentPlan: "미확인",
  impression: "Acute coronary syndrome 의증",
  outcome: "",
};

export const outcomeOptions = ["귀가", "입원", "전원", "사망", "기타"];

export type CheckStatus = "complete" | "review" | "missing";

export const checkStatusMeta: Record<CheckStatus, { label: string; badge: string; text: string }> =
  {
    complete: {
      label: "작성 완료",
      badge: "bg-risk-stable-soft text-risk-stable border-risk-stable/35",
      text: "text-risk-stable",
    },
    review: {
      label: "확인 필요",
      badge: "bg-risk-rising-soft text-risk-rising border-risk-rising/40",
      text: "text-risk-rising",
    },
    missing: {
      label: "누락",
      badge: "bg-risk-critical-soft text-risk-critical border-risk-critical/40",
      text: "text-risk-critical",
    },
  };

export const followUpQuestions: { question: string; field: RecordFieldKey }[] = [
  { question: "약이나 주사를 맞은 뒤 알레르기 반응이 있었던 적이 있나요?", field: "allergy" },
  { question: "현재 복용 중인 약의 정확한 이름을 알고 계신가요?", field: "medication" },
  { question: "현재 또는 과거에 흡연한 적이 있나요?", field: "socialHistory" },
  { question: "흉부 진찰에서 특이 소견이 있나요?", field: "physicalExam" },
];

export const kcdCandidates = [
  {
    rank: 1,
    name: "급성 심근경색증 의심",
    code: "I21.9",
    fitness: 82,
    reasons: [
      "쥐어짜는 흉통",
      "좌측 팔 및 턱으로의 방사통",
      "호흡곤란 및 발한",
      "고혈압 및 당뇨병 과거력",
    ],
  },
  {
    rank: 2,
    name: "불안정 협심증",
    code: "I20.0",
    fitness: 64,
    reasons: ["지속적인 흉통", "방사통", "심혈관 위험요인"],
  },
  {
    rank: 3,
    name: "상세불명의 흉통",
    code: "R07.4",
    fitness: 41,
    reasons: ["검사 결과 확정 전 증상 중심 분류 가능"],
  },
];

export const currentUser = { name: "김의사", dept: "응급의학과", role: "전문의" };
