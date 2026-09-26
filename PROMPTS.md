# Claude Code prompts, in order. Paste one at a time. Wait for tests to pass before the next.

## P0 (setup, 5 min)
Read CLAUDE.md and mock/STAGES.md fully. Then: create the layout from CLAUDE.md, install requirements.txt into .venv,
write scripts/seed.sh (drops the use case with pmp.db.reset, creates the collections, and creates the Atlas Search index
`steps_vec_idx` on steps_vec via pymongo `create_search_index` with the vector definition in CLAUDE.md). Write
tests/test_predicate.py covering every op in pmp/predicate.py with at least one true and one false case each, plus
fields_referenced on a nested and/or. Run pytest. Commit "scaffold + predicate engine".

## P1 (compile, target 12:15)
Write pmp/compile.py per Stage 1 in mock/STAGES.md. It parses mock/inputs/northwind/SKILL.md (frontmatter + the YAML
blocks under each "## A<n>" heading and the top-level YAML sections), and mock/inputs/lakeside/bnpl-arazzo.yaml merged
with merge-profile.yaml (step_annotations, handoffs, policies). Emit nodes and transitions that validate against
spec/node.schema.json and spec/transition.schema.json (use jsonschema with a RefResolver over spec/). Each node's `text`
is the prose paragraph plus name; never include confidential_notes in text. Run LINT-01 (a $steps.X reference where X is
the step itself or a step that does not declare that output) and LINT-02 (a policy sentence in prose with a money
threshold and no policy field → add policy + a human_gate node). Hash each submission with sha256 over canonical JSON.
Write to `submissions` and `findings` (stage compile). CLI: `python -m pmp.compile --use-case bnpl_checkout_v1`.
tests/test_compile.py: Northwind 10 nodes / 11 transitions with exactly one human_gate; Lakeside 10 nodes / 12
transitions; exactly 2 LINT-01 for B and 1 LINT-02 for A; both hashes start with "sha256:". Run pytest. Commit.

## P2 (align, target 1:00)
Write pmp/align.py per Stage 2. Embed every node's text with text-embedding-3-small into steps_vec. For each A node
that is handoff_out/handoff_in or has a side_effect tool, run $vectorSearch on steps_vec_idx filtered to org_id "B"
(numCandidates 50, limit 5). For each candidate pair, call the LLM (gpt-4o-mini is fine) with: both nodes' name, text,
inputs, outputs, tool effect, and their predecessor/successor names. Ask for strict JSON matching spec/alignment.schema.json
minus _id and confirmed_by; retry once on invalid JSON. Keep only proposals with confidence >= 0.5, dedupe by (a, b),
write to `alignments` with confirmed_by null. CLI flag --dry-run prints the table. tests/test_align.py must pass offline:
mock the embedding and LLM calls with fixtures in mock/stages/2_alignments.json and assert the 5 expected pairs and proposals
from STAGES.md. Add an integration test marked @pytest.mark.live that hits Atlas and OpenAI. Run pytest -m "not live". Commit.

## P3 (merge + validate, target 2:00)
Write pmp/merge.py: union both submissions over alignments with proposal in {provides_input, on_failure, merge}; create
boundary edges h1, h2, h3 as in Stage 3; write `merged` version 1. Write pmp/validate.py with a check registry: each check
is a function (merged, submissions, alignments, questions) -> list[finding]. Implement IO-01 (unit/type/name mismatch across
a boundary edge by comparing sender outputs to receiver inputs), IO-02 (pii/financial field crossing without an allowlist),
STR-01 (reachability to a terminal via $graphLookup on `merged`), STR-03 (cycle detection), PRE-01 (receiver precondition
references a field not in the boundary schema; use predicate.fields_referenced), POL-01 (lattice: requires_human_approval
OR, max_amount min; pass and record as guard), FAIL-01 (handoff_out terminal on B with no incoming failure edge from A),
DUP-02 (alignment with sigma >= 0.85 and no decision), TOOL-01..03 (own tools only; every action has a tool; side_effect
money tools have a gate on their path). Every finding gets rung, stakes and owner_org per STAGES.md; stakes is low only if
the fix is derivable from the two submissions alone and touches no money, pii or customer messaging. CLI:
`python -m pmp.validate --use-case bnpl_checkout_v1 --version 1`. tests/test_validate.py: exactly the 7 findings of Stage 3
with the stated verdicts and stakes. Commit.

