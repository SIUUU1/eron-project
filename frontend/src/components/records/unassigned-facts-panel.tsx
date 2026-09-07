import type { UnassignedClinicalFact } from "@/api/types";

const assertionLabels: Record<string, string> = {
  PRESENT: "있음 / 확인됨",
  DENIED: "명시적으로 부인함",
  UNCERTAIN: "불확실함",
};

export function UnassignedFactsPanel({ facts }: { facts: UnassignedClinicalFact[] }) {
  if (facts.length === 0) return null;
  return (
    <section aria-label="초안 미반영 정보" className="rounded-md border border-risk-rising/30 p-3">
      <h3 className="text-sm font-semibold text-risk-rising">
        초안 미반영 정보 · {facts.length}건
      </h3>
      <p className="mt-1 text-xs text-muted-foreground">
        AI가 추출했지만 어떤 임상 필드에도 연결하지 않은 정보입니다. 근거를 확인하고 필요한 내용을
        직접 기록해 주세요. 아래 내용은 초안에 자동 추가되지 않습니다.
      </p>
      <div className="mt-2 max-h-64 space-y-3 overflow-y-auto">
        {facts.map(({ fact_id, fact, candidate_label, evidence }) => (
          <article
            key={fact_id}
            className="space-y-1 rounded-md bg-secondary p-2 text-xs break-words"
          >
            <p className="whitespace-pre-wrap font-medium">
              {fact.text || candidate_label || "추출 정보: 아래 근거 확인 필요"}
            </p>
            <p>상태: {assertionLabels[fact.assertion ?? ""] ?? "확인 필요"}</p>
            {fact.values && (
              <pre className="whitespace-pre-wrap break-words">
                {JSON.stringify(fact.values, null, 2)}
              </pre>
            )}
            {evidence.length === 0 && <p>연결된 대화 근거를 확인할 수 없습니다.</p>}
            {evidence.map((segment) => (
              <div key={segment.segment_id} className="space-y-1 border-t pt-1">
                <p className="text-muted-foreground">
                  {segment.segment_id}
                  {Number.isFinite(segment.start) && Number.isFinite(segment.end)
                    ? ` · ${segment.start!.toFixed(2)}–${segment.end!.toFixed(2)}초`
                    : ""}
                </p>
                <p className="whitespace-pre-wrap">원문: {segment.raw_text || "근거 원문 없음"}</p>
                {segment.translated_text_en && (
                  <p className="whitespace-pre-wrap">영문 번역: {segment.translated_text_en}</p>
                )}
              </div>
            ))}
          </article>
        ))}
      </div>
    </section>
  );
}
