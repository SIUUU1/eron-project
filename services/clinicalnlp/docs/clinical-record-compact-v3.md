# Clinical Record Compact v3

Status: internal foundation, not connected to the production workflow.

## Purpose

Compact v3 is the internal Gemma output contract for the next clinical-record
workflow. It keeps clinical interpretation in the model while keeping evidence,
terminology provenance, and deterministic validation in backend code.

This contract does not replace `clinical-workflow-v2` yet. The existing workflow
and frontend response remain unchanged while individual emergency-record fields
are migrated and verified.

## Responsibility seam

Gemma owns:

- extracting explicit clinical facts from the supplied conversation;
- preserving `PRESENT`, `DENIED`, and `UNCERTAIN` assertions;
- selecting only candidate references supplied in its input;
- placing facts in approved clinical fields; and
- writing each field's complete display text.

Backend code owns:

- validating fact, segment, and candidate references;
- resolving immutable candidate snapshots;
- attaching RAW, translation, timestamps, CUI, semantic types, and scores;
- validating numeric, enum, guardrail, and threshold contracts;
- projecting fact issues to field and document statuses; and
- adapting approved Compact v3 fields to the existing UI contract.

Backend code must not assemble or rewrite clinical sentences, move facts between
fields, change assertions, choose a replacement candidate, or delete unsupported
model text. Unsupported text is preserved and blocked or marked for review.

## Common envelope

All twelve field keys are present. `NOT_MENTIONED` means the conversation did not
provide the information; it never means a negative clinical assertion.

```json
{
  "schema_version": "clinical-record-compact-v3",
  "facts": {},
  "fields": {
    "chief_complaint": {
      "generation_status": "NOT_MENTIONED",
      "text": null,
      "fact_refs": []
    }
  }
}
```

Generation statuses:

- `GENERATED`: display text and at least one valid fact reference are required.
- `NOT_MENTIONED`: text, fact references, and error code are empty.
- `FAILED`: text and fact references are empty and `error_code` is required.

The common fact variants are:

- `MATCHED_TERM`: references an immutable candidate snapshot;
- `UNMATCHED_TERM`: preserves an explicit expression with `NO_MATCH`;
- `NARRATIVE`: preserves a grounded model-written clinical statement; and
- `MEASUREMENT`: carries structured values for later field-specific validation.

Field-specific `values` shapes and display consistency checks are intentionally
deferred until each field contract is approved.

## History of present illness

The HPI field follows OLDCARTS without requiring every element. Gemma writes the
complete Korean clinical narrative and references only conversation-grounded
facts for onset, location, duration, character, aggravating and alleviating
factors, radiation, timing, severity, associated symptoms, pre-hospital care,
and arrival or transfer details.

- `PRESENT` records an explicitly supported finding.
- `DENIED` records only an explicit denial; it is the internal equivalent of the
  legacy public `NONE` status.
- `UNCERTAIN` preserves ambiguous or conflicting statements without selecting
  one source as true.
- an unasked element has no fact and is not mentioned in the display text; it
  must never be rendered as an absent finding.

One HPI element may reference multiple facts. This is required for conflicting
accounts such as different onset times from a patient and guardian. The field
text must describe the conflict, and the backend projects the unresolved
conflict to `REVIEW_REQUIRED` without rewriting the narrative. Facts referenced
by pain assessment or review of systems may also be referenced by HPI; sharing a
fact does not create a duplicate clinical assertion.

## Past medical history

Past history contains conversation-grounded pre-existing diseases, completed
surgeries or transplants, and clinically relevant previous admissions. It does
not duplicate medication, allergy, smoking, or alcohol facts owned by their
separate fields. The display text is a concise comma-separated list rather than
a narrative paragraph.

- explicit, unambiguous items use validated standard terminology or a common
  clinical abbreviation and the `(+)` marker;
- uncertain disease identity preserves the broader statement and uses an
  `UNCERTAIN` fact, for example `Cardiac disease(+, unspecified)`;
- relative or calendar timing is displayed only when explicitly stated;
- the legacy display `특이 과거력 없음` is allowed only when medical,
  surgical, and admission history each have an explicit `DENIED` fact;
- an unasked category has no fact and cannot contribute to a whole-field denial;
- no mentioned past-history information produces `NOT_MENTIONED`, which the
  legacy UI adapts to `미확인`.

