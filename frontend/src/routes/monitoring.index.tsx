import { createFileRoute } from "@tanstack/react-router";

import { PatientListTable } from "@/components/patient-list-table";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export const Route = createFileRoute("/monitoring/")({
  head: () => ({
    meta: [
      { title: "환자 모니터링 · ER-GUARD AI" },
      {
        name: "description",
        content: "응급실 재실 환자의 위험도와 악화 예측 확률을 위험도 순으로 확인합니다.",
      },
      { property: "og:title", content: "환자 모니터링 · ER-GUARD AI" },
      {
        property: "og:description",
        content: "응급실 재실 환자의 위험도와 악화 예측 확률을 확인하는 모니터링 목록.",
      },
    ],
  }),
  component: MonitoringListPage,
});

function MonitoringListPage() {
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">환자 모니터링</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          위험도가 높은 환자가 상단에 정렬됩니다. 환자를 선택하면 상세 모니터링 화면으로 이동합니다.
        </p>
      </div>

      <Card>
        <CardHeader className="border-b py-3">
          <CardTitle className="text-base">응급실 환자 목록</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <PatientListTable base="/monitoring" />
        </CardContent>
      </Card>
    </div>
  );
}
