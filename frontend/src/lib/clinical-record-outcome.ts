export const outcomeOptions = ["귀가", "입원", "전원", "사망", "기타"] as const;

type ClinicalRecordOutcome = (typeof outcomeOptions)[number];

const englishOutcomeAliases: Record<string, ClinicalRecordOutcome> = {
  discharge: "귀가",
  admission: "입원",
  admit: "입원",
  admitted: "입원",
  transfer: "전원",
  death: "사망",
  expired: "사망",
  other: "기타",
};

function matchesOutcomePrefix(value: string, prefix: string): boolean {
  if (!value.startsWith(prefix)) return false;
  const remainder = value.slice(prefix.length);
  return remainder === "" || /^[\s([{:/,;\-–—]/u.test(remainder);
}

export function normalizeClinicalRecordOutcome(value: string): string {
  const trimmed = value.trim();
  const exact = outcomeOptions.find((option) => option === trimmed);
  if (exact) return exact;

  const lower = trimmed.toLocaleLowerCase("en-US");
  for (const [alias, outcome] of Object.entries(englishOutcomeAliases)) {
    if (matchesOutcomePrefix(lower, alias)) return outcome;
  }

  return outcomeOptions.find((option) => matchesOutcomePrefix(trimmed, option)) ?? "";
}
