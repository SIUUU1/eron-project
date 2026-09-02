import assert from "node:assert/strict";
import test from "node:test";

import { ApiError } from "../src/api/client.ts";
import {
  clinicalAudioTranscriptionErrorMessage,
  clinicalDraftErrorMessage,
  clinicalDraftPartialMessage,
  createClinicalRecordDraft,
  createClinicalRecordDraftFromAudio,
  dialogueToWhisperDraftRequest,
  parseWhisperDraftJson,
  transcribeClinicalRecordAudio,
  whisperDraftToDialogue,
  workflowDraftToEmergencyRecord,
  workflowDraftToFieldProvenance,
  workflowDraftToFieldStatuses,
} from "../src/api/clinical-records.ts";
import { applyTerminologyCandidateDecision } from "../src/lib/clinical-provenance.ts";
import {
  normalizeClinicalRecordOutcome,
  outcomeOptions,
} from "../src/lib/clinical-record-outcome.ts";

test("의료진이 선택한 후보는 초안 원문을 유지하고 새 줄에 추가한다", () => {
  const candidate = {
    id: "seg_1:0:candidate:0",
    selectionGroupId: "seg_1:0",
    query: "tiotropium",
    canonicalValue: "Tiotropium",
    selectionGroupIds: ["seg_1:0"],
    source: "UMLS",
    cui: "C0040165",
    semanticType: "T121",
    similarity: 0.96,
  };

  const result = applyTerminologyCandidateDecision(
    "디오트로피움과 살부타몰 복용 중",
    candidate,
    null,
    "selected",
  );

  assert.deepEqual(result, {
    value: "디오트로피움과 살부타몰 복용 중\nTiotropium",
    changed: true,
  });
});

test("다른 후보를 선택하거나 제외하면 추가된 후보 줄만 교체하거나 제거한다", () => {
  const tiotropium = {
    id: "seg_1:0:candidate:0",
    query: "tiotropium",
    canonicalValue: "Tiotropium",
    selectionGroupIds: ["seg_1:0"],
    source: "UMLS",
    cui: "C0040165",
    semanticType: "T121",
    similarity: 0.96,
  };
  const tiotropiumBromide = {
    ...tiotropium,
    id: "seg_1:0:candidate:1",
    canonicalValue: "Tiotropium bromide",
  };

  const changed = applyTerminologyCandidateDecision(
    "디오트로피움과 살부타몰 복용 중\nTiotropium",
    tiotropiumBromide,
    tiotropium,
    "selected",
  );
  const restored = applyTerminologyCandidateDecision(
    changed.value,
    tiotropiumBromide,
    tiotropiumBromide,
    "excluded",
  );

  assert.equal(changed.value, "디오트로피움과 살부타몰 복용 중\nTiotropium bromide");
  assert.equal(restored.value, "디오트로피움과 살부타몰 복용 중");
});

test("의료진이 초안을 수정한 뒤에도 기존 내용을 보존하고 후보만 추가한다", () => {
  const candidate = {
    id: "seg_1:0:candidate:0",
    query: "tiotropium",
    canonicalValue: "Tiotropium",
    selectionGroupIds: ["seg_1:0"],
    source: "UMLS",
    cui: "C0040165",
    semanticType: "T121",
    similarity: 0.96,
  };

  const result = applyTerminologyCandidateDecision(
    "의료진 메모: 디오트로피움 직접 확인",
    candidate,
    null,
    "selected",
  );

  assert.deepEqual(result, {
    value: "의료진 메모: 디오트로피움 직접 확인\nTiotropium",
    changed: true,
  });
});

test("Whisper JSON의 원문 segment와 추가 필드를 변경하지 않고 읽는다", () => {
  const source = {
    language: "ko",
    processing_metadata: { model: "whisper-turbo" },
    segments: [
      {
        id: "seg_original_001",
        start: 30.43,
        end: 32.99,
        text: "어큐트 앵글 클로저 글루코마 가능성 있습니다.",
        speaker: "SPEAKER_00",
        words: [{ word: "어큐트", start: 30.43, end: 30.81 }],
      },
    ],
  };

  assert.deepEqual(parseWhisperDraftJson(JSON.stringify(source)), source);
});

