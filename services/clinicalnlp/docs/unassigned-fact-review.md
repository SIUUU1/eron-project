# Unassigned Fact review

`UNASSIGNED_FACT` means an extracted Fact is not referenced by any generated field.
It does not prove that the source information is absent from all prose, nor does
it detect information that was never extracted.

The Compact projection keeps existing `review_items` and adds an optional
`unassigned_fact` object to these issues: `fact_id`, the unchanged `fact`,
`candidate_label` from the request's immutable candidate snapshot (when available),
and all resolvable source `evidence` with timestamps, raw text and translation.
Assertions are preserved, including DENIED and UNCERTAIN. Missing evidence is not
invented. This change performs no additional retrieval or LLM requests.

The records UI displays these separately as `초안 미반영 정보`, including assertion
and source context. It does not append text, choose a clinical field, or mark the
information resolved automatically. Draft saves retain the list in the existing
JSON `record_payload.unassigned_facts`; older drafts without this key show no list.
Replacing the source clears the old list; successful regeneration replaces it.

Legacy extraction continues to use internal `drug_allergy` and public `allergy`.
This is a key mapping, not a restriction to drug allergies: drug, food, contrast,
latex and other supported allergens remain in scope.
