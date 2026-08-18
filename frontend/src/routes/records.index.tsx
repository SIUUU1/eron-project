import { createFileRoute } from "@tanstack/react-router";

import { PatientListTable } from "@/components/patient-list-table";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export const Route = createFileRoute("/records/")({
  head: () => ({
    meta: [
      { title: "AI 진료기록 및 누락 검사 · ER-GUARD AI" },
      {
        name: "description",
        content:
          "대화 기반 응급진료기록 작성부터 누락 검사, KCD 코드 추천, 의사 인증까지 이어지는 워크플로우.",
      },
      { property: "og:title", content: "AI 진료기록 및 누락 검사 · ER-GUARD AI" },
      {
        property: "og:description",
        content: "기록 작성 → 누락 검사 → 진단코드 → 최종 기록 → 인증 저장 워크플로우.",
      },
    ],
  }),
  component: RecordsListPage,
});

function RecordsListPage() {
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">AI 진료기록 및 누락 검사</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          기록을 작성할 환자를 선택하세요. 선택 후 기록 작성 → 누락 검사 → 진단코드 → 최종 기록 →
          인증 저장 순서로 진행됩니다.
        </p>
      </div>

      <Card>
        <CardHeader className="border-b py-3">
          <CardTitle className="text-base">응급실 환자 목록</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <PatientListTable base="/records" />
        </CardContent>
      </Card>
    </div>
  );
}
