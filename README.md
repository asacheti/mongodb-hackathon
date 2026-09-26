# InterLock

**Two companies. Two private playbooks. One signed joint procedure, without either side reading the other's.**

Team **InterLock**: Aditi Kumari, Austin Zhao, Arnav Sacheti, Shun-Hsun Liang.
Built in one day at the MongoDB Harness Engineering & Model Wrangling Hackathon (Cerebral Valley, NYC, 26 Sep 2026).

> Merge without disclosing. Check without trusting. Change one rule, re-stitch one seam.

**See it in two minutes:** `scripts/view.sh` and open http://127.0.0.1:8000. Six scenes, everything precomputed, model outputs included. No Atlas, no API key, no services needed.

---

## The problem

When two organizations have to work together on one job, each already has a step-by-step procedure for its half. Reconciling the two is done by email today: weeks of "what do you send us?", "what counts as paid?", "who converts the units?". Both procedures are commercial assets, so neither side wants to hand the other its playbook. And once agents execute these procedures, a wrong handoff is a wrong API call.

Then the procedures change. A threshold moves, a field becomes required, a tool starts doing more than it used to. Today that reopens the whole email thread.

## What InterLock does

A trusted mediator on MongoDB Atlas takes both procedures in full, lines up the steps that correspond, joins the two into one graph, checks that the result is sound, asks each company a handful of questions only where it cannot decide alone, and issues a **signed contract**. Each company gets back **only its own slice**, with the partner collapsed into a single opaque box that says what goes in, what comes out, and how it can end.

At run time each company's agent executes its slice with its own tools and hands typed payloads across the boundary. A handoff that breaks the agreed conditions is refused, the contract is flagged, and the mediator repairs it.

When a company later changes a rule, every guardrail in the contract records what it was built from, so only the parts built from that rule are redone.

Privacy holds **between the two organizations**, not from the mediator. That is the same trust model as a data clean room.

```
compile ─▶ align ─▶ merge ─▶ validate ─▶ decide ─▶ contract ─▶ runtime ─▶ repair ─▶ update
```

## The worked example (real inputs)

| Org | Role | Procedure | Source |
|---|---|---|---|
| Northwind Outdoor (`A`) | merchant | `mock/inputs/northwind/SKILL.md`: a Claude-style skill with typed YAML steps | Stripe, *Integrate with the Invoicing API* |
| Lakeside BNPL (`B`) | lender | `mock/inputs/lakeside/bnpl-arazzo.yaml` + `merge-profile.yaml` sidecar | OpenAPI Initiative, Arazzo 1.0.0 example, used verbatim |

The company names are invented; the procedures are not. Two formats on purpose: a merchant writing agent skills and a lender publishing an API workflow spec would never share one. The compiler normalises both into the same typed graph, and along the way finds two real mistakes in the published Arazzo example (a step reading its own output; a loan id no step declares).

What each side guards: Northwind does not want Lakeside to know it uses Stripe or that managers approve big orders. Lakeside does not want Northwind to see how it judges eligibility. What they want together is a checkout where a Lakeside loan pays for a Northwind order.

## The demo: InterLock, six stages

`scripts/view.sh` serves `ui/index.html`, the InterLock console. Everything it shows comes from `ui/demo.js`: every stage's results and the live model outputs, computed once by `python -m pmp.snapshot`. The page keeps its own layout and interactions (the merge plays out link by link, the interview is answered by clicking, each org signs the contract, the runs play step by step); a small data overlay at the bottom of the file fills in the real inputs, alignments, findings, questions, certificate, handoffs, rejection, repair and the threshold update. Arrow keys move between stages. `ui/walkthrough.html` is a second, denser view of the same data.

