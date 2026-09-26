# PMP: Procedure Merge Profile

**Two companies. Two private playbooks. One signed joint procedure, without either side reading the other's.**

Team **InterLock**. Built in one day at the MongoDB Harness Engineering & Model Wrangling hackathon (.local NYC, 26 Sep 2026).

## The problem

When two organizations have to work together on one job, each already has a step-by-step procedure for
its half. Reconciling the two is done by email today: weeks of "what do you send us?", "what counts as
paid?", "who converts the units?". Both procedures are commercial assets, so neither side wants to hand
the other its playbook. And once agents execute these procedures, a wrong handoff is a wrong API call.

## What PMP does

A trusted mediator on MongoDB Atlas takes both procedures in full, lines up the steps that correspond,
joins the two into one graph, checks that the result is sound, asks each company a handful of questions
only where it cannot decide alone, and issues a **signed contract**. Each company gets back **only its own
slice**, with the partner collapsed into a single opaque box that says what goes in, what comes out, and
how it can end. At run time each company's agent executes its slice with its own tools and hands typed
payloads across the boundary. A handoff that breaks the agreed conditions is refused, the contract is
flagged, and the mediator repairs it.

Privacy holds **between the two organizations**, not from the mediator. That is the same trust model as a
data clean room.

## The worked example (real inputs)

| Org | Role | Procedure | Source |
|---|---|---|---|
| Northwind Outdoor (`A`) | merchant | `mock/inputs/northwind/SKILL.md`: a Claude-style skill with typed YAML steps | Stripe, *Integrate with the Invoicing API* |
| Lakeside BNPL (`B`) | lender | `mock/inputs/lakeside/bnpl-arazzo.yaml` + `merge-profile.yaml` sidecar | OpenAPI Initiative, Arazzo 1.0.0 example, used verbatim |

The company names are invented; the procedures are not. Two formats on purpose: a merchant writing agent
skills and a lender publishing an API workflow spec would never share one. The compiler normalises both
into the same typed graph, and along the way finds two real mistakes in the published Arazzo example (a
step reading its own output; a loan id no step declares).

## What the demo shows, stage by stage

```
compile ─▶ align ─▶ merge ─▶ validate ─▶ decide ─▶ contract ─▶ runtime
```

| # | Stage | What happens | Numbers |
|---|---|---|---|
| 1 | **Compile** | Both inputs become graphs of typed nodes and transitions: I/O schemas with units and sensitivity, preconditions as a JSON predicate AST, tool effect classes, policies. Money thresholds written in prose become policy objects. | A: 10 nodes / 11 edges. B: 10 nodes / 12 edges. 3 LINT findings. |
| 2 | **Align** | Every step is embedded into `steps_vec`; **Atlas Vector Search** proposes candidates; an LLM proposes how each pair relates. Deterministic guards keep it honest: tool effects decide the effect class, shared names floor similarity, nothing side-effecting merges, input only crosses from a handoff-out step into a handoff-in step. **This is the only stage that calls an LLM.** | 5 alignments, none confirmed yet. |
| 3 | **Merge + validate** | Deterministic union over the alignments; three boundary edges (basket → lender, plan → merchant, fulfilment → lender). A registry of checks runs; reachability uses **`$graphLookup`** over per-node adjacency docs. | 20 nodes, 26 edges. Exactly 7 findings: unit mismatch (cents vs major), pii crossing with no allowlist, a "what counts as paid" deadlock, a missing loan id, a policy lattice, a missing decline path, a near-duplicate step. |
| 4 | **Decide** | Low stakes resolve by rule (stricter policy wins). Each high-stakes finding becomes one question, routed by the owner org's own routing table, always with a default. Question text is built from findings and boundary fields only; confidential notes never reach it. Answers compile to guardrails. | 1 rule decision, 5 questions (4 to Northwind, 1 to Lakeside), 3 defaults accepted. Guardrails: 3 allowlists, 2 adapter nodes, 1 predicate, 1 compensation edge, 1 keep-both decision. |
| 5 | **Contract** | Revalidate. Build projection A and projection B: own nodes plus one opaque partner node carrying only the boundary interface. Each org checks four local invariants; the mediator checks that nothing of the partner leaked. Certificate with real sha256 over every input. | 35 checks: 33 pass, 2 guarded. Projection A 13 nodes, B 11. Contract v1 active. |
| 6 | **Runtime** | Each org's agent executes its projection (tools stubbed from fixtures) and sends A2A-shaped handoffs. The receiver runs four checks in order: contract active, edge in projection, payload matches schema + allowlist, preconditions hold. | Run 0001 (USD 2,780): h1, h2, h3 accepted. |
| 7 | **Self-repair** | Lakeside republishes requiring a postal code. The first h1 is rejected (`MISSING_FIELDS`), the contract goes *suspect*. The mediator reads the rejection from an **Atlas change stream**, finds `customer.shipping_address` in Northwind's own data model, extends Northwind's adapter, revalidates, issues contract v2 and supersedes v1. The retry passes. | Run 0002: 1 rejection, contract v2, completed. |
| 8 | **Update** | Northwind raises its approval threshold from $10,000 to $15,000. Every guardrail, check and projection recorded what it was built from (the **derivation index**: `elements` + `derivations`), so the change is a reverse lookup: rebuild only the h3 guard and the gate condition, re-run only the checks that read them, carry everything else from certificate v2, re-ask nobody (all five answers carried as precedent), and require signatures only from orgs whose slice changed. The system predicts the friction before the change goes in: "Lakeside will need to re-sign but not answer anything." | 6 elements changed, class 2 (boundary), 3 guardrails rebuilt, 9 checks re-run, 16 carried, 0 questions, contract v3. |