test("형식이 잘못된 Whisper segment는 파일 입력 단계에서 거부한다", () => {
  assert.throws(
    () =>
      parseWhisperDraftJson(
        JSON.stringify({
          segments: [
            { id: "seg_1", start: 2, end: 1, text: "첫 문장" },
            { id: "seg_1", start: 2, end: 3, text: "중복 ID" },
          ],
        }),
      ),
    /Whisper segment/,
  );
});

test("Whisper 화자명은 화면 표시에서도 유지하고 누락된 화자만 구분한다", () => {
  assert.deepEqual(
    whisperDraftToDialogue({
      segments: [
        { id: "seg_1", start: 1, end: 2, text: "첫 문장", speaker: "SPEAKER_00" },
        { id: "seg_2", start: 2, end: 3, text: "둘째 문장" },
      ],
    }),
    [
      { speaker: "SPEAKER_00", text: "첫 문장" },
      { speaker: "화자 미확인", text: "둘째 문장" },
    ],
  );
});

test("ClinicalNLP 초안의 모든 응급기록 필드를 화면 기록으로 변환한다", () => {
  const field = (fieldId, value) => ({ field_id: fieldId, value });
  const workflow = {
    draft: {
      fields: {
        chief_complaint: field("chief_complaint", "기침"),
        pain_assessment: field("pain_assessment", "NRS 3"),
        history_of_present_illness: field("history_of_present_illness", "3일 전부터 악화"),
        past_history: field("past_history", "고혈압"),
        medications: field("medications", "암로디핀 확인 필요"),
        allergy: field("allergy", "미확인"),
        social_history: field("social_history", "비흡연"),
        review_of_systems: field("review_of_systems", "호흡곤란 (+)"),
        physical_examination: field("physical_examination", "Tachypnea"),
        treatment_plan: field("treatment_plan", "산소 적용 및 모니터링"),
        impression: field("impression", "폐렴 의증"),
        outcome: field("outcome", "입원"),
      },
    },
  };

  assert.deepEqual(workflowDraftToEmergencyRecord(workflow), {
    chiefComplaint: "기침",
    painAssessment: "NRS 3",
    presentIllness: "3일 전부터 악화",
    pastHistory: "고혈압",
    medication: "암로디핀 확인 필요",
    allergy: "미확인",
    socialHistory: "비흡연",
    systemReview: "호흡곤란 (+)",
    physicalExam: "Tachypnea",
    treatmentPlan: "산소 적용 및 모니터링",
    impression: "폐렴 의증",
    outcome: "입원",
  });
});

test("응급진료결과는 고정 선택지만 반영하고 미확인 값은 선택하지 않는다", () => {
  assert.deepEqual(outcomeOptions, ["귀가", "입원", "전원", "사망", "기타"]);
  const workflow = {
    draft: {
      fields: {
        chief_complaint: { value: "" },
        pain_assessment: { value: "" },
        history_of_present_illness: { value: "" },
        past_history: { value: "" },
        medications: { value: "" },
        drug_allergy: { value: "" },
        social_history: { value: "" },
        review_of_systems: { value: "" },
        physical_examination: { value: "" },
        treatment_plan: { value: "" },
        impression: { value: "" },
        outcome: { value: "진료 진행 중" },
      },
    },
  };

  assert.equal(workflowDraftToEmergencyRecord(workflow).outcome, "");
  assert.equal(workflowDraftToFieldStatuses(workflow).outcome, "missing");
  workflow.draft.fields.outcome.value = "";
  assert.equal(workflowDraftToEmergencyRecord(workflow).outcome, "");
  assert.equal(workflowDraftToFieldStatuses(workflow).outcome, "missing");
  workflow.draft.fields.outcome.value = "입원";
  assert.equal(workflowDraftToFieldStatuses(workflow).outcome, "complete");
});

test("응급진료결과의 영문 카테고리와 상세정보를 UI 선택값으로 정규화한다", () => {
  assert.equal(normalizeClinicalRecordOutcome("입원"), "입원");
  assert.equal(normalizeClinicalRecordOutcome("Admission"), "입원");
  assert.equal(normalizeClinicalRecordOutcome("admission"), "입원");
  assert.equal(normalizeClinicalRecordOutcome("Admission (surgery)"), "입원");
  assert.equal(normalizeClinicalRecordOutcome("입원 - 외과"), "입원");
  assert.equal(normalizeClinicalRecordOutcome("Discharge"), "귀가");
  assert.equal(normalizeClinicalRecordOutcome(""), "");
  assert.equal(normalizeClinicalRecordOutcome("환자가 퇴원을 원함"), "");
});

