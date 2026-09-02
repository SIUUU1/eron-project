# Clinical Record Compact v3.1 Lean

Compact v3.1 Lean is the sparse internal Gemma contract used before projection
to the unchanged public `clinical-workflow-v2` response.

## Contract boundary

Gemma returns only explicitly supported atomic Facts and mentioned fields. A
field contains only model-authored `text` and `fact_refs`. An omitted field means
`NOT_ASSESSED`; an explicit denial remains a `DENIED` Fact. Backend code inserts
only the missing empty field envelope required by the UI and never writes or
rewrites clinical prose.

Each Fact requires `type`, `assertion`, and one or more source segment IDs.
`MATCHED_TERM` adds an immutable `candidate_ref`; `UNMATCHED_TERM` and
`NARRATIVE` add bounded text; `MEASUREMENT` adds at most eight explicit values
including `kind` and `value`. High-risk examination, assessment, plan, and
outcome Facts may carry the bounded `fact_type` semantic act.

The model receives a minimal candidate projection only: `candidate_ref`,
`segment_id`, source `surface`, `canonical`, `semantic_types`, and retrieval
`source`. The response schema deliberately contains no runtime candidate or
segment enums and no per-candidate conditional bindings. Backend validation
checks the full immutable snapshot, candidate-to-segment binding, source
segment, Fact reference, assertion, numeric, and field-specific contracts.

## Bounded generation

Ordinary inputs use one sparse record call when the serialized input estimate
is at most 3,500 tokens, there are at most 16 segments, and expected output is
at most 4,096 tokens. Oversized input uses segment-boundary Fact chunks with a
maximum of 16 owned segments and 64 candidate references per chunk. The prior
chunk's final segment may be context-only and cannot generate a Fact.

At most eight initial Fact chunks run with concurrency two. Fact IDs are renamed
structurally (`c01_f001`) and exact structural duplicates are collapsed; facts
from different source segments are not semantically merged. A final call writes
only sparse fields from validated Facts. If that response reaches its length
limit, three fixed field groups are attempted within the global call budget.

The logical model-call budget is nine, recursive chunk split depth is four, the
ClinicalNLP request deadline is 620 seconds, and an Ollama call is bounded to
240 seconds. `done_reason=length` skips same-size repair and selects the bounded
long-input path. Schema-invalid `stop` responses retain one repair and one
original-context regeneration. Transient network failures receive at most one
retry when the deadline permits.

Successful chunks are preserved. Failed segment ranges produce `partial` and
`CHUNK_GENERATION_FAILED`; they never become missing clinical information.
Clinical `BLOCK` is a validation result, not a technical failure. Runtime audit
stores chunk IDs, source segment IDs, status, timing, output hashes, and Fact-ID
mapping without duplicating RAW dialogue, translations, or full model output.

## Rollout

- `legacy`: established Compact v3 output remains authoritative.
- `lean_shadow`: legacy remains authoritative and Lean runs for local review.
- `lean_primary`: Lean is projected to the unchanged UI contract.

The older `off`, `compare`, and `primary` values remain rollback-compatible.
OCI code is updated only after local tests and a Git commit; no runtime server is
edited directly.