End to end, seed to contract v3, takes about two minutes on the venue network.

## The walkthrough

Everything above is **precomputed once** and saved to `ui/demo.json`, model outputs included, so the demo never
waits on Atlas or an LLM. The page walks through six scenes: **Input · Merge · Interview · Contract · Run · Update**.

```bash
scripts/view.sh              # http://127.0.0.1:8000, static: no Atlas, no key, no services
python -m pmp.snapshot       # regenerate ui/demo.json (uses the live alignments in Atlas when present)
```

On the Contract scene, switch **View as Northwind**: Lakeside collapses into one sealed box showing only the
three boundary edges, their fields and guards. On the Update scene, the change is traced through the derivation
index: red elements changed, amber guardrails and checks rebuilt or re-run, indigo projections re-signed, grey
carried forward untouched.

## MongoDB Atlas features used

- **Vector Search** (`steps_vec_idx`: 1536-dim cosine, filtered by `org_id` and `use_case_id`) for alignment candidates.
- **`$graphLookup`** for reachability over the merged graph, stored as one graph document plus one adjacency document per node.
- **Change streams** on `rejections`: the mediator is event-driven; a runtime rejection triggers the repair loop.
- Thirteen collections mirroring the pipeline (`submissions`, `steps_vec`, `alignments`, `merged`, `findings`,
  `merge_questions`, `merge_log`, `contracts`, `projections`, `handoffs`, `rejections`, `elements`, `derivations`). Every document carries
  `use_case_id` and, where it belongs to one org, `org_id`, so one query answers "what does org A know?".

## Design rules that held all day

- **The LLM proposes; everything after it is deterministic.** Same inputs and same decision records give the
  same merged graph, byte for byte. Hashes in the certificate make that checkable.
- **Privacy between orgs, not from the mediator.** Each org gets its own projection; the partner is one sealed box.
- **People are asked only when it matters.** Every question comes from a validator finding, goes to the org
  that owns the decision, and carries a default. Five questions replaced what is normally weeks of email.
- **Nothing is resolved by quietly deleting evidence.** A refused answer leaves a first-class conflict that blocks
  the contract. Every finding names its rung on the resolution ladder (lattice → adapter → reorder →
  compensation edge → ask → reject) and its stakes.
- **Predicates are data.** One JSON AST, one evaluator (`pmp/predicate.py`), never an LLM.

## Run it

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env      # MONGODB_URI, OPENAI_API_KEY (+ OPENAI_BASE_URL for OpenRouter), PMP_DB=pmp