test("ClinicalNLP의 구조화 상태를 응급기록 필드 상태로 변환한다", () => {
  const field = (fieldId, informationStatus, suggestionStatus = "UNCHANGED") => ({
    field_id: fieldId,
    value: `${fieldId} 값`,
    information_status: informationStatus,
    suggestion_status: suggestionStatus,
  });
  const workflow = {
    draft: {
      fields: {
        chief_complaint: field("chief_complaint", "PRESENT"),
        pain_assessment: field("pain_assessment", "NONE"),
        history_of_present_illness: field("history_of_present_illness", "NOT_ASSESSED"),
        past_history: field("past_history", "UNCERTAIN"),
        medications: field("medications", "PRESENT", "UNRESOLVED"),
        allergy: field("allergy", "NONE"),
        social_history: field("social_history", "PRESENT"),
        review_of_systems: field("review_of_systems", "PRESENT"),
        physical_examination: field("physical_examination", "PRESENT"),
        treatment_plan: field("treatment_plan", "PRESENT"),
        impression: field("impression", "PRESENT"),
        outcome: { ...field("outcome", "PRESENT"), value: "입원" },
      },
    },
  };

  assert.deepEqual(workflowDraftToFieldStatuses(workflow), {
    chiefComplaint: "complete",
    painAssessment: "complete",
    presentIllness: "missing",
    pastHistory: "review",
    medication: "review",
    allergy: "complete",
    socialHistory: "complete",
    systemReview: "complete",
    physicalExam: "complete",
    treatmentPlan: "complete",
    impression: "complete",
    outcome: "complete",
  });
});

