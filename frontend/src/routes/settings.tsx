import { createFileRoute } from "@tanstack/react-router";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { currentUser } from "@/lib/mock-data";

export const Route = createFileRoute("/settings")({
  head: () => ({
    meta: [
      { title: "시스템 설정 · ER-GUARD AI" },
      {
        name: "description",
        content: "AI 경고 임계값, 알림, 기록 검사 정책 등 시연용 시스템 설정 화면입니다.",
      },
      { property: "og:title", content: "시스템 설정 · ER-GUARD AI" },
      { property: "og:description", content: "AI 경고 임계값과 알림 정책을 확인하는 설정 화면." },
    ],
  }),
  component: SettingsPage,
});

function SettingsPage() {
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">시스템 설정</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          시연용 설정 화면입니다. 변경 사항은 저장되지 않습니다.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-5">
        <Card>
          <CardHeader className="border-b py-3">
            <CardTitle className="text-base">AI 경고 임계값</CardTitle>
          </CardHeader>
          <CardContent className="space-y-6 pt-5">
            <div className="space-y-2">
              <div className="flex items-center justify-between text-sm">
                <Label>즉시 재평가 필요 (빨강)</Label>
                <span className="tabular font-semibold text-risk-critical">80% 이상</span>
              </div>
              <Slider defaultValue={[80]} max={100} step={1} />
            </div>
            <div className="space-y-2">
              <div className="flex items-center justify-between text-sm">
                <Label>위험 증가 (주황)</Label>
                <span className="tabular font-semibold text-risk-rising">60% 이상</span>
              </div>
              <Slider defaultValue={[60]} max={100} step={1} />
            </div>
            <div className="space-y-2">
              <div className="flex items-center justify-between text-sm">
                <Label>관찰 필요 (노랑)</Label>
                <span className="tabular font-semibold text-risk-watch">30% 이상</span>
              </div>
              <Slider defaultValue={[30]} max={100} step={1} />
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="border-b py-3">
            <CardTitle className="text-base">알림 및 기록 정책</CardTitle>
          </CardHeader>
          <CardContent className="divide-y pt-2">
            {[
              ["AI 악화 경고 실시간 알림", true],
              ["환자용 상태 안내 화면 사용", true],
              ["기록 완전성 자동 검사", true],
              ["필수 누락 항목 미작성 시 다음 단계 차단", true],
              ["진단코드 자동 확정", false],
            ].map(([label, on]) => (
              <div key={label as string} className="flex items-center justify-between py-3">
                <Label className="text-sm font-normal">{label as string}</Label>
                <Switch defaultChecked={on as boolean} />
              </div>
            ))}
          </CardContent>
        </Card>

        <Card className="col-span-2">
          <CardHeader className="border-b py-3">
            <CardTitle className="text-base">계정 및 시스템 정보</CardTitle>
          </CardHeader>
          <CardContent className="grid grid-cols-4 gap-6 pt-5 text-sm">
            <div>
              <p className="text-xs text-muted-foreground">로그인 사용자</p>
              <p className="mt-1 font-semibold">
                {currentUser.dept} {currentUser.name}
              </p>
            </div>
            <div>
              <p className="text-xs text-muted-foreground">권한</p>
              <p className="mt-1 font-semibold">{currentUser.role} · 기록 인증 권한 보유</p>
            </div>
            <div>
              <p className="text-xs text-muted-foreground">진단코드 기준</p>
              <p className="mt-1 font-semibold">KCD-9차</p>
            </div>
            <div>
              <p className="text-xs text-muted-foreground">데이터 연동</p>
              <p className="mt-1">
                <Badge variant="outline">Mock 데이터</Badge>
              </p>
            </div>
            <Separator className="col-span-4" />
            <p className="col-span-4 text-xs text-muted-foreground">
              본 시스템은 프로젝트 시연용이며 실제 EMR, 음성인식, AI 모델과 연동되어 있지 않습니다.
            </p>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
