import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link, useRouterState } from "@tanstack/react-router";
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
  Plus,
  Save,
  Send,
  Square,
  Stethoscope,
  Upload,
  X,
  XCircle,
} from "lucide-react";
import { type ChangeEvent, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import {
  clinicalAudioTranscriptionErrorMessage,
  clinicalRecordDiagnosisEntries,
  normalizeClinicalRecordImpression,
  clinicalDraftErrorMessage,
  clinicalDraftPartialMessage,
  createClinicalRecordDraft,
  dialogueToWhisperDraftRequest,
  getPersistedClinicalRecord,
  parseWhisperDraftJson,
  saveClinicalRecordDraft,
  searchKcdCodes,
  signClinicalRecord,
  transcribeClinicalRecordAudio,
  whisperDraftToDialogue,
  workflowDraftToEmergencyRecord,
  workflowDraftToFieldProvenance,
  workflowDraftToFieldStatuses,
  type DraftDialogueTurn,
} from "@/api/clinical-records";
import { formatDateTime, sexLabel } from "@/api/display";
import { edStayKeys, getEdStay } from "@/api/ed-stays";
import type { PersistedClinicalRecord, WhisperDraftRequest } from "@/api/types";
import {
  BrowserAudioRecorder,
  audioRecordingErrorMessage,
  createAudioRecordingPreview,
  settleAudioRecordingPreview,
  type AudioRecordingPreview,
  type AudioRecorderState,
} from "@/lib/browser-audio-recorder";
import { FieldProvenancePanel } from "@/components/records/field-provenance-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
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
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import {
  checkStatusMeta,
  currentUser,
  emptyRecord,
  recordFieldLabels,
  type CheckStatus,
  type EmergencyRecord,
  type RecordFieldKey,
} from "@/lib/mock-data";
import { outcomeOptions } from "@/lib/clinical-record-outcome";
import type { FieldProvenanceMap } from "@/lib/clinical-provenance";

export const Route = createFileRoute("/records/$patientId")({
  head: ({ params }) => {
    const ogTitle = `환자 ${params.patientId} AI 응급진료기록 · ER:ON(이로운)`;
    return {
      meta: [
        {
          name: "description",
          content: "대화 기반 응급진료기록 작성, 누락 검사, KCD 코드 추천 및 의사 인증 워크플로우.",
        },
        { property: "og:title", content: ogTitle },
      ],
    };
  },
  component: RecordWorkflowPage,
});

const steps = ["기록 작성", "기록 작성 및 누락 검사", "최종 기록", "인증 저장"];

type AudioPreviewSource = "recording" | "file";

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

function formatRecordingDuration(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${String(minutes).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function displayAudioType(file: File): string {
  return file.type || file.name.split(".").pop()?.toUpperCase() || "브라우저 기본 형식";
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
  const { patientId } = Route.useParams();
  const detailQuery = useQuery({
    queryKey: edStayKeys.detail(patientId),
    queryFn: ({ signal }) => getEdStay(patientId, signal),
  });
  const recordQuery = useQuery({
    queryKey: ["clinical-record", patientId],
    queryFn: ({ signal }) => getPersistedClinicalRecord(patientId, signal),
    refetchInterval: 5_000,
  });

  if (detailQuery.isPending || recordQuery.isPending) {
    return (
      <div className="space-y-5">
        <Skeleton className="h-10 w-48" />
        <Skeleton className="h-28 w-full" />
        <Skeleton className="h-96 w-full" />
      </div>
    );
  }

  if (detailQuery.isError || recordQuery.isError) {
    const queryError = detailQuery.error ?? recordQuery.error;
    return (
      <div className="flex flex-col items-center gap-3 py-20 text-center">
        <AlertCircle className="size-9 text-risk-critical" />
        <p className="font-semibold">환자 정보를 불러오지 못했습니다</p>
        <p className="text-sm text-muted-foreground">{queryError?.message}</p>
        <div className="flex gap-2">
          <Button asChild variant="outline">
            <Link to="/records" search={(prev) => prev}>
              목록으로
            </Link>
          </Button>
          <Button
            onClick={() => {
              void detailQuery.refetch();
              void recordQuery.refetch();
            }}
          >
            다시 시도
          </Button>
        </div>
      </div>
    );
  }

  const detail = detailQuery.data;
  const vital = detail.triage;
  const patient = {
    id: detail.stay_id,
    name: detail.display_name ?? `ED-${detail.stay_id}`,
    sex: sexLabel(detail.sex),
    age: detail.age ?? "-",
    arrivedAt: formatDateTime(detail.arrived_at),
    ktas: detail.acuity ?? "-",
    recordStatus: "미작성",
    vitals: {
      hr: vital.heart_rate ?? "-",
      rr: vital.resp_rate ?? "-",
      sbp: vital.sbp ?? "-",
      dbp: vital.dbp ?? "-",
      bt: vital.temperature_c ?? "-",
      spo2: vital.spo2 ?? "-",
      mental: "-",
    },
  };

  return (
    <RecordWorkflow
      key={`${patient.id}:${recordQuery.data?.updated_at ?? "new"}`}
      patient={patient}
      persisted={recordQuery.data}
    />
  );
}

function RecordWorkflow({
  patient,
  persisted,
}: {
  patient: ReturnType<typeof createWorkflowPatient>;
  persisted: PersistedClinicalRecord | null;
}) {
  const isMobileCompact = useRouterState({
    select: (state) =>
      String((state.location.search as Record<string, unknown> | undefined)?.mobile) === "1",
  });
  const queryClient = useQueryClient();
  const savedPayload = persisted?.record_payload;
  const savedRecordPayload = savedPayload?.record as EmergencyRecord | undefined;
  const savedRecord = savedRecordPayload
    ? {
        ...savedRecordPayload,
        outcome: savedRecordPayload.outcome === "진료 진행 중" ? "" : savedRecordPayload.outcome,
      }
    : undefined;
  const savedStatuses = savedPayload?.field_statuses as Record<RecordFieldKey, CheckStatus> | null;
  const savedProvenance = (savedPayload?.field_provenance ?? {}) as FieldProvenanceMap;
  const savedWhisperPayload = savedPayload?.whisper_payload ?? null;
  const initiallySigned = persisted?.status === "SIGNED";
  const [step, setStep] = useState(initiallySigned ? 4 : 1);
  const [dialogue, setDialogue] = useState<DraftDialogueTurn[]>(
    savedWhisperPayload ? whisperDraftToDialogue(savedWhisperPayload) : [],
  );
  const [uploadedWhisperPayload, setUploadedWhisperPayload] = useState<WhisperDraftRequest | null>(
    savedWhisperPayload,
  );
  const [uploadedWhisperFileName, setUploadedWhisperFileName] = useState<string | null>(null);
  const [uploadedAudioFile, setUploadedAudioFile] = useState<File | null>(null);
  const [transcribing, setTranscribing] = useState(false);
  const [transcribingFileName, setTranscribingFileName] = useState<string | null>(null);
  const [recording, setRecording] = useState<AudioRecorderState>("idle");
  const [recordingSeconds, setRecordingSeconds] = useState(0);
  const [recordedAudio, setRecordedAudio] = useState<AudioRecordingPreview | null>(null);
  const [audioPreviewSource, setAudioPreviewSource] = useState<AudioPreviewSource | null>(null);
  const [conversationSentAt, setConversationSentAt] = useState<string | null>(
    savedPayload?.conversation_sent_at ?? null,
  );
  const [mobileWorkflowExpanded, setMobileWorkflowExpanded] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [record, setRecord] = useState<EmergencyRecord>(savedRecord ?? emptyRecord);
  const [fieldProvenance, setFieldProvenance] = useState<FieldProvenanceMap>(savedProvenance);
  const [provenanceRevision, setProvenanceRevision] = useState(0);
  const [clinicalFieldStatuses, setClinicalFieldStatuses] = useState<Record<
    RecordFieldKey,
    CheckStatus
  > | null>(savedStatuses ?? null);
  const [generated, setGenerated] = useState(savedPayload?.generated ?? Boolean(persisted));
  const [generationNotice, setGenerationNotice] = useState<{
    kind: "partial" | "error";
    message: string;
  } | null>(null);

  const [checking, setChecking] = useState(false);
  const [checked, setChecked] = useState(false);
  const [highlight, setHighlight] = useState<RecordFieldKey | null>(null);
  const [blockOpen, setBlockOpen] = useState(false);

  const [selectedKcds, setSelectedKcds] = useState<
    Array<{ code: string; name: string; is_rule_out?: boolean }>
  >(
    Array.isArray(persisted?.selected_kcd)
      ? persisted.selected_kcd
      : persisted?.selected_kcd
        ? [persisted.selected_kcd]
        : [],
  );
  const savedRuleOuts = savedPayload?.diagnosis_rule_outs as boolean[] | undefined;
  const [diagnosisRuleOuts, setDiagnosisRuleOuts] = useState<boolean[]>(
    savedRuleOuts ??
      (Array.isArray(persisted?.selected_kcd)
        ? persisted.selected_kcd.map((item) => item.is_rule_out ?? false)
        : persisted?.selected_kcd
          ? [persisted.selected_kcd.is_rule_out ?? false]
          : []),
  );
  const [kcdSearch, setKcdSearch] = useState("");
  const [certifyOpen, setCertifyOpen] = useState(false);
  const [agreed, setAgreed] = useState(false);
  const [persistedRecord, setPersistedRecord] = useState(persisted);
  const [saving, setSaving] = useState(false);
  const [signing, setSigning] = useState(false);
  const certifiedAt = persistedRecord?.signed_at
    ? new Date(persistedRecord.signed_at).toLocaleString("ko-KR", {
        dateStyle: "medium",
        timeStyle: "short",
      })
    : null;
  const isSigned = persistedRecord?.status === "SIGNED";

  const fieldRefs = useRef<Record<string, HTMLElement | null>>({});
  const whisperFileInputRef = useRef<HTMLInputElement | null>(null);
  const audioFileInputRef = useRef<HTMLInputElement | null>(null);
  const audioRecorderRef = useRef<BrowserAudioRecorder | null>(null);
  const recordedAudioUrlRef = useRef<string | null>(null);
  const transcribingRef = useRef(false);
  const overwriteConfirmedRef = useRef(false);

  const clearRecordedAudio = () => {
    if (recordedAudio && recordedAudioUrlRef.current) {
      settleAudioRecordingPreview(recordedAudio, true);
    }
    recordedAudioUrlRef.current = null;
    setRecordedAudio(null);
    setAudioPreviewSource(null);
  };

  useEffect(
    () => () => {
      audioRecorderRef.current?.dispose();
      audioRecorderRef.current = null;
      if (recordedAudioUrlRef.current) URL.revokeObjectURL(recordedAudioUrlRef.current);
      recordedAudioUrlRef.current = null;
    },
    [],
  );

  useEffect(() => {
    if (recording !== "recording") return;
    const timer = window.setInterval(() => setRecordingSeconds((seconds) => seconds + 1), 1000);
    return () => window.clearInterval(timer);
  }, [recording]);

  const statuses = useMemo(() => {
    const out = {} as Record<RecordFieldKey, CheckStatus>;
    fieldOrder.forEach((k) => (out[k] = clinicalFieldStatuses?.[k] ?? statusOf(record[k])));
    return out;
  }, [clinicalFieldStatuses, record]);

  const missingRequired = requiredFields.filter((k) => statuses[k] === "missing");
  const completeCount = fieldOrder.filter((k) => statuses[k] === "complete").length;
  const reviewCount = fieldOrder.filter((k) => statuses[k] === "review").length;
  const hasSavedDraft = persistedRecord?.status === "DRAFT";
  const canProceedToCheck = generated || Boolean(persistedRecord);
  const completeness = canProceedToCheck
    ? Math.round(((completeCount + reviewCount * 0.5) / fieldOrder.length) * 100)
    : 0;

  const diagnosisEntries = clinicalRecordDiagnosisEntries(record.impression);
  const diagnosisCount = diagnosisEntries.filter((diagnosis) => diagnosis.trim()).length;
  const nextDiagnosisIndex = selectedKcds.length;
  const nextDiagnosis = diagnosisEntries[nextDiagnosisIndex]?.trim() ?? "";
  const kcdQueryText = kcdSearch.trim() || nextDiagnosis;
  const kcdSearchQuery = useQuery({
    queryKey: ["kcd", "search", kcdQueryText],
    queryFn: ({ signal }) => searchKcdCodes(kcdQueryText, signal),
    enabled: checked && Boolean(kcdQueryText),
    staleTime: 5 * 60 * 1000,
  });
  const v = patient.vitals;

  const persistDraft = async (showToast = true, sentAt = conversationSentAt) => {
    if (isSigned) {
      toast.error("인증 완료된 기록은 수정하거나 다시 저장할 수 없습니다.");
      return null;
    }
    setSaving(true);
    try {
      const saved = await saveClinicalRecordDraft(patient.id, {
        record_payload: {
          record: {
            ...record,
            impression: normalizeClinicalRecordImpression(record.impression),
          },
          field_statuses: clinicalFieldStatuses,
          field_provenance: fieldProvenance as Record<string, unknown>,
          generated,
          diagnosis_rule_outs: diagnosisRuleOuts,
          ...(dialogue.length > 0
            ? {
                whisper_payload:
                  uploadedWhisperPayload ?? dialogueToWhisperDraftRequest(dialogue),
              }
            : {}),
          ...(sentAt ? { conversation_sent_at: sentAt } : {}),
        },
        selected_kcd: selectedKcds,
        clinician_id: "DEMO-DR-001",
        clinician_name: currentUser.name,
      });
      setPersistedRecord(saved);
      void queryClient.invalidateQueries({ queryKey: ["ed", "stays"] });
      if (showToast) toast.success("응급진료기록을 임시저장했습니다.");
      return saved;
    } catch (error) {
      toast.error("임시저장하지 못했습니다.", {
        description: error instanceof Error ? error.message : undefined,
      });
      return null;
    } finally {
      setSaving(false);
    }
  };

  const sendConversationToDesktop = async () => {
    if (dialogue.length === 0 || saving || conversationSentAt) return;
    const sentAt = new Date().toISOString();
    const saved = await persistDraft(false, sentAt);
    if (!saved) return;
    setConversationSentAt(sentAt);
    toast.success("대화 내용을 PC로 전송했습니다.");
  };

  const certifyRecord = async () => {
    setSigning(true);
    const saved = await persistDraft(false);
    if (!saved) {
      setSigning(false);
      return;
    }
    try {
      const signed = await signClinicalRecord(saved.id, {
        clinician_id: "DEMO-DR-001",
        clinician_name: currentUser.name,
      });
      setPersistedRecord(signed);
      void queryClient.invalidateQueries({ queryKey: ["ed", "stays"] });
      setCertifyOpen(false);
      setStep(4);
      toast.success("응급진료기록이 인증 저장되었습니다.");
    } catch (error) {
      toast.error("인증 저장하지 못했습니다.", {
        description: error instanceof Error ? error.message : undefined,
      });
    } finally {
      setSigning(false);
    }
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
      setUploadedAudioFile(null);
      setDialogue(whisperDraftToDialogue(payload));
      setFieldProvenance({});
      setClinicalFieldStatuses(null);
      setGenerated(false);
      setChecked(false);
      setGenerationNotice(null);
      setConversationSentAt(null);
      toast.success("Whisper JSON을 불러왔습니다.", {
        description: `${payload.segments.length}개 segment의 원문과 시간 정보를 유지합니다.`,
      });
    } catch (error) {
      toast.error("Whisper JSON을 불러오지 못했습니다.", {
        description: error instanceof Error ? error.message : "JSON 파일 형식을 확인해 주세요.",
      });
    }
  };

  const transcribeAudioFile = async (file: File): Promise<boolean> => {
    if (transcribingRef.current) return false;
    if (file.size === 0) {
      toast.error("빈 음성 파일은 사용할 수 없습니다.");
      return false;
    }
    if (file.size > 25 * 1024 * 1024) {
      toast.error("음성 파일은 25MB 이하여야 합니다.");
      return false;
    }
    transcribingRef.current = true;
    setTranscribing(true);
    setTranscribingFileName(file.name);
    toast.info("음성 인식을 시작했습니다.", {
      description: "완료되면 대화 기록에 자동으로 표시됩니다.",
    });
    try {
      const payload = await transcribeClinicalRecordAudio(file);
      if (payload.segments.length === 0) {
        throw new Error("음성에서 대화 segment를 찾지 못했습니다.");
      }
      setUploadedAudioFile(file);
      setUploadedWhisperPayload(payload);
      setUploadedWhisperFileName(null);
      setDialogue(whisperDraftToDialogue(payload));
      setFieldProvenance({});
      setClinicalFieldStatuses(null);
      setGenerated(false);
      setChecked(false);
      setGenerationNotice(null);
      setConversationSentAt(null);
      toast.success("음성 인식이 완료되었습니다.", {
        description: `${payload.segments.length}개 segment를 대화 기록에 표시했습니다.`,
      });
      return true;
    } catch (error) {
      toast.error("음성 파일을 인식하지 못했습니다.", {
        description: clinicalAudioTranscriptionErrorMessage(error),
      });
      return false;
    } finally {
      transcribingRef.current = false;
      setTranscribing(false);
      setTranscribingFileName(null);
    }
  };

  const loadAudioFile = (event: ChangeEvent<HTMLInputElement>) => {
    const input = event.currentTarget;
    const file = input.files?.[0];
    input.value = "";
    if (!file) return;
    if (file.size === 0) {
      toast.error("빈 음성 파일은 사용할 수 없습니다.");
      return;
    }
    if (file.size > 25 * 1024 * 1024) {
      toast.error("음성 파일은 25MB 이하여야 합니다.");
      return;
    }

    clearRecordedAudio();
    setRecordingSeconds(0);
    const preview = createAudioRecordingPreview(file, 0);
    recordedAudioUrlRef.current = preview.objectUrl;
    setRecordedAudio(preview);
    setAudioPreviewSource("file");
    toast.success("음성 파일을 불러왔습니다.", {
      description: "미리듣기 후 사용할 음성인지 선택해 주세요.",
    });
  };

  const startRecording = async () => {
    if (recording === "recording" || generating || transcribing) return;
    if (isSigned) {
      toast.error("최종 인증된 기록은 새로 녹음할 수 없습니다.");
      return;
    }
    const resuming = recording === "paused";
    const replacingSavedDraft = !resuming && hasSavedDraft && !overwriteConfirmedRef.current;
    if (
      replacingSavedDraft &&
      !window.confirm(
        "이미 임시저장된 기록이 있습니다. 새로 녹음하면 기존 임시저장 내용을 덮어씁니다. 새로 녹음하시겠습니까?",
      )
    ) {
      return;
    }
    try {
      if (!resuming) {
        clearRecordedAudio();
        setRecordingSeconds(0);
      }
      const recorder = audioRecorderRef.current ?? new BrowserAudioRecorder();
      audioRecorderRef.current = recorder;
      await recorder.start();
      if (replacingSavedDraft) {
        overwriteConfirmedRef.current = true;
        setUploadedWhisperPayload(null);
        setUploadedWhisperFileName(null);
        setUploadedAudioFile(null);
        setDialogue([]);
        setRecord({ ...emptyRecord });
        setFieldProvenance({});
        setClinicalFieldStatuses(null);
        setSelectedKcds([]);
        setDiagnosisRuleOuts([]);
        setGenerated(false);
        setChecked(false);
        setGenerationNotice(null);
        setConversationSentAt(null);
      }
      setRecording(recorder.state);
      toast.info(resuming ? "녹음을 재개했습니다." : "녹음을 시작했습니다.");
    } catch (error) {
      audioRecorderRef.current?.dispose();
      audioRecorderRef.current = null;
      setRecording("idle");
      toast.error("음성 녹음을 시작하지 못했습니다.", {
        description: audioRecordingErrorMessage(error),
      });
    }
  };

  const pauseRecording = () => {
    const recorder = audioRecorderRef.current;
    if (!recorder || recording !== "recording") return;
    recorder.pause();
    setRecording(recorder.state);
    toast.info("녹음을 일시정지했습니다.");
  };

  const stopRecording = async () => {
    const recorder = audioRecorderRef.current;
    if (!recorder || recording === "idle") return;
    try {
      const audio = await recorder.stop();
      setRecording("idle");
      audioRecorderRef.current = null;
      const preview = createAudioRecordingPreview(audio, recordingSeconds);
      recordedAudioUrlRef.current = preview.objectUrl;
      setRecordedAudio(preview);
      setAudioPreviewSource("recording");
      toast.success("녹음이 완료되었습니다.", {
        description: "미리듣기 후 사용할 녹음인지 선택해 주세요.",
      });
    } catch (error) {
      toast.error("음성 녹음을 완료하지 못했습니다.", {
        description: audioRecordingErrorMessage(error),
      });
    } finally {
      recorder.dispose();
      audioRecorderRef.current = null;
      setRecording("idle");
    }
  };

  const useRecordedAudio = async () => {
    if (!recordedAudio || transcribing) return;
    const preview = settleAudioRecordingPreview(
      recordedAudio,
      await transcribeAudioFile(recordedAudio.file),
    );
    if (!preview) {
      recordedAudioUrlRef.current = null;
      setAudioPreviewSource(null);
    }
    setRecordedAudio(preview);
  };

  const cancelRecordedAudio = () => {
    if (transcribing) return;
    clearRecordedAudio();
    setRecordingSeconds(0);
  };

  const generateRecord = async () => {
    if (dialogue.length === 0) {
      toast.error("먼저 대화를 불러오거나 녹음을 진행해 주세요.");
      return;
    }
    setGenerating(true);
    setGenerationNotice(null);
    try {
      const workflow = await createClinicalRecordDraft(
        uploadedWhisperPayload ?? dialogueToWhisperDraftRequest(dialogue),
      );
      setRecord(workflowDraftToEmergencyRecord(workflow));
      setFieldProvenance(workflowDraftToFieldProvenance(workflow));
      setProvenanceRevision((revision) => revision + 1);
      setClinicalFieldStatuses(workflowDraftToFieldStatuses(workflow));
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
    if (diagnosisCount === 0) {
      toast.error("주진단을 입력해 주세요.");
      return;
    }
    if (selectedKcds.length !== diagnosisCount) {
      toast.error("모든 진단에 KCD-9차 코드를 1개씩 선택해 주세요.", {
        description: `입력한 진단 ${diagnosisCount}개 · 선택한 코드 ${selectedKcds.length}개`,
      });
      return;
    }
    setStep(3);
  };

  const setField = (key: RecordFieldKey, value: string) => {
    setRecord((prev) => ({ ...prev, [key]: value }));
    setClinicalFieldStatuses((prev) => (prev ? { ...prev, [key]: statusOf(value) } : prev));
  };

  const setDiagnosis = (index: number, value: string) => {
    const next = [...diagnosisEntries];
    const diagnosisChanged = next[index] !== value;
    next[index] = value;
    setField("impression", next.join("\n"));
    if (diagnosisChanged && selectedKcds[index]) {
      setSelectedKcds((current) => current.slice(0, index));
      setKcdSearch("");
    }
  };

  const addSecondaryDiagnosis = () => {
    if (!diagnosisEntries.at(-1)?.trim()) {
      toast.error("현재 진단명을 먼저 입력해 주세요.");
      return;
    }
    setField("impression", [...diagnosisEntries, ""].join("\n"));
    setDiagnosisRuleOuts((current) => [...current, false]);
  };

  const removeSecondaryDiagnosis = (index: number) => {
    setField(
      "impression",
      diagnosisEntries.filter((_, diagnosisIndex) => diagnosisIndex !== index).join("\n"),
    );
    setSelectedKcds((current) =>
      current.filter((_, diagnosisIndex) => diagnosisIndex !== index),
    );
    setDiagnosisRuleOuts((current) =>
      current.filter((_, diagnosisIndex) => diagnosisIndex !== index),
    );
    setKcdSearch("");
  };

  const setDiagnosisRuleOut = (index: number, checked: boolean) => {
    setDiagnosisRuleOuts((current) => {
      const next = [...current];
      next[index] = checked;
      return next;
    });
    setSelectedKcds((current) =>
      current.map((item, diagnosisIndex) =>
        diagnosisIndex === index ? { ...item, is_rule_out: checked } : item,
      ),
    );
  };

  if (isMobileCompact && (mobileWorkflowExpanded || isSigned)) {
    return (
      <div className="mx-auto w-full max-w-lg space-y-4 pb-8">
        <div className="flex items-center justify-between gap-3">
          {isSigned ? (
            <Button asChild variant="ghost" size="sm" className="min-h-11 px-2">
              <Link to="/records" search={(previous) => previous}>
                <ArrowLeft className="size-4" /> 환자 변경
              </Link>
            </Button>
          ) : (
            <Button
              variant="ghost"
              size="sm"
              className="min-h-11 px-2"
              onClick={() => setMobileWorkflowExpanded(false)}
            >
              <ArrowLeft className="size-4" /> 녹음 화면
            </Button>
          )}
          <Badge
            variant="outline"
            className={isSigned ? "bg-risk-stable-soft text-risk-stable" : "bg-mint-soft text-navy"}
          >
            {isSigned ? "최종 인증 완료" : "생성된 초안"}
          </Badge>
        </div>

        <div className="rounded-xl border bg-card px-4 py-3 shadow-sm">
          <p className="truncate text-sm font-semibold">
            {patient.name} · {patient.sex} {patient.age}세 · KTAS {patient.ktas} · {patient.id}
          </p>
          {isSigned ? (
            <p className="mt-1 text-xs text-muted-foreground">
              {certifiedAt} 최종 인증되어 녹음 및 수정 기능을 표시하지 않습니다.
            </p>
          ) : null}
        </div>

        <Card className="overflow-hidden rounded-2xl">
          <CardHeader className="border-b py-4">
            <CardTitle className="text-lg">
              {isSigned ? "최종 응급진료기록" : "생성된 응급진료기록 초안"}
            </CardTitle>
          </CardHeader>
          <CardContent className="divide-y px-4 py-0">
            {fieldOrder.map((key) => (
              <div key={key} className="py-4">
                <p className="mb-1 text-xs font-semibold text-muted-foreground">
                  {recordFieldLabels[key]}
                </p>
                <p className="whitespace-pre-wrap break-words text-sm leading-6">
                  {record[key]?.trim() || "미확인"}
                </p>
              </div>
            ))}
          </CardContent>
        </Card>

        {!isSigned ? (
          <p className="px-1 text-xs leading-5 text-muted-foreground">
            이 화면은 모바일 확인용입니다. 항목 수정, 누락 검사, KCD 코드 선택 및 최종 인증은
            데스크톱 기록 화면에서 진행해 주세요.
          </p>
        ) : null}
      </div>
    );
  }

  if (isMobileCompact && !mobileWorkflowExpanded) {
    return (
      <div className="mx-auto w-full max-w-lg space-y-4 pb-8">
        <div className="flex items-center justify-between gap-3">
          <Button asChild variant="ghost" size="sm" className="min-h-11 px-2">
            <Link to="/records" search={(previous) => previous}>
              <ArrowLeft className="size-4" /> 환자 변경
            </Link>
          </Button>
          <Badge variant="outline" className="shrink-0 bg-mint-soft text-navy">
            {generated ? "초안 생성 완료" : "녹음 대기"}
          </Badge>
        </div>

        <div className="rounded-2xl bg-navy px-5 py-4 text-navy-foreground shadow-sm">
          <p className="text-xs text-navy-foreground/70">선택한 환자</p>
          <p className="mt-1 text-xl font-bold">{patient.name}</p>
          <p className="mt-1 text-sm text-navy-foreground/80">
            {patient.sex} {patient.age}세 · KTAS {patient.ktas} · {patient.id}
          </p>
        </div>

        <Card className="overflow-hidden rounded-2xl">
          <CardContent className="flex flex-col items-center gap-5 px-4 py-7 text-center">
            <div>
              <p className="text-lg font-bold">
                {recording === "recording"
                  ? "대화를 녹음하고 있습니다"
                  : recording === "paused"
                    ? "녹음이 일시정지되었습니다"
                    : recordedAudio
                      ? audioPreviewSource === "file"
                        ? "음성 파일을 확인해 주세요"
                        : "녹음 내용을 확인해 주세요"
                      : "대화를 녹음하세요"}
              </p>
              {audioPreviewSource === "file" && recordedAudio ? (
                <p className="mt-2 break-all text-sm font-medium">{recordedAudio.file.name}</p>
              ) : (
                <p className="tabular mt-2 text-4xl font-bold tracking-tight">
                  {formatRecordingDuration(recordingSeconds)}
                </p>
              )}
            </div>

            {recording === "idle" && !recordedAudio ? (
              <Button
                className="size-28 rounded-full text-base shadow-lg"
                onClick={startRecording}
                disabled={transcribing || generating || isSigned}
                aria-label="녹음 시작"
              >
                <Mic className="size-9" />
                녹음 시작
              </Button>
            ) : null}

            {recording === "recording" ? (
              <div className="grid w-full grid-cols-2 gap-3">
                <Button className="min-h-14" variant="outline" onClick={pauseRecording}>
                  <Pause className="size-5" /> 일시정지
                </Button>
                <Button className="min-h-14" onClick={stopRecording}>
                  <Square className="size-5" /> 녹음 종료
                </Button>
              </div>
            ) : null}

            {recording === "paused" ? (
              <div className="grid w-full grid-cols-2 gap-3">
                <Button className="min-h-14" onClick={startRecording}>
                  <Mic className="size-5" /> 녹음 재개
                </Button>
                <Button className="min-h-14" variant="outline" onClick={stopRecording}>
                  <Square className="size-5" /> 녹음 종료
                </Button>
              </div>
            ) : null}

            {recording !== "idle" ? (
              <p className="text-xs text-muted-foreground">
                화면을 잠그거나 다른 앱으로 전환하면 녹음이 중단될 수 있습니다.
              </p>
            ) : null}

            {recordedAudio ? (
              <div className="w-full space-y-4 text-left">
                <audio
                  className="h-12 w-full"
                  controls
                  preload="metadata"
                  src={recordedAudio.objectUrl}
                />
                <p className="break-words text-xs text-muted-foreground">
                  {displayAudioType(recordedAudio.file)} ·
                  {` ${(recordedAudio.file.size / 1024).toFixed(1)}KB`}
                </p>
                <Button
                  className="min-h-14 w-full text-base"
                  onClick={useRecordedAudio}
                  disabled={transcribing}
                >
                  {transcribing ? (
                    <Loader2 className="size-5 animate-spin" />
                  ) : (
                    <Check className="size-5" />
                  )}
                  {transcribing ? "대화를 변환하고 있습니다" : "이 녹음 사용"}
                </Button>
                <div
                  className={`grid gap-3 ${
                    audioPreviewSource === "recording" ? "grid-cols-2" : "grid-cols-1"
                  }`}
                >
                  {audioPreviewSource === "recording" ? (
                    <Button
                      className="min-h-12"
                      variant="outline"
                      onClick={startRecording}
                      disabled={transcribing}
                    >
                      <Mic className="size-4" /> 다시 녹음
                    </Button>
                  ) : null}
                  <Button
                    className="min-h-12"
                    variant="outline"
                    onClick={cancelRecordedAudio}
                    disabled={transcribing}
                  >
                    <X className="size-4" /> 취소
                  </Button>
                </div>
              </div>
            ) : null}

            {recording === "idle" && !recordedAudio ? (
              <>
                <div className="flex w-full items-center gap-3 text-xs text-muted-foreground">
                  <Separator className="flex-1" /> 또는 <Separator className="flex-1" />
                </div>
                <input
                  ref={audioFileInputRef}
                  type="file"
                  accept="audio/*,.wav,.mp3,.m4a,.mp4,.webm,.ogg,.flac"
                  className="hidden"
                  onChange={loadAudioFile}
                />
                <Button
                  className="min-h-12 w-full"
                  variant="secondary"
                  onClick={() => audioFileInputRef.current?.click()}
                  disabled={transcribing}
                >
                  <Upload className="size-4" /> 음성 파일 불러오기
                </Button>
              </>
            ) : null}
          </CardContent>
        </Card>

        {dialogue.length > 0 ? (
          <Card className="rounded-2xl">
            <CardHeader className="border-b py-3">
              <CardTitle className="text-base">변환된 대화 · {dialogue.length}개</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4 pt-4">
              <div
                className="max-h-[50dvh] min-h-48 overflow-y-scroll overscroll-contain touch-pan-y rounded-lg border bg-secondary/30 p-3"
                style={{ WebkitOverflowScrolling: "touch", scrollbarGutter: "stable" }}
              >
                <ul className="space-y-3">
                  {dialogue.map((turn, index) => (
                    <li key={index} className="flex justify-end">
                      <div className="max-w-[92%] rounded-xl bg-primary px-4 py-3 text-left text-primary-foreground shadow-sm">
                        <p className="whitespace-pre-wrap break-words text-sm leading-6">{turn.text}</p>
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
              <Button
                className="min-h-14 w-full text-base"
                onClick={() => void sendConversationToDesktop()}
                disabled={saving || transcribing || Boolean(conversationSentAt)}
              >
                {saving ? (
                  <Loader2 className="size-5 animate-spin" />
                ) : conversationSentAt ? (
                  <Check className="size-5" />
                ) : (
                  <Send className="size-5" />
                )}
                {saving
                  ? "PC로 전송 중"
                  : conversationSentAt
                    ? "전송 완료"
                    : "대화 내용 PC로 보내기"}
              </Button>
              {conversationSentAt ? (
                <p className="text-center text-sm leading-6 text-muted-foreground">
                  전송이 완료되었습니다. 나머지 응급진료기록 작성과 검토는 PC에서 진행해 주세요.
                </p>
              ) : null}
            </CardContent>
          </Card>
        ) : null}
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <Button asChild variant="ghost" size="sm">
          <Link to="/records" search={(prev) => prev}>
            <ArrowLeft className="size-4" /> 환자 목록
          </Link>
        </Button>
        <Badge variant="outline" className="bg-mint-soft text-navy">
          기록 상태: {certifiedAt
            ? "의사 인증 완료"
            : hasSavedDraft
              ? "임시저장"
              : generated
                ? "작성 중"
                : patient.recordStatus}
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
              <Stethoscope className="size-3.5" /> EMR 연동 Vital 정보
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
        <div className="grid gap-5 md:grid-cols-2">
          <Card>
            <CardHeader className="border-b py-3">
              <CardTitle className="text-base">대화 기록</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 pt-4">
              <div className="flex flex-wrap gap-2">
                <Button
                  size="sm"
                  className="min-h-11 flex-1 sm:flex-none"
                  variant={recording === "recording" ? "default" : "outline"}
                  onClick={startRecording}
                  disabled={isSigned || generating || transcribing || recording === "recording"}
                >
                  <Mic className="size-4" />
                  {recording === "paused" ? " 녹음 재개" : " 녹음 시작"}
                </Button>
                <Button
                  size="sm"
                  className="min-h-11 flex-1 sm:flex-none"
                  variant="outline"
                  onClick={pauseRecording}
                  disabled={generating || transcribing || recording !== "recording"}
                >
                  <Pause className="size-4" /> 녹음 일시정지
                </Button>
                <Button
                  size="sm"
                  className="min-h-11 flex-1 sm:flex-none"
                  variant="outline"
                  onClick={stopRecording}
                  disabled={generating || transcribing || recording === "idle"}
                >
                  <Square className="size-4" /> 녹음 종료
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
                  className="hidden min-h-11 w-full sm:w-auto"
                  variant="secondary"
                  onClick={() => whisperFileInputRef.current?.click()}
                  disabled={generating || transcribing || recording !== "idle"}
                >
                  <Upload className="size-4" /> Whisper JSON 불러오기
                </Button>
                <input
                  ref={audioFileInputRef}
                  type="file"
                  accept="audio/*,.wav,.mp3,.m4a,.mp4,.webm,.ogg,.flac"
                  className="hidden"
                  onChange={loadAudioFile}
                />
                <Button
                  size="sm"
                  className="min-h-11 w-full sm:w-auto"
                  variant="secondary"
                  onClick={() => audioFileInputRef.current?.click()}
                  disabled={generating || transcribing || recording !== "idle"}
                >
                  {transcribing ? (
                    <Loader2 className="size-4 animate-spin" />
                  ) : (
                    <Upload className="size-4" />
                  )}
                  {transcribing ? " 음성 인식 중" : " 음성 파일 불러오기"}
                </Button>
              </div>
              {uploadedWhisperFileName && uploadedWhisperPayload ? (
                <p className="text-xs text-muted-foreground">
                  입력 파일: {uploadedWhisperFileName} · {uploadedWhisperPayload.segments.length}개
                  segment · 브라우저 메모리에서만 사용
                </p>
              ) : null}
              {uploadedAudioFile ? (
                <p className="text-xs text-muted-foreground">
                  입력 음성: {uploadedAudioFile.name} ·
                  {` ${(uploadedAudioFile.size / (1024 * 1024)).toFixed(1)}MB`} · API1 STT 완료 ·
                  {` ${uploadedWhisperPayload?.segments.length ?? 0}개 segment`}
                </p>
              ) : null}
              {transcribing && transcribingFileName ? (
                <p className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Loader2 className="size-3.5 animate-spin" /> {transcribingFileName} 음성 인식 중
                </p>
              ) : null}
              {recording !== "idle" && (
                <p className="flex flex-wrap items-center gap-2 text-xs text-risk-critical">
                  <span
                    className={`size-2 rounded-full bg-risk-critical ${
                      recording === "recording" ? "animate-pulse" : ""
                    }`}
                  />
                  {recording === "recording" ? "녹음 중" : "녹음 일시정지"} ·
                  {formatRecordingDuration(recordingSeconds)} · 종료 후 미리듣기에서 전송할 수
                  있습니다.
                </p>
              )}
              {recording !== "idle" ? (
                <p className="text-xs text-muted-foreground">
                  화면 잠금 또는 브라우저가 백그라운드로 전환되면 녹음이 중단될 수 있습니다.
                </p>
              ) : null}
              {recordedAudio ? (
                <div className="space-y-3 rounded-md border bg-secondary/30 p-3">
                  <audio
                    className="h-11 w-full max-w-full"
                    controls
                    preload="metadata"
                    src={recordedAudio.objectUrl}
                  >
                    이 브라우저에서는 오디오 미리듣기를 지원하지 않습니다.
                  </audio>
                  <p className="break-words text-xs text-muted-foreground">
                    {audioPreviewSource === "recording"
                      ? `녹음 시간 ${formatRecordingDuration(recordedAudio.durationSeconds)} · `
                      : `파일명 ${recordedAudio.file.name} · `}
                    파일 형식{" "}
                    {displayAudioType(recordedAudio.file)} · 파일 크기{" "}
                    {(recordedAudio.file.size / 1024).toFixed(1)}KB
                  </p>
                  <div
                    className={`grid grid-cols-1 gap-2 ${
                      audioPreviewSource === "recording" ? "sm:grid-cols-3" : "sm:grid-cols-2"
                    }`}
                  >
                    <Button className="min-h-11" onClick={useRecordedAudio} disabled={transcribing}>
                      {transcribing ? (
                        <Loader2 className="size-4 animate-spin" />
                      ) : (
                        <Check className="size-4" />
                      )}
                      이 녹음 사용
                    </Button>
                    {audioPreviewSource === "recording" ? (
                      <Button
                        className="min-h-11"
                        variant="outline"
                        onClick={startRecording}
                        disabled={transcribing}
                      >
                        <Mic className="size-4" /> 다시 녹음
                      </Button>
                    ) : null}
                    <Button
                      className="min-h-11"
                      variant="outline"
                      onClick={cancelRecordedAudio}
                      disabled={transcribing}
                    >
                      <X className="size-4" /> 취소
                    </Button>
                  </div>
                  {transcribing ? (
                    <p className="text-xs text-muted-foreground">
                      전송 실패 시 이 녹음은 유지되며 같은 파일로 다시 시도할 수 있습니다.
                    </p>
                  ) : null}
                </div>
              ) : null}
              <ScrollArea className="h-[420px] rounded-md border bg-secondary/30 p-3">
                {dialogue.length === 0 ? (
                  <p className="py-20 text-center text-sm text-muted-foreground">
                    {transcribing
                      ? "음성을 인식하고 있습니다. 완료되면 대화 기록이 표시됩니다."
                      : "대화 기록이 없습니다. 음성 녹음이나 음성 파일을 불러와 주세요."}
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
                          <p className="mb-0.5 text-[11px] tabular-nums opacity-70">
                            {t.segmentId} · {t.timestamp}
                          </p>
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
              <Button
                size="sm"
                onClick={generateRecord}
                disabled={generating || transcribing || recording !== "idle"}
              >
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
                  {uploadedAudioFile
                    ? "음성을 전사한 뒤 응급기록 초안을 생성하고 있습니다. 잠시 기다려 주세요."
                    : "대화를 분석하여 응급기록 초안을 생성하고 있습니다. 잠시 기다려 주세요."}
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
                  {fieldOrder.map((key) => {
                    const provenance = fieldProvenance[key];
                    return (
                      <div key={key}>
                        <label className="mb-1 flex items-center justify-between text-xs font-semibold">
                          {recordFieldLabels[key]}
                          {generated && (
                            <Badge
                              variant="outline"
                              className={checkStatusMeta[statuses[key]].badge}
                            >
                              {checkStatusMeta[statuses[key]].label}
                            </Badge>
                          )}
                        </label>
                        {key === "impression" ? (
                          <div className="space-y-2">
                            {diagnosisEntries.map((diagnosis, index) => (
                              <div key={index} className="flex items-center gap-2">
                                <span className="w-20 shrink-0 text-xs font-semibold text-muted-foreground">
                                  {index + 1}. {index === 0 ? "주진단" : "부진단"}
                                </span>
                                <Input
                                  value={diagnosis}
                                  placeholder={index === 0 ? "주진단 입력" : "부진단 입력"}
                                  onChange={(event) => setDiagnosis(index, event.target.value)}
                                />
                                <label className="flex shrink-0 items-center gap-1.5 text-xs font-semibold">
                                  <Checkbox
                                    checked={diagnosisRuleOuts[index] ?? false}
                                    onCheckedChange={(checked) =>
                                      setDiagnosisRuleOut(index, checked === true)
                                    }
                                  />
                                  R/O
                                </label>
                                {index > 0 ? (
                                  <Button
                                    type="button"
                                    size="icon"
                                    variant="ghost"
                                    aria-label={`${index + 1}번 부진단 삭제`}
                                    onClick={() => removeSecondaryDiagnosis(index)}
                                  >
                                    <X className="size-4" />
                                  </Button>
                                ) : null}
                              </div>
                            ))}
                            <Button
                              type="button"
                              size="sm"
                              variant="outline"
                              onClick={addSecondaryDiagnosis}
                            >
                              <Plus className="size-4" /> 부진단 추가
                            </Button>
                          </div>
                        ) : key === "outcome" ? (
                          <Select
                            {...(record.outcome ? { value: record.outcome } : {})}
                            onValueChange={(val) => setField("outcome", val)}
                          >
                            <SelectTrigger>
                              <SelectValue placeholder="선택되지 않음" />
                            </SelectTrigger>
                            <SelectContent>
                              {record.outcome &&
                              record.outcome !== "진료 진행 중" &&
                              !outcomeOptions.some((option) => option === record.outcome) ? (
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
                        {generated && provenance ? (
                          <FieldProvenancePanel
                            key={`${provenanceRevision}:${key}`}
                            provenance={provenance}
                            draftValue={record[key]}
                            onDraftValueChange={(value) => setField(key, value)}
                          />
                        ) : null}
                      </div>
                    );
                  })}
                </div>
              </ScrollArea>
              <div className="flex justify-end gap-2 border-t pt-3">
                <Button
                  variant="outline"
                  disabled={saving || isSigned}
                  onClick={() => void persistDraft()}
                >
                  {saving ? (
                    <Loader2 className="size-4 animate-spin" />
                  ) : (
                    <Save className="size-4" />
                  )}
                  임시저장
                </Button>
                <Button
                  onClick={() => {
                    if (!canProceedToCheck) {
                      toast.error("AI 응급기록을 생성하거나 직접 작성한 기록을 임시저장해 주세요.");
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
              <Button
                variant="outline"
                disabled={saving || isSigned}
                onClick={() => void persistDraft()}
              >
                {saving ? (
                  <Loader2 className="size-4 animate-spin" />
                ) : (
                  <Save className="size-4" />
                )}
                임시저장
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
                  <CardTitle className="text-base">기록 작성 및 누락 항목 보완</CardTitle>
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
                        {key === "impression" ? (
                          <div className="space-y-2">
                            {diagnosisEntries.map((diagnosis, index) => (
                              <div key={index} className="flex items-center gap-2">
                                <span className="w-20 shrink-0 text-xs font-semibold text-muted-foreground">
                                  {index + 1}. {index === 0 ? "주진단" : "부진단"}
                                </span>
                                <Input
                                  value={diagnosis}
                                  placeholder={index === 0 ? "주진단 입력" : "부진단 입력"}
                                  onChange={(event) => setDiagnosis(index, event.target.value)}
                                />
                                <label className="flex shrink-0 items-center gap-1.5 text-xs font-semibold">
                                  <Checkbox
                                    checked={diagnosisRuleOuts[index] ?? false}
                                    onCheckedChange={(checked) =>
                                      setDiagnosisRuleOut(index, checked === true)
                                    }
                                  />
                                  R/O
                                </label>
                                {index > 0 ? (
                                  <Button
                                    type="button"
                                    size="icon"
                                    variant="ghost"
                                    aria-label={`${index + 1}번 부진단 삭제`}
                                    onClick={() => removeSecondaryDiagnosis(index)}
                                  >
                                    <X className="size-4" />
                                  </Button>
                                ) : null}
                              </div>
                            ))}
                            <Button
                              type="button"
                              size="sm"
                              variant="outline"
                              onClick={addSecondaryDiagnosis}
                            >
                              <Plus className="size-4" /> 부진단 추가
                            </Button>
                          </div>
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
                      <Brain className="size-4 text-primary" /> KCD-9차 진단코드 검색 및 추천
                      <Badge variant="outline" className="ml-auto bg-mint-soft text-navy">
                        AI
                      </Badge>
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-3 pt-4">
                    {selectedKcds.length > 0 ? (
                      <div className="space-y-2 rounded-md border border-primary/40 bg-accent/30 p-3">
                        {selectedKcds.map((item, index) => (
                          <div
                            key={`${item.code}-${item.name}`}
                            className="flex items-center justify-between gap-2"
                          >
                            <div className="min-w-0">
                              <p className="text-xs font-semibold text-muted-foreground">
                                {index + 1}. {index === 0 ? "주진단" : "부진단"}
                                {diagnosisRuleOuts[index] ? " · R/O" : ""}
                              </p>
                              <p className="truncate text-sm font-semibold">
                                {item.name}{" "}
                                <span className="font-mono text-primary">{item.code}</span>
                              </p>
                            </div>
                            <Button
                              type="button"
                              size="icon"
                              variant="ghost"
                              aria-label={`${item.name} 선택 해제`}
                              onClick={() => {
                                setSelectedKcds((current) =>
                                  current.slice(0, index),
                                );
                                setKcdSearch("");
                              }}
                            >
                              <X className="size-4" />
                            </Button>
                          </div>
                        ))}
                      </div>
                    ) : null}
                    <div>
                      <p className="text-xs font-semibold text-muted-foreground">
                        {nextDiagnosisIndex + 1}. {nextDiagnosisIndex === 0 ? "주진단" : "부진단"} 코드 추천
                      </p>
                      {nextDiagnosis ? (
                        <p className="mt-1 text-sm font-semibold">{nextDiagnosis}</p>
                      ) : (
                        <p className="mt-1 text-xs text-muted-foreground">
                          왼쪽에서 {nextDiagnosisIndex === 0 ? "주진단" : "부진단"}을 입력하거나 먼저 검색하세요.
                        </p>
                      )}
                    </div>
                    <Input
                      value={kcdSearch}
                      placeholder="KCD 코드 또는 진단명 검색"
                      onChange={(event) => setKcdSearch(event.target.value)}
                    />
                    {kcdQueryText ? (
                      <div className="space-y-2">
                        {kcdSearchQuery.isFetching ? (
                          <p className="flex items-center gap-2 rounded-md border border-dashed p-3 text-xs text-muted-foreground">
                            <Loader2 className="size-3 animate-spin" /> KCD-9차 상병마스터를 검색하고
                            있습니다.
                          </p>
                        ) : null}
                        {kcdSearchQuery.data?.items.map((candidate) => {
                          const active = selectedKcds.some(
                            (item) => item.code === candidate.code && item.name === candidate.name,
                          );
                          return (
                            <div
                              key={`${candidate.code}-${candidate.name}`}
                              className={`rounded-md border p-3 ${
                                active ? "border-primary bg-accent/40" : "bg-secondary/40"
                              }`}
                            >
                              <div className="flex items-start justify-between gap-3">
                                <div>
                                  <p className="text-sm font-semibold">{candidate.name}</p>
                                  <p className="font-mono text-sm font-bold text-primary">
                                    {candidate.code}
                                  </p>
                                  {candidate.name_en ? (
                                    <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">
                                      {candidate.name_en}
                                    </p>
                                  ) : null}
                                </div>
                                <Button
                                  size="sm"
                                  variant={active ? "default" : "outline"}
                                  disabled={active}
                                  onClick={() => {
                                    const selection = {
                                      code: candidate.code,
                                      name: candidate.name,
                                      is_rule_out: diagnosisRuleOuts[nextDiagnosisIndex] ?? false,
                                    };
                                    if (!nextDiagnosis) {
                                      setDiagnosis(nextDiagnosisIndex, candidate.name);
                                    }
                                    setSelectedKcds((current) => [...current, selection]);
                                    setKcdSearch("");
                                  }}
                                >
                                  {active
                                    ? "선택됨"
                                    : nextDiagnosisIndex === 0
                                      ? "주진단 선택"
                                      : "부진단 선택"}
                                </Button>
                              </div>
                            </div>
                          );
                        })}
                        {kcdSearchQuery.isError ? (
                          <p className="rounded-md border border-risk-critical/40 bg-risk-critical-soft p-3 text-xs text-risk-critical">
                            KCD 상병마스터를 조회하지 못했습니다. 잠시 후 다시 시도해 주세요.
                          </p>
                        ) : null}
                        {!kcdSearchQuery.isFetching && kcdSearchQuery.data?.items.length === 0 ? (
                          <div className="space-y-2 rounded-md border border-dashed p-3">
                            <p className="text-xs text-muted-foreground">
                              일치하는 KCD-9차 코드가 없습니다. 코드 또는 진단명을 다시 확인해 주세요.
                            </p>
                          </div>
                        ) : null}
                      </div>
                    ) : (
                      <p className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
                        주진단 또는 부진단을 입력하면 해당 순서의 KCD-9차 코드 후보가 자동으로
                        표시됩니다. 먼저 검색해서 코드를 선택하면 진단명도 자동으로 입력됩니다.
                      </p>
                    )}
                    <p className="text-xs font-medium text-muted-foreground">
                      입력한 진단 {diagnosisCount}개 · 선택한 코드 {selectedKcds.length}개
                    </p>
                    <p className="text-xs text-muted-foreground">
                      AI 추천 결과는 의료진의 진단 및 질병분류를 보조하는 정보입니다.
                    </p>
                  </CardContent>
                </Card>

                <Card>
                  <CardContent className="space-y-3 py-4">
                    <p className="text-xs text-muted-foreground">
                      필수 누락 항목 {missingRequired.length}건이 남아 있습니다.
                    </p>
                    <div className="grid grid-cols-2 gap-2">
                      <Button variant="outline" onClick={() => setStep(1)}>
                        이전
                      </Button>
                      <Button onClick={goStep3}>
                        다음: 최종 기록
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
            <CardTitle className="text-base">최종 응급진료기록</CardTitle>
          </CardHeader>
          <CardContent className="pt-4">
            <dl className="divide-y">
              {fieldOrder.map((key) => (
                <div key={key} className="grid grid-cols-[160px_1fr] gap-4 py-2.5">
                  <dt className="text-sm font-semibold text-muted-foreground">
                    {recordFieldLabels[key]}
                  </dt>
                  <dd className="text-sm">
                    {key === "impression" ? (
                      <div className="space-y-1">
                        {diagnosisEntries.filter((diagnosis) => diagnosis.trim()).map((diagnosis, index) => (
                          <p key={`${index}-${diagnosis}`}>
                            <span className="mr-2 font-semibold">
                              {index === 0 ? "(주)" : "(부)"}
                              {diagnosisRuleOuts[index] ? "(R/O)" : ""}
                            </span>
                            {diagnosis}
                          </p>
                        ))}
                      </div>
                    ) : (
                      record[key] || "미확인"
                    )}
                  </dd>
                </div>
              ))}
              <div className="grid grid-cols-[160px_1fr] gap-4 py-2.5">
                <dt className="text-sm font-semibold text-muted-foreground">선택된 KCD 진단코드</dt>
                <dd className="space-y-3 text-sm">
                  {selectedKcds.map((item, index) => (
                    <div key={`${item.code}-${item.name}`}>
                      <p>
                        <span className="mr-2 font-semibold">
                          {index === 0 ? "(주)" : "(부)"}
                          {diagnosisRuleOuts[index] ? "(R/O)" : ""}
                        </span>
                        <span className="text-muted-foreground">추정진단:</span>{" "}
                        <span className="font-semibold">{diagnosisEntries[index]?.trim()}</span>
                      </p>
                      <p className="ml-10">
                        <span className="text-muted-foreground">KCD 진단:</span>{" "}
                        <span className="font-semibold">{item.name}</span>{" "}
                        <span className="font-mono font-semibold text-primary">({item.code})</span>
                      </p>
                    </div>
                  ))}
                </dd>
              </div>
            </dl>
            <Separator className="my-4" />
            <div className="flex items-center justify-between">
              <p className="text-xs text-muted-foreground">
                최종 검토용 요약 화면입니다. 수정이 필요하면 이전 단계로 이동하세요.
              </p>
              <div className="flex gap-2">
                <Button variant="outline" onClick={() => setStep(2)}>
                  <ArrowLeft className="size-4" /> 이전
                </Button>
                <Button
                  disabled={isSigned}
                  onClick={() => {
                    setAgreed(false);
                    setCertifyOpen(true);
                  }}
                >
                  <FileCheck2 className="size-4" /> 의사 검토 및 최종 인증
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* STEP 4 */}
      {step === 4 && (
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
                <dt className="text-xs text-muted-foreground">진단명</dt>
                <dd className="font-medium">
                  {selectedKcds
                    .map(
                      (item, index) =>
                        `${index === 0 ? "(주)" : "(부)"}${item.is_rule_out ? "(R/O)" : ""} ${item.name}`,
                    )
                    .join(", ")}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">KCD 코드</dt>
                <dd className="font-mono font-medium text-primary">
                  {selectedKcds.map((item) => item.code).join(", ")}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">검토 의사 / 인증 일시</dt>
                <dd className="font-medium">
                  {currentUser.dept} {currentUser.name} · {certifiedAt}
                </dd>
              </div>
            </dl>
            <div className="flex justify-center gap-2 pt-4">
              <Button variant="outline" onClick={() => setStep(3)}>
                최종 기록 보기
              </Button>
              <Button asChild>
                <Link to="/records" search={(prev) => prev}>
                  환자 목록으로
                </Link>
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
              [
                "최종 진단명",
                selectedKcds
                  .map(
                    (item, index) =>
                      `${index === 0 ? "(주)" : "(부)"}${item.is_rule_out ? "(R/O)" : ""} ${item.name}`,
                  )
                  .join(", ") || "-",
              ],
              ["선택된 KCD 코드", selectedKcds.map((item) => item.code).join(", ") || "-"],
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
            <Button disabled={!agreed || signing || saving} onClick={() => void certifyRecord()}>
              {signing ? <Loader2 className="size-4 animate-spin" /> : null}
              인증 저장
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function createWorkflowPatient() {
  return {
    id: "",
    name: "",
    sex: "",
    age: "" as number | string,
    arrivedAt: "",
    ktas: "" as number | string,
    recordStatus: "",
    vitals: {
      hr: "" as number | string,
      rr: "" as number | string,
      sbp: "" as number | string,
      dbp: "" as number | string,
      bt: "" as number | string,
      spo2: "" as number | string,
      mental: "",
    },
  };
}
