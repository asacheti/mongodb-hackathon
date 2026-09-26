# Procedure Merge Profile

Design document (text export of the original page, https://claude.ai/artifact/Niy4sKmaRmw3KiULp8Z6iG). Sections 1 to 7; section 6 is the change-management design that pmp/update.py implements.

Procedure Merge Profile

 Cross-organization procedure composition

 Procedure Merge Profile

 Two companies each have a private, step-by-step way of doing something. For one shared job, a trusted middle service turns the two into a single joint procedure that both companies' agents can run, handing work back and forth, without either company reading the other's playbook. This page explains how, in plain terms first; the technical detail sits at the end of each section.

 1. The whole system in one picture

 Each company submits its full procedure to the middle service (the mediator, hosted on MongoDB). The mediator lines up the steps that correspond, joins the two procedures into one, checks that the result is sound, asks each company a handful of questions where it cannot decide alone, and issues a signed contract. Each company gets back only its own part of the contract, with the other company's part collapsed into a single opaque box that says what goes in, what comes out, and how it can end.

 At run time, each company's agent executes its own steps with its own tools. Where the procedure crosses the boundary, the sender's agent hands a small, agreed set of fields to the receiver's agent. If a handoff arrives that breaks the agreed conditions, the receiver refuses it and the contract is flagged for review.

 ORG A (private)
 MEDIATOR (merge service)
 sees both full graphs · neither org sees the other's
 ORG B (private)

 Procedural memory
 MongoDB · org_id = A
 SKILL.md → typed nodes + edges
 I/O schema · preconditions · policy · tools

 Procedural memory
 MongoDB · org_id = B
 SKILL.md → typed nodes + edges
 I/O schema · preconditions · policy · tools

 submit full graph, nodes tagged

 Submission A
 internal + handoff nodes · hash h_A

 submit full graph, nodes tagged

 Submission B
 internal + handoff nodes · hash h_B

 1 · Align
 LLM matches steps, structure-guided

 mapping + confidence

 2 · Merge
 deterministic graph union over mapping

 3 · Validate
 every node reaches END · I/O schemas fit
 policy lattice · tool bindings · no hidden cycles

 ambiguities only

 4 · Merge interview
 questions → answers → guardrails

 Owner A
 answers A-side questions

 Owner B
 answers B-side questions

 5 · Contract + certificate
 projection A · projection B · boundary interface
 checks passed, signed over h_A, h_B, h_merge

 Agent A
 receives projection A only; B is opaque
 MCP tools A (never shared)

 Agent B
 receives projection B only; A is opaque
 MCP tools B (never shared)

 A2A task per handoff step
 contract_id · typed outputs only

 runtime precondition failure → re-validate

 LATER · AN UPDATE (section 6) · only what was built from the change is redone

 A or B changes
 a rule · policy · tool · answer
 → graph-delta

 Reverse lookup
 derivation index:
 what was built from it?

 Premise + precedent check
 old answers still valid?
 old answers reusable?

 Follow-up question
 only if a premise broke,
 to the same owner

 Certificate n+1
 sign only if
 your slice changed

 rebuild + re-check only what was affected

 supersedes contract version n

 Privacy holds between the two organizations, not from the mediator. Each side submits its full graph; the mediator sees both, and each side gets back only its own projection with the other side collapsed to an opaque interface, plus a certificate listing the checks that passed. At run time each agent executes its projection with its own tools and hands typed outputs across the boundary as A2A tasks; a handoff that fails its condition reopens validation. The bottom band is the update loop from section 6: a later change becomes a graph-delta, the derivation index says what was built from it, old answers are checked for broken premises and reusable precedents, a follow-up question goes out only where a premise broke, and a new certificate version supersedes the old one, signed only by the org whose slice changed.

 Technical detail
 Pipeline: submit (full graph, nodes tagged internal/handoff, hashed) → align (LLM, structure-guided; proposals only) → merge (deterministic graph union over the confirmed mapping) → validate (check registry) → interview → contract + certificate → per-org projections. Handoffs are A2A tasks carrying a metadata.pmp block (contract id, version, certificate id, edge id, run id, sequence, attestations). Collections: procedures, transitions, submissions, alignments, merge_questions, merge_log, contracts. Full schemas: spec v0.1 §1–§4 and spec/*.schema.json.

 2. Asking people only when it matters

 Most of a merge is mechanical. What is not mechanical is judgment: whether two differently named steps are really the same, whether a customer's email may be shared, what counts as "paid" when one side thinks in invoices and the other in loans. For these the mediator asks a short, plain question to the company that owns the decision, always with a suggested default.

 Every answer turns into something the system can check afterwards: a field that may or may not cross, a converter step, a condition, a fallback path. Answers are recorded, signed into the contract, and reused the next time, so nobody is asked the same thing twice. In the worked example, five questions and fifteen minutes replaced what would normally be weeks of email.

 Technical detail
 Questions are generated only from validator findings (low-confidence alignment, I/O mismatch, unsatisfied precondition, differing policy field, sensitive field crossing, missing failure path, undecided near-duplicate). Each answer compiles to a guardrail of type alignment_decision | adapter_node | predicate | policy_value | field_allowlist | compensation_edge | dedupe_mode, stored in merge_questions with answering org, user, time and answer hash; the certificate references them. Cap the interview to findings, order by blocked nodes, always offer a default.

 3. Tools stay at home

 Each company's steps call that company's own software (its payment system, its warehouse, its credit-check API). The merge never gives one company access to the other's tools. If company B needs something only company A's tool can produce, the answer crosses the boundary as data, not the tool. The merge does check that every step declares its tools, that no step points at the other side's tools, and that any step which does something irreversible (moves money, ships goods) sits behind a human approval or an explicit policy.

 Technical detail
 Each node carries tools: [{server, tool, side_effect_class: pure|verify|side_effecting}]; SKILL.md allowed-tools compiles here. Checks: TOOL-01 binding exists in own catalog snapshot, TOOL-02 no cross-org tool reference, TOOL-03 side-effecting tools gated on all paths. Deliberate sharing is an explicit shared_tool grant via MCP Enterprise Managed Authorization. Runtime calls go through each org's own MCP client; the contract only constrains them.

 4. Steps that look alike, and steps that clash

 At a company boundary, near-duplicates are normal: both sides check the same things because neither trusted the other yet. The merge separates "same meaning" from "same obligation". Two address checks with the same meaning and no legal duty attached run once, and the other side accepts a note saying who checked it. Two sanctions screens that each company is required to run stay as two steps. And a step that changes something in the world is never collapsed without a signed answer.

 Clashes show up in predictable places: words that mean different things, data in different shapes, a condition one side needs that the other never produces, two opposite orderings, two different policies, an irreversible step before a fallible one, and unclear ownership. They are resolved by climbing a ladder from cheap to expensive: an automatic rule where one exists (stricter policy wins), a converter step, a reorder, a fallback path, a question to a person, and finally an explicit refusal that blocks the contract. Nothing is ever resolved by quietly deleting evidence.

 Near-duplicate decision

 step a
 org A

 step b
 org B

 Signals
 σ name + description embedding
 ι I/O schema compatibility
 τ same tool / side effect class
 ν neighborhood (same pred/succ)

 MERGE · run once, attest

 RELATE · both run, linked

 KEEP BOTH · internal duty

 CONFLICT → ladder

 σ high · ι compatible · τ pure or verify-only → MERGE
 σ high · τ side-effecting or org obligation → KEEP BOTH
 σ mid · ι partial → RELATE (or ask)
 ι incompatible or ν contradicts order → CONFLICT

 Conflict resolution ladder
 try each rung; stop at the first that resolves

 1 · Lattice auto-resolve
 policies with a natural order: min threshold, intersection of regions

 2 · Insert adapter node
 unit / format / field-name mismatch; owner = the side that answers

 3 · Reorder
 only if both orderings satisfy every precondition (graph check)

 4 · Add compensation or escalation edge
 irreversible step before a fallible one

 5 · Ask (merge interview)
 answer compiles to a guardrail and is signed

 6 · Reject, keep as first-class conflict
 never silently dropped; blocks the contract until resolved

 Runtime: a handoff whose precondition fails is logged as a conflict and re-enters at rung 1.

 Left: every candidate pair from the aligner is scored on four signals and routed to one of four outcomes; only the MERGE outcome removes a step from execution, and only when its side-effect class allows one side to trust the other's attestation. Right: conflicts climb the ladder from cheap automatic rules to human questions to explicit rejection.

 Technical detail
 Duplicate signals: σ name/description similarity, ι I/O schema compatibility, τ tool side-effect class, ν neighborhood. Outcomes: MERGE (only if τ ∈ {pure, verify}; attestation field added), RELATE, KEEP BOTH, CONFLICT. One canonical executor per node (default: the side whose graph reaches it first). Ladder rungs 1–6: lattice → adapter → reorder (only if all preconditions still hold) → compensation/escalation edge → ask → reject. Every rung writes a MELD-style patch (decision, signals fired, approver) to merge_log; a runtime handoff failing its precondition enters at rung 0.

 5. Trusting the result without reading the pipeline

 The mediator is trusted with both procedures; the two companies are not trusted with each other's. This is the same arrangement as a data clean room in advertising, where a neutral operator sees everyone's data and each party sees only agreed outputs. Seeing both procedures is what lets the mediator do a thorough job.

 Neither company should have to read the mediator's code to believe the result. So the mediator issues a certificate with the contract: a signed list of every check it ran and what each one found. Most checks can be re-run by a company on its own slice of the contract, and it does so before signing. Each company also verifies four simple things by itself: its slice contains only its own steps plus what its own answers added; every one of its steps is run by it; nothing of its was removed without its say-so; and every tool it needs is one it has. The few checks that only the mediator can perform are listed separately, so the trust surface is explicit rather than hidden.

 What this does not cover: a mediator that is itself compromised and leaks one company's procedure to the other. That needs confidential computing or multi-party computation and is out of scope for the prototype.

 Technical detail
 Certificate signed over h_A, h_B, h_alignments, h_answers, h_merge, h_projection_A/B, h_boundary_interface and tool catalog snapshots; mediator signs first, orgs countersign after re-running all local checks. Registry: STR-01..04, IO-01..02, PRE-01..03, POL-01..02, TOOL-01..03, DUP-01, FAIL-01, CONF-01 (local) and ALN-01, DUP-02, PRIV-01 (mediator-only). Local invariants INV-1..4. The merge is deterministic given the signed alignment and answer records; the aligner itself is not, which is why its output is a record. Preconditions and guards use one decidable predicate language (comparisons, membership, existence, flags, AND/OR/NOT, JSON AST). Spec §2–§3, §6–§7.

 6. Changing one thing without redoing everything

 Procedures change. A company raises an approval threshold, shortens a payment term, adds a rule, swaps a tool. The merged pipeline should absorb that like a spreadsheet absorbs an edited cell: only the formulas that reference the cell recalculate.

 Four kinds of thing can change. A rule is a condition on one step ("don't ship until paid"). A context policy is a company-wide setting on a step ("orders over $10,000 need a manager"); policies are the one kind the merge combines, by taking the stricter side. Tool access is which software a step may call and whether the call merely checks or actually does something. Guardrails are the formula cells: the checks, allowed-field lists, converters and fallbacks the merge derived from the first three and from people's answers. Nobody edits a guardrail directly; they change its inputs.

 Every guardrail, check result and question records what it was built from. A change is therefore a reverse lookup: find everything built from the changed element, rebuild just that, re-run just the checks that read it, re-ask only the questions whose inputs moved (offering the old answer as the default), and require a new signature only from a company whose visible slice changed. The direction of the change matters too: tightening can make an old guarantee insufficient and so re-runs checks; loosening is the trust-sensitive direction and re-asks the questions that assumed the stricter world; reshaping (renaming a step, changing a data shape) is the only kind that touches the expensive step-matching work.

 | Internal rule | Handoff-step rule | Context policy | Answer (guardrail input) | Tool access | 

 Tighten | own slice only; owner signs | precondition checks re-run; maybe one field question; both sign | guard rebuilt if the merged value moves; both sign, no questions | derived guardrail rebuilt; maybe one question | own checks only | 

 Loosen | same | same, plus re-ask questions built on this rule | guard rebuilt; re-ask questions built on this policy (usually none) | same | a handoff step going from "checks" to "does something" re-asks what may be sent to it | 

 Reshape | own slice only | handoff contract rebuilt; step matching may re-run; both sign | n/a | n/a | catalog and tool checks only | 

 Add | a constraint: same as tighten. A step, edge or outcome: structural checks re-run; internal → own slice, but the mediator confirms no guarantee the partner relies on changed; a new handoff step → a small new merge on that edge (match, schema, questions), the rest carried | same as tighten | a new answer → same as an answer revision | own checks; a shared-tool grant → both sign | 

 Remove | a constraint: same as loosen. A step, edge or output field: structural checks re-run; internal → own slice plus a check that nothing the partner consumed disappeared; a handoff step → its questions are closed, dependent guardrails re-justified; a consumed output field → a two-party conflict, not a follow-up | same as loosen | n/a | own checks; a removed gate on a side-effecting tool fails the owner's own validation first | 

 Adding or removing a constraint is just tightening or loosening under another name. Adding or removing structure always re-runs the structural checks and then depends on where the element sits: inside one company, only that company's slice moves; on the boundary, it is either a small new merge grafted onto the old one or the closing of the questions that were about the removed piece.

 TYPED IN · elements
 DERIVED · guardrails
 CHECKS · outputs

 A policy · max_amount$10,000 → $15,000
 A rule · fulfil_order precondition
 A tool · wms.create_shipment
 B rule · updateOrderStatus precond.
 B tool · findEligibleProducts (verify)
 A answer · q3 "what counts as paid"

 h3 boundary guardrebuilt (lattice)
 A approval-gate edge conditionrebuilt
 h1 allowlist (q1)
 mark_paid adapter (q3)
 h3 field allowlist (q4)
 h2 runtime guard (q3)

 PRE-03 on h3re-run
 CONF-01 on h3re-run
 Projection Bchanged → Lakeside re-signs, no questions
 Projection Achanged → Northwind re-signs
 IO-01 on h1 · carried
 PRE-03 on h2 · carried

 changed element
 rebuilt / re-run / re-signed
 carried forward untouched
 "built from" (not affected)
 "built from" (affected)

 One change traced through the derivation index: Northwind raises its approval threshold. Only the elements column is typed in by people; everything to the right records what it was built from. Following the "built from" arrows out of the changed policy touches two guardrails, two checks and both projections. The other four guardrails, the two checks that read them, and all five interview answers are carried forward, so nobody is asked anything.

 A company can see, before submitting a change, how much friction it will cause its partner ("Lakeside will need to re-sign but not answer anything"). That prediction is a real reason to use the system instead of email.

 Technical detail
 Republish = graph-delta (added/removed/modified node and edge ids with hashes). Collections: elements {element_id, kind: rule|policy|tool_binding|schema|node|edge, org_id, host, hash} and derivations {derived_id, kind, inputs:[{element_id, hash}], rebuild_fn: lattice|adapter|allowlist|guard|align|project|check|human:<org>, output_hash}, indexed on inputs.element_id. Alignment inputs are limited to name, description, I/O schemas and neighborhood hash, so rule and policy edits never re-run the aligner. Change classes 0–3 (cosmetic, internal, boundary, trust-relevant) in spec §9.1; certificate n+1 lists supersedes, delta hash, checks re-run vs carried, questions re-asked, signatures required. In-flight runs finish on their version. Spec §9.

 7. Where this lands first

 The value is the same everywhere: two companies that guard their procedures currently spend weeks reconciling handoffs by email, and the merge turns that into hours with an audit trail. What differs is the cost of a wrong merge. Early adopters are where a mistake is noticed quickly and reversed cheaply, handoffs are frequent, and the boundary already has some structure to grip.

 Managed-service-provider to software-vendor incident escalation is the strongest first user: runbooks are commercial assets, a wrong handoff is a ticket in the wrong queue fixed in minutes, and the tools are already agent-shaped. Shipper to freight forwarder is the strongest first user with money attached: routing rules and carrier contracts are secret, errors are bounded (re-file, re-book), and EDI, HS codes and Incoterms give the aligner structured vocabulary. Trial sponsor to clinical research organisation is the largest prize and the worst fit for autonomous execution: SOP reconciliation takes months, but patient-facing steps are irreversible and everything must be validated, so use the merge there as a design-time assistant that produces the reconciled procedure, the question list and the conflict log for humans to sign.

 Rule of thumb: the cost of a wrong merge must be smaller than the cost of one week of the reconciliation it replaces. IT service handoffs and logistics clear that bar today; finance and pharma clear it only for the design-time product.