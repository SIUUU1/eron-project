import assert from "node:assert/strict";
import test from "node:test";

import { ApiError } from "../src/api/client.ts";
import {
  clinicalDraftErrorMessage,
  clinicalDraftPartialMessage,
  createClinicalRecordDraft,
  dialogueToWhisperDraftRequest,
  workflowDraftToEmergencyRecord,
} from "../src/api/clinical-records.ts";

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
        drug_allergy: field("drug_allergy", "미확인"),
        social_history: field("social_history", "비흡연"),
        review_of_systems: field("review_of_systems", "호흡곤란 (+)"),
        physical_examination: field("physical_examination", "Tachypnea"),
        treatment_plan: field("treatment_plan", "산소 적용 및 모니터링"),
        impression: field("impression", "폐렴 의증"),
        outcome: field("outcome", "진료 진행 중"),
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
    outcome: "진료 진행 중",
  });
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
