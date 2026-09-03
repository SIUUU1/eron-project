import { ApiError, apiGet, apiPost, apiPostFormData, apiPut } from "./client.ts";
import type {
  ClinicalAppliedCandidate,
  ClinicalCandidateProvenance,
  ClinicalDraftField,
  ClinicalDraftReviewItem,
  ClinicalRecordWorkflowResponse,
  KcdSearchResponse,
  PersistedClinicalRecord,
  WhisperDraftRequest,
} from "./types.ts";
import type {
  CandidateSource,
  FieldProvenance,
  FieldProvenanceMap,
  PatientEvidence,
  TerminologyCandidate,
} from "../lib/clinical-provenance.ts";
import { normalizeClinicalRecordOutcome } from "../lib/clinical-record-outcome.ts";
import type { CheckStatus, EmergencyRecord, RecordFieldKey } from "../lib/mock-data.ts";

const recordFieldKeyByClinicalId: Record<string, RecordFieldKey> = {
  chief_complaint: "chiefComplaint",
  pain_assessment: "painAssessment",
  history_of_present_illness: "presentIllness",
  past_history: "pastHistory",
  medications: "medication",
  allergy: "allergy",
  drug_allergy: "allergy",
  social_history: "socialHistory",
  review_of_systems: "systemReview",
  physical_examination: "physicalExam",
  treatment_plan: "treatmentPlan",
  impression: "impression",
  outcome: "outcome",
};

export interface DraftDialogueTurn {
  speaker: string;
  text: string;
}

const diagnosisListPrefix =
  /^\s*(?:(?:\d+\s*[.)]\s*)?(?:추정\s*진단|주진단|부진단)\s*\d*\s*[:：-]?|\d+\s*[.)])\s*/i;

/** Convert the model-authored impression display into ordered UI diagnosis rows. */
export function clinicalRecordDiagnosisEntries(value: string): string[] {
  const normalized = value.replace(/\r\n?/g, "\n").trim();
  if (!normalized || normalized === "미확인") return [""];

  const entries = normalized
    .replace(
      /;\s*(?=(?:(?:추정\s*진단|주진단|부진단)\s*\d*\s*[:：-]?|\d+\s*[.)]))/gi,
      "\n",
    )
    .split(/(?:\n+|\s*[,;]\s*)/)
    .map((entry) => entry.replace(diagnosisListPrefix, "").trim())
    .filter(Boolean);

  return entries.length > 0 ? entries : [""];
}

