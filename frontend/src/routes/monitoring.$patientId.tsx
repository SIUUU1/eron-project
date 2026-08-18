import { createFileRoute, Link, notFound } from "@tanstack/react-router";
import {
  ArrowLeft,
  BellRing,
  Brain,
  CheckCircle2,
  ClipboardCheck,
  Info,
  Monitor,
  PhoneCall,
  ShieldAlert,
} from "lucide-react";
import { useState } from "react";
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

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Separator } from "@/components/ui/separator";
import { currentUser, getPatient, riskMeta } from "@/lib/mock-data";

export const Route = createFileRoute("/monitoring/$patientId")({
  loader: ({ params }) => {
    const patient = getPatient(params.patientId);
    if (!patient) throw notFound();
    return { patient };
  },
  head: ({ loaderData }) => {
    if (!loaderData) {
      return { meta: [{ title: "환자 정보 없음 · ER-GUARD AI" }, { name: "robots", content: "noindex" }] };
    }
    const p = loaderData.patient;
    const title = `${p.name} 환자 모니터링 · ER-GUARD AI`;
    const desc = `${p.name} (${p.sex} ${p.age}세) · ${p.chiefComplaint} · AI 악화 예측 확률 ${p.deteriorationProbability}%`;
    return {
      meta: [
        { title },
        { name: "description", content: desc },
        { property: "og:title", content: title },
        { property: "og:description", content: desc },
      ],
    };
  },
  component: PatientMonitoringPage,
});

const series = [
  { key: "probability", name: "AI 악화 확률 (%)", color: "var(--chart-1)", axis: "right" as const },
  { key: "hr", name: "HR (bpm)", color: "var(--chart-2)", axis: "left" as const },
  { key: "sbp", name: "수축기 혈압 (mmHg)", color: "var(--chart-3)", axis: "left" as const },
  { key: "dbp", name: "이완기 혈압 (mmHg)", color: "var(--chart-4)", axis: "left" as const },
  { key: "spo2", name: "SpO₂ (%)", color: "var(--chart-6)", axis: "left" as const },
  { key: "bt", name: "체온 (℃)", color: "var(--chart-5)", axis: "right" as const },
];

