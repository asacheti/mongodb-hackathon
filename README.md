# PMP: Procedure Merge Profile

Two organizations each have a private, step-by-step procedure. For one shared job, a mediator on
MongoDB Atlas turns the two into a single signed, executable joint procedure that both companies'
agents can run, handing work back and forth, **without either company reading the other's playbook**.

Built in one day at the MongoDB Harness Engineering & Model Wrangling hackathon, NYC, 26 Sep 2026.

## The use case

`bnpl_checkout_v1`: a merchant's Stripe invoicing procedure meets a lender's buy-now-pay-later loan
workflow. Both inputs are real:

| Org | Format | Source |
|---|---|---|
| Northwind Outdoor (merchant, `A`) | `SKILL.md` with typed YAML steps | Stripe, *Integrate with the Invoicing API* |
| Lakeside BNPL (lender, `B`) | Arazzo 1.0.0 workflow + a merge-profile sidecar | OpenAPI Initiative, `examples/1.0.0/bnpl-arazzo.yaml`, verbatim |

The company names are invented; the procedures are not. The compiler even finds two mistakes in the
published Arazzo example (a step reading its own output, an undeclared loan id).

## What happens

```
compile ─▶ align ─▶ merge ─▶ validate ─▶ decide ─▶ contract ─▶ runtime (agents + mediator)
 typed     LLM +    union    17 checks   1 rule    certificate  A2A handoffs, 4 receiver
 graphs    vector   h1 h2 h3 7 findings  5 Qs      projections  checks, rejection ▶ patch
```

1. **Compile.** Each input becomes a graph of typed nodes and transitions (I/O schemas, preconditions as a
   JSON predicate AST, tool effects, policies). Two LINT findings on Lakeside, one on Northwind.
2. **Align.** Every node text is embedded into `steps_vec`; `$vectorSearch` proposes candidate pairs; an LLM
   proposes how each pair relates (`distinct | provides_input | relate | on_failure | merge`). Deterministic
   guards keep the LLM honest: tool effects decide τ, shared names floor σ, nothing side-effecting merges,
   input only crosses handoff-out → handoff-in. This is the **only** stage that calls an LLM.
3. **Merge.** Deterministic union over the alignments. Three boundary edges: h1 basket → lender, h2 plan →
   merchant, h3 fulfilment → lender. Written as one graph doc plus per-node adjacency docs.
4. **Validate.** A check registry (IO, STR, PRE, POL, FAIL, DUP, TOOL, ...). STR-01 uses `$graphLookup`.
   Version 1 yields exactly seven findings: unit mismatch, pii crossing, a "what counts as paid" deadlock, a
   missing loan id, a policy lattice, a missing decline path, a near-duplicate step.
5. **Decide.** Low stakes resolve by rule (the policy lattice). High stakes become one question each,
   routed by the owner org's own `question_routing`, always with a default. Question text is generated from
   findings and boundary fields only, never from `confidential_notes`. Answers compile to guardrails:
   allowlists, two adapter nodes, a predicate, a compensation edge, a keep-both decision.
6. **Contract.** Revalidate (35 checks, 2 guarded), build projection A (13 nodes) and projection B (11): own
   nodes plus **one opaque node** for the partner carrying only the boundary interface. INV-1..4 locally,
   PRIV-01 as mediator. Certificate with real sha256 inputs; contract v1 active.
7. **Runtime.** Each org's agent runs its projection with stubbed tools and sends handoffs as A2A tasks. The
   receiver runs four checks in order (contract active, edge in projection, schema + allowlist, preconditions).
   Run 0001 completes. In run 0002 Lakeside republishes requiring a postal code; the first h1 is rejected
   (`MISSING_FIELDS`), the contract goes suspect, the mediator finds the field in Northwind's own data model,
   extends the adapter, revalidates, issues contract v2, and the retry passes.

## Atlas features used

- **Vector Search** (`steps_vec_idx`, 1536-dim cosine, filtered by `org_id` / `use_case_id`) for alignment candidates.
- **`$graphLookup`** over per-node adjacency docs in `merged` for reachability (STR-01).
- **Change streams** on `rejections`: the mediator reacts to a runtime rejection and republishes the contract.
- Eleven collections, every document carrying `use_case_id` and, where it belongs to one org, `org_id`.