| Stage | What you see | Numbers |
|---|---|---|
| 1 **Input** | Both procedures as written, the compiled graphs side by side, and the compile findings (two mistakes in the published Arazzo file, one prose policy turned into a human gate). | A: 10 nodes / 11 edges. B: 10 nodes / 12 edges. 3 LINT findings. |
| 2 **Merge** | The alignment table with the model's rationale per pair, the merged graph with its three boundary edges drawn between the lanes, and the validator's findings placed on the resolution ladder. | 5 alignments. 20 nodes, 26 edges. Exactly 7 findings, 12 other checks pass. |
| 3 **Interview** | The one rule decision, then five questions as a conversation: who was asked, which role, the default, the answer, and the guardrail each answer became. | 5 questions (4 to Northwind, 1 to Lakeside), 3 defaults accepted. |
| 4 **Contract** | The joint procedure with the certificate's real counts and hashes; each org reviews its four invariants and signs. **Northwind sees** collapses Lakeside into one sealed box listing only what crosses and under which guard. | 35 checks: 33 pass, 2 guarded. Projection A 13 nodes, B 11. Contract v1 active. |
| 5 **Run** | Run 0001 plays step by step over the graph, with every handoff's real payload. Run 0002: Lakeside changes its mind, h1 is refused, the mediator repairs the contract from Northwind's own data model, the retry passes. | Run 0001: h1, h2, h3 accepted. Run 0002: 1 rejection, contract v2, completed. |
| 6 **Update** | Northwind raises its approval threshold: the computed change, what it touched, which checks re-ran, what was carried, who signs. Three further changes are shown as projections of the same rules. | 6 elements changed, 3 guardrails rebuilt, 9 checks re-run, 16 carried, 0 questions, contract v3. |

## What happens, stage by stage

| # | Stage | What happens |
|---|---|---|
| 1 | **Compile** | Both inputs become graphs of typed nodes and transitions: I/O schemas with units and sensitivity, preconditions as a JSON predicate AST, tool effect classes, policies. Money thresholds written in prose become policy objects. |
| 2 | **Align** | Every step is embedded into `steps_vec`; **Atlas Vector Search** proposes candidates; an LLM proposes how each pair relates, scored on four signals (σ name similarity, ι schema fit, τ tool effect class, ν neighborhood). Deterministic guards keep it honest: nothing side-effecting merges, input only crosses from a handoff-out step into a handoff-in step. **This is the only stage that calls an LLM.** |
| 3 | **Merge + validate** | Deterministic union over the alignments; three boundary edges (basket → lender, plan → merchant, fulfilment → lender). A registry of checks runs; reachability uses **`$graphLookup`** over per-node adjacency docs. Exactly 7 findings: unit mismatch (cents vs major), PII crossing with no allowlist, a "what counts as paid" deadlock, a missing loan id, a policy lattice, a missing decline path, a near-duplicate step. |
| 4 | **Decide** | Low stakes resolve by rule (stricter policy wins). Each high-stakes finding becomes one question, routed by the owner org's own routing table, always with a default. Question text is built from findings and boundary fields only; confidential notes never reach it. Answers compile to guardrails: 3 allowlists, 2 adapter nodes, 1 predicate, 1 compensation edge, 1 keep-both decision. |
| 5 | **Contract** | Revalidate. Build projection A and projection B: own nodes plus one opaque partner node carrying only the boundary interface. Each org checks four local invariants; the mediator checks that nothing of the partner leaked. Certificate with real sha256 over every input. |
| 6 | **Runtime** | Each org's agent executes its projection (tools stubbed from fixtures) and sends A2A-shaped handoffs. The receiver runs four checks in order: contract active, edge in projection, payload matches schema + allowlist, preconditions hold. |
| 7 | **Self-repair** | Lakeside republishes requiring a postal code. The first h1 is rejected (`MISSING_FIELDS`), the contract goes *suspect*. The mediator reads the rejection from an **Atlas change stream**, finds `customer.shipping_address` in Northwind's own data model, extends Northwind's adapter, revalidates, issues contract v2 and supersedes v1. The retry passes. |
| 8 | **Update** | Northwind raises its approval threshold from $10,000 to $15,000. A reverse lookup over the derivation index rebuilds only the h3 guard and the gate condition, re-runs only the checks that read them, carries the rest, re-asks nobody, and requires both signatures because both visible slices changed. Contract v3 supersedes v2. |

Live, seed to contract v3 takes about two minutes on the venue network (`scripts/demo.sh`).

### The five questions

One question per unresolved finding, to whichever company owns the decision, each with a default. Three of five defaults were accepted as-is.

| | To | Question | Answer | Becomes |
|---|---|---|---|---|
| q1 | Northwind ops lead | "create customer" and "createCustomer" share a name, not a meaning. Keep both? May name and email go to Lakeside? | keep both; send name and email, never the Stripe ID or payment method | keep-both decision; allowlist on h1 |
| q2 | Northwind (default) | Stripe prices are cents with a price ID; Lakeside wants dollars and a product code. Who converts? | Northwind converts; product code = price ID | adapter node `adapt_basket_for_lender` |
| q3 | Northwind finance controller | Does an authorized Lakeside plan count as payment, so shipping can start? | yes, if the plan is authorized and its total matches the invoice | adapter node `mark_invoice_paid_out_of_band` with that predicate; the deadlock disappears |
| q4 | Lakeside (default) | Your last step needs the loan ID, which Northwind never sees. Include it in the plan you return? | yes | allowlists on h2 and h3 |
| q5 | Northwind (default) | If Lakeside declines, what happens to the invoice? | fall back to card payment | compensation edge `lender.declined → collect_payment` |

