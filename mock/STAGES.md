# STAGES: expected output of every stage for `bnpl_checkout_v1`

Source of truth is the BNPL Checkout Merge design doc. This file restates it as module contracts.
Each stage lists: input → output → Atlas collection → what the test asserts.

## Stage 0: inputs (no code)
`mock/inputs/northwind/SKILL.md`, `mock/inputs/lakeside/bnpl-arazzo.yaml`, `mock/inputs/lakeside/merge-profile.yaml`.

## Stage 1: compile (`pmp/compile.py`)
Input: the three files. Output: node + transition documents per org, submission hash.
Collection: `submissions` (one doc per org: `{org_id, use_case_id, nodes:[...], transitions:[...], hash, version}`).

Node shape (see `spec/node.schema.json`):
`_id "A.finalize_invoice"`, `org_id`, `use_case_id`, `name`, `kind: action|decision|human_gate|handoff|wait|terminal`,
`visibility: internal|handoff_out|handoff_in`, `role`, `tool: {server, name, effect: pure|verify|side_effect, reversible, money}`,
`inputs: [{path, type, unit?, currency?, sensitivity?, may_cross?}]`, `outputs: [...]`, `pre`, `post` (predicate AST),
`branches: [{when: AST, then: node_id}]`, `policy`, `obligation`, `dedupe`, `text` (prose for embedding).
Transition: `{_id, org_id, from, to, condition: AST, guidance, pitfalls}`.

Expected: Northwind 10 nodes / 11 edges (8 steps + 2 terminals; A7 is `human_gate`). Lakeside 10 nodes / 12 edges
(7 Arazzo steps + END_not_eligible terminal as `handoff_out` + 2 internal decision points).

LINT findings (collection `findings`, stage `compile`):
- B LINT-01 dangling reference: `steps[4].parameters.redirectAuthToken` references itself; produced by `initiateBnplTransaction`. Compiled with corrected source; flagged.
- B LINT-01 dangling reference: `steps[5].parameters.loanTransactionId` declared by `initiateBnplTransaction`. Compiled as derived; flagged.
- A LINT-02 policy in prose: "USD 10,000 needs a manager's approval" → `policy {requires_human_approval: true, max_amount: {USD, 10000}}` + HUMAN_GATE node.
Test: exactly 2 LINT-01 for org B, 1 LINT-02 for org A, Northwind has exactly one `human_gate`.

## Stage 2: align (`pmp/align.py`)
Input: `submissions`. Steps: embed each node's `text` → `steps_vec` `{_id, org_id, use_case_id, embedding}`;
for each A node of `visibility != internal` plus each A node with a side-effecting tool, run `$vectorSearch` on
`steps_vec_idx` filtered to `org_id: "B"`, `numCandidates: 50, limit: 5`; for each candidate pair ask the LLM
(with both nodes, their neighborhoods, I/O schemas, tool effect classes) to return JSON:
`{sigma, iota: compatible|partial|incompatible|missing:<field>, tau: "<A effect>/<B effect>", proposal, confidence}`
with proposal ∈ `distinct | provides_input | relate | on_failure | merge`.
Collection: `alignments` `{_id, use_case_id, a, b, sigma, iota, tau, proposal, confidence, confirmed_by: null}`.

Expected rows:
| a | b | σ | ι | τ | proposal | conf |
|---|---|---|---|---|---|---|
| create_customer | createCustomer | 0.93 | incompatible | side_effect/side_effect | distinct | 0.88 |
| finalize_invoice | checkLoanCanBeProvided | 0.34 | partial | side_effect/verify | provides_input | 0.90 |
| await_invoice_paid | retrieveFinalizedPaymentPlan | 0.41 | partial | verify/verify | relate | 0.62 |
| fulfil_order | updateOrderStatus | 0.71 | missing loanTransactionId | side_effect/side_effect | provides_input | 0.86 |
| collect_payment | END_not_eligible | 0.12 | n/a | n/a | on_failure | 0.60 |
Test: these 5 pairs exist with the same proposals (σ within ±0.1; confidence within ±0.15). Nothing is `confirmed_by` yet.

## Stage 3: merge + validate (`pmp/merge.py`, `pmp/validate.py`)
merge: union of both graphs over alignments with proposal ∈ {provides_input, on_failure, merge}; boundary edges
h1 (finalize_invoice → checkLoanCanBeProvided), h2 (retrieveFinalizedPaymentPlan → await_invoice_paid),
h3 (fulfil_order → updateOrderStatus). Collection: `merged` `{use_case_id, version, nodes, edges, boundary_edges}`.
validate: check registry, each check returns `{id, verdict: pass|fail|guarded, scope, detail, rung, stakes: low|high, owner_org}`.
Collection: `findings` (stage `validate`).