When an unsupported whole-field denial is generated, backend validation
preserves the text and raises the field to `REVIEW_REQUIRED`; it does not rewrite
the medical statement.

## Current medications

Current medications contains every explicitly stated ongoing medication. The
display text is a comma-separated list of drug names; clinical importance or
drug class must never be used to omit a grounded medication. Drug class, dose,
route, frequency, and last-dose metadata are separate structured values and are
included only when supported.

- a one-time medication taken or administered before arrival belongs to HPI;
- a newly administered or ordered emergency-department medication belongs to
  treatment plan;
- a spoken ingredient must not be converted into a product name, and a spoken
  product must not be replaced with an ingredient name;
- an unmatched or unclear medication is preserved with an `UNCERTAIN` fact;
- whole-field `DENIED` requires an explicit broad medication-history denial;
  denial of one named drug is not sufficient;
- an unassessed medication history produces `NOT_MENTIONED`, adapted to
  `미확인` by the legacy UI.

Backend validation compares the number of grounded current-medication items with
the comma-separated display entries. A likely omission preserves the generated
text and raises the field to `REVIEW_REQUIRED`. Validated terminology snapshots
may attach drug-class metadata, but the class cannot replace a medication name
or automatically select a product candidate.

## Allergy

The public compatibility key remains `drug_allergy`, but the field covers Drug,
Contrast media, Latex, Food, and Other allergies. Each positive item carries its
allergy type, a grounded allergen fact, and an optional grounded reaction fact.
The display uses `[Allergen] - [Reaction]`, or only `[Allergen]` when no reaction
was stated, with multiple positive items separated by commas.

- a specific allergen or category denial is retained separately and does not
  become whole-field `NONE`;
- when positive items and specific denials coexist, only positive items appear
  in the main display while the denial remains available for audit;
- whole-field `NONE` requires a grounded broad allergy-history denial;
- an unassessed allergy history produces `NOT_MENTIONED`, never `NONE`;
- an adverse effect or intolerance is not an allergy unless the dialogue calls
  it an allergy; genuine ambiguity is retained as `UNCERTAIN` and requires
  review; and
- terminology may standardize an unambiguous allergen or reaction only through
  a validated candidate snapshot. Unmatched wording is preserved for review.

Backend validation preserves model-written display text and raises
`REVIEW_REQUIRED` for an unsupported whole-field denial, uncertain item,
semicolon-separated display, or likely omission of a grounded positive item.

## Social history

Social history contains independently assessed smoking and alcohol facts. The
model writes one concise display line for each mentioned category and joins the
lines with a newline. An unasked category is omitted and remains
`NOT_MENTIONED`; it is never converted into `NONE` because the other category
was assessed.

Smoking preserves current or former use, daily amount, total duration, and
cessation duration when explicitly stated. `Never smoker` requires explicit
lifetime non-use; a denial of current smoking alone cannot establish it.
Pack-years are deterministic backend measurements, calculated only when daily
amount and duration are both grounded:

- `packs_per_day * duration_years`; or
- `cigarettes_per_day / 20 * duration_years`, using the approved conversion of
  20 cigarettes per pack.

The backend stores the calculated value and provenance, checks it against the
model-written `PY` display, and marks a mismatch or conflicting daily units for
review without rewriting the text. Missing calculation inputs produce no
Pack-years value.

Alcohol preserves frequency, beverage type, and amount per occasion in a short
Korean display. The model must not infer `Heavy drinker`, `High-risk drinker`,
or another risk category. Explicit smoking and alcohol denials are retained in
their own category; one denial does not determine the status of the other.

## Review of systems

Review of systems contains every explicitly reported symptom assertion from
chief complaint, HPI, and targeted system review. It excludes diagnoses,
medications, tests or plans, and clinician-observed examination findings. A
fact shared with HPI keeps the same evidence identity while ROS renders only its
presence assertion.

The model writes a comma-separated symbol display:

- `PRESENT` -> `[Symptom](+)`;
- `DENIED` -> `[Symptom](-)`; and
- `UNCERTAIN` -> `[Symptom](?)`.

Each distinct symptom appears once. Conflicting positive and negative
statements become one uncertain item rather than selecting a source. Onset,
duration, severity, frequency, NRS, color, and course remain in HPI and are not
allowed in the ROS display. A conversation with no reported symptom produces
`text: null` and an empty item list.