function PatientMonitoringPage() {
  const { patient } = Route.useLoaderData();
  const [reassessedAt, setReassessedAt] = useState<string | null>(null);
  const [ackAt, setAckAt] = useState<string | null>(null);
  const [patientViewOpen, setPatientViewOpen] = useState(false);

  const stamp = () =>
    new Date().toLocaleString("ko-KR", { dateStyle: "medium", timeStyle: "short" });

  const v = patient.vitals;
  const abnormal = {
    hr: v.hr > 100 || v.hr < 50,
    rr: v.rr > 20 || v.rr < 10,
    bp: v.sbp < 100 || v.sbp > 160,
    bt: v.bt >= 37.5,
    spo2: v.spo2 < 94,
  };

  const vitalCards = [
    { label: "HR", value: `${v.hr}`, unit: "bpm", bad: abnormal.hr },
    { label: "RR", value: `${v.rr}`, unit: "회/분", bad: abnormal.rr },
    { label: "BP", value: `${v.sbp}/${v.dbp}`, unit: "mmHg", bad: abnormal.bp },
    { label: "Temperature", value: `${v.bt}`, unit: "℃", bad: abnormal.bt },
    { label: "SpO₂", value: `${v.spo2}`, unit: "%", bad: abnormal.spo2 },
    { label: "Mental", value: v.mental, unit: "", bad: false },
  ];

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
            <Link to="/records/$patientId" params={{ patientId: patient.id }}>
              <ClipboardCheck className="size-4" /> AI 진료기록 작성
            </Link>
          </Button>
        </div>
      </div>

      {/* 환자 핵심 정보 */}
      <Card className="overflow-hidden">
        <div className="flex items-stretch">
          <div className={`w-1.5 ${riskMeta[patient.risk].dot}`} />
          <CardContent className="flex flex-1 items-center gap-8 py-5">
            <div>
              <p className="text-xs text-muted-foreground">{patient.id}</p>
              <p className="text-2xl font-bold tracking-tight">
                {patient.name}
                <span className="ml-2 text-base font-medium text-muted-foreground">
                  {patient.sex === "남" ? "남성" : "여성"}, {patient.age}세
                </span>
              </p>
            </div>
            <Separator orientation="vertical" className="h-12" />
            <dl className="grid grid-cols-4 gap-x-8 gap-y-1.5 text-sm">
              <div>
                <dt className="text-xs text-muted-foreground">내원시간</dt>
                <dd className="tabular font-medium">{patient.arrivedAt}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">내원경로</dt>
                <dd className="font-medium">{patient.arrivalRoute}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">내원수단</dt>
                <dd className="font-medium">{patient.arrivalMeans}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">KTAS</dt>
                <dd className="font-bold text-risk-rising">Level {patient.ktas}</dd>
              </div>
              <div className="col-span-3">
                <dt className="text-xs text-muted-foreground">주증상</dt>
                <dd className="font-medium">{patient.chiefComplaintDetail}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">현재 상태</dt>
                <dd>
                  <Badge variant="outline" className={riskMeta[patient.risk].badge}>
                    {riskMeta[patient.risk].label}
                  </Badge>
                </dd>
              </div>
            </dl>
            <div className="ml-auto rounded-lg bg-navy px-6 py-3 text-center text-navy-foreground">
              <p className="text-xs opacity-75">AI 악화 예측 확률</p>
              <p className="tabular text-3xl font-bold">{patient.deteriorationProbability}%</p>
            </div>
          </CardContent>
        </div>
      </Card>

      <div className="grid grid-cols-[1fr_360px] gap-5">
        <div className="space-y-5">
          {/* Vital */}
          <Card>
            <CardHeader className="border-b py-3">
              <CardTitle className="text-base">현재 Vital</CardTitle>
            </CardHeader>
            <CardContent className="grid grid-cols-6 gap-3 pt-4">
              {vitalCards.map((c) => (
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
                    <span className="ml-1 text-xs font-medium text-muted-foreground">{c.unit}</span>
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
              <div className="h-72">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={patient.trend} margin={{ top: 8, right: 8, bottom: 0, left: -8 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                    <XAxis dataKey="time" tick={{ fontSize: 12 }} stroke="var(--muted-foreground)" />
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
                        strokeWidth={s.key === "probability" ? 3 : 2}
                        dot={{ r: 3 }}
                        activeDot={{ r: 5 }}
                        isAnimationActive={false}
                      />
                    ))}
                  </LineChart>
                </ResponsiveContainer>
              </div>

              {/* 숫자 테이블 (AI 악화 확률 최상단) */}
              <div className="overflow-hidden rounded-md border">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-navy text-navy-foreground">
                      <th className="px-3 py-2 text-left font-medium">항목</th>
                      {patient.trend.map((t) => (
                        <th key={t.time} className="tabular px-3 py-2 text-center font-medium">
                          {t.time}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {series.map((s) => (
                      <tr key={s.key} className={s.key === "probability" ? "bg-risk-critical-soft" : ""}>
                        <td className="px-3 py-2">
                          <span className="flex items-center gap-2">
                            <span
                              className="h-0.5 w-4 rounded"
                              style={{ backgroundColor: s.color }}
                            />
                            <span
                              className={
                                s.key === "probability" ? "font-bold text-risk-critical" : ""
                              }
                            >
                              {s.name}
                            </span>
                          </span>
                        </td>
                        {patient.trend.map((t) => (
                          <td
                            key={t.time}
                            className={`tabular px-3 py-2 text-center ${
                              s.key === "probability" ? "font-bold text-risk-critical" : ""
                            }`}
                          >
                            {t[s.key as keyof typeof t] as number}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </CardContent>
          </Card>
        </div>

        {/* AI 분석 */}
        <div className="space-y-4">
          <Card className="border-risk-critical/30">
            <CardHeader className="border-b py-3">
              <CardTitle className="flex items-center gap-2 text-base">
                <Brain className="size-4 text-primary" /> AI 분석
                <Badge variant="outline" className="ml-auto bg-mint-soft text-navy">
                  AI
                </Badge>
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4 pt-4">
              <div
                className={`rounded-md border px-4 py-3 text-center ${riskMeta[patient.risk].badge}`}
              >
                <p className="text-xs opacity-80">위험도</p>
                <p className="text-lg font-bold">{riskMeta[patient.risk].label}</p>
              </div>

              <div>
                <p className="mb-2 flex items-center gap-1.5 text-sm font-semibold">
                  <ShieldAlert className="size-4 text-risk-rising" /> 주요 위험요인
                </p>
                <ul className="space-y-1.5">
                  {patient.riskFactors.map((f) => (
                    <li key={f} className="flex items-start gap-2 text-sm text-muted-foreground">
                      <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-risk-rising" />
                      {f}
                    </li>
                  ))}
                </ul>
              </div>

              <Separator />

              <div>
                <p className="mb-2 text-sm font-semibold">AI 권고</p>
                <ul className="space-y-1.5">
                  {patient.recommendations.map((r) => (
                    <li key={r} className="flex items-start gap-2 text-sm text-muted-foreground">
                      <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-primary" />
                      {r}
                    </li>
                  ))}
                </ul>
              </div>

              <p className="flex gap-1.5 rounded-md bg-secondary px-3 py-2 text-xs text-muted-foreground">
                <Info className="mt-0.5 size-3.5 shrink-0" />
                AI의 권고사항은 확정적인 처방이 아니라 참고용 의사결정 지원 정보입니다.
              </p>

              <div className="grid gap-2">
                <Button
                  onClick={() => {
                    const t = stamp();
                    setReassessedAt(t);
                    toast.success("의료진 재평가 완료로 기록되었습니다.", {
                      description: `${currentUser.dept} ${currentUser.name} · ${t}`,
                    });
                  }}
                >
                  <CheckCircle2 className="size-4" /> 의료진 재평가 완료
                </Button>
                <Button
                  variant="outline"
                  onClick={() => {
                    const t = stamp();
                    setAckAt(t);
                    toast.success("AI 경고를 확인 처리했습니다.", {
                      description: `${currentUser.dept} ${currentUser.name} · ${t}`,
                    });
                  }}
                >
                  <BellRing className="size-4" /> AI 경고 확인
                </Button>
              </div>

              {(reassessedAt || ackAt) && (
                <div className="space-y-1 rounded-md border bg-risk-stable-soft px-3 py-2 text-xs text-risk-stable">
                  {reassessedAt && (
                    <p>
                      재평가 완료 · {currentUser.dept} {currentUser.name} · {reassessedAt}
                    </p>
                  )}
                  {ackAt && (
                    <p>
                      경고 확인 · {currentUser.dept} {currentUser.name} · {ackAt}
                    </p>
                  )}
                  <p className="font-semibold">알림 상태: 확인 처리됨</p>
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

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
