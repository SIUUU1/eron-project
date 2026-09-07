import { Link, useNavigate } from "@tanstack/react-router";
import { Check, ChevronRight } from "lucide-react";

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
import { bandMeta } from "@/api/display";
import type { RiskBandApi } from "@/api/types";

const ktasStyle: Record<number, string> = {
  1: "bg-risk-critical text-primary-foreground",
  2: "bg-risk-rising text-primary-foreground",
  3: "bg-risk-watch text-navy",
  4: "bg-risk-stable text-primary-foreground",
  5: "bg-secondary text-secondary-foreground",
};

/** 환자 목록 표가 그리는 데 필요한 API 표시 형태. */
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
  /** 모델 3구간(green/amber/red). 예측이 없으면 null — 임의 등급을 만들지 않는다. */
  risk: RiskBandApi | null;
  /** 0~100. 예측이 없으면 null. */
  probability: number | null;
  /** 현재 최신 예측을 의료진이 재검토 완료했는가. 다음 예측이 생기면 풀린다. */
  reviewed: boolean;
  /** 미작성, 임시저장 또는 인증 완료 상태. */
  recordStatus: string | null;
}

export function PatientListTable({
  base,
  rows,
}: {
  base: "/monitoring" | "/records";
  rows: PatientRow[];
}) {
  const navigate = useNavigate();

  // 행 클릭과 상세보기 버튼이 항상 같은 곳으로 가도록 이동 경로를 한 군데서 만든다.
  const detailLink = (patientId: string) =>
    ({ to: `${base}/$patientId`, params: { patientId } }) as const;

  // 이동 동작은 같지만, 기록 화면에서는 버튼이 기록 작성 진입점으로 읽히도록 이름만 바꾼다.
  const detailLabel = base === "/records" ? "기록작성" : "상세보기";

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
        {rows.map((p) => (
          // 퇴실한 환자는 밝은 회색으로 구분한다
          <TableRow
            key={p.id}
            className={`cursor-pointer ${p.discharge ? "bg-muted/60" : ""}`}
            // 행 어디를 눌러도 상세보기 버튼과 동일하게 동작한다
            onClick={() => void navigate(detailLink(p.id))}
          >
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
                <Badge variant="outline" className={bandMeta[p.risk].badge}>
                  <span className={`mr-1 size-1.5 rounded-full ${bandMeta[p.risk].dot}`} />
                  {bandMeta[p.risk].label}
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
                      className={`h-full rounded-full ${p.risk ? bandMeta[p.risk].dot : "bg-muted-foreground"}`}
                      style={{ width: `${p.probability}%` }}
                    />
                  </div>
                  <span
                    className={`tabular text-sm font-bold ${p.risk ? bandMeta[p.risk].text : ""}`}
                  >
                    {p.probability}%
                  </span>
                  {/* 확인 표시는 '지금 화면에 보이는 그 예측'에 대한 것이다 */}
                  {p.reviewed ? (
                    <span
                      className="flex items-center gap-0.5 text-xs font-medium text-risk-stable"
                      title="의료진 재검토 완료"
                    >
                      <Check className="size-3.5" /> 확인
                    </span>
                  ) : null}
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
            {/* 버튼이 이미 이동을 처리하므로 행 클릭까지 겹쳐 실행되지 않게 막는다 */}
            <TableCell onClick={(e) => e.stopPropagation()}>
              <Button asChild size="sm" variant="secondary">
                <Link {...detailLink(p.id)}>
                  {detailLabel} <ChevronRight className="size-3.5" />
                </Link>
              </Button>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
