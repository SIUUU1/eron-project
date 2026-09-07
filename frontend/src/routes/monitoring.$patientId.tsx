import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import {
  AlertCircle,
  ArrowLeft,
  Brain,
  CheckCircle2,
  ClipboardCheck,
  Info,
  Monitor,
  PhoneCall,
  ShieldAlert,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { toast } from "sonner";

import { acknowledgeAlert } from "@/api/dashboard";
import { edStayKeys, getEdStay, getEdStayPredictions, getEdStayVitals } from "@/api/ed-stays";
import { invalidatePredictionQueries } from "@/api/refresh";
import {
  bandMeta,
  formatDateTime,
  formatTime,
  num,
  routeLabel,
  sexLabel,
  toPercent,
  transportLabel,
} from "@/api/display";
import type { ReasonType } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
// recharts 의 Tooltip 과 이름이 겹치므로 안내문구용 Tooltip 만 별칭으로 가져온다.
import {
  Tooltip as InfoTooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { currentUser } from "@/lib/mock-data";

export const Route = createFileRoute("/monitoring/$patientId")({
  head: ({ params }) => {
    const ogTitle = `환자 ${params.patientId} 모니터링 · ER:ON(이로운)`;
    return {
      meta: [
        { name: "description", content: "MIMIC-IV 기반 응급실 환자 상세 모니터링 화면." },
        { property: "og:title", content: ogTitle },
      ],
    };
  },
  component: PatientMonitoringPage,
});

const BASE_SERIES = [
  { key: "hr", name: "HR (bpm)", color: "var(--chart-2)", axis: "left" as const },
  { key: "sbp", name: "SBP (mmHg)", color: "var(--chart-3)", axis: "left" as const },
  { key: "dbp", name: "DBP (mmHg)", color: "var(--chart-4)", axis: "left" as const },
  { key: "spo2", name: "SpO₂ (%)", color: "var(--chart-6)", axis: "left" as const },
  { key: "bt", name: "BT (℃)", color: "var(--chart-5)", axis: "right" as const },
];
/** reason_title 이 없을 때만 쓰는 대체 라벨. 문구는 "원인"이 아니라 "신호" 로 쓴다. */
const REASON_TYPE_LABEL: Record<ReasonType, string> = {
  risk_increase_clinical_worsening_signal: "직전 예측 대비 임상적 악화 신호",
  risk_increase_without_confirmed_clinical_worsening_signal: "확인된 임상적 악화 신호 없음",
  current_risk_signal: "현재 위험도 기여",
};

/** 모델이 내려주지만 화면에서는 숨기는 기본 안내 문구(내용 중복이라 노출하지 않는다). */
const HIDDEN_REASON_NOTICE =
  "위험도 변화는 calibrated probability 기준이며, Δcontribution은 LightGBM raw-score SHAP 기준입니다. 직전 대비 악화 신호에는 exact feature 값이 실제로 변했고 사전 정의된 clinical-direction gate에서 worsening으로 판정된 항목만 표시합니다. 개선/중립/방향 미정 변화는 악화 근거에서 제외하며, 임상적 인과관계를 의미하지 않습니다.";

/** 확률 변화(0~1) → 표시용 %p. */
function deltaPoint(delta: number): string {
  const sign = delta >= 0 ? "+" : "−";
  return `${sign}${Math.abs(delta * 100).toFixed(1)}%p`;
}

/**
 * reason_title 배지의 축소 단계. 짧은 문구는 기존 11px 그대로이고,
 * 길어질수록 글자를 줄이면서 좌우 여백·자간을 함께 좁혀 한 줄 안에 전부 넣는다.
 * (7px 은 문구가 매우 길 때만 쓰는 마지막 단계다.)
 */
const REASON_LABEL_BASE_STEP = { size: 11, padding: "0.625rem", tracking: "normal" } as const;
const REASON_LABEL_STEPS = [
  REASON_LABEL_BASE_STEP,
  { size: 10, padding: "0.625rem", tracking: "normal" },
  { size: 9, padding: "0.5rem", tracking: "-0.01em" },
  { size: 8, padding: "0.375rem", tracking: "-0.02em" },
  { size: 7, padding: "0.375rem", tracking: "-0.02em" },
] as const;

const reasonLabelStep = (index: number) => REASON_LABEL_STEPS[index] ?? REASON_LABEL_BASE_STEP;

/** 첫 렌더(SSR 포함)용 근사 단계. 화면에 붙은 뒤 실제 폭으로 다시 맞춘다. */
function estimateReasonLabelStep(label: string): number {
  if (label.length <= 20) return 0;
  if (label.length <= 23) return 1;
  if (label.length <= 26) return 2;
  if (label.length <= 30) return 3;
  return REASON_LABEL_STEPS.length - 1;
}

/**
 * 예측 근거 제목 배지. 문구가 길어도 줄바꿈하거나 잘라내지 않고, 남은 폭에 맞춰
 * 글자 크기만 단계적으로 줄여 한 줄로 보여준다(문구·데이터는 그대로 둔다).
 */
function ReasonLabelBadge({ label }: { label: string }) {
  const textRef = useRef<HTMLSpanElement | null>(null);
  const [step, setStep] = useState(() => estimateReasonLabelStep(label));

  useEffect(() => {
    const text = textRef.current;
    const box = text?.parentElement;
    if (!text || !box) return;

    let frame = 0;
    let disposed = false;
    const fit = () => {
      if (disposed) return;
      let next = 0;
      // 큰 글자부터 시도해 한 줄에 들어가는 첫 단계를 고른다.
      for (; next < REASON_LABEL_STEPS.length; next += 1) {
        const { size, padding, tracking } = reasonLabelStep(next);
        box.style.paddingLeft = padding;
        box.style.paddingRight = padding;
        text.style.fontSize = `${size}px`;
        text.style.letterSpacing = tracking;
        // scrollWidth/clientWidth 는 정수로 반올림되므로 1px 여유를 둔다.
        const fits = text.scrollWidth <= text.clientWidth + 1;
        if (fits || next === REASON_LABEL_STEPS.length - 1) break;
      }
      setStep(next);
    };

    fit();
    // 카드 폭이 바뀌면(창 크기 변경·좁은 화면) 다시 맞춘다.
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(fit);
    });
    observer.observe(box);
    // 본문 웹폰트(Noto Sans KR)는 늦게 도착한다. 폴백 폰트로 잰 결과가 남으면
    // 폰트가 바뀐 뒤 문구가 잘리므로, 로드가 끝나면 한 번 더 맞춘다.
    void document.fonts.ready.then(fit);

    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [label]);

  const { size, padding, tracking } = reasonLabelStep(step);
  return (
    <Badge
      variant="outline"
      className="ml-auto min-w-0 shrink font-normal text-muted-foreground"
      style={{ paddingLeft: padding, paddingRight: padding }}
    >
      {/* leading 을 고정해 글자 크기가 줄어도 배지 높이는 그대로 유지한다. */}
      <span
        ref={textRef}
        className="block min-w-0 overflow-hidden text-ellipsis whitespace-nowrap leading-[15px]"
        style={{ fontSize: `${size}px`, letterSpacing: tracking }}
      >
        {label}
      </span>
    </Badge>
  );
}

function PatientMonitoringPage() {
  const { patientId } = Route.useParams();
  const queryClient = useQueryClient();
  const [reassessedAt, setReassessedAt] = useState<string | null>(null);
  const [patientViewOpen, setPatientViewOpen] = useState(false);
  const [probTrendOpen, setProbTrendOpen] = useState(false);

  const detailQ = useQuery({
    queryKey: edStayKeys.detail(patientId),
    queryFn: ({ signal }) => getEdStay(patientId, signal),
  });
  const vitalsQ = useQuery({
    queryKey: edStayKeys.vitals(patientId),
    queryFn: ({ signal }) => getEdStayVitals(patientId, signal),
  });
  const predQ = useQuery({
    queryKey: edStayKeys.predictions(patientId),
    queryFn: ({ signal }) => getEdStayPredictions(patientId, signal),
  });

  const stamp = () =>
    new Date().toLocaleString("ko-KR", { dateStyle: "medium", timeStyle: "short" });

  /**
   * 재검토 완료. **어느 예측에 대한 확인인지는 서버가 정한다**(현재 최신 예측).
   * 성공하면 종 카운트·환자 목록·경고 목록이 같은 서버 상태를 다시 읽도록 무효화한다.
   * 확인 여부는 예측에서 파생되는 값이라 예측 계열만 받으면 된다(활력징후는 그대로 둔다).
   */
  const acknowledge = useMutation({
    mutationFn: () => acknowledgeAlert(patientId, `${currentUser.dept} ${currentUser.name}`),
    onSuccess: (result) => {
      setReassessedAt(stamp());
      void invalidatePredictionQueries(queryClient);
      toast.success(`재검토 필요 알림 ${result.acknowledged}건을 확인 처리했습니다.`, {
        description: `${currentUser.dept} ${currentUser.name} · 남은 미확인 ${result.unread_count}건`,
        duration: 1000,
      });
    },
    onError: (e: Error) =>
      toast.error("의료진 재검토를 기록하지 못했습니다.", { description: e.message }),
  });

  // ⚠ 상세가 늦다고 화면 전체를 막지 않는다. 활력징후·예측은 각자 도착하는 대로 그린다.
  //   (상세가 404 면 나머지도 같은 stay_id 라 의미가 없으므로 에러만 전체 화면으로 둔다.)
  if (detailQ.isError) {
    const notFound = detailQ.error.message === "ED stay not found";
    return (
      <div className="flex min-h-[50vh] flex-col items-center justify-center gap-3 text-center">
        <AlertCircle className="size-9 text-risk-critical" />
        <h1 className="text-lg font-semibold">
          {notFound ? "환자를 찾을 수 없습니다" : "환자 정보를 불러오지 못했습니다"}
        </h1>
        <p className="text-sm text-muted-foreground">{detailQ.error.message}</p>
        <div className="mt-2 flex gap-2">
          <Button variant="outline" onClick={() => void detailQ.refetch()}>
            다시 시도
          </Button>
          <Button asChild>
            <Link to="/monitoring">환자 목록</Link>
          </Button>
        </div>
      </div>
    );
  }

  // 상세는 아직 로딩 중일 수 있다. 이 값에 기대는 영역만 각자 Skeleton 을 띄운다.
  const patient = detailQ.data;
  const displayName = patient?.display_name ?? `ED-${patientId}`;
  // 화면 배지는 모델 3구간(재평가 필요/관찰 필요/저위험)으로 통일한다.
  const band = patient?.risk_band ?? null;
  const probability = patient ? toPercent(patient.risk_probability) : null;

  const latest = vitalsQ.data?.latest;
  const v = {
    hr: latest?.heart_rate ?? null,
    rr: latest?.resp_rate ?? null,
    sbp: latest?.sbp ?? null,
    dbp: latest?.dbp ?? null,
    bt: latest?.temperature_c ?? null,
    spo2: latest?.spo2 ?? null,
  };
  const abnormal = {
    hr: v.hr !== null && (v.hr > 100 || v.hr < 50),
    rr: v.rr !== null && (v.rr > 20 || v.rr < 10),
    bp: v.sbp !== null && (v.sbp < 100 || v.sbp > 160),
    bt: v.bt !== null && v.bt >= 37.5,
    spo2: v.spo2 !== null && v.spo2 < 94,
  };

  const vitalCards = [
    { label: "HR", value: num(v.hr), unit: "bpm", bad: abnormal.hr },
    { label: "RR", value: num(v.rr), unit: "회/분", bad: abnormal.rr },
    {
      label: "BP",
      value: v.sbp === null && v.dbp === null ? "-" : `${num(v.sbp)}/${num(v.dbp)}`,
      unit: "mmHg",
      bad: abnormal.bp,
    },
    { label: "BT", value: num(v.bt, 1), unit: "℃", bad: abnormal.bt },
    { label: "SpO₂", value: num(v.spo2), unit: "%", bad: abnormal.spo2 },
    { label: "Mental", value: latest?.consciousness ?? "-", unit: "", bad: false },
  ];

  const predictions = predQ.data?.predictions ?? [];
  const hasPrediction = predictions.length > 0;

  // "AI 악화 예측 확률" 타일을 누르면 보여줄 예측 확률 추이.
  // 예측 시점(ED 도착 +1h 부터 1시간 간격)이 그대로 x축이 된다.
  const probTrend = predictions.map((p) => ({
    time: formatTime(p.prediction_time),
    pct: Math.round(p.risk_probability * 1000) / 10,
  }));
  const probMax = Math.max(10, ...probTrend.map((p) => p.pct));
  // 위험 확률은 AI 분석 카드에서만 보여준다. 이 표/그래프는 활력징후 추이 전용이다.
  const series = BASE_SERIES;

  const trend = (vitalsQ.data?.vitals ?? []).map((p) => ({
    time: formatTime(p.measured_at),
    hr: p.heart_rate,
    sbp: p.sbp,
    dbp: p.dbp,
    spo2: p.spo2,
    bt: p.temperature_c,
  }));

  // 최신 예측 시점의 기여 신호. 모델이 만든 문장을 그대로 쓴다(프론트에서 만들지 않는다).
  const latestPrediction = predQ.data?.latest;
  const riskFactors = latestPrediction?.risk_factors ?? [];
  const reasonType = latestPrediction?.reason_type ?? null;
  // 위험은 올랐지만 임상 방향 gate 를 통과한 악화 변화가 없는 상태.
  // 신호 목록이 비는 것이 정상이므로 "모델이 제공하지 않음" 으로 표시하면 안 된다.
  const noConfirmedWorsening =
    latestPrediction?.reason_type === "risk_increase_without_confirmed_clinical_worsening_signal";
  // 제목은 모델이 만든 reason_title 을 우선한다(프론트에서 문구를 만들지 않는다).
  // 예외: "확인된 악화 신호 없음" 유형만 배지 폭에 맞는 짧은 고정 라벨을 쓴다.
  //   ("직전 대비" 라는 비교 기준은 바로 아래 본문 문장에 그대로 남아 있다.)
  const reasonLabel = noConfirmedWorsening
    ? REASON_TYPE_LABEL.risk_increase_without_confirmed_clinical_worsening_signal
    : (latestPrediction?.reason_title ?? (reasonType ? REASON_TYPE_LABEL[reasonType] : null));
  const reasonNotice = latestPrediction?.reason_notice ?? null;
  const riskDelta = latestPrediction?.risk_delta ?? null;

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <Button asChild variant="ghost" size="sm">
          <Link to="/monitoring">
            <ArrowLeft className="size-4" /> 환자 목록
          </Link>
        </Button>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => setPatientViewOpen(true)}>
            <Monitor className="size-4" /> 환자 모니터 화면 미리보기
          </Button>
          <Button asChild>
            <Link to="/records/$patientId" params={{ patientId }}>
              <ClipboardCheck className="size-4" /> AI 진료기록 작성
            </Link>
          </Button>
        </div>
      </div>

      {/* 환자 핵심 정보 */}
      <Card className="overflow-hidden">
        <div className="flex items-stretch">
          <div className={`w-1.5 ${band ? bandMeta[band].dot : "bg-border"}`} />
          <CardContent className="flex flex-1 items-center gap-8 py-5">
            {!patient ? (
              // 상세만 늦게 오는 경우 — 이 카드 자리만 잡아 두고 아래 영역은 그대로 그린다.
              <div className="flex flex-1 items-center gap-8">
                <Skeleton className="h-12 w-56" />
                <Separator orientation="vertical" className="h-12" />
                <div className="grid flex-1 grid-cols-4 gap-x-8 gap-y-1.5">
                  {Array.from({ length: 6 }).map((_, i) => (
                    <Skeleton key={i} className="h-9" />
                  ))}
                </div>
                <Skeleton className="ml-auto h-[76px] w-32" />
              </div>
            ) : (
              <>
                <div>
                  <p className="text-xs text-muted-foreground">{patient.stay_id}</p>
                  <p className="text-2xl font-bold tracking-tight">
                    {displayName}
                    <span className="ml-2 text-base font-medium text-muted-foreground">
                      {sexLabel(patient.sex) === "남"
                        ? "남성"
                        : sexLabel(patient.sex) === "여"
                          ? "여성"
                          : "-"}
                      {patient.age !== null ? `, ${patient.age}세` : ""}
                    </span>
                  </p>
                </div>
                <Separator orientation="vertical" className="h-12" />
                <dl className="grid grid-cols-4 gap-x-8 gap-y-1.5 text-sm">
                  <div>
                    <dt className="text-xs text-muted-foreground">내원시간</dt>
                    <dd className="tabular font-medium">{formatDateTime(patient.arrived_at)}</dd>
                  </div>
                  <div>
                    <dt className="text-xs text-muted-foreground">내원경로</dt>
                    <dd className="font-medium">{routeLabel(patient.arrival_route)}</dd>
                  </div>
                  <div>
                    <dt className="text-xs text-muted-foreground">내원수단</dt>
                    <dd className="font-medium">{transportLabel(patient.arrival_transport)}</dd>
                  </div>
                  <div>
                    <dt className="text-xs text-muted-foreground">KTAS</dt>
                    <dd className="font-bold text-risk-rising">
                      {patient.acuity === null ? "-" : `Level ${patient.acuity}`}
                    </dd>
                  </div>
                  <div className="col-span-3">
                    <dt className="text-xs text-muted-foreground">주증상</dt>
                    <dd className="font-medium">{patient.chief_complaint_detail ?? "-"}</dd>
                  </div>
                  <div>
                    <dt className="text-xs text-muted-foreground">현재 상태</dt>
                    <dd>
                      {band ? (
                        <Badge variant="outline" className={bandMeta[band].badge}>
                          {bandMeta[band].label}
                        </Badge>
                      ) : (
                        <Badge variant="outline" className="text-muted-foreground">
                          평가 대기
                        </Badge>
                      )}
                    </dd>
                  </div>
                </dl>
                <button
                  type="button"
                  onClick={() => setProbTrendOpen(true)}
                  disabled={!hasPrediction}
                  className="ml-auto rounded-lg bg-navy px-6 py-3 text-center text-navy-foreground transition-colors hover:bg-navy/85 disabled:cursor-default disabled:hover:bg-navy"
                >
                  <p className="text-xs opacity-75">AI 악화 예측 확률</p>
                  <p className="tabular text-3xl font-bold">
                    {probability === null ? "–" : `${probability}%`}
                  </p>
                  <p className="text-[11px] opacity-70">
                    {hasPrediction ? "클릭하면 예측 추이" : "예측 대기"}
                  </p>
                </button>
              </>
            )}
          </CardContent>
        </div>
      </Card>

      {/* 왼쪽(시간별 상태) 내용이 아무리 넓어져도 AI 분석 열이 밀리지 않도록
          트랙을 minmax(0,1fr) 로 잡고 열 자체에 min-w-0 을 준다.
          좁은 화면에서는 가로 스크롤 대신 한 열로 쌓는다. */}
      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="min-w-0 space-y-5">
          {/* Vital */}
          <Card>
            <CardHeader className="border-b py-3">
              <CardTitle className="text-base">
                현재 Vital
                {latest?.measured_at ? (
                  <span className="ml-2 text-xs font-normal text-muted-foreground">
                    {formatDateTime(latest.measured_at)} 측정
                  </span>
                ) : null}
              </CardTitle>
            </CardHeader>
            <CardContent className="grid grid-cols-6 gap-3 pt-4">
              {vitalsQ.isPending
                ? Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-16" />)
                : vitalCards.map((c) => (
                    <div
                      key={c.label}
                      className={`rounded-md border px-3 py-2.5 ${
                        c.bad ? "border-risk-critical/40 bg-risk-critical-soft" : "bg-secondary/40"
                      }`}
                    >
                      <p className="text-xs text-muted-foreground">{c.label}</p>
                      <p
                        className={`tabular text-xl font-bold ${c.bad ? "text-risk-critical" : "text-foreground"}`}
                      >
                        {c.value}
                        <span className="ml-1 text-xs font-medium text-muted-foreground">
                          {c.unit}
                        </span>
                      </p>
                    </div>
                  ))}
            </CardContent>
          </Card>

          {/* 통합 그래프 */}
          <Card>
            <CardHeader className="border-b py-3">
              <CardTitle className="text-base">시간별 상태 변화</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4 pt-4">
              {vitalsQ.isPending ? (
                <Skeleton className="h-72 w-full" />
              ) : trend.length === 0 ? (
                <p className="py-20 text-center text-sm text-muted-foreground">
                  측정된 활력징후가 없습니다.
                </p>
              ) : (
                <>
                  <div className="h-72">
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart data={trend} margin={{ top: 8, right: 8, bottom: 0, left: -8 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                        <XAxis
                          dataKey="time"
                          tick={{ fontSize: 12 }}
                          stroke="var(--muted-foreground)"
                        />
                        <YAxis
                          yAxisId="left"
                          domain={[0, 180]}
                          tick={{ fontSize: 11 }}
                          stroke="var(--muted-foreground)"
                        />
                        <YAxis
                          yAxisId="right"
                          orientation="right"
                          domain={[0, 100]}
                          tick={{ fontSize: 11 }}
                          stroke="var(--muted-foreground)"
                        />
                        <Tooltip
                          contentStyle={{
                            borderRadius: 8,
                            border: "1px solid var(--border)",
                            fontSize: 12,
                          }}
                        />
                        <Legend wrapperStyle={{ fontSize: 12 }} />
                        {series.map((s) => (
                          <Line
                            key={s.key}
                            yAxisId={s.axis}
                            type="monotone"
                            dataKey={s.key}
                            name={s.name}
                            stroke={s.color}
                            strokeWidth={2}
                            dot={{ r: 3 }}
                            activeDot={{ r: 5 }}
                            isAnimationActive={false}
                            connectNulls
                          />
                        ))}
                      </LineChart>
                    </ResponsiveContainer>
                  </div>

                  {/* 측정 시각이 늘어나도 세로로 잘리지 않도록 가로 스크롤만 사용한다 */}
                  <div className="overflow-x-auto rounded-md border">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="text-navy-foreground">
                          <th className="sticky top-0 z-10 bg-navy px-3 py-2 text-left font-medium">
                            항목
                          </th>
                          {trend.map((t, i) => (
                            <th
                              key={i}
                              className="tabular sticky top-0 z-10 bg-navy px-3 py-2 text-center font-medium"
                            >
                              {t.time}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody className="divide-y">
                        {series.map((s) => (
                          <tr key={s.key}>
                            <td className="px-3 py-2">
                              <span className="flex items-center gap-2">
                                <span
                                  className="h-0.5 w-4 rounded"
                                  style={{ backgroundColor: s.color }}
                                />
                                {s.name}
                              </span>
                            </td>
                            {trend.map((t, i) => {
                              const value = t[s.key as keyof typeof t];
                              return (
                                <td key={i} className="tabular px-3 py-2 text-center">
                                  {value === null || value === undefined ? "-" : String(value)}
                                </td>
                              );
                            })}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </CardContent>
          </Card>
        </div>

        {/* AI 분석 */}
        <div className="space-y-4">
          <Card className={band === "red" ? "border-risk-critical/30" : ""}>
            <CardHeader className="border-b py-3">
              <CardTitle className="flex items-center gap-2 text-base">
                <Brain className="size-4 text-primary" /> AI 분석
                {/* 안내문구는 항상 노출하지 않고 이 아이콘에 hover 했을 때만 띄운다 */}
                <TooltipProvider delayDuration={100}>
                  <InfoTooltip>
                    <TooltipTrigger
                      type="button"
                      aria-label="AI 분석 산출 기준 안내"
                      className="-ml-1 rounded-full text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                    >
                      <Info className="size-3.5" />
                    </TooltipTrigger>
                    <TooltipContent
                      side="bottom"
                      align="start"
                      collisionPadding={12}
                      className="max-w-xs text-[11px] font-normal leading-relaxed"
                    >
                      직전 예측 대비 실제로 악화된 주요 임상 지표만 표시합니다. 개선된 항목은
                      제외되며, 의료진 판단을 위한 참고 정보입니다.
                    </TooltipContent>
                  </InfoTooltip>
                </TooltipProvider>
                <Badge variant="outline" className="ml-auto bg-mint-soft text-navy">
                  AI
                </Badge>
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4 pt-4">
              {predQ.isPending ? (
                // 예측만 늦게 오는 경우 — 환자정보·활력징후는 이미 화면에 떠 있다.
                <div className="space-y-3">
                  <Skeleton className="h-16 w-full" />
                  <Skeleton className="h-28 w-full" />
                </div>
              ) : !hasPrediction ? (
                <div className="rounded-md border bg-secondary/40 px-4 py-6 text-center">
                  <p className="text-sm font-semibold">AI 분석 대기 중</p>
                  <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">
                    악화 예측 모델이 연동되지 않아 위험도·위험요인·권고를 표시할 수 없습니다.
                    <br />
                    환자 정보와 활력징후는 실제 데이터입니다.
                  </p>
                </div>
              ) : (
                <>
                  {band ? (
                    <div
                      className={`rounded-md border px-4 py-3 text-center ${bandMeta[band].badge}`}
                    >
                      <p className="text-xs opacity-80">위험도</p>
                      <p className="text-lg font-bold">{bandMeta[band].label}</p>
                      {riskDelta !== null ? (
                        <p className="tabular text-xs opacity-80">
                          직전 예측 대비 {deltaPoint(riskDelta)}
                        </p>
                      ) : null}
                    </div>
                  ) : (
                    // 예측은 왔는데 상세(위험도 배지)가 아직인 경우
                    <Skeleton className="h-16 w-full" />
                  )}

                  <div>
                    <p className="mb-2 flex items-center gap-1.5 text-sm font-semibold">
                      <ShieldAlert className="size-4 shrink-0 text-risk-rising" />
                      <span className="shrink-0">주요 위험 신호</span>
                      {reasonLabel ? <ReasonLabelBadge label={reasonLabel} /> : null}
                    </p>
                    {riskFactors.length === 0 ? (
                      <div className="rounded-md border bg-secondary/40 px-3 py-2.5">
                        <p className="text-xs font-medium">
                          {noConfirmedWorsening
                            ? "직전 예측 대비 확인된 임상적 악화 신호가 없습니다."
                            : "표시할 신호가 없습니다."}
                        </p>
                        {noConfirmedWorsening ? (
                          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                            악화 확률은 올랐지만, 값이 실제로 나빠진 지표가 확인되지 않았습니다.
                            개선·중립·방향 미정 변화는 악화 근거로 쓰지 않습니다.
                          </p>
                        ) : null}
                      </div>
                    ) : (
                      <>
                        <ul className="space-y-1.5">
                          {riskFactors.map((f) => (
                            <li
                              key={f}
                              className="flex items-start gap-2 text-sm text-muted-foreground"
                            >
                              <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-risk-rising" />
                              {f}
                            </li>
                          ))}
                        </ul>
                        {reasonNotice && reasonNotice !== HIDDEN_REASON_NOTICE ? (
                          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                            {reasonNotice}
                          </p>
                        ) : null}
                      </>
                    )}
                  </div>
                </>
              )}

              {/* 🔑 버튼 활성 조건은 '현재 데모 시각까지 도래한 미확인 재검토 필요 알림'이다.
                  화면 상태가 아니라 서버가 계산한 alert_unread 를 그대로 쓴다. */}
              <div className="grid gap-2">
                <Button
                  disabled={!patient || acknowledge.isPending || patient.alert_unread === 0}
                  onClick={() => acknowledge.mutate()}
                >
                  <CheckCircle2 className="size-4" />
                  {!patient || patient.alert_unread > 0 ? "의료진 재검토" : "의료진 재검토 완료"}
                </Button>
                {patient && patient.alert_unread > 0 ? (
                  <p className="text-center text-xs text-muted-foreground">
                    미확인 재검토 필요 알림 {patient.alert_unread}건
                  </p>
                ) : null}
              </div>

              {patient?.reviewed && (
                <div className="space-y-1 rounded-md border bg-risk-stable-soft px-3 py-2 text-xs text-risk-stable">
                  <p>
                    의료진 재검토 완료 · {currentUser.dept} {currentUser.name}
                    {reassessedAt ? ` · ${reassessedAt}` : ""}
                  </p>
                  {/* 확인은 '그 시점 알림'에 대한 것이다. 다음 예측이 red 면 다시 활성화된다. */}
                  <p className="font-semibold">
                    재검토 필요 알림 {patient.alert_total}건 확인됨 — 다음 예측에서 다시 재검토
                    필요가 나오면 버튼이 다시 활성화됩니다.
                  </p>
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      {/* AI 악화 예측 확률 추이 */}
      <Dialog open={probTrendOpen} onOpenChange={setProbTrendOpen}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              AI 악화 예측 확률 추이
              {band ? (
                <Badge variant="outline" className={bandMeta[band].badge}>
                  {bandMeta[band].label}
                </Badge>
              ) : null}
            </DialogTitle>
          </DialogHeader>

          {probTrend.length === 0 ? (
            <p className="py-16 text-center text-sm text-muted-foreground">
              표시할 예측이 없습니다.
            </p>
          ) : (
            <div className="space-y-4">
              <div className="h-64">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={probTrend} margin={{ top: 8, right: 8, bottom: 0, left: -12 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                    <XAxis
                      dataKey="time"
                      tick={{ fontSize: 12 }}
                      stroke="var(--muted-foreground)"
                    />
                    <YAxis
                      domain={[0, Math.ceil(probMax * 1.2)]}
                      unit="%"
                      tick={{ fontSize: 11 }}
                      stroke="var(--muted-foreground)"
                    />
                    <Tooltip
                      formatter={(v: number) => [`${v}%`, "악화 확률"]}
                      contentStyle={{
                        borderRadius: 8,
                        border: "1px solid var(--border)",
                        fontSize: 12,
                      }}
                    />
                    <Line
                      type="monotone"
                      dataKey="pct"
                      name="AI 악화 확률 (%)"
                      stroke="var(--chart-1)"
                      strokeWidth={3}
                      dot={{ r: 3 }}
                      activeDot={{ r: 5 }}
                      isAnimationActive={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>

              <div className="grid grid-cols-3 gap-3 text-center">
                {[
                  ["첫 예측", probTrend[0]?.pct],
                  ["최고", probMax],
                  ["최근", probTrend[probTrend.length - 1]?.pct],
                ].map(([label, value]) => (
                  <div key={String(label)} className="rounded-md border bg-secondary/40 px-3 py-2">
                    <p className="text-xs text-muted-foreground">{label}</p>
                    <p className="tabular text-lg font-bold">{value ?? "-"}%</p>
                  </div>
                ))}
              </div>

              <p className="text-xs leading-relaxed text-muted-foreground">
                각 시점의 값은 그 시각 기준 3시간 내 악화 확률(보정 확률)입니다. 예측은 ED 도착
                +1시간부터 1시간 간격으로 생성되며, 의료진 의사결정 지원 정보입니다.
              </p>
            </div>
          )}
        </DialogContent>
      </Dialog>

      {/* 환자용 상태 안내 화면 */}
      <Dialog open={patientViewOpen} onOpenChange={setPatientViewOpen}>
        <DialogContent className="max-w-2xl border-0 bg-navy text-navy-foreground">
          <DialogHeader>
            <DialogTitle className="text-navy-foreground">환자 모니터 화면 미리보기</DialogTitle>
          </DialogHeader>
          <div className="space-y-5 rounded-lg bg-navy-soft p-7 text-center">
            <Badge className="bg-risk-rising text-primary-foreground">주의가 필요한 상태</Badge>
            <p className="text-2xl font-bold leading-relaxed">
              현재 의료진의 재평가가 필요한 상태입니다.
            </p>
            <p className="text-lg text-navy-foreground/90">
              안전을 위해 의료진의 안내 없이 응급실을 떠나지 마세요.
            </p>
            <p className="text-base text-navy-foreground/80">
              담당 의료진이 현재 상태를 확인하고 있습니다.
            </p>

            <div className="grid grid-cols-3 gap-3 pt-2 text-left">
              {[
                ["접수", "완료"],
                ["진찰 및 검사", "진행 중"],
                ["결과 확인", "대기"],
              ].map(([step, state], i) => (
                <div
                  key={step}
                  className={`rounded-md px-4 py-3 ${i === 1 ? "bg-risk-rising/25 ring-1 ring-risk-rising" : "bg-navy/50"}`}
                >
                  <p className="text-xs opacity-70">진료 단계 {i + 1}</p>
                  <p className="mt-0.5 text-base font-bold">{step}</p>
                  <p className="text-sm opacity-85">{state}</p>
                </div>
              ))}
            </div>

            <div className="rounded-md bg-navy/50 px-4 py-3 text-base">
              예상 대기 상태 · <span className="font-bold">의료진 확인 중 (약 10분 내 안내)</span>
            </div>

            <Button
              size="lg"
              className="w-full bg-risk-rising text-primary-foreground hover:bg-risk-rising/90"
              onClick={() => toast.success("담당 의료진을 호출했습니다. 잠시만 기다려 주세요.")}
            >
              <PhoneCall className="size-5" /> 의료진 호출
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
