import { createFileRoute, Link, notFound } from "@tanstack/react-router";
import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  Brain,
  Check,
  CheckCircle2,
  CircleDashed,
  FileCheck2,
  Info,
  Loader2,
  Mic,
  Pause,
  Save,
  Sparkles,
  Square,
  Stethoscope,
  Upload,
  XCircle,
} from "lucide-react";
import { type ChangeEvent, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import {
  clinicalDraftErrorMessage,
  clinicalDraftPartialMessage,
  createClinicalRecordDraft,
  dialogueToWhisperDraftRequest,
  parseWhisperDraftJson,
  whisperDraftToDialogue,
  workflowDraftToEmergencyRecord,
  type DraftDialogueTurn,
} from "@/api/clinical-records";
import type { WhisperDraftRequest } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Progress } from "@/components/ui/progress";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Textarea } from "@/components/ui/textarea";
import {
  checkStatusMeta,
  currentUser,
  emptyRecord,
  followUpQuestions,
  getPatient,
  kcdCandidates,
  outcomeOptions,
  recordFieldLabels,
  sampleDialogue,
  type CheckStatus,
  type EmergencyRecord,
  type RecordFieldKey,
} from "@/lib/mock-data";

export const Route = createFileRoute("/records/$patientId")({
  loader: ({ params }) => {
    const patient = getPatient(params.patientId);
    if (!patient) throw notFound();
    return { patient };
  },
  head: ({ loaderData }) => {
    if (!loaderData) {
      return {
        meta: [{ title: "환자 정보 없음 · ER-GUARD AI" }, { name: "robots", content: "noindex" }],
      };
    }
    const p = loaderData.patient;
    const title = `${p.name} AI 응급진료기록 · ER-GUARD AI`;
    const desc = `${p.name} 환자의 대화 기반 응급진료기록 작성, 누락 검사, KCD 코드 추천 및 의사 인증 워크플로우.`;
    return {
      meta: [
        { title },
        { name: "description", content: desc },
        { property: "og:title", content: title },
        { property: "og:description", content: desc },
      ],
    };
  },
  component: RecordWorkflowPage,
});

const steps = ["기록 작성", "누락 검사", "진단코드", "최종 기록", "인증 저장"];

const fieldOrder: RecordFieldKey[] = [
  "chiefComplaint",
  "painAssessment",
  "presentIllness",
  "pastHistory",
  "medication",
  "allergy",
  "socialHistory",
  "systemReview",
  "physicalExam",
  "treatmentPlan",
  "impression",
  "outcome",
];

const requiredFields: RecordFieldKey[] = [
  "chiefComplaint",
  "painAssessment",
  "presentIllness",
  "pastHistory",
  "medication",
  "allergy",
  "socialHistory",
  "systemReview",
  "physicalExam",
  "outcome",
];

function statusOf(value: string): CheckStatus {
  const v = value.trim();
  if (v === "" || v === "미확인") return "missing";
  if (v.includes("확인 필요")) return "review";
  return "complete";
}

const StatusIcon = ({ status }: { status: CheckStatus }) =>
  status === "complete" ? (
    <CheckCircle2 className="size-4" />
  ) : status === "review" ? (
    <AlertCircle className="size-4" />
  ) : (
    <XCircle className="size-4" />
  );

