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
import { riskMeta, sortedPatients } from "@/lib/mock-data";

const ktasStyle: Record<number, string> = {
  1: "bg-risk-critical text-primary-foreground",
  2: "bg-risk-rising text-primary-foreground",
  3: "bg-risk-watch text-navy",
  4: "bg-risk-stable text-primary-foreground",
  5: "bg-secondary text-secondary-foreground",
};

export function PatientListTable({ base }: { base: "/monitoring" | "/records" }) {
  return (
    <Table>
      <TableHeader>
        <TableRow className="bg-navy hover:bg-navy">
          {[
            "환자번호",
            "이름",
            "성별/나이",
            "내원시간",
            "KTAS",
            "주증상",
            "현재 위험도",
            "악화 예측 확률",
            "최근 Vital",
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
        {sortedPatients.map((p) => (
          <TableRow key={p.id} className="cursor-pointer">
            <TableCell className="tabular font-mono text-xs">{p.id}</TableCell>
            <TableCell className="font-semibold">{p.name}</TableCell>
            <TableCell className="tabular">
              {p.sex} {p.age}세
            </TableCell>
            <TableCell className="tabular text-muted-foreground">
              {p.arrivedAt.slice(11)}
            </TableCell>
            <TableCell>
              <span
                className={`inline-flex size-6 items-center justify-center rounded text-xs font-bold ${ktasStyle[p.ktas]}`}
              >
                {p.ktas}
              </span>
            </TableCell>
            <TableCell>{p.chiefComplaint}</TableCell>
            <TableCell>
              <Badge variant="outline" className={riskMeta[p.risk].badge}>
                <span className={`mr-1 size-1.5 rounded-full ${riskMeta[p.risk].dot}`} />
                {riskMeta[p.risk].label}
              </Badge>
            </TableCell>
            <TableCell>
              <div className="flex items-center gap-2">
                <div className="h-1.5 w-16 overflow-hidden rounded-full bg-secondary">
                  <div
                    className={`h-full rounded-full ${riskMeta[p.risk].dot}`}
                    style={{ width: `${p.deteriorationProbability}%` }}
                  />
                </div>
                <span className={`tabular text-sm font-bold ${riskMeta[p.risk].text}`}>
                  {p.deteriorationProbability}%
                </span>
              </div>
            </TableCell>
            <TableCell className="tabular text-xs text-muted-foreground">
              HR {p.vitals.hr} · BP {p.vitals.sbp}/{p.vitals.dbp} · SpO₂ {p.vitals.spo2}%
            </TableCell>
            <TableCell>
              <span className="text-xs text-muted-foreground">{p.recordStatus}</span>
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
