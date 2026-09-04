import {
  Check,
  ChevronDown,
  MessageSquareText,
  Search,
  X,
} from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  applyTerminologyCandidateDecision,
  candidateSourceLabel,
  type CandidateSource,
  type FieldProvenance,
  type TerminologyCandidate,
} from "@/lib/clinical-provenance";
import { cn } from "@/lib/utils";

const sourceBadgeClasses: Record<CandidateSource, string> = {
  RAW_EXACT: "border-risk-stable/30 bg-risk-stable-soft text-risk-stable",
  UMLS: "border-primary/30 bg-accent text-primary",
  NGRAM_FALLBACK: "border-risk-watch/40 bg-risk-watch-soft text-navy",
  UNRESOLVED: "border-risk-critical/30 bg-risk-critical-soft text-risk-critical",
};

function similarityLabel(similarity: number | null): string {
  return similarity === null ? "제공되지 않음" : `${Math.round(similarity * 100)}%`;
}

interface FieldProvenancePanelProps {
  provenance: FieldProvenance;
  draftValue: string;
  onDraftValueChange: (value: string) => void;
}

export function FieldProvenancePanel({
  provenance,
  draftValue,
  onDraftValueChange,
}: FieldProvenancePanelProps) {
  const [selectedByGroup, setSelectedByGroup] = useState<Record<string, string>>({});
  const [excludedCandidates, setExcludedCandidates] = useState<Record<string, boolean>>({});
  const visibleCandidates = provenance.candidates.filter(
    (candidate) => candidate.canonicalValue?.trim(),
  );

  const selectCandidate = (candidate: TerminologyCandidate) => {
    const groupIds = candidate.selectionGroupIds ?? [candidate.id];
    const previousCandidates = Array.from(
      new Set(groupIds.map((groupId) => selectedByGroup[groupId]).filter(Boolean)),
    )
      .map((candidateId) => provenance.candidates.find((item) => item.id === candidateId))
      .filter((item): item is TerminologyCandidate => item !== undefined);
    const result = applyTerminologyCandidateDecision(
      draftValue,
      candidate,
      previousCandidates,
      "selected",
    );
    if (!result.changed) return;
    onDraftValueChange(result.value);
    setSelectedByGroup((current) => {
      const next = { ...current };
      groupIds.forEach((groupId) => {
        next[groupId] = candidate.id;
      });
      return next;
    });
    setExcludedCandidates((current) => ({ ...current, [candidate.id]: false }));
  };

  const excludeCandidate = (candidate: TerminologyCandidate) => {
    const groupIds = candidate.selectionGroupIds ?? [candidate.id];
    const isSelected = groupIds.some((groupId) => selectedByGroup[groupId] === candidate.id);
    if (isSelected) {
      const result = applyTerminologyCandidateDecision(
        draftValue,
        candidate,
        candidate,
        "excluded",
      );
      if (result.changed) onDraftValueChange(result.value);
      setSelectedByGroup((current) => {
        const next = { ...current };
        groupIds.forEach((groupId) => {
          if (next[groupId] === candidate.id) delete next[groupId];
        });
        return next;
      });
    }
    setExcludedCandidates((current) => ({ ...current, [candidate.id]: true }));
  };

  return (
    <section
      aria-label="환자 대화 근거와 의학용어 후보"
      className="mt-2 space-y-2 rounded-lg border border-primary/15 bg-secondary/20 p-2.5"
    >
      {provenance.evidence.length > 0 ? (
        <div className="rounded-md border bg-card p-2.5">
          <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold">
            <MessageSquareText className="size-3.5 text-primary" />
            환자 대화 근거
            <Badge variant="outline" className="ml-auto font-normal">
              {provenance.evidence.length}건
            </Badge>
          </div>
          <div className="space-y-2">
            {provenance.evidence.map((evidence) => (
              <article key={evidence.segmentId} className="min-w-0 rounded-md bg-secondary/50 px-2.5 py-2">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-foreground">
                  <span className="font-mono font-semibold text-foreground">
                    {evidence.segmentId}
                  </span>
                  <span>{evidence.timestamp}</span>
                </div>
                <dl className="mt-2 grid gap-1.5 text-xs">
                  <div className="grid grid-cols-[62px_1fr] gap-2">
                    <dt className="font-semibold text-muted-foreground">RAW</dt>
                    <dd className="min-w-0 whitespace-pre-wrap break-words [overflow-wrap:anywhere]">
                      {evidence.raw}
                    </dd>
                  </div>
                  {evidence.corrected ? (
                    <div className="grid grid-cols-[62px_1fr] gap-2">
                      <dt className="font-semibold text-muted-foreground">CORRECTED</dt>
                      <dd className="min-w-0 whitespace-pre-wrap break-words [overflow-wrap:anywhere]">
                        {evidence.corrected}
                      </dd>
                    </div>
                  ) : null}
                  {evidence.translated ? (
                    <div className="grid grid-cols-[62px_1fr] gap-2">
                      <dt className="font-semibold text-muted-foreground">영문 번역</dt>
                      <dd
                        lang="en"
                        className="min-w-0 whitespace-pre-wrap break-words [overflow-wrap:anywhere]"
                      >
                        {evidence.translated}
                      </dd>
                    </div>
                  ) : null}
                  <div className="grid grid-cols-[62px_1fr] gap-2">
                    <dt className="font-semibold text-primary">초안 반영</dt>
                    <dd className="min-w-0 whitespace-pre-wrap break-words font-medium [overflow-wrap:anywhere]">
                      {draftValue || "미반영"}
                    </dd>
                  </div>
                </dl>
              </article>
            ))}
          </div>
        </div>
      ) : null}

      {visibleCandidates.length > 0 ? (
        <div className="rounded-md border bg-card p-2.5">
          <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold">
            <Search className="size-3.5 text-primary" />
            의학용어 후보
            <span className="ml-auto text-[11px] font-normal text-muted-foreground">
              자동 확정 안 함
            </span>
          </div>
          <div className="space-y-2">
            {visibleCandidates.map((candidate) => {
              const groupIds = candidate.selectionGroupIds ?? [candidate.id];
              const selected =
                groupIds.length > 0 &&
                groupIds.every((groupId) => selectedByGroup[groupId] === candidate.id);
              const excluded = excludedCandidates[candidate.id] === true;
              const selectable = !candidate.alreadyApplied;
              return (
                <article
                  key={candidate.id}
                  className={cn(
                    "rounded-md border px-2.5 py-2 transition-colors",
                    selected && "border-risk-stable/50 bg-risk-stable-soft/40",
                    excluded && "bg-secondary/40 opacity-65",
                  )}
                >
                  <div className="flex flex-wrap items-start gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        {(candidate.sources ?? [candidate.source]).map((source) => (
                          <Badge
                            key={source}
                            variant="outline"
                            className={sourceBadgeClasses[source]}
                          >
                            {candidateSourceLabel(source)}
                          </Badge>
                        ))}
                        {candidate.alreadyApplied ? (
                          <Badge className="bg-risk-stable text-primary-foreground">
                            초안 반영됨
                          </Badge>
                        ) : selected ? (
                          <Badge className="bg-risk-stable text-primary-foreground">선택됨</Badge>
                        ) : excluded ? (
                          <Badge variant="secondary">제외됨</Badge>
                        ) : (
                          <Badge variant="outline" className="text-muted-foreground">
                            검토 후보
                          </Badge>
                        )}
                      </div>
                      <p className="mt-1.5 whitespace-pre-wrap break-words text-sm font-semibold [overflow-wrap:anywhere]">
                        {candidate.canonicalValue}
                      </p>
                      <p className="mt-0.5 whitespace-pre-wrap break-words text-[11px] text-muted-foreground [overflow-wrap:anywhere]">
                        검색어 · {candidate.query}
                      </p>
                    </div>
                    {selectable ? (
                      <div className="flex shrink-0 gap-1.5">
                        <Button
                          type="button"
                          size="sm"
                          variant={selected ? "default" : "outline"}
                          className="h-7 px-2 text-xs"
                          aria-pressed={selected}
                          onClick={() => selectCandidate(candidate)}
                        >
                          <Check className="size-3" /> 후보 선택
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
                          className="h-7 px-2 text-xs"
                          aria-pressed={excluded}
                          onClick={() => excludeCandidate(candidate)}
                        >
                          <X className="size-3" /> 제외
                        </Button>
                      </div>
                    ) : null}
                  </div>

                  <details className="group mt-2 border-t pt-1.5">
                    <summary className="flex cursor-pointer list-none items-center gap-1 text-[11px] font-medium text-muted-foreground hover:text-foreground">
                      <ChevronDown className="size-3 transition-transform group-open:rotate-180" />
                      검색 상세 보기
                    </summary>
                    <dl className="mt-2 grid grid-cols-1 gap-x-4 gap-y-1 rounded bg-secondary/50 p-2 text-[11px] sm:grid-cols-2">
                      <div>
                        <dt className="text-muted-foreground">CUI</dt>
                        <dd className="font-mono">{candidate.cui ?? "—"}</dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">Semantic type</dt>
                        <dd>{candidate.semanticType ?? "—"}</dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">검색 유사도</dt>
                        <dd className="font-semibold">{similarityLabel(candidate.similarity)}</dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">검색 출처</dt>
                        <dd>{candidateSourceLabel(candidate.source)}</dd>
                      </div>
                    </dl>
                    <p className="mt-1.5 text-[10px] text-muted-foreground">
                      검색 유사도는 진단 확률이 아닙니다.
                    </p>
                  </details>
                </article>
              );
            })}
          </div>
          <p className="mt-2 text-[10px] text-muted-foreground">
            선택한 후보는 초안 원문을 바꾸지 않고 별도 줄에 추가됩니다.
          </p>
        </div>
      ) : null}
    </section>
  );
}