## Run it

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env      # MONGODB_URI, OPENAI_API_KEY (+ OPENAI_BASE_URL for OpenRouter), PMP_DB=pmp

scripts/demo.sh           # seed → ... → contract → agents + mediator + API → run 0001 → run 0002
                          # UI at http://127.0.0.1:8000 ; add --fixture-align to skip the LLM
```

Stage by stage:

```bash
scripts/seed.sh                                    # reset the use case, create collections + vector index
python -m pmp.compile  --use-case bnpl_checkout_v1 --fixture
python -m pmp.align    --use-case bnpl_checkout_v1 [--dry-run | --from-fixture]
python -m pmp.merge    --use-case bnpl_checkout_v1
python -m pmp.validate --use-case bnpl_checkout_v1 --version 1
python -m pmp.decide   --use-case bnpl_checkout_v1 [--answer-defaults | --answers mock/stages/4_answers.json]   # interactive otherwise
python -m pmp.contract --use-case bnpl_checkout_v1
python -m pmp.runtime.run --run 0001 && python -m pmp.runtime.run --run 0002      # in-process, against Atlas
python -m pmp.runtime.run --run 0002 --in-memory                                  # no Atlas at all
```

Tests: `pytest -q` (offline, in-memory backend, ~120 tests) and `pytest -m live` (Atlas + LLM).

## Modules

| Module | Stage | Reads → writes |
|---|---|---|
| `pmp/compile.py` | 1 | inputs → `submissions`, `findings` (LINT) |
| `pmp/align.py` | 2 | `submissions` → `steps_vec`, `alignments` (LLM here, and only here) |
| `pmp/merge.py` | 3a | `submissions`, `alignments` → `merged` (graph + adjacency docs) |
| `pmp/validate.py` | 3b, 5 | `merged` → `findings` (check registry; `$graphLookup`) |
| `pmp/decide.py` | 4 | `findings` → `merge_questions`, `merge_log`, `merged` v2 |
| `pmp/contract.py` | 5 | `merged` v2 → `contracts` (certificate), `projections` |
| `pmp/runtime/a2a.py` | 6 | handoff envelope + the four receiver checks |
| `pmp/runtime/agent.py` | 6 | executes a projection; FastAPI `/a2a`, `/run/{id}`, `/state` |
| `pmp/runtime/mediator.py` | 6 | change stream on `rejections` → patch → contract v+1 |
| `pmp/runtime/api.py` | 7 | serves `ui/index.html`, aggregates `/state` |
| `pmp/runtime/run.py` | 6 | drives run 0001 / 0002 (in-process, HTTP, or `--in-memory`) |
| `pmp/predicate.py` | all | the one predicate evaluator (no LLM, ever) |
| `pmp/spec.py`, `spec/*.schema.json` | all | every document validates against a JSON Schema |
| `pmp/db.py`, `pmp/memstore.py` | all | single Atlas entry point; in-memory stand-in for tests |

`mock/STAGES.md` is the spec each module is built to; `mock/stages/*.json` hold expected outputs and
runtime fixtures (and serve as fallbacks: `pmp.align --from-fixture`).

## Design rules that held all day

- The LLM proposes; everything after it is deterministic. Same inputs + same decision records = same output, byte for byte.
- Privacy holds between the two orgs, not from the mediator. Each org gets its own projection; the partner is one sealed box.
- Nothing is resolved by quietly deleting evidence: a refused answer leaves a first-class conflict that blocks the contract.
- Every finding names its rung on the resolution ladder (lattice → adapter → reorder → compensation edge → ask → reject) and its stakes.

## What was built today

Everything in `pmp/`, `spec/`, `mock/stages/`, `scripts/`, `ui/` and `tests/`: compiler for two input formats,
LLM aligner with vector search, deterministic merge, a 15-check validator, the interview engine, projections
with privacy checks, certificates, two runtime agents, the self-healing mediator loop, the demo UI, and ~120
tests. The LLM defaults to `gpt-4.1` via OpenRouter (about $0.25 per alignment run); `gpt-4o-mini` was not
good enough to reproduce the alignment table.