### What each side actually receives

**Northwind** gets its own steps, the two converter steps it agreed to, and one opaque box labelled "Lakeside" that says what to send, what comes back, and that it can end in *completed* or *lender.declined*. It never sees the loan process or the lender's tools.

**Lakeside** gets its own steps and one opaque box labelled "Northwind" that sends a basket, receives a plan, and later sends "fulfilled" under a condition. It never learns that Northwind uses Stripe or that a manager approves large orders; it only sees the guard predicate `invoice.total_minor < 1000000 OR approval.granted`.

Each company checks four things about its slice by itself before signing (INV-1..4): only its own steps plus what its own answers added; every one of its steps is run by it; nothing of its was removed without its say-so; every tool it needs is one it has. The mediator checks the fifth (PRIV-01): no internal node name, node text, confidential note or tool of the partner appears anywhere in the projection.

## Changing one thing without starting over

Procedures change. The merged contract should absorb that the way a spreadsheet absorbs an edited cell: only the formulas that reference the cell recalculate.

Every guardrail, guard, check result, alignment and projection records what it was built from: the **derivation index**, two Atlas collections (`elements`: rules, policies, tool bindings, schemas, nodes, edges, each with a hash; `derivations`: what was built from which elements, and how). A change is a reverse lookup:

1. Find everything built from the changed elements: one **`$graphLookup`** over `derivations`, transitively.
2. Rebuild just that (lattice, adapter, guard, allowlist).
3. Re-run just the checks that read it; carry the rest from the previous certificate.
4. Re-ask only the questions whose inputs moved, with the old answer as the default.
5. Require a new signature only from a company whose visible slice changed.

**Direction matters, and it is judged from the joint procedure's point of view, not the company's.**

| Direction | Meaning | What it costs |
|---|---|---|
| Tightening | fewer situations get through: a new condition, a stricter threshold, an extra required field | the checks comparing "what the sender guarantees" to "what the receiver requires" re-run; if a field is now missing, someone is asked to supply it. Nobody's earlier answer becomes wrong, only insufficient |
| Loosening | more situations get through, or a step now does more than it used to | the trust-sensitive direction: questions whose answers assumed the stricter world are re-asked |
| Reshaping | a step is renamed or split, a field changes type | the only direction that can disturb step matching, because matching was built from names, descriptions and shapes |

The worked change in the demo, and what the same machinery predicts for others:

| Change | Kind | Rebuilt | Who is asked | Who signs | Status |
|---|---|---|---|---|---|
| Northwind raises the approval threshold to $15,000 | policy, loosening, class 2 | the h3 guard Lakeside enforces and the approval-gate condition; 9 checks re-run, 16 carried | nobody: all five answers carried as precedent | both | **implemented, in the demo** |
| Lakeside requires a postal code on h1 | schema, tightening, at runtime | the basket adapter, the h1 allowlist; revalidate | nobody: the field exists in Northwind's own data model | Northwind auto-countersigns (its own preference allows adapter changes from its own catalog); Lakeside signs | **implemented, in the demo** (run 0002) |
| Northwind shortens invoice terms 30 → 14 days | policy, tightening, internal | Northwind's slice only | nobody | Northwind only | classified, not exercised |
| Lakeside's eligibility check starts leaving a credit inquiry | tool access, loosening | the h1 allowlist | q1 re-asked with the old answer as default | both | specified in `docs/` |
| Finance controller tightens q3: plan counts as paid only after the first instalment | an earlier answer, tightening | the mark-paid adapter, its predicate, the h2 field list | one question to Lakeside | both | specified in `docs/` |

Adding or removing a constraint is tightening or loosening under another name. Adding or removing structure (a step, an edge, an outcome, a tool) always re-runs the structural checks; inside one company it moves only that company's slice, on the boundary it is a small new merge grafted onto the old one.

A company can see, before submitting a change, how much friction it will cause its partner. For the demo change the system says: *"Lakeside will need to re-sign but not answer anything."* That prediction is a reason to use the system instead of email.

## Where this lands first

