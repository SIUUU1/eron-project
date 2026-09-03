import { Link } from "@tanstack/react-router";
import { ChevronRight } from "lucide-react";

import { bandMeta } from "@/api/display";
import { Badge } from "@/components/ui/badge";

import type { PatientRow } from "@/components/patient-list-table";

const ktasStyle: Record<number, string> = {
  1: "bg-risk-critical text-primary-foreground",
  2: "bg-risk-rising text-primary-foreground",
  3: "bg-risk-watch text-navy",
  4: "bg-risk-stable text-primary-foreground",
  5: "bg-secondary text-secondary-foreground",
};

/**
 * 좁은 화면(휴대폰 녹음 전용 진입점, ?mobile=1)용 환자 목록.
 * PatientListTable과 같은 데이터를 세로 카드로 보여준다.
 */
export function PatientListMobile({
  base,
  rows,
}: {
  base: "/monitoring" | "/records";
  rows: PatientRow[];
}) {
  return (
    <ul className="divide-y">
      {rows.map((p) => (
        <li key={p.id}>
          <Link
            to={`${base}/$patientId`}
            params={{ patientId: p.id }}
            search={{ mobile: "1" }}
            className={`flex min-h-[56px] items-center justify-between gap-3 px-3 py-3 active:bg-muted/60 ${
              p.discharge ? "bg-muted/40" : ""
            }`}
          >
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                {p.ktas === null ? (
                  <span className="inline-flex size-6 shrink-0 items-center justify-center rounded bg-secondary text-xs font-bold text-muted-foreground">
                    -
                  </span>
                ) : (
                  <span
                    className={`inline-flex size-6 shrink-0 items-center justify-center rounded text-xs font-bold ${ktasStyle[p.ktas]}`}
                  >
                    {p.ktas}
                  </span>
                )}
                <span className="truncate font-semibold">{p.name}</span>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {p.sex} {p.age === null ? "" : `${p.age}세`}
                </span>
              </div>
              <p className="mt-0.5 truncate text-sm text-muted-foreground">{p.chiefComplaint}</p>
              <div className="mt-1 flex items-center gap-2">
                {p.risk ? (
                  <Badge variant="outline" className={bandMeta[p.risk].badge}>
                    <span className={`mr-1 size-1.5 rounded-full ${bandMeta[p.risk].dot}`} />
                    {bandMeta[p.risk].label}
                  </Badge>
                ) : null}
                {p.recordStatus ? (
                  <span className="text-xs text-muted-foreground">{p.recordStatus}</span>
                ) : null}
              </div>
            </div>
            <ChevronRight className="size-5 shrink-0 text-muted-foreground" />
          </Link>
        </li>
      ))}
    </ul>
  );
}
