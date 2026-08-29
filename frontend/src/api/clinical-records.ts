import { ApiError, apiPost } from "./client.ts";
import type {
  ClinicalDraftField,
  ClinicalRecordWorkflowResponse,
  WhisperDraftRequest,
} from "./types.ts";
import type { CheckStatus, EmergencyRecord, RecordFieldKey } from "../lib/mock-data.ts";

export interface DraftDialogueTurn {
  speaker: string;
  text: string;
}

export function parseWhisperDraftJson(source: string): WhisperDraftRequest {
  const value: unknown = JSON.parse(source);
  if (
    typeof value !== "object" ||
    value === null ||
    !Array.isArray((value as { segments?: unknown }).segments)
  ) {
    throw new Error("Whisper JSON에는 segments 배열이 필요합니다.");
  }
  const segments = (value as { segments: unknown[] }).segments;
  const segmentIds = new Set<string | number>();
  segments.forEach((segment, index) => {
    if (typeof segment !== "object" || segment === null) {
      throw new Error(`Whisper segment ${index + 1} 형식이 올바르지 않습니다.`);
    }
    const candidate = segment as Record<string, unknown>;
    const { id, start, end, text } = candidate;
    if (
      (typeof id !== "string" && typeof id !== "number") ||
      typeof start !== "number" ||
      !Number.isFinite(start) ||
      typeof end !== "number" ||
      !Number.isFinite(end) ||
      start > end ||
      typeof text !== "string" ||
      segmentIds.has(id)
    ) {
      throw new Error(`Whisper segment ${index + 1} 형식이 올바르지 않습니다.`);
    }
    segmentIds.add(id);
  });
  return value as WhisperDraftRequest;
}

export function whisperDraftToDialogue(
  request: WhisperDraftRequest,
): DraftDialogueTurn[] {
  return request.segments.map((segment) => ({
    speaker:
      typeof segment.speaker === "string" && segment.speaker.length > 0
        ? segment.speaker
        : "화자 미확인",
    text: segment.text,
  }));
}

export function createClinicalRecordDraft(
  request: WhisperDraftRequest,
  signal?: AbortSignal,
): Promise<ClinicalRecordWorkflowResponse> {
  return apiPost<WhisperDraftRequest, ClinicalRecordWorkflowResponse>(
    "/api/clinical-records/draft",
    request,
    signal,
  );
}

export function clinicalDraftErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return "응급기록 초안을 생성하지 못했습니다.";
  }
  switch (error.status) {
    case 0:
      return "서버에 연결할 수 없습니다.";
    case 400:
      return "대화 데이터 형식을 확인해 주세요.";
    case 502:
      return "AI 응답 형식을 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.";
    case 503:
      return "임상 초안 서비스를 사용할 수 없습니다.";
    case 504:
      return "응급기록 초안 생성 시간이 초과되었습니다. 다시 시도해 주세요.";
    default:
      return "응급기록 초안을 생성하지 못했습니다.";
  }
}

export function clinicalDraftPartialMessage(
  workflow: Pick<ClinicalRecordWorkflowResponse, "processing_status" | "errors">,
): string | null {
  if (workflow.processing_status !== "partial") return null;
  const issueCount = workflow.errors.length;
  return issueCount > 0
    ? `일부 처리 단계가 완료되지 않았습니다. 확인이 필요한 항목이 ${issueCount}건 있습니다.`
    : "일부 처리 단계가 완료되지 않았습니다. 생성된 초안을 확인해 주세요.";
}

export function dialogueToWhisperDraftRequest(
  dialogue: readonly DraftDialogueTurn[],
): WhisperDraftRequest {
  return {
    segments: dialogue.map((turn, index) => ({
      id: `ui_seg_${String(index + 1).padStart(4, "0")}`,
      start: index,
      end: index + 1,
      text: turn.text,
      speaker: turn.speaker,
    })),
  };
}

export function workflowDraftToEmergencyRecord(
  workflow: Pick<ClinicalRecordWorkflowResponse, "draft">,
): EmergencyRecord {
  const fields = workflow.draft.fields;
  return {
    chiefComplaint: fields.chief_complaint.value,
    painAssessment: fields.pain_assessment.value,
    presentIllness: fields.history_of_present_illness.value,
    pastHistory: fields.past_history.value,
    medication: fields.medications.value,
    allergy: fields.drug_allergy.value,
    socialHistory: fields.social_history.value,
    systemReview: fields.review_of_systems.value,
    physicalExam: fields.physical_examination.value,
    treatmentPlan: fields.treatment_plan.value,
    impression: fields.impression.value,
    outcome: fields.outcome.value,
  };
}

function clinicalDraftFieldStatus(field: ClinicalDraftField): CheckStatus {
  if (
    field.information_status === "UNCERTAIN" ||
    field.suggestion_status === "UNRESOLVED"
  ) {
    return "review";
  }
  if (field.information_status === "NOT_ASSESSED") return "missing";
  return "complete";
}

export function workflowDraftToFieldStatuses(
  workflow: Pick<ClinicalRecordWorkflowResponse, "draft">,
): Record<RecordFieldKey, CheckStatus> {
  const fields = workflow.draft.fields;
  return {
    chiefComplaint: clinicalDraftFieldStatus(fields.chief_complaint),
    painAssessment: clinicalDraftFieldStatus(fields.pain_assessment),
    presentIllness: clinicalDraftFieldStatus(fields.history_of_present_illness),
    pastHistory: clinicalDraftFieldStatus(fields.past_history),
    medication: clinicalDraftFieldStatus(fields.medications),
    allergy: clinicalDraftFieldStatus(fields.drug_allergy),
    socialHistory: clinicalDraftFieldStatus(fields.social_history),
    systemReview: clinicalDraftFieldStatus(fields.review_of_systems),
    physicalExam: clinicalDraftFieldStatus(fields.physical_examination),
    treatmentPlan: clinicalDraftFieldStatus(fields.treatment_plan),
    impression: clinicalDraftFieldStatus(fields.impression),
    outcome: clinicalDraftFieldStatus(fields.outcome),
  };
}