## P4 (decide, target 2:45)
Write pmp/decide.py. Low-stakes findings: apply the rule (lattice, adapter, extend_from_own_tools, field_rename, relate),
write a decision doc (spec/decision.schema.json) to merge_log and patch `merged` into version 2. High-stakes findings: create
one question each (spec/question.schema.json) with a default, routed by the owner org's merge_preferences.question_routing.
The question text must be generated from the finding and the boundary fields only, never from confidential_notes. CLI:
`python -m pmp.decide --use-case bnpl_checkout_v1` asks interactively; `--answer-defaults` accepts defaults; `--answers file.json`
loads answers. Compile each answer into a guardrail and apply it to `merged` v2 (allowlist on the boundary edge, adapter node
with origin = question id, predicate as node precondition, compensation edge, alignment decision). tests/test_decide.py:
with mock/stages/4_answers.json the 5 questions of Stage 4 exist, 3 are defaults, and merged v2 has adapters
A.adapt_basket_for_lender and A.mark_invoice_paid_out_of_band plus the compensation edge. Commit.

## P5 (contract, target 3:15)
Write pmp/contract.py: re-run validate on merged v2 (stage revalidate); require every finding pass or guarded; build the
certificate (spec/certificate.schema.json) with real hashes over submissions, alignments, answers, merged, projections and the
boundary interface; build projections A and B (own nodes + adapter nodes with origin + one opaque node for the other org carrying
only the boundary interface) and check INV-1..4 and PRIV-01; write `contracts` (status active, version 1) and `projections`.
CLI: `python -m pmp.contract --use-case bnpl_checkout_v1`. tests/test_contract.py: A projection has 13 nodes, B has 11, no
string from any B confidential_notes or internal node name appears in projection A's JSON and vice versa. Commit.

## P6 (runtime, target 4:15)
Write pmp/runtime/a2a.py (build handoff per spec/handoff.schema.json; receiver_check returns None or a rejection per
spec/rejection.schema.json, running the four checks in order), pmp/runtime/agent.py (FastAPI app, `--org A|B --port N`,
loads its projection, executes nodes in order with stubbed tools that return data from mock/stages/6_fixtures.json, POSTs
handoffs to the other agent's /a2a endpoint, exposes /state), pmp/runtime/mediator.py (opens a change stream on `rejections`;
on insert, plans a patch: MISSING_FIELDS → look up the sender org's submission for a tool whose outputs include the missing
field → extend the adapter's inputs or insert a step; TYPE_MISMATCH → adapter; re-run validate; write contract v+1 active and
mark the previous superseded; write the patch to merge_log), and pmp/runtime/api.py (serves ui/index.html at / and aggregates
/state from both agents and the latest contract). Runs: `python -m pmp.runtime.run --run 0001` and `--run 0002` where 0002
first republishes Lakeside requiring customerPostalCode on h1. tests/test_runtime.py (in-process, no network): run 0001
completes with h1, h2, h3 accepted; run 0002 yields exactly one rejection with code MISSING_FIELDS and a contract version 2. Commit.

## P7 (demo, target 4:45)
Write scripts/demo.sh that runs seed → compile → align → validate → decide --answer-defaults → contract → starts agent A,
agent B, mediator and api in the background → run 0001 → run 0002, printing one line per stage with elapsed time. Write
ui/index.html: three columns (Northwind, Atlas mediator, Lakeside), pipeline strip, contract pill (active / suspect + version),
stopwatch from seed to run 0002 success, a "View as" toggle that after signing renders the other org as one sealed box
showing only the boundary interface, and an activity log polled from /state every second. Plain HTML + CSS + JS, no framework.
Finish README.md: pitch, architecture, how to run, what each module does, what was built today. Commit.
