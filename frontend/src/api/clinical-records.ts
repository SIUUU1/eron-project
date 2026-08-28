import { ApiError, apiPost } from "./client.ts";
import type { ClinicalRecordWorkflowResponse, WhisperDraftRequest } from "./types.ts";
import type { DialogueTurn, EmergencyRecord } from "../lib/mock-data.ts";

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

export function dialogueToWhisperDraftRequest(dialogue: DialogueTurn[]): WhisperDraftRequest {
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