export function normalizeClinicalRecordImpression(value: string): string {
  return clinicalRecordDiagnosisEntries(value).filter(Boolean).join("\n");
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

export function whisperDraftToDialogue(request: WhisperDraftRequest): DraftDialogueTurn[] {
  return request.segments.map((segment) => ({
    speaker:
      typeof segment.speaker === "string" && segment.speaker.length > 0
        ? segment.speaker
        : "화자 미지정",
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

export interface ClinicalRecordSavePayload {
  record_payload: PersistedClinicalRecord["record_payload"];
  selected_kcd: PersistedClinicalRecord["selected_kcd"];
  clinician_id: string;
  clinician_name: string;
}

export function getPersistedClinicalRecord(stayId: string, signal?: AbortSignal) {
  return apiGet<PersistedClinicalRecord | null>(
    `/api/clinical-records/by-stay/${encodeURIComponent(stayId)}`,
    signal,
  );
}

export function saveClinicalRecordDraft(
  stayId: string,
  payload: ClinicalRecordSavePayload,
  signal?: AbortSignal,
) {
  return apiPut<ClinicalRecordSavePayload, PersistedClinicalRecord>(
    `/api/clinical-records/by-stay/${encodeURIComponent(stayId)}`,
    payload,
    signal,
  );
}

export function signClinicalRecord(
  recordId: number,
  clinician: { clinician_id: string; clinician_name: string },
  signal?: AbortSignal,
) {
  return apiPost<typeof clinician, PersistedClinicalRecord>(
    `/api/clinical-records/${recordId}/sign`,
    clinician,
    signal,
  );
}

export function searchKcdCodes(query: string, signal?: AbortSignal) {
  return apiGet<KcdSearchResponse>(
    `/api/kcd/search?q=${encodeURIComponent(query)}&limit=10`,
    signal,
  );
}

export function createClinicalRecordDraftFromAudio(
  audio: File,
  signal?: AbortSignal,
): Promise<ClinicalRecordWorkflowResponse> {
  const body = new FormData();
  body.append("audio", audio);
  return apiPostFormData<ClinicalRecordWorkflowResponse>(
    "/api/clinical-records/draft/audio",
    body,
    signal,
  );
}

export function transcribeClinicalRecordAudio(
  audio: File,
  signal?: AbortSignal,
): Promise<WhisperDraftRequest> {
  const body = new FormData();
  body.append("audio", audio);
  return apiPostFormData<WhisperDraftRequest>("/api/clinical-records/transcribe", body, signal);
}

export function clinicalAudioTranscriptionErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return "음성 파일을 인식하지 못했습니다.";
  }
  switch (error.status) {
    case 0:
      return "서버에 연결할 수 없습니다.";
    case 400:
      return "음성 파일을 읽을 수 없습니다.";
    case 413:
      return "음성 파일은 25MB 이하여야 합니다.";
    case 502:
      return "음성 인식 결과를 확인하지 못했습니다. 다시 시도해 주세요.";
    case 503:
      return "음성 인식 서비스를 사용할 수 없습니다.";
    case 504:
      return "음성 인식 시간이 초과되었습니다. 다시 시도해 주세요.";
    default:
      return "음성 파일을 인식하지 못했습니다.";
  }
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
  const outcome = normalizeClinicalRecordOutcome(fields.outcome.value);
  const allergy = fields.allergy ?? fields.drug_allergy;
  return {
    chiefComplaint: fields.chief_complaint.value,
    painAssessment: fields.pain_assessment.value,
    presentIllness: fields.history_of_present_illness.value,
    pastHistory: fields.past_history.value,
    medication: fields.medications.value,
    allergy: allergy?.value ?? "",
    socialHistory: fields.social_history.value,
    systemReview: fields.review_of_systems.value,
    physicalExam: fields.physical_examination.value,
    treatmentPlan: fields.treatment_plan.value,
    impression: normalizeClinicalRecordImpression(fields.impression.value),
    outcome,
  };
}

function clinicalDraftFieldStatus(field: ClinicalDraftField): CheckStatus {
  if (field.information_status === "UNCERTAIN" || field.suggestion_status === "UNRESOLVED") {
    return "review";
  }
  if (field.information_status === "NOT_ASSESSED") return "missing";
  return "complete";
}

function clinicalDraftOutcomeStatus(field: ClinicalDraftField): CheckStatus {
  if (!normalizeClinicalRecordOutcome(field.value)) return "missing";
  return clinicalDraftFieldStatus(field);
}

export function workflowDraftToFieldStatuses(
  workflow: Pick<ClinicalRecordWorkflowResponse, "draft">,
): Record<RecordFieldKey, CheckStatus> {
  const fields = workflow.draft.fields;
  const allergy = fields.allergy ?? fields.drug_allergy;
  return {
    chiefComplaint: clinicalDraftFieldStatus(fields.chief_complaint),
    painAssessment: clinicalDraftFieldStatus(fields.pain_assessment),
    presentIllness: clinicalDraftFieldStatus(fields.history_of_present_illness),
    pastHistory: clinicalDraftFieldStatus(fields.past_history),
    medication: clinicalDraftFieldStatus(fields.medications),
    allergy: allergy ? clinicalDraftFieldStatus(allergy) : "missing",
    socialHistory: clinicalDraftFieldStatus(fields.social_history),
    systemReview: clinicalDraftFieldStatus(fields.review_of_systems),
    physicalExam: clinicalDraftFieldStatus(fields.physical_examination),
    treatmentPlan: clinicalDraftFieldStatus(fields.treatment_plan),
    impression: clinicalDraftFieldStatus(fields.impression),
    outcome: clinicalDraftOutcomeStatus(fields.outcome),
  };
}

function timestampPart(seconds: number | undefined): string | null {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return null;
  const minutes = Math.floor(seconds / 60);
  const remainder = (seconds - minutes * 60).toFixed(2).padStart(5, "0");
  return `${String(minutes).padStart(2, "0")}:${remainder}`;
}

function evidenceTimestamp(start: number | undefined, end: number | undefined): string {
  const startLabel = timestampPart(start);
  const endLabel = timestampPart(end);
  if (startLabel && endLabel) return `${startLabel}–${endLabel}`;
  return startLabel ?? endLabel ?? "시간 정보 없음";
}

function candidateSource(value: unknown): CandidateSource | null {
  return value === "RAW_EXACT" || value === "UMLS" || value === "NGRAM_FALLBACK" ? value : null;
}

function candidateSimilarity(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function appliedCandidate(
  fieldId: string,
  index: number,
  candidate: ClinicalAppliedCandidate,
): TerminologyCandidate {
  return {
    id: `${fieldId}:applied:${index}`,
    query: candidate.display_value,
    canonicalValue: candidate.display_value,
    source: candidate.source,
    cui: null,
    semanticType: null,
    similarity: null,
    alreadyApplied: true,
  };
}

function reviewCandidate(
  item: ClinicalDraftReviewItem,
  index: number,
  provenance: ClinicalCandidateProvenance,
): TerminologyCandidate | null {
  const source = candidateSource(provenance.source);
  if (!source || !provenance.display_value.trim()) return null;
  const semanticTypes = (provenance.semantic_types ?? []).filter(Boolean);
  return {
    id: `${item.id}:candidate:${index}`,
    query: item.search_terms_en?.[0] ?? item.source ?? provenance.display_value,
    canonicalValue: provenance.display_value,
    source,
    cui: provenance.cui?.trim() || null,
    semanticType: semanticTypes.length > 0 ? semanticTypes.join(", ") : null,
    similarity: candidateSimilarity(provenance.similarity),
    selectionGroupIds: [item.id],
  };
}

const candidateSourcePriority: Record<CandidateSource, number> = {
  RAW_EXACT: 0,
  UMLS: 1,
  NGRAM_FALLBACK: 2,
  UNRESOLVED: 3,
};

function normalizedCandidateConcept(candidate: TerminologyCandidate): string {
  return (candidate.canonicalValue ?? candidate.query)
    .normalize("NFKC")
    .trim()
    .toLocaleLowerCase("en-US")
    .replace(/[\s‐‑‒–—-]+/gu, " ");
}

function mergeTerminologyCandidates(
  left: TerminologyCandidate,
  right: TerminologyCandidate,
): TerminologyCandidate {
  const representative =
    candidateSourcePriority[left.source] <= candidateSourcePriority[right.source]
      ? left
      : right;
  const supplement = representative === left ? right : left;
  const sources = Array.from(
    new Set([
      ...(left.sources ?? [left.source]),
      ...(right.sources ?? [right.source]),
    ]),
  ).sort((a, b) => candidateSourcePriority[a] - candidateSourcePriority[b]);
  const similarities = [left.similarity, right.similarity].filter(
    (value): value is number => value !== null,
  );
  const selectionGroupIds = Array.from(
    new Set([...(left.selectionGroupIds ?? []), ...(right.selectionGroupIds ?? [])]),
  );
  return {
    ...representative,
    cui: representative.cui ?? supplement.cui,
    semanticType: representative.semanticType ?? supplement.semanticType,
    similarity: similarities.length > 0 ? Math.max(...similarities) : null,
    alreadyApplied: left.alreadyApplied === true || right.alreadyApplied === true,
    ...(sources.length > 1 ? { sources } : {}),
    ...(selectionGroupIds.length > 0 ? { selectionGroupIds } : {}),
  };
}

function consolidateTerminologyCandidates(
  candidates: TerminologyCandidate[],
): TerminologyCandidate[] {
  const output: TerminologyCandidate[] = [];
  const indexesByConcept = new Map<string, number[]>();
  candidates.forEach((candidate) => {
    const concept = normalizedCandidateConcept(candidate);
    const indexes = indexesByConcept.get(concept) ?? [];
    const compatibleIndex = indexes.find((index) => {
      const existing = output[index];
      return !existing.cui || !candidate.cui || existing.cui === candidate.cui;
    });
    if (compatibleIndex !== undefined) {
      output[compatibleIndex] = mergeTerminologyCandidates(
        output[compatibleIndex],
        candidate,
      );
      return;
    }
    indexes.push(output.length);
    indexesByConcept.set(concept, indexes);
    output.push(candidate);
  });
  return output;
}

export function workflowDraftToFieldProvenance(
  workflow: Pick<
    ClinicalRecordWorkflowResponse,
    "api3" | "query_expansion" | "draft"
  >,
): FieldProvenanceMap {
  const segments = new Map(
    workflow.api3.segments.map((segment) => [String(segment.id), segment]),
  );
  const translations = new Map(
    (workflow.query_expansion?.translated_segments ?? []).map((segment) => [
      String(segment.segment_id),
      segment.translated_text_en,
    ]),
  );
  const output: FieldProvenanceMap = {};
  const fields = workflow.draft.fields as unknown as Record<string, ClinicalDraftField>;

  const ensureField = (clinicalFieldId: string): FieldProvenance | null => {
    const fieldKey = recordFieldKeyByClinicalId[clinicalFieldId];
    if (!fieldKey) return null;
    output[fieldKey] ??= { fieldKey, evidence: [], candidates: [] };
    return output[fieldKey] ?? null;
  };

  const addEvidence = (
    fieldId: string,
    segmentId: string | number | undefined,
    fallback?: ClinicalDraftReviewItem,
  ) => {
    if (segmentId === undefined) return;
    const target = ensureField(fieldId);
    if (!target || target.evidence.some((item) => item.segmentId === String(segmentId))) return;
    const segment = segments.get(String(segmentId));
    const raw = segment?.raw_text ?? fallback?.evidence ?? "";
    if (!raw) return;
    const correctedText = segment?.corrected_text?.trim();
    const corrected = correctedText && correctedText !== raw ? correctedText : null;
    const translated = translations.get(String(segmentId));
    const field = fields[fieldId];
    const evidence: PatientEvidence = {
      segmentId: String(segmentId),
      timestamp: evidenceTimestamp(
        segment?.start ?? fallback?.evidence_start,
        segment?.end ?? fallback?.evidence_end,
      ),
      speaker: segment?.speaker?.trim() || "화자 미지정",
      raw,
      corrected,
      ...(translated ? { translated } : {}),
      appliedValue: field?.value ?? "",
    };
    target.evidence.push(evidence);
  };

  Object.entries(fields).forEach(([fieldId, field]) => {
    field.evidence.forEach((evidence) =>
      addEvidence(fieldId, evidence.source_segment_id ?? evidence.segment_id),
    );
    const target = ensureField(fieldId);
    field.applied_candidates.forEach((candidate, index) => {
      target?.candidates.push(appliedCandidate(fieldId, index, candidate));
    });
  });

  workflow.draft.review_items.forEach((item) => {
    const target = ensureField(item.field_id);
    if (!target) return;
    addEvidence(item.field_id, item.segment_id, item);
    const candidates = (item.candidate_provenance ?? [])
      .map((provenance, index) => reviewCandidate(item, index, provenance))
      .filter((candidate): candidate is TerminologyCandidate => candidate !== null);
    if (candidates.length > 0) {
      target.candidates.push(...candidates);
    }
  });

  Object.values(output).forEach((field) => {
    if (!field) return;
    field.candidates = consolidateTerminologyCandidates(field.candidates);
  });

  return Object.fromEntries(
    Object.entries(output).filter(
      ([, field]) => field && (field.evidence.length > 0 || field.candidates.length > 0),
    ),
  ) as FieldProvenanceMap;
}