Expected findings, version 1:
| check | verdict | scope | detail | rung | stakes | owner |
|---|---|---|---|---|---|---|
| IO-01 | fail | h1 | sender `unit_amount_minor` (cents) + `price_id`; receiver `purchaseAmount{currency, amount}` (major) + `productCode` | 2 adapter | low | A |
| IO-02 | fail | h1 | customer name + email cross; no allowlist | 5 ask | high (pii) | A |
| STR-03 | fail | merged | cycle: A fulfils only when `invoice.status == paid`; B activates only after `updateOrderStatus`, which needs fulfilment | 5 ask | high (money) | A |
| PRE-01 | fail | h3 | receiver needs `loanTransactionId`; A never carries it | 5 ask | high | B |
| POL-01 | pass | h3 | `requires_human_approval` A true, B unset → true; `max_amount` A 10000, B unset → 10000 (lattice) | 1 | low | agent |
| FAIL-01 | fail | h1 | no failure path in A for lender terminal END_not_eligible | 4 → 5 | high (customer) | A |
| DUP-02 | fail | create_customer ~ createCustomer | near-duplicate σ 0.93 undecided; both side-effecting | 5 ask | high | A |
Structural checks use `$graphLookup` on `merged` for reachability (STR-01) and cycle detection (STR-03).
Test: exactly these 7 findings; POL-01 resolves without a question.

## Stage 4: decide (`pmp/decide.py`)
Low-stakes findings resolved by rule and logged to `merge_log` (`{type: "decision", id: d1.., finding, rule, patch}`).
High-stakes findings become one question each with a default, routed by the owner org's `merge_preferences.question_routing`.
Collection: `merge_questions` `{_id: q1.., use_case_id, trigger, to_org, to_role, text, default, answer, answered_by, ts, guardrail}`.
Questions are answered in a CLI (`python -m pmp.decide --answer-defaults` accepts all defaults).

Expected:
- q1 DUP-02 → northwind ops.lead: keep both; send `customer.name`, `customer.email`; never `customer_id` or payment method.
  Guardrails: alignment decision `keep_both`; allowlist on h1.
- q2 IO-01 → northwind (default): Northwind converts; `purchaseAmount.amount = unit_amount_minor × quantity / 100`; `productCode = price_id`.
  Guardrail: adapter node `A.adapt_basket_for_lender`, owner A.
- q3 STR-03 → northwind finance.controller: authorized plan counts as paid if `finalizedPaymentPlan.status == "authorized"`
  and `totalLoanAmount == invoice.total`. Guardrail: adapter node `A.mark_invoice_paid_out_of_band` with that predicate as precondition.
- q4 PRE-01 → lakeside (default): include `loanTransactionId` in the returned plan. Guardrail: allowlist on h2 includes `finalizedPaymentPlan.loanTransactionId`.
- q5 FAIL-01 → northwind (default): fall back to card payment. Guardrail: compensation edge opaque(B) `lender.declined` → `A.collect_payment`.
Guardrail types: `allowlist`, `adapter_node`, `predicate`, `compensation_edge`, `alignment_decision`.
Test: 5 questions, 3 defaults, every guardrail applied to `merged` version 2.

## Stage 5: contract (`pmp/contract.py`)
Re-run validate on merged v2: all checks pass or guarded. Certificate: `{certificate_id, use_case_id, contract_id, contract_version,
inputs: {h_A, h_B, h_alignments, h_answers, h_merge, h_projection_A, h_projection_B, h_boundary_interface}, checks:[...],
alignments:[... confirmed_by], questions:[...], issued_at, signatures: {mediator, A, B}}` (signatures are placeholders).
Projections: `projections` `{org_id, contract_version, nodes (own + adapters + 1 opaque), edges, boundary_interface, invariants: INV-1..4}`.
Expected: A projection 13 nodes (10 own + 2 adapters + 1 opaque Lakeside); B projection 11 nodes (10 own + 1 opaque Northwind).
Guarded: PRE-03 on h2 (`status == authorized AND totalLoanAmount == invoice.total`), PRE-03 on h3 (`invoice_total < 10000 OR approval.granted`).
Test: INV-1..4 pass for both; PRIV-01: no B node content in projection A and vice versa; contract v1 `active`.

## Stage 6: runtime (`pmp/runtime/`)
`agent.py --org A|B --port ...`: loads its projection, executes nodes with stubbed tools (return fixture data), sends handoffs as A2A-shaped
JSON: `{metadata: {pmp: {contract, cert, edge, run, seq}}, payload}`. Receiver checks: contract signed and active; edge in its projection;
payload matches boundary schema + allowlist; preconditions hold. Any failure → typed rejection `{code, edge, run, missing|field|got}`
written to `rejections`; contract status → `suspect`.
`mediator.py`: change stream on `rejections` → plan patch from the rejection (MISSING_FIELDS: search the sender org's own tool catalog
for a producer → extend or insert; TYPE_MISMATCH: adapter) → re-validate → new contract version → `active`.
Run 0001 (USD 2,780): h1 accepted, plan authorized, invoice marked paid out of band, fulfilled, h3 activates loan.
Run 0002: Lakeside republishes requiring `customerPostalCode` on h1 → rejection → mediator finds Northwind's `customer.shipping_address`
tool output → adapter extended → contract v2 → retry passes.
Test: run 0001 completes; run 0002 produces exactly one rejection and contract v2.

## Stage 7: UI + demo (`ui/`, `scripts/demo.sh`)
Three columns (Northwind, Atlas mediator, Lakeside), pipeline strip, contract pill, stopwatch, view toggle that turns the other org into
one sealed box after signing. `scripts/demo.sh` runs seed → compile → align → merge/validate → decide --answer-defaults → contract → agents
→ run 0001 → run 0002.