The value is the same everywhere: two companies that guard their procedures spend weeks reconciling handoffs by email, and the merge turns that into hours with an audit trail. What differs is the cost of a wrong merge.

- **MSP ↔ software vendor incident escalation**: strongest first user. Runbooks are commercial assets, a wrong handoff is a ticket in the wrong queue, fixed in minutes, and the tools are already agent-shaped.
- **Shipper ↔ freight forwarder**: strongest first user with money attached. Errors are bounded (re-file, re-book) and EDI, HS codes and Incoterms give the aligner structured vocabulary.
- **Merchant ↔ lender** (this demo): money at the boundary makes the policy conflicts real, and two public procedures existed to build from.
- **Trial sponsor ↔ CRO**: the largest prize and the worst fit for autonomous execution. Use it as a design-time assistant that produces the reconciled procedure, the questions and the conflict log for humans to sign.

Rule of thumb: the cost of a wrong merge must be smaller than the cost of one week of the reconciliation it replaces.

## MongoDB Atlas features used

- **Vector Search** (`steps_vec_idx`: 1536-dim cosine, filtered by `org_id` and `use_case_id`) for alignment candidates.
- **`$graphLookup`** twice: reachability over the merged graph (one adjacency document per node), and the reverse lookup over the derivation index when something changes.
- **Change streams** on `rejections`: the mediator is event-driven; a runtime rejection triggers the repair loop.
- Thirteen collections mirroring the pipeline (`submissions`, `steps_vec`, `alignments`, `merged`, `findings`, `merge_questions`, `merge_log`, `contracts`, `projections`, `handoffs`, `rejections`, `elements`, `derivations`). Every document carries `use_case_id` and, where it belongs to one org, `org_id`, so one query answers "what does org A know?".

## Design rules that held all day

- **The LLM proposes; everything after it is deterministic.** Same inputs and same decision records give the same merged graph, byte for byte. Hashes in the certificate make that checkable.
- **Privacy between orgs, not from the mediator.** Each org gets its own projection; the partner is one sealed box.
- **Tools stay at home.** No step ever points at the other side's tools; if B needs something only A's tool produces, the data crosses, not the access. Every irreversible step (money, shipping) sits behind a human gate or an explicit policy.
- **Same meaning is not same obligation.** Two verify-only steps with no legal duty can run once with an attestation; anything side-effecting is never collapsed without a signed answer.
- **People are asked only when it matters.** Every question comes from a validator finding, goes to the org that owns the decision, and carries a default. Five questions replaced what is normally weeks of email.
- **Nothing is resolved by quietly deleting evidence.** Conflicts climb a ladder (lattice → adapter → reorder → compensation edge → ask → reject) and a refused answer leaves a first-class conflict that blocks the contract.
- **Predicates are data.** One JSON AST, one evaluator (`pmp/predicate.py`), never an LLM.

## The certificate

The inspection report that ships with the contract: every check that was run and its verdict (`pass` or `guarded`, meaning tested live on every handoff), hashes of both submissions, the alignments, the answers, the merged graph and both projections, every question with who answered it, and three signatures. Local checks (each org re-runs them on its own slice) and mediator-only checks are listed separately so the trust surface is explicit. When a change is applied, certificate n+1 lists what it supersedes, the delta hash, which checks were re-run and which carried, which questions were re-asked, and whose signatures were required.

What this does not cover: a mediator that is itself compromised and leaks one company's procedure to the other. That needs confidential computing or multi-party computation and is out of scope.

## Run it

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt

