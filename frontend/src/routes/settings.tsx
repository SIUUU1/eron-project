import { createFileRoute } from "@tanstack/react-router";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { currentUser } from "@/lib/mock-data";

export const Route = createFileRoute("/settings")({
  head: () => ({
    meta: [
      {
        name: "description",
        content: "계정 및 시스템 정보를 확인하는 시연용 시스템 설정 화면입니다.",
      },
      { property: "og:title", content: "시스템 설정 · ER:ON(이로운)" },
      { property: "og:description", content: "계정 및 시스템 정보를 확인하는 설정 화면." },
    ],
  }),
  component: SettingsPage,
});

function SettingsPage() {
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">시스템 설정</h1>
        <p className="mt-1 text-sm text-muted-foreground">시스템 설정을 관리할 수 있습니다.</p>
      </div>

      <Card>
        <CardHeader className="border-b py-3">
          <CardTitle className="text-base">계정 및 시스템 정보</CardTitle>
        </CardHeader>
        <CardContent className="grid grid-cols-3 gap-6 pt-5 text-sm">
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
        </CardContent>
      </Card>
    </div>
  );
}
