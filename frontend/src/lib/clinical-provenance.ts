import type { RecordFieldKey } from "./mock-data.ts";

export type CandidateSource = "RAW_EXACT" | "UMLS" | "NGRAM_FALLBACK" | "UNRESOLVED";

export interface PatientEvidence {
  segmentId: string;
  timestamp: string;
  speaker: string;
  raw: string;
  corrected: string | null;
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
}

export interface FieldProvenance {
  fieldKey: RecordFieldKey;
  evidence: PatientEvidence[];
  candidates: TerminologyCandidate[];
}

export type FieldProvenanceMap = Partial<Record<RecordFieldKey, FieldProvenance>>;

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