test("ClinicalNLP 응답의 대화 근거와 검색 후보를 응급기록 필드별로 연결한다", () => {
  const field = (fieldId) => ({
    field_id: fieldId,
    value: "",
    information_status: "PRESENT",
    suggestion_status: "UNCHANGED",
    evidence: [],
    applied_candidates: [],
  });
  const fields = {
    chief_complaint: field("chief_complaint"),
    pain_assessment: field("pain_assessment"),
    history_of_present_illness: field("history_of_present_illness"),
    past_history: field("past_history"),
    medications: field("medications"),
    allergy: field("allergy"),
    social_history: field("social_history"),
    review_of_systems: field("review_of_systems"),
    physical_examination: field("physical_examination"),
    treatment_plan: field("treatment_plan"),
    impression: field("impression"),
    outcome: field("outcome"),
  };
  fields.chief_complaint.value = "호흡곤란";
  fields.chief_complaint.evidence = [{ source_segment_id: "seg_1" }];
  fields.chief_complaint.applied_candidates = [
    {
      collection: "symptom_terms",
      entity_id: "symptom:dyspnea",
      display_value: "호흡곤란",
      source: "RAW_EXACT",
    },
  ];
  fields.impression.value = "급성 폐쇄각 녹내장 가능성";
  fields.impression.evidence = [{ source_segment_id: "seg_2" }];
  fields.medications.value = "암로디핀 확인 필요";

  const result = workflowDraftToFieldProvenance({
    query_expansion: {
      translated_segments: [
        {
          segment_id: "seg_1",
          translated_text_en: "I am short of breath.",
        },
      ],
    },
    api3: {
      segments: [
        {
          id: "seg_1",
          start: 12.4,
          end: 15.8,
          speaker: "환자",
          raw_text: "숨이 차요.",
          corrected_text: "숨이 차요.",
        },
        {
          id: "seg_2",
          start: 30.43,
          end: 32.99,
          speaker: "의료진",
          raw_text: "어큐트 앵글 클로저 글루코마 가능성입니다.",
          corrected_text: "Acute angle-closure glaucoma 가능성입니다.",
        },
      ],
    },
    draft: {
      fields,
      review_items: [
        {
          id: "seg_2:0",
          field_id: "impression",
          segment_id: "seg_2",
          source: "어큐트 앵글 클로저 글루코마",
          evidence: "어큐트 앵글 클로저 글루코마 가능성입니다.",
          evidence_start: 30.43,
          evidence_end: 32.99,
          search_terms_en: ["acute angle closure glaucoma"],
          candidate_provenance: [
            {
              display_value: "Acute angle-closure glaucoma",
              source: "UMLS",
              cui: "C0154946",
              semantic_types: ["T047"],
              similarity: 0.91,
            },
            {
              display_value: "Angle-closure glaucoma",
              source: "NGRAM_FALLBACK",
              cui: null,
              semantic_types: [],
              similarity: 0.83,
            },
            {
              display_value: "Acute angle closure glaucoma",
              source: "NGRAM_FALLBACK",
              cui: null,
              semantic_types: [],
              similarity: 0.86,
            },
          ],
          needs_review: true,
        },
        {
          id: "seg_3:0",
          field_id: "medications",
          segment_id: "seg_3",
          source: "암로디핀",
          evidence: "암로디핀을 복용합니다.",
          evidence_start: 45,
          evidence_end: 48,
          search_terms_en: ["amlodipine"],
          candidate_provenance: [],
          candidates: [],
          needs_review: true,
        },
      ],
    },
  });

  assert.deepEqual(result.chiefComplaint, {
    fieldKey: "chiefComplaint",
    evidence: [
      {
        segmentId: "seg_1",
        timestamp: "00:12.40–00:15.80",
        speaker: "환자",
        raw: "숨이 차요.",
        corrected: null,
        translated: "I am short of breath.",
        appliedValue: "호흡곤란",
      },
    ],
    candidates: [
      {
        id: "chief_complaint:applied:0",
        query: "호흡곤란",
        canonicalValue: "호흡곤란",
        source: "RAW_EXACT",
        cui: null,
        semanticType: null,
        similarity: null,
        alreadyApplied: true,
      },
    ],
  });
  assert.deepEqual(
    result.impression.candidates.map((candidate) => ({
      canonicalValue: candidate.canonicalValue,
      source: candidate.source,
      cui: candidate.cui,
      semanticType: candidate.semanticType,
      similarity: candidate.similarity,
      sources: candidate.sources,
    })),
    [
      {
        canonicalValue: "Acute angle-closure glaucoma",
        source: "UMLS",
        cui: "C0154946",
        semanticType: "T047",
        similarity: 0.91,
        sources: ["UMLS", "NGRAM_FALLBACK"],
      },
      {
        canonicalValue: "Angle-closure glaucoma",
        source: "NGRAM_FALLBACK",
        cui: null,
        semanticType: null,
        similarity: 0.83,
        sources: undefined,
      },
    ],
  );
  assert.deepEqual(result.impression.candidates[0].selectionGroupIds, ["seg_2:0"]);
  assert.deepEqual(result.medication.candidates, []);
});

test("초안 API 오류를 의료진이 이해할 수 있는 안내로 구분한다", () => {
  assert.deepEqual(
    [400, 502, 503, 504, 0].map((status) =>
      clinicalDraftErrorMessage(new ApiError(status, "internal_error")),
    ),
    [
      "대화 데이터 형식을 확인해 주세요.",
      "AI 응답 형식을 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.",
      "임상 초안 서비스를 사용할 수 없습니다.",
      "응급기록 초안 생성 시간이 초과되었습니다. 다시 시도해 주세요.",
      "서버에 연결할 수 없습니다.",
    ],
  );
});

test("partial 응답은 생성된 초안을 버리지 않고 경고 문구를 만든다", () => {
  assert.equal(
    clinicalDraftPartialMessage({
      processing_status: "partial",
      errors: [{ message: "translation unavailable" }, { message: "fallback used" }],
    }),
    "일부 처리 단계가 완료되지 않았습니다. 확인이 필요한 항목이 2건 있습니다.",
  );
  assert.equal(clinicalDraftPartialMessage({ processing_status: "completed", errors: [] }), null);
});

test("화면 대화를 원문을 바꾸지 않고 Whisper segments 계약으로 변환한다", () => {
  assert.deepEqual(
    dialogueToWhisperDraftRequest([
      { speaker: "의료진", text: "어디가 불편하세요?" },
      { speaker: "환자", text: "기침이 심해졌어요." },
    ]),
    {
      segments: [
        {
          id: "ui_seg_0001",
          start: 0,
          end: 1,
          text: "어디가 불편하세요?",
          speaker: "의료진",
        },
        {
          id: "ui_seg_0002",
          start: 1,
          end: 2,
          text: "기침이 심해졌어요.",
          speaker: "환자",
        },
      ],
    },
  );
});