scripts/view.sh                    # the precomputed walkthrough at http://127.0.0.1:8000 (nothing else needed)
```

Live, against Atlas (needs `.env` with `MONGODB_URI`, `OPENAI_API_KEY` and `OPENAI_BASE_URL` for OpenRouter, `PMP_DB=pmp`):

```bash
scripts/demo.sh                    # seed → … → contract → agents + mediator + API → run 0001 → run 0002 → update
scripts/demo.sh --fixture-align    # same, skipping the LLM (deterministic, no key needed)
python -m pmp.snapshot             # recompute ui/demo.json from the live alignments in Atlas
```

Stage by stage:

```bash
scripts/seed.sh                                              # reset the use case, collections, vector index
python -m pmp.compile  --use-case bnpl_checkout_v1 --fixture
python -m pmp.align    --use-case bnpl_checkout_v1           # or --dry-run / --from-fixture
python -m pmp.merge    --use-case bnpl_checkout_v1
python -m pmp.validate --use-case bnpl_checkout_v1 --version 1
python -m pmp.decide   --use-case bnpl_checkout_v1           # interactive; or --answer-defaults / --answers mock/stages/4_answers.json
python -m pmp.contract --use-case bnpl_checkout_v1
python -m pmp.runtime.run --run 0001 && python -m pmp.runtime.run --run 0002     # in-process, against Atlas
python -m pmp.update --org A --policy large_order_approval --max-amount 15000    # the §6 update → contract v3
python -m pmp.runtime.run --run 0002 --in-memory                                 # no Atlas at all
```

Tests: `pytest -q` runs about 125 tests offline against an in-memory backend, including both runtime runs and the update. `pytest -m live` hits Atlas and the LLM.

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
| `pmp/update.py` | 8 | `elements`, `derivations` → graph delta → `$graphLookup` reverse lookup → rebuild → contract v+1 |
| `pmp/snapshot.py` | demo | runs everything once in memory and writes `ui/demo.json` and `ui/demo.js` |
| `pmp/runtime/api.py`, `ui/index.html` | UI | the InterLock console over the snapshot (`ui/walkthrough.html`: the denser second view) |
| `pmp/runtime/run.py` | 6, 7 | drives run 0001 and 0002 (in-process, HTTP, or `--in-memory`) |
| `pmp/predicate.py` | all | the one predicate evaluator |
| `pmp/spec.py`, `spec/*.schema.json` | all | every document validates against a JSON Schema |
| `pmp/db.py`, `pmp/memstore.py` | all | single Atlas entry point; in-memory stand-in for tests |
| `mock/STAGES.md` | spec | the expected output of every stage, which the tests assert |
| `mock/stages/*.json` | fixtures | expected outputs and runtime fixtures, also the fallbacks |
| `docs/` | design | the Procedure Merge Profile and the BNPL Checkout Merge walkthrough (stages 0 to 9) |
| `scripts/view.sh`, `scripts/demo.sh`, `scripts/seed.sh` | demo | one command each |

## What is real and what is mocked

Real: both input procedures, the compiler, the vector search, the LLM alignment, every check, the interview logic, the projections and privacy check, the hashes, the handoff protocol, the four receiver checks, the change-stream repair loop, the derivation index and the incremental update, and all of the numbers in the tables above.

Mocked: the tools (Stripe, the warehouse, the lender's API return fixture data from `mock/stages/6_fixtures.json`), the signatures (placeholders; the mediator "signs" first, each org "countersigns" after re-running its local checks), and the human answers in the automated demo (two answered from `mock/stages/4_answers.json`, three defaults; the interactive CLI asks for real).

## Known limits

- One use case. A second pair of procedures would exercise the compiler's generality, which has only been proven on a skill file and an Arazzo workflow.
- Two parties. Three would be two seams plus a check that the guarantees compose.
- The aligner needs a capable model. `gpt-4.1` via OpenRouter reproduces the alignment table for about $0.25 per run; `gpt-4o-mini` did not. The fixture path exists so the demo never depends on it.
- Change management is implemented for policy and rule republishes (the threshold change) and for schema changes caught at runtime (the postal code). Re-asking questions on a loosening change is classified and specified in `docs/`, not yet exercised.
- Placeholder signatures and no confidential computing: a compromised mediator is out of scope.

## What was built today

Everything in `pmp/`, `spec/`, `mock/stages/`, `scripts/`, `ui/`, `docs/` and `tests/`, in the order of the seven prompts in `PROMPTS.md` plus the update stage: a compiler for two input formats, an LLM aligner with vector search, a deterministic merge, a 15-check validator, the interview engine, projections with a privacy proof, certificates, two runtime agents, the self-healing mediator, the derivation index and incremental update, the precomputed six-scene walkthrough, and about 125 tests. The commit history is the record.

## Team

- **Aditi Kumari**: MEng CS, Cornell Tech; eight years in semiconductor and storage validation (Samsung, Western Digital, Toshiba). Merge and validation core.
- **Austin Zhao**: ML researcher and CS teaching assistant at Cornell; previously MathWorks. Runtime agents and A2A handoffs.
- **Arnav Sacheti**: co-founder and lead data scientist, HistOracle; previously JPMorgan Chase. Alignment pipeline and Atlas Vector Search.
- **Shun-Hsun (Hannibal) Liang**: AI engineer, Cognito Health; built a FastAPI clinical AI pipeline with RAG and persistent memory. Mediator, change streams and the repair loop.
