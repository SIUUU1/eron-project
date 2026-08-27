import { Link } from "@tanstack/react-router";
import { ChevronRight } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { riskMeta, sortedPatients, type RiskLevel } from "@/lib/mock-data";

const ktasStyle: Record<number, string> = {
  1: "bg-risk-critical text-primary-foreground",
  2: "bg-risk-rising text-primary-foreground",
  3: "bg-risk-watch text-navy",
  4: "bg-risk-stable text-primary-foreground",
  5: "bg-secondary text-secondary-foreground",
};

/** 표가 그리는 데 필요한 최소 형태. mock 과 API 양쪽에서 이 모양으로 맞춘다. */
export interface PatientRow {
  id: string;
  name: string;
  sex: string;
  age: number | null;
  arrivedLabel: string;
  /** 퇴실 유형 표기(ICU·입원·귀가·사망). 아직 퇴실 전이면 빈 문자열. */
  discharge: string;
  ktas: number | null;
  chiefComplaint: string;
  /** 예측이 없으면 null — 임의 등급을 만들지 않는다. */
  risk: RiskLevel | null;
  /** 0~100. 예측이 없으면 null. */
  probability: number | null;
  /** 기록 영역은 이번 연동 범위 밖이라 API 행에서는 null 이다. */
  recordStatus: string | null;
}

const mockRows: PatientRow[] = sortedPatients.map((p) => ({
  id: p.id,
  name: p.name,
  sex: p.sex,
  age: p.age,
  arrivedLabel: p.arrivedAt.slice(11),
  discharge: "", // mock 환자는 재실 중으로 둔다
  ktas: p.ktas,
  chiefComplaint: p.chiefComplaint,
  risk: p.risk,
  probability: p.deteriorationProbability,
  recordStatus: p.recordStatus,
}));

export function PatientListTable({
  base,
  rows,
}: {
  base: "/monitoring" | "/records";
  rows?: PatientRow[];
}) {
  const data = rows ?? mockRows;

  return (
    <Table>
      <TableHeader>
        <TableRow className="bg-navy hover:bg-navy">
          {[
            "환자번호",
            "이름",
            "성별/나이",
            "내원시간",
            "퇴실",
            "KTAS",
            "주증상",
            "현재 위험도",
            "악화 예측 확률",
            "기록 상태",
            "상세보기",
          ].map((h) => (
            <TableHead key={h} className="text-navy-foreground/85">
              {h}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {data.map((p) => (
          // 퇴실한 환자는 밝은 회색으로 구분한다
          <TableRow key={p.id} className={`cursor-pointer ${p.discharge ? "bg-muted/60" : ""}`}>
            <TableCell className="tabular font-mono text-xs">{p.id}</TableCell>
            <TableCell className="font-semibold">{p.name}</TableCell>
            <TableCell className="tabular">
              {p.sex} {p.age === null ? "-" : `${p.age}세`}
            </TableCell>
            <TableCell className="tabular text-muted-foreground">{p.arrivedLabel}</TableCell>
            <TableCell className="text-sm text-muted-foreground">{p.discharge}</TableCell>
            <TableCell>
              {p.ktas === null ? (
                <span className="inline-flex size-6 items-center justify-center rounded bg-secondary text-xs font-bold text-muted-foreground">
                  -
                </span>
              ) : (
                <span
                  className={`inline-flex size-6 items-center justify-center rounded text-xs font-bold ${ktasStyle[p.ktas]}`}
                >
                  {p.ktas}
                </span>
              )}
            </TableCell>
            <TableCell>{p.chiefComplaint}</TableCell>
            <TableCell>
              {p.risk ? (
                <Badge variant="outline" className={riskMeta[p.risk].badge}>
                  <span className={`mr-1 size-1.5 rounded-full ${riskMeta[p.risk].dot}`} />
                  {riskMeta[p.risk].label}
                </Badge>
              ) : (
                <Badge variant="outline" className="text-muted-foreground">
                  평가 대기
                </Badge>
              )}
            </TableCell>
            <TableCell>
              {p.probability === null ? (
                <span className="text-xs text-muted-foreground">모델 미연동</span>
              ) : (
                <div className="flex items-center gap-2">
                  <div className="h-1.5 w-16 overflow-hidden rounded-full bg-secondary">
                    <div
                      className={`h-full rounded-full ${p.risk ? riskMeta[p.risk].dot : "bg-muted-foreground"}`}
                      style={{ width: `${p.probability}%` }}
                    />
                  </div>
                  <span
                    className={`tabular text-sm font-bold ${p.risk ? riskMeta[p.risk].text : ""}`}
                  >
                    {p.probability}%
                  </span>
                </div>
              )}
            </TableCell>
            <TableCell>
              <span
                className="text-xs text-muted-foreground"
                title={p.recordStatus ?? "기록 연동 범위 밖"}
              >
                {p.recordStatus ?? "-"}
              </span>
            </TableCell>
            <TableCell>
              <Button asChild size="sm" variant="secondary">
                <Link to={`${base}/$patientId`} params={{ patientId: p.id }}>
                  상세보기 <ChevronRight className="size-3.5" />
                </Link>
              </Button>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