test("응급기록 초안 API에 Whisper JSON을 POST하고 응답을 보존한다", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => {
    globalThis.fetch = originalFetch;
  });

  const request = {
    segments: [{ id: "seg_0001", start: 0, end: 1, text: "기침이 심해졌어요." }],
  };
  const workflow = {
    schema_version: "clinical-workflow-v2",
    processing_status: "partial",
    record_status: "DRAFT",
    errors: [{ message: "Query expansion unavailable" }],
  };
  let receivedUrl;
  let receivedInit;
  globalThis.fetch = async (url, init) => {
    receivedUrl = url;
    receivedInit = init;
    return new Response(JSON.stringify(workflow), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  const result = await createClinicalRecordDraft(request);

  assert.equal(receivedUrl, "/api/clinical-records/draft");
  assert.equal(receivedInit.method, "POST");
  assert.equal(receivedInit.headers.Accept, "application/json");
  assert.equal(receivedInit.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(receivedInit.body), request);
  assert.deepEqual(result, workflow);
});

test("STT API 오류를 음성 인식 단계에 맞는 안내로 구분한다", () => {
  assert.deepEqual(
    [400, 413, 502, 503, 504, 0].map((status) =>
      clinicalAudioTranscriptionErrorMessage(new ApiError(status, "internal_error")),
    ),
    [
      "음성 파일을 읽을 수 없습니다.",
      "음성 파일은 25MB 이하여야 합니다.",
      "음성 인식 결과를 확인하지 못했습니다. 다시 시도해 주세요.",
      "음성 인식 서비스를 사용할 수 없습니다.",
      "음성 인식 시간이 초과되었습니다. 다시 시도해 주세요.",
      "서버에 연결할 수 없습니다.",
    ],
  );
});

test("음성 파일은 multipart로 STT 통합 초안 API에 직접 전달한다", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => {
    globalThis.fetch = originalFetch;
  });

  const audio = new File(["synthetic-audio"], "synthetic.wav", { type: "audio/wav" });
  const workflow = {
    schema_version: "clinical-workflow-v2",
    processing_status: "completed",
    record_status: "DRAFT",
    errors: [],
  };
  let receivedUrl;
  let receivedInit;
  globalThis.fetch = async (url, init) => {
    receivedUrl = url;
    receivedInit = init;
    return new Response(JSON.stringify(workflow), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  const result = await createClinicalRecordDraftFromAudio(audio);

  assert.equal(receivedUrl, "/api/clinical-records/draft/audio");
  assert.equal(receivedInit.method, "POST");
  assert.equal(receivedInit.headers.Accept, "application/json");
  assert.equal(receivedInit.headers["Content-Type"], undefined);
  assert.equal(receivedInit.body.get("audio"), audio);
  assert.deepEqual(result, workflow);
});

test("음성 파일은 STT 전용 API에서 Whisper segment를 받아 대화 입력으로 사용한다", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => {
    globalThis.fetch = originalFetch;
  });

  const audio = new File(["synthetic-audio"], "synthetic.wav", { type: "audio/wav" });
  const whisperPayload = {
    api_version: "v1",
    status: "completed",
    language: "ko",
    segments: [
      {
        id: "seg_0001",
        start: 0,
        end: 1.5,
        text: "합성 흉통 문장",
        speaker: "SPEAKER_00",
      },
    ],
  };
  let receivedUrl;
  let receivedInit;
  globalThis.fetch = async (url, init) => {
    receivedUrl = url;
    receivedInit = init;
    return new Response(JSON.stringify(whisperPayload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  const result = await transcribeClinicalRecordAudio(audio);

  assert.equal(receivedUrl, "/api/clinical-records/transcribe");
  assert.equal(receivedInit.method, "POST");
  assert.equal(receivedInit.headers.Accept, "application/json");
  assert.equal(receivedInit.headers["Content-Type"], undefined);
  assert.equal(receivedInit.body.get("audio"), audio);
  assert.deepEqual(result, whisperPayload);
  assert.deepEqual(whisperDraftToDialogue(result), [
    { speaker: "SPEAKER_00", text: "합성 흉통 문장" },
  ]);
});