scripts/view.sh                    # the precomputed walkthrough (recommended for judging)
scripts/demo.sh                    # the whole story live against Atlas, services included; Ctrl-C to stop
scripts/demo.sh --fixture-align    # same, skipping the LLM (deterministic, no key needed)
```

Stage by stage, if you want to watch each one:

```bash
scripts/seed.sh                                              # reset the use case, collections, vector index
python -m pmp.compile  --use-case bnpl_checkout_v1 --fixture
python -m pmp.align    --use-case bnpl_checkout_v1           # or --dry-run / --from-fixture
python -m pmp.merge    --use-case bnpl_checkout_v1
python -m pmp.validate --use-case bnpl_checkout_v1 --version 1
python -m pmp.decide   --use-case bnpl_checkout_v1           # interactive; or --answer-defaults / --answers mock/stages/4_answers.json
python -m pmp.contract --use-case bnpl_checkout_v1
python -m pmp.runtime.run --run 0001 && python -m pmp.runtime.run --run 0002     # in-process, against Atlas
python -m pmp.update --org A --policy large_order_approval --max-amount 15000    # §6: republish, contract v3
python -m pmp.runtime.run --run 0002 --in-memory                                 # no Atlas at all
```

Tests: `pytest -q` runs about 120 tests offline against an in-memory backend, including both runtime runs.
`pytest -m live` hits Atlas and the LLM.

## Repository map

| Path | Stage | Reads → writes |
|---|---|---|
| `pmp/compile.py` | 1 | inputs → `submissions`, `findings` (LINT-01/02) |
| `pmp/align.py` | 2 | `submissions` → `steps_vec`, `alignments` (the only LLM call) |
| `pmp/merge.py` | 3a | `submissions`, `alignments` → `merged` |
| `pmp/validate.py` | 3b, 5 | `merged` → `findings` (15-check registry) |
| `pmp/decide.py` | 4 | `findings` → `merge_questions`, `merge_log`, `merged` v2 |
| `pmp/contract.py` | 5 | `merged` v2 → `contracts` (with certificate), `projections` |
| `pmp/runtime/a2a.py` | 6 | handoff envelope and the four receiver checks |
| `pmp/runtime/agent.py` | 6 | executes a projection; FastAPI `/a2a`, `/run/{id}`, `/state` |
| `pmp/runtime/mediator.py` | 7 | change stream on `rejections` → patch → contract v+1 |
| `pmp/update.py` | 8 | derivation index (`elements`, `derivations`) → graph delta → rebuild only what moved → contract v+1 |
| `pmp/snapshot.py` | demo | runs everything once in memory and writes `ui/demo.json` |
| `pmp/runtime/api.py`, `ui/index.html` | UI | the six-scene walkthrough over `ui/demo.json` |
| `pmp/runtime/run.py` | 6, 7 | drives run 0001 and 0002 (in-process, HTTP, or `--in-memory`) |
| `pmp/predicate.py` | all | the one predicate evaluator |
| `pmp/spec.py`, `spec/*.schema.json` | all | every document validates against a JSON Schema |
| `pmp/db.py`, `pmp/memstore.py` | all | single Atlas entry point; in-memory stand-in for tests |
| `mock/STAGES.md` | spec | the expected output of every stage, which the tests assert |
| `mock/stages/*.json` | fixtures | expected outputs and runtime fixtures, also the fallbacks |
| `scripts/view.sh`, `scripts/demo.sh`, `scripts/seed.sh` | demo | one command each |

## What is real and what is mocked

Real: both input procedures, the compiler, the vector search, the LLM alignment, every check, the
interview logic, the projections and privacy check, the hashes, the handoff protocol, the four receiver
checks, the change-stream repair loop, and all of the numbers in the table above.

Mocked: the tools (Stripe, the warehouse, the lender's API return fixture data from `mock/stages/6_fixtures.json`),
the signatures (placeholders; the mediator "signs" first, each org "countersigns" after re-running its local checks),
and the human answers in the automated demo (defaults accepted; the interactive CLI asks for real).

## Known limits

- One use case. A second pair of procedures would exercise the compiler's generality, which has only been
  proven on a skill file and an Arazzo workflow.
- The aligner needs a capable model. `gpt-4.1` via OpenRouter reproduces the alignment table for about
  $0.25 per run; `gpt-4o-mini` did not. The fixture path exists so the demo never depends on it.
- Change management (§6) is implemented for policy and rule republishes with a worked threshold change;
  reshaping changes that would re-run the aligner are classified but not exercised.
- Placeholder signatures and no confidential computing: a compromised mediator is out of scope.

## What was built today

Everything in `pmp/`, `spec/`, `mock/stages/`, `scripts/`, `ui/` and `tests/`, in the order of the seven prompts
in `PROMPTS.md`: a compiler for two input formats, an LLM aligner with vector search, a deterministic merge,
a 15-check validator, the interview engine, projections with a privacy proof, certificates, two runtime
agents, the self-healing mediator, the derivation index and incremental update, the precomputed six-scene
walkthrough, and about 125 tests.
