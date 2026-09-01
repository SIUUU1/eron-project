import type { RecordFieldKey } from "./mock-data.ts";

export type CandidateSource = "RAW_EXACT" | "UMLS" | "NGRAM_FALLBACK" | "UNRESOLVED";

export interface PatientEvidence {
  segmentId: string;
  timestamp: string;
  speaker: string;
  raw: string;
  corrected: string | null;
  translated?: string;
  appliedValue: string;
}

export interface TerminologyCandidate {
  id: string;
  query: string;
  canonicalValue: string | null;
  source: CandidateSource;
  cui: string | null;
  semanticType: string | null;
  similarity: number | null;
  sources?: CandidateSource[];
  selectionGroupIds?: string[];
  alreadyApplied?: boolean;
}

export interface FieldProvenance {
  fieldKey: RecordFieldKey;
  evidence: PatientEvidence[];
  candidates: TerminologyCandidate[];
}

export type FieldProvenanceMap = Partial<Record<RecordFieldKey, FieldProvenance>>;

export type TerminologyCandidateDecision = "selected" | "excluded";

export interface TerminologyCandidateDecisionResult {
  value: string;
  changed: boolean;
}

function removeLastExactLine(value: string, line: string): string {
  const lines = value.split("\n");
  const index = lines.lastIndexOf(line);
  if (index < 0) return value;
  lines.splice(index, 1);
  return lines.join("\n");
}

function appendLine(value: string, line: string): string {
  if (!value) return line;
  return `${value}${value.endsWith("\n") ? "" : "\n"}${line}`;
}

export function applyTerminologyCandidateDecision(
  currentValue: string,
  candidate: TerminologyCandidate,
  previousCandidates: TerminologyCandidate | TerminologyCandidate[] | null,
  decision: TerminologyCandidateDecision,
): TerminologyCandidateDecisionResult {
  const canonicalValue = candidate.canonicalValue?.trim();
  if (!canonicalValue) {
    return { value: currentValue, changed: false };
  }

  const previous = previousCandidates
    ? Array.isArray(previousCandidates)
      ? previousCandidates
      : [previousCandidates]
    : [];
  let value = currentValue;
  if (decision === "excluded") {
    value = removeLastExactLine(value, canonicalValue);
    return { value, changed: value !== currentValue };
  }

  const previousValues = Array.from(
    new Set(
      previous
        .filter((item) => item.id !== candidate.id)
        .map((item) => item.canonicalValue?.trim())
        .filter((item): item is string => Boolean(item)),
    ),
  );
  previousValues.forEach((previousValue) => {
    value = removeLastExactLine(value, previousValue);
  });
  if (previous.some((item) => item.id === candidate.id) && value === currentValue) {
    return { value: currentValue, changed: false };
  }
  value = appendLine(value, canonicalValue);

  return { value, changed: value !== currentValue };
}

export function candidateSourceLabel(source: CandidateSource): string {
  switch (source) {
    case "RAW_EXACT":
      return "RAW exact";
    case "UMLS":
      return "UMLS";
    case "NGRAM_FALLBACK":
      return "n-gram 보완";
    case "UNRESOLVED":
      return "미해결";
  }
}