Backend validation preserves model-written text and checks item count,
assertion-symbol agreement, duplicate labels, detailed-context leakage, source
evidence, and agreement between the English display label and the grounded
translation or terminology candidate. A failed check raises
`REVIEW_REQUIRED`; it does not rewrite or delete the display.

## Physical examination

Physical examination contains only findings explicitly obtained or stated
during a clinician's direct examination. Patient-reported symptoms remain in
HPI or ROS and cannot establish tenderness, weakness, neurologic findings, or
another objective sign. Normal findings are limited to systems actually
examined; the model never fills an unexamined template system.

The model writes concise `[System]: [Finding]` lines using General, HEENT,
Chest, Abdomen, Back / Spine, Extremities / Musculoskeletal, Neurology, or
Other. Each structured finding carries its exact source phrase and one
assertion:

- `PRESENT`: an observed abnormal finding or an explicitly stated normal result;
- `ABSENT`: an examined abnormal finding explicitly found to be absent; and
- `UNCERTAIN`: an attempted examination that was limited or indeterminate.

Vital measurements remain in structured vitals, and diagnoses or causal
interpretations are excluded from the examination display. No mentioned
examination produces `text: null` and an empty finding list.

Backend validation preserves the model-written text and checks the supported
system set, system-line rendering, assertion agreement, source evidence, and
the presence of objective examination context in the current or immediately
preceding dialogue segment. A patient symptom projected as an examination
finding raises non-destructive G09 `BLOCK`; an uncertain or malformed finding
raises review without rewriting or deleting the draft.

## Treatment plan

Treatment plan contains only clinician-stated decisions, orders, completed
current-visit actions, conditional plans, cancellations, and patient refusals.
A patient request alone is not a plan until the clinician agrees. Broad wording
is preserved at its stated level: a blood-test plan cannot be expanded into an
unstated panel, and a generic medication or procedure cannot gain an invented
product, dose, route, or technique.

The model writes concise category lines for Diagnostic Workup, Medication /
Procedure, Consultation, and Disposition / Safety-netting. Each item carries an
exact source phrase, assertion, and execution state:

- `PRESENT` with `PLANNED`, `ORDERED`, or `COMPLETED`;
- `DENIED` with `CANCELED` or `REFUSED`; and
- `UNCERTAIN` with `CONDITIONAL`.

Completed, future, canceled, refused, and conditional actions remain distinct.
Diagnoses belong to impression; only the associated investigation, treatment,
consultation, disposition, follow-up, or safety-net action belongs here. No
mentioned plan produces `text: null` and an empty item list.

Backend validation preserves the model-written text and checks category
rendering, status/assertion agreement, source grounding, clinician plan context,
and whether every displayed action is supported by the category's evidence. An
unstated action raises non-destructive G08 `BLOCK`; conditional content remains
visible as `REVIEW_REQUIRED` without becoming a definite order.

## Candidate snapshot invariant

A `candidate_ref` points to the retrieval result stored when the draft was
created. It is not a key for re-running a live dictionary or vector search.

The sealed snapshot includes the request and query identity, source segment and
span, selected candidate, terminology provenance, score and rank, runtime data
versions, and creation time. Its hash detects later mutation. Updating UMLS,
dictionaries, or vectors requires a new workflow execution and new references;
an old reference must continue to resolve to its original snapshot.

## Non-destructive validation

`validate_compact_record` returns issues and projected statuses without modifying
the supplied draft.

- unknown candidate reference: preserve text, `REVIEW_REQUIRED`;
- changed candidate snapshot: preserve text, `BLOCK`;
- unknown segment or unsupported evidence: preserve text, `BLOCK`;
- generated text without a valid fact: preserve text, `BLOCK`;
- failed field generation: explicitly distinguish from not mentioned;
- unresolved opposing assertions: emit G04-compatible review issue;
- issue propagation: fact -> every referencing field -> document, using the
  highest severity (`PASS < REVIEW_REQUIRED < BLOCK`).

Numeric text/value consistency and fixed assertion display checks belong to the
individual field contracts because parsing arbitrary narrative text in backend
code would cross the responsibility seam.

## Migration order

1. Add and test the common envelope and validation module.
2. Approve and implement each of the twelve field contracts.
3. Add a pass-through adapter to the existing UI response.
4. Run fixed and unseen Whisper JSON regression tests behind an opt-in mode.
5. Switch the default only after all fields and the existing UI contract pass.
6. Remove legacy semantic string assemblers in a separate cleanup change.