function RecordWorkflowPage() {
  const { patient } = Route.useLoaderData();

  const [step, setStep] = useState(1);
  const [dialogue, setDialogue] = useState<DraftDialogueTurn[]>([]);
  const [uploadedWhisperPayload, setUploadedWhisperPayload] =
    useState<WhisperDraftRequest | null>(null);
  const [uploadedWhisperFileName, setUploadedWhisperFileName] = useState<string | null>(null);
  const [recording, setRecording] = useState<"idle" | "on" | "paused">("idle");
  const [generating, setGenerating] = useState(false);
  const [record, setRecord] = useState<EmergencyRecord>(emptyRecord);
  const [generated, setGenerated] = useState(false);
  const [generationNotice, setGenerationNotice] = useState<{
    kind: "partial" | "error";
    message: string;
  } | null>(null);

  const [checking, setChecking] = useState(false);
  const [checked, setChecked] = useState(false);
  const [highlight, setHighlight] = useState<RecordFieldKey | null>(null);
  const [blockOpen, setBlockOpen] = useState(false);

  const [selectedCode, setSelectedCode] = useState<string | null>(null);
  const [certifyOpen, setCertifyOpen] = useState(false);
  const [agreed, setAgreed] = useState(false);
  const [certifiedAt, setCertifiedAt] = useState<string | null>(null);

  const fieldRefs = useRef<Record<string, HTMLElement | null>>({});
  const whisperFileInputRef = useRef<HTMLInputElement | null>(null);

  const statuses = useMemo(() => {
    const out = {} as Record<RecordFieldKey, CheckStatus>;
    fieldOrder.forEach((k) => (out[k] = statusOf(record[k])));
    return out;
  }, [record]);

  const missingRequired = requiredFields.filter((k) => statuses[k] === "missing");
  const reviewCount = fieldOrder.filter((k) => statuses[k] === "review").length;
  const completeness = generated
    ? Math.max(
        0,
        Math.round(100 - fieldOrder.filter((k) => statuses[k] === "missing").length * 5 - reviewCount * 3.5),
      )
    : 0;

  const selectedCandidate = kcdCandidates.find((c) => c.code === selectedCode);
  const v = patient.vitals;

  const loadSample = () => {
    setDialogue(sampleDialogue);
    setUploadedWhisperPayload(null);
    setUploadedWhisperFileName(null);
    setGenerated(false);
    setGenerationNotice(null);
    toast.success("샘플 환자-의료진 대화를 불러왔습니다.");
  };

  const loadWhisperJson = async (event: ChangeEvent<HTMLInputElement>) => {
    const input = event.currentTarget;
    const file = input.files?.[0];
    input.value = "";
    if (!file) return;

    try {
      const payload = parseWhisperDraftJson(await file.text());
      if (payload.segments.length === 0) {
        throw new Error("Whisper JSON에 대화 segment가 없습니다.");
      }
      setUploadedWhisperPayload(payload);
      setUploadedWhisperFileName(file.name);
      setDialogue(whisperDraftToDialogue(payload));
      setGenerated(false);
      setChecked(false);
      setGenerationNotice(null);
      toast.success("Whisper JSON을 불러왔습니다.", {
        description: `${payload.segments.length}개 segment의 원문과 화자 정보를 유지합니다.`,
      });
    } catch (error) {
      toast.error("Whisper JSON을 불러오지 못했습니다.", {
        description:
          error instanceof Error ? error.message : "JSON 파일 형식을 확인해 주세요.",
      });
    }
  };

  const generateRecord = async () => {
    if (dialogue.length === 0) {
      toast.error("먼저 대화를 불러오거나 녹음을 진행해 주세요.");
      return;
    }
    setGenerating(true);
    setGenerationNotice(null);
    try {
      const request =
        uploadedWhisperPayload ?? dialogueToWhisperDraftRequest(dialogue);
      const workflow = await createClinicalRecordDraft(request);
      setRecord(workflowDraftToEmergencyRecord(workflow));
      setGenerated(true);
      setChecked(false);
      const partialMessage = clinicalDraftPartialMessage(workflow);
      if (partialMessage) {
        setGenerationNotice({ kind: "partial", message: partialMessage });
        toast.warning("응급진료기록 초안이 일부 생성되었습니다.", {
          description: partialMessage,
        });
      } else {
        toast.success("AI 응급진료기록 초안이 생성되었습니다.", {
          description: "대화에서 확인되지 않은 항목은 공란 또는 미확인으로 남겨두었습니다.",
        });
      }
    } catch (error) {
      const message = clinicalDraftErrorMessage(error);
      setGenerationNotice({ kind: "error", message });
      toast.error("응급진료기록 초안을 생성하지 못했습니다.", { description: message });
    } finally {
      setGenerating(false);
    }
  };

  const runCheck = () => {
    setChecking(true);
    setChecked(false);
    setTimeout(() => {
      setChecking(false);
      setChecked(true);
    }, 1500);
  };

  const focusField = (key: RecordFieldKey) => {
    setHighlight(key);
    const el = fieldRefs.current[key];
    el?.scrollIntoView({ behavior: "smooth", block: "center" });
  };

  const goStep3 = () => {
    if (!checked) {
      toast.error("먼저 AI 기록 완전성 검사를 실행해 주세요.");
      return;
    }
    if (missingRequired.length > 0) {
      setBlockOpen(true);
      return;
    }
    setStep(3);
  };

  const setField = (key: RecordFieldKey, value: string) =>
    setRecord((prev) => ({ ...prev, [key]: value }));

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <Button asChild variant="ghost" size="sm">
          <Link to="/records">
            <ArrowLeft className="size-4" /> 환자 목록
          </Link>
        </Button>
        <Badge variant="outline" className="bg-mint-soft text-navy">
          기록 상태: {certifiedAt ? "의사 인증 완료" : generated ? "작성 중" : patient.recordStatus}
        </Badge>
      </div>

      {/* Step indicator */}
      <Card>
        <CardContent className="flex items-center gap-2 py-4">
          {steps.map((s, i) => {
            const n = i + 1;
            const active = n === step;
            const done = n < step;
            return (
              <div key={s} className="flex flex-1 items-center gap-2">
                <button
                  type="button"
                  onClick={() => n < step && setStep(n)}
                  className={`flex flex-1 items-center gap-2 rounded-md border px-3 py-2 text-sm transition-colors ${
                    active
                      ? "border-primary bg-primary text-primary-foreground shadow-sm"
                      : done
                        ? "border-risk-stable/40 bg-risk-stable-soft text-risk-stable"
                        : "border-border bg-secondary/50 text-muted-foreground"
                  }`}
                >
                  <span
                    className={`flex size-5 shrink-0 items-center justify-center rounded-full text-xs font-bold ${
                      active
                        ? "bg-primary-foreground text-primary"
                        : done
                          ? "bg-risk-stable text-primary-foreground"
                          : "bg-border text-foreground"
                    }`}
                  >
                    {done ? <Check className="size-3" /> : n}
                  </span>
                  <span className="font-semibold">{s}</span>
                </button>
                {n < steps.length && <ArrowRight className="size-4 shrink-0 text-border" />}
              </div>
            );
          })}
        </CardContent>
      </Card>

      {/* 환자 기본 정보 + Vital */}
      <div className="grid grid-cols-[1fr_2fr] gap-4">
        <Card>
          <CardContent className="flex h-full items-center gap-4 py-4">
            <div>
              <p className="text-xs text-muted-foreground">{patient.id}</p>
              <p className="text-xl font-bold">
                {patient.name}
                <span className="ml-2 text-sm font-medium text-muted-foreground">
                  {patient.sex} {patient.age}세
                </span>
              </p>
              <p className="tabular mt-1 text-xs text-muted-foreground">
                KTAS {patient.ktas} · 내원시간 {patient.arrivedAt.slice(11)}
              </p>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="py-3">
            <p className="mb-2 flex items-center gap-1.5 text-xs text-muted-foreground">
              <Stethoscope className="size-3.5" /> EMR 연동 Vital 정보 (AI 생성 아님)
            </p>
            <div className="grid grid-cols-6 gap-2 text-center">
              {[
                ["BP", `${v.sbp}/${v.dbp}`, "mmHg"],
                ["HR", `${v.hr}`, "/min"],
                ["RR", `${v.rr}`, "/min"],
                ["BT", `${v.bt}`, "℃"],
                ["SpO₂", `${v.spo2}`, "%"],
                ["Mental", v.mental, ""],
              ].map(([l, val, u]) => (
                <div key={l} className="rounded-md bg-secondary/50 px-2 py-1.5">
                  <p className="text-[11px] text-muted-foreground">{l}</p>
                  <p className="tabular text-sm font-bold">
                    {val}
                    <span className="ml-0.5 text-[10px] font-normal text-muted-foreground">
                      {u}
                    </span>
                  </p>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* STEP 1 */}
      {step === 1 && (
        <div className="grid grid-cols-2 gap-5">
          <Card>
            <CardHeader className="border-b py-3">
              <CardTitle className="text-base">환자-의료진 대화 기록</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 pt-4">
              <div className="flex flex-wrap gap-2">
                <Button
                  size="sm"
                  variant={recording === "on" ? "default" : "outline"}
                  onClick={() => {
                    setRecording("on");
                    toast.info("녹음을 시작했습니다. (시연용)");
                  }}
                >
                  <Mic className="size-4" /> 녹음 시작
                </Button>
                <Button size="sm" variant="outline" onClick={() => setRecording("paused")}>
                  <Pause className="size-4" /> 녹음 일시정지
                </Button>
                <Button size="sm" variant="outline" onClick={() => setRecording("idle")}>
                  <Square className="size-4" /> 녹음 종료
                </Button>
                <Button size="sm" variant="secondary" onClick={loadSample}>
                  <Sparkles className="size-4" /> 샘플 대화 불러오기
                </Button>
                <input
                  ref={whisperFileInputRef}
                  type="file"
                  accept=".json,application/json"
                  className="hidden"
                  onChange={loadWhisperJson}
                />
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => whisperFileInputRef.current?.click()}
                  disabled={generating}
                >
                  <Upload className="size-4" /> Whisper JSON 불러오기
                </Button>
              </div>
              {uploadedWhisperFileName && uploadedWhisperPayload ? (
                <p className="text-xs text-muted-foreground">
                  입력 파일: {uploadedWhisperFileName} · {uploadedWhisperPayload.segments.length}개
                  segment · 브라우저 메모리에서만 사용
                </p>
              ) : null}
              {recording !== "idle" && (
                <p className="flex items-center gap-2 text-xs text-risk-critical">
                  <span className="size-2 animate-pulse rounded-full bg-risk-critical" />
                  {recording === "on" ? "녹음 중" : "일시정지"} · 실제 음성인식은 시연에서
                  제공되지 않습니다.
                </p>
              )}
              <ScrollArea className="h-[420px] rounded-md border bg-secondary/30 p-3">
                {dialogue.length === 0 ? (
                  <p className="py-20 text-center text-sm text-muted-foreground">
                    대화 기록이 없습니다. “샘플 대화 불러오기”를 눌러 주세요.
                  </p>
                ) : (
                  <ul className="space-y-2">
                    {dialogue.map((t, i) => (
                      <li
                        key={i}
                        className={`flex ${t.speaker === "의료진" ? "justify-start" : "justify-end"}`}
                      >
                        <div
                          className={`max-w-[85%] rounded-lg px-3 py-2 text-sm ${
                            t.speaker === "의료진"
                              ? "bg-card text-foreground shadow-sm"
                              : "bg-primary text-primary-foreground"
                          }`}
                        >
                          <p className="mb-0.5 text-[11px] opacity-70">{t.speaker}</p>
                          {t.text}
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </ScrollArea>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex-row items-center justify-between border-b py-3">
              <CardTitle className="text-base">AI 응급진료기록</CardTitle>
              <Button size="sm" onClick={generateRecord} disabled={generating}>
                {generating ? (
                  <>
                    <Loader2 className="size-4 animate-spin" /> 생성 중
                  </>
                ) : (
                  <>
                    <Brain className="size-4" /> AI 응급기록 생성
                  </>
                )}
              </Button>
            </CardHeader>
            <CardContent className="space-y-3 pt-4">
              <p className="flex gap-1.5 rounded-md bg-secondary px-3 py-2 text-xs text-muted-foreground">
                <Info className="mt-0.5 size-3.5 shrink-0" />
                AI는 대화에서 확인된 내용만 기록합니다. 언급되지 않은 항목은 공란 또는 “미확인”,
                불확실한 항목은 “확인 필요”로 표시되며 임의로 추측하지 않습니다.
              </p>
              {generating ? (
                <p
                  role="status"
                  aria-live="polite"
                  className="flex gap-1.5 rounded-md bg-secondary px-3 py-2 text-xs text-muted-foreground"
                >
                  <Loader2 className="size-3.5 shrink-0 animate-spin" />
                  대화를 분석하여 응급기록 초안을 생성하고 있습니다. 약 40초 정도 걸릴 수 있습니다.
                </p>
              ) : null}
              {generationNotice ? (
                <p
                  role="alert"
                  className={`flex gap-1.5 rounded-md px-3 py-2 text-xs ${
                    generationNotice.kind === "partial"
                      ? "bg-risk-rising-soft text-risk-rising"
                      : "bg-risk-critical-soft text-risk-critical"
                  }`}
                >
                  <AlertCircle className="size-3.5 shrink-0" />
                  {generationNotice.message}
                </p>
              ) : null}
              <ScrollArea className="h-[420px] pr-3">
                <div className="space-y-3">
                  {fieldOrder.map((key) => (
                    <div key={key}>
                      <label className="mb-1 flex items-center justify-between text-xs font-semibold">
                        {recordFieldLabels[key]}
                        {generated && (
                          <Badge variant="outline" className={checkStatusMeta[statuses[key]].badge}>
                            {checkStatusMeta[statuses[key]].label}
                          </Badge>
                        )}
                      </label>
                      {key === "outcome" ? (
                        <Select
                          {...(record.outcome ? { value: record.outcome } : {})}
                          onValueChange={(val) => setField("outcome", val)}
                        >
                          <SelectTrigger>
                            <SelectValue placeholder="선택되지 않음" />
                          </SelectTrigger>
                          <SelectContent>
                            {record.outcome && !outcomeOptions.includes(record.outcome) ? (
                              <SelectItem value={record.outcome}>{record.outcome}</SelectItem>
                            ) : null}
                            {outcomeOptions.map((o) => (
                              <SelectItem key={o} value={o}>
                                {o}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      ) : (
                        <Textarea
                          value={record[key]}
                          rows={key === "presentIllness" || key === "treatmentPlan" ? 3 : 2}
                          placeholder="미확인"
                          onChange={(e) => setField(key, e.target.value)}
                        />
                      )}
                    </div>
                  ))}
                </div>
              </ScrollArea>
              <div className="flex justify-end gap-2 border-t pt-3">
                <Button
                  variant="outline"
                  onClick={() => toast.success("응급진료기록이 임시저장되었습니다.")}
                >
                  <Save className="size-4" /> 임시저장
                </Button>
                <Button
                  onClick={() => {
                    if (!generated) {
                      toast.error("먼저 AI 응급기록을 생성해 주세요.");
                      return;
                    }
                    setStep(2);
                  }}
                >
                  다음: 누락 검사 <ArrowRight className="size-4" />
                </Button>
              </div>
            </CardContent>
          </Card>
        </div>
      )}

      {/* STEP 2 */}
      {step === 2 && (
        <div className="space-y-4">
          <Card>
            <CardContent className="flex items-center gap-6 py-4">
              <Button onClick={runCheck} disabled={checking}>
                {checking ? (
                  <>
                    <Loader2 className="size-4 animate-spin" /> AI가 기록을 검토하고 있습니다
                  </>
                ) : (
                  <>
                    <FileCheck2 className="size-4" /> AI 기록 완전성 검사
                  </>
                )}
              </Button>
              <div className="flex-1">
                <div className="mb-1 flex items-center justify-between text-xs">
                  <span className="text-muted-foreground">기록 완성도 (시연용 지표)</span>
                  <span className="tabular font-bold">{checked ? completeness : 0}%</span>
                </div>
                <Progress value={checked ? completeness : 0} />
              </div>
              {checked && (
                <div className="flex gap-4 text-xs">
                  {(["complete", "review", "missing"] as CheckStatus[]).map((s) => (
                    <span key={s} className={`flex items-center gap-1 ${checkStatusMeta[s].text}`}>
                      <StatusIcon status={s} />
                      {checkStatusMeta[s].label}{" "}
                      {fieldOrder.filter((k) => statuses[k] === s).length}
                    </span>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          {!checked && !checking && (
            <Card>
              <CardContent className="py-16 text-center text-sm text-muted-foreground">
                <CircleDashed className="mx-auto mb-3 size-8 opacity-40" />
                “AI 기록 완전성 검사”를 실행하면 주호소와 기록 내용을 기준으로 추가 확인이 필요한
                임상정보를 찾아 표시합니다.
              </CardContent>
            </Card>
          )}

          {checking && (
            <Card>
              <CardContent className="py-16 text-center">
                <Loader2 className="mx-auto mb-3 size-8 animate-spin text-primary" />
                <p className="text-sm font-medium">AI가 응급진료기록을 검토하고 있습니다…</p>
                <p className="mt-1 text-xs text-muted-foreground">
                  주호소(흉통) 기준 필수 임상정보 항목을 대조하는 중입니다.
                </p>
              </CardContent>
            </Card>
          )}

          {checked && (
            <div className="grid grid-cols-[1fr_360px] gap-5">
              <Card>
                <CardHeader className="border-b py-3">
                  <CardTitle className="text-base">검사 결과 및 누락 항목 보완</CardTitle>
                </CardHeader>
                <CardContent className="space-y-3 pt-4">
                  {fieldOrder.map((key) => {
                    const st = statuses[key];
                    return (
                      <div
                        key={key}
                        ref={(el) => {
                          fieldRefs.current[key] = el;
                        }}
                        className={`rounded-md border px-3 py-2.5 transition-colors ${
                          highlight === key
                            ? "border-primary bg-accent/40 ring-2 ring-primary/40"
                            : "border-border"
                        }`}
                      >
                        <div className="mb-1.5 flex items-center justify-between">
                          <span className="text-sm font-semibold">{recordFieldLabels[key]}</span>
                          <Badge variant="outline" className={checkStatusMeta[st].badge}>
                            <span className="mr-1">
                              <StatusIcon status={st} />
                            </span>
                            {checkStatusMeta[st].label}
                          </Badge>
                        </div>
                        {st === "complete" ? (
                          <p className="text-sm text-muted-foreground">{record[key]}</p>
                        ) : key === "outcome" ? (
                          <Select
                            {...(record.outcome ? { value: record.outcome } : {})}
                            onValueChange={(val) => setField("outcome", val)}
                          >
                            <SelectTrigger>
                              <SelectValue placeholder="응급진료결과를 선택하세요" />
                            </SelectTrigger>
                            <SelectContent>
                              {outcomeOptions.map((o) => (
                                <SelectItem key={o} value={o}>
                                  {o}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        ) : (
                          <Textarea
                            rows={2}
                            value={record[key] === "미확인" ? "" : record[key]}
                            placeholder="의료진이 확인한 내용을 직접 입력하세요"
                            onChange={(e) => setField(key, e.target.value)}
                          />
                        )}
                      </div>
                    );
                  })}
                </CardContent>
              </Card>

              <div className="space-y-4">
                <Card>
                  <CardHeader className="border-b py-3">
                    <CardTitle className="flex items-center gap-2 text-sm">
                      <Brain className="size-4 text-primary" /> AI 추천 추가 질문
                      <Badge variant="outline" className="ml-auto bg-mint-soft text-navy">
                        AI
                      </Badge>
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-3 pt-4">
                    {followUpQuestions.map((q) => (
                      <div key={q.question} className="rounded-md border bg-secondary/40 p-3">
                        <p className="text-sm">{q.question}</p>
                        <Button
                          size="sm"
                          variant="outline"
                          className="mt-2 w-full"
                          onClick={() => {
                            focusField(q.field);
                            toast.info(`${recordFieldLabels[q.field]} 항목을 입력해 주세요.`);
                          }}
                        >
                          기록에 반영
                        </Button>
                      </div>
                    ))}
                  </CardContent>
                </Card>

                <Card>
                  <CardContent className="space-y-3 py-4">
                    <p className="text-xs text-muted-foreground">
                      필수 누락 항목 {missingRequired.length}건이 남아 있습니다.
                    </p>
                    <div className="flex gap-2">
                      <Button variant="outline" className="flex-1" onClick={() => setStep(1)}>
                        이전
                      </Button>
                      <Button className="flex-1" onClick={goStep3}>
                        다음: 진단코드 추천
                      </Button>
                    </div>
                  </CardContent>
                </Card>
              </div>
            </div>
          )}
        </div>
      )}

      {/* STEP 3 */}
      {step === 3 && (
        <Card>
          <CardHeader className="border-b py-3">
            <CardTitle className="text-base">AI 진단코드 추천</CardTitle>
            <p className="text-xs text-muted-foreground">
              작성된 응급진료기록과 추정진단을 기반으로 KCD-9차 진단코드 후보를 추천합니다.
            </p>
          </CardHeader>
          <CardContent className="space-y-4 pt-4">
            <div className="rounded-md bg-secondary/50 px-4 py-2.5 text-sm">
              <span className="text-muted-foreground">추정진단 · </span>
              <span className="font-semibold">{record.impression || "미확인"}</span>
            </div>
            <div className="grid grid-cols-3 gap-4">
              {kcdCandidates.map((c) => {
                const active = selectedCode === c.code;
                return (
                  <div
                    key={c.code}
                    className={`flex flex-col rounded-lg border p-4 transition-all ${
                      active
                        ? "border-primary bg-accent/40 shadow-md ring-2 ring-primary/30"
                        : "border-border bg-card"
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <Badge variant="outline">후보 {c.rank}</Badge>
                      {active && (
                        <Badge className="bg-primary text-primary-foreground">
                          <Check className="mr-1 size-3" /> 선택됨
                        </Badge>
                      )}
                    </div>
                    <p className="mt-3 text-lg font-bold">{c.name}</p>
                    <p className="mt-1 font-mono text-2xl font-bold text-primary">{c.code}</p>
                    <div className="mt-3">
                      <div className="mb-1 flex justify-between text-xs">
                        <span className="text-muted-foreground">적합도</span>
                        <span className="tabular font-bold">{c.fitness}%</span>
                      </div>
                      <Progress value={c.fitness} />
                    </div>
                    <p className="mt-4 text-xs font-semibold">추천 근거</p>
                    <ul className="mt-1.5 flex-1 space-y-1">
                      {c.reasons.map((r) => (
                        <li key={r} className="flex gap-2 text-xs text-muted-foreground">
                          <span className="mt-1.5 size-1 shrink-0 rounded-full bg-primary" />
                          {r}
                        </li>
                      ))}
                    </ul>
                    <Button
                      className="mt-4"
                      variant={active ? "default" : "outline"}
                      onClick={() => {
                        setSelectedCode(c.code);
                        toast.success(`${c.name} (${c.code})을 선택했습니다.`);
                      }}
                    >
                      이 코드 선택
                    </Button>
                  </div>
                );
              })}
            </div>
            <p className="rounded-md border border-risk-watch/40 bg-risk-watch-soft px-4 py-2.5 text-xs text-navy">
              AI 추천 결과는 진단 및 질병분류를 보조하기 위한 정보입니다. 최종 진단과 KCD 코드는
              담당 의사의 검토 및 인증 후 확정됩니다.
            </p>
            <div className="flex justify-end gap-2 border-t pt-3">
              <Button variant="outline" onClick={() => setStep(2)}>
                이전
              </Button>
              <Button
                onClick={() => {
                  if (!selectedCode) {
                    toast.error("진단코드를 1개 선택해야 다음 단계로 이동할 수 있습니다.");
                    return;
                  }
                  setStep(4);
                }}
              >
                다음: 최종 기록 확인 <ArrowRight className="size-4" />
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* STEP 4 */}
      {step === 4 && (
        <Card>
          <CardHeader className="flex-row items-center justify-between border-b py-3">
            <CardTitle className="text-base">최종 응급진료기록</CardTitle>
            <Button variant="outline" size="sm" onClick={() => setStep(1)}>
              기록 수정
            </Button>
          </CardHeader>
          <CardContent className="pt-4">
            <dl className="divide-y">
              {fieldOrder.map((key) => (
                <div key={key} className="grid grid-cols-[160px_1fr] gap-4 py-2.5">
                  <dt className="text-sm font-semibold text-muted-foreground">
                    {recordFieldLabels[key]}
                  </dt>
                  <dd className="text-sm">{record[key] || "미확인"}</dd>
                </div>
              ))}
              <div className="grid grid-cols-[160px_1fr] gap-4 py-2.5">
                <dt className="text-sm font-semibold text-muted-foreground">
                  선택된 KCD 진단코드
                </dt>
                <dd className="text-sm font-semibold">
                  {selectedCandidate?.name}{" "}
                  <span className="font-mono text-primary">({selectedCandidate?.code})</span>
                </dd>
              </div>
            </dl>
            <Separator className="my-4" />
            <div className="flex items-center justify-between">
              <p className="text-xs text-muted-foreground">
                최종 검토용 요약 화면입니다. 수정이 필요하면 “기록 수정”을 눌러 이전 단계로
                이동하세요.
              </p>
              <Button
                onClick={() => {
                  setAgreed(false);
                  setCertifyOpen(true);
                }}
              >
                <FileCheck2 className="size-4" /> 의사 검토 및 최종 인증
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* STEP 5 */}
      {step === 5 && (
        <Card className="border-risk-stable/40">
          <CardContent className="space-y-4 py-10 text-center">
            <CheckCircle2 className="mx-auto size-12 text-risk-stable" />
            <p className="text-lg font-bold">응급진료기록이 최종 인증 및 저장되었습니다.</p>
            <Badge variant="outline" className="bg-risk-stable-soft text-risk-stable">
              <Check className="mr-1 size-3" /> 의사 인증 완료
            </Badge>
            <dl className="mx-auto grid max-w-xl grid-cols-2 gap-x-8 gap-y-2 pt-4 text-left text-sm">
              <div>
                <dt className="text-xs text-muted-foreground">환자명 / 환자번호</dt>
                <dd className="font-medium">
                  {patient.name} · {patient.id}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">주진단명</dt>
                <dd className="font-medium">{selectedCandidate?.name}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">KCD 코드</dt>
                <dd className="font-mono font-medium text-primary">{selectedCandidate?.code}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">검토 의사 / 인증 일시</dt>
                <dd className="font-medium">
                  {currentUser.dept} {currentUser.name} · {certifiedAt}
                </dd>
              </div>
            </dl>
            <div className="flex justify-center gap-2 pt-4">
              <Button variant="outline" onClick={() => setStep(4)}>
                최종 기록 보기
              </Button>
              <Button asChild>
                <Link to="/records">환자 목록으로</Link>
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* 누락 차단 다이얼로그 */}
      <Dialog open={blockOpen} onOpenChange={setBlockOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <AlertCircle className="size-5 text-risk-critical" />
              필수 누락항목을 확인하고 작성해주세요.
            </DialogTitle>
            <DialogDescription>
              누락된 필수 기록을 보완한 후 다음 단계로 이동할 수 있습니다.
            </DialogDescription>
          </DialogHeader>
          <ul className="space-y-1.5">
            {missingRequired.map((k) => (
              <li key={k} className="flex items-center gap-2 text-sm text-risk-critical">
                <XCircle className="size-4" /> {recordFieldLabels[k]}
              </li>
            ))}
          </ul>
          <DialogFooter>
            <Button
              onClick={() => {
                setBlockOpen(false);
                const first = missingRequired[0];
                if (first) focusField(first);
              }}
            >
              누락항목 확인
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 최종 인증 다이얼로그 */}
      <Dialog open={certifyOpen} onOpenChange={setCertifyOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>최종 기록 인증</DialogTitle>
            <DialogDescription>
              아래 내용을 검토한 후 최종 인증 및 저장을 진행해 주세요.
            </DialogDescription>
          </DialogHeader>
          <dl className="space-y-2 rounded-md border bg-secondary/40 p-4 text-sm">
            {[
              ["환자명", patient.name],
              ["환자번호", patient.id],
              ["최종 주진단명", selectedCandidate?.name ?? "-"],
              ["선택된 KCD 코드", selectedCandidate?.code ?? "-"],
              ["검토 의사", `${currentUser.dept} ${currentUser.name}`],
              [
                "인증 일시",
                new Date().toLocaleString("ko-KR", { dateStyle: "medium", timeStyle: "short" }),
              ],
            ].map(([k, val]) => (
              <div key={k} className="flex justify-between gap-4">
                <dt className="text-muted-foreground">{k}</dt>
                <dd className="font-medium">{val}</dd>
              </div>
            ))}
          </dl>
          <label className="flex items-start gap-2 text-sm">
            <Checkbox
              checked={agreed}
              onCheckedChange={(c) => setAgreed(c === true)}
              className="mt-0.5"
            />
            본인은 위 응급진료기록과 진단코드를 검토하였습니다.
          </label>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCertifyOpen(false)}>
              취소
            </Button>
            <Button
              disabled={!agreed}
              onClick={() => {
                setCertifiedAt(
                  new Date().toLocaleString("ko-KR", { dateStyle: "medium", timeStyle: "short" }),
                );
                setCertifyOpen(false);
                setStep(5);
                toast.success("응급진료기록이 최종 인증 및 저장되었습니다.");
              }}
            >
              최종 인증 및 저장
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
