# BNPL Checkout Merge

Worked example (text export of the original page, https://claude.ai/artifact/MkzUbBKgRrEfTk4hMTEV41): stages 0 to 9 of the bnpl_checkout_v1 merge. mock/STAGES.md restates it as module contracts.

BNPL Checkout Merge

 Procedure Merge Profile · end-to-end example

 BNPL Checkout Merge

 An online shop and a "buy now, pay later" lender each have their own way of working. Here they are joined into one procedure so a shopper can pay for an order with a loan, and then the joined procedure is run, refused once, and updated. Stage 0 uses two real public sources; everything after it is a mock of what the system would produce.

 0 The two companies1 Compile2 Match3 Join and check4 Questions5 Joint procedure6 Certificate7 What each side gets8 Running it9 Changing it

 Fixtures for every stage are in mock/stages/*.json in the hackathon repo; they validate against the spec/ schemas, and python mock/run_mock.py --seed loads them into MongoDB. The company names are invented; the procedures are not.

 STAGE 0 The two companies

 Northwind Outdoor the shop

 Procedure taken from Stripe's Invoicing integration guide, written as a SKILL.md

 How it bills a customer: create the customer record, create an invoice, add the items, lock the invoice, collect payment, wait until Stripe confirms the money arrived, then ship. One house rule on top: orders of $10,000 or more need a manager's sign-off before shipping.

 Lakeside BNPL the lender

 Procedure is the OpenAPI Initiative's own Arazzo 1.0.0 BNPL example, used verbatim

 How it grants a loan at checkout: look at the basket and decide which items qualify, show the terms, check the customer is eligible, open the loan, send the customer to authorize it, retrieve the finished payment plan, and switch the loan on once the shop confirms delivery.

 Each treats its procedure as private. Northwind doesn't want Lakeside to know it uses Stripe or that managers approve big orders; Lakeside doesn't want Northwind to see how it judges eligibility. What they want together is a checkout where a Lakeside loan pays for a Northwind order, without the weeks of email that normally takes.

 Source excerpts
# Northwind SKILL.md (from Stripe docs)
1. Create a Customer (POST /v1/customers) with name, email.
2. Create an Invoice (POST /v1/invoices) collection_method=send_invoice, days_until_due=30.
3. Add invoice items (POST /v1/invoiceitems); max 250.
4. Finalize (POST /v1/invoices/{id}/finalize); monetary values immutable afterwards.
5. Collect payment (Payment Element / send invoice).
6. Listen for invoice.paid before fulfilling; on invoice.payment_failed offer another attempt.
7. On invoice.paid start shipping. Orders >= USD 10,000 need a manager's approval (company policy).

# Lakeside Arazzo workflow ApplyForLoanAtCheckout (steps, verbatim ids)
checkLoanCanBeProvided -> getCustomerTermsAndConditions -> createCustomer (201 eligible / 200 not)
-> initiateBnplTransaction (202) -> authenticateCustomerAndAuthorizeLoan (302)
-> retrieveFinalizedPaymentPlan (200) -> updateOrderStatus (204, "order fulfilled" activates the loan)

 STAGE 1 Turn both into graphs mock from here on

 Each procedure becomes a graph of steps and transitions, submitted in full to the mediator. Steps are marked as either private or "handoff" (the ones the other company may eventually see). Reading the lender's example carefully, the compiler finds two small mistakes in the published file itself: one step refers to its own output instead of an earlier step's, and another uses a loan ID that no step actually declares. Catching those before any merging happens is the first payoff of working with graphs rather than prose.

 ORG_A · 10 nodes · 11 edges · h_A = sha256:1e4818f4…

 create_customer
 create_invoice
 add_items
 finalize_invoice
 collect_payment
 await_invoice_paid
 approve ≥ $10k
 fulfil_order
 END

 total < $10k
 ≥ $10k
 approval.granted
 invoice.payment_failed → retry

 ORG_B · 10 nodes · 12 edges · h_B = sha256:5f745390…

 checkLoanCanBeProvided
 getCustomerTermsAndConditions
 createCustomer
 initiateBnplTransaction
 authenticateCustomerAndAuthorizeLoan
 retrieveFinalizedPaymentPlan
 updateOrderStatus
 END

 eligibilityCheckRequired == false
 redirectAuthToken == null
 END_not_eligible (handoff)

 Compiled graphs before merging. Filled boxes are handoff-visible nodes. Lakeside's three "not eligible" exits collapse into one terminal that the merchant will be allowed to see, because a decline is something the merchant has to react to.

 Technical detail
 Northwind: 10 nodes, 11 edges, handoff-visible finalize_invoice, fulfil_order. Lakeside: 10 nodes, 12 edges, handoff-visible checkLoanCanBeProvided, retrieveFinalizedPaymentPlan, updateOrderStatus, END_not_eligible. Findings: LINT-01 ×2 on Lakeside (steps[4].parameters.redirectAuthToken self-reference; steps[5].parameters.loanTransactionId undeclared, compiled as derived field), LINT-02 on Northwind (prose policy compiled to policy{max_amount, requires_human_approval} + HUMAN_GATE). Hashes h_A = sha256:1e4818f4…, h_B = sha256:5f745390….

 STAGE 2 Match the steps

 An LLM, shown both graphs with their data shapes and tool types, proposes which steps correspond. It notices that both sides have a step called "create customer" that mean completely different things (a billing record versus an eligibility check), that the shop's finished invoice is the basket the lender needs to look at, that "invoice paid" and "payment plan finalized" are cousins but not twins, that the shop's shipping step is what the lender is waiting for, and that if the lender says no the shop will need a fallback. None of this is decided yet; every proposal has to be confirmed or defaulted in Stage 4.

 Technical detail

 A | B | σ | ι | Proposal | 

 create_customer | createCustomer | 0.93 | incompatible | distinct 0.88 (τ both side-effecting forbids MERGE) | 

 finalize_invoice | checkLoanCanBeProvided | 0.34 | partial | provides_input_for 0.90 | 

 await_invoice_paid | retrieveFinalizedPaymentPlan | 0.41 | partial | relate 0.62 | 

 fulfil_order | updateOrderStatus | 0.71 | missing loanTransactionId | provides_input_for 0.86 | 

 collect_payment | END_not_eligible | 0.12 | n/a | on_failure 0.58 | 

 STAGE 3 Join them and check

 The two graphs are joined over the proposed matches and the result is checked. Seven problems come up. Money is in cents on one side and dollars on the other. The customer's name and email would cross the boundary with nobody having said that's allowed. The two procedures disagree on order: the shop ships only after payment, but the lender counts the loan as paid only after the shop reports shipping, a loop with no way in. The lender's last step needs a loan ID the shop never receives. The lender might decline and the shop has no plan for that. And the two "create customer" steps still need a ruling. One thing resolves itself: the shop's $10,000 approval rule has no counterpart on the lender's side, so the stricter rule simply applies at the boundary.

 Technical detail
 Findings: IO-01 fail h1 (unit/field mismatch → rung 2), IO-02 fail h1 (no allowlist → ask), STR-03 fail merged (cycle paid↔fulfilled → ask), PRE-01 fail h3 (loanTransactionId unresolvable → ask), FAIL-01 fail h1 (no failure path → rung 4/ask), DUP-02 fail (pair undecided → ask), POL-01 pass h3 (requires_human_approval OR → true; max_amount min → USD 10,000; recorded as boundary guard).

 STAGE 4 Five questions, fifteen minutes

 One question per unresolved problem, sent to whichever company owns the decision, each with a suggested answer. Three of the five defaults were accepted as-is.

 q1 · to Northwind
Your "create customer" and Lakeside's "createCustomer" share a name, not a meaning. Keep both? And may the customer's name and email go to Lakeside? Answer: keep both; send name and email, never the Stripe ID or payment method.

 q2 · to Northwind · default accepted
Stripe prices are in cents with a price ID; Lakeside wants dollars and a product code. Who converts? Answer: Northwind converts; product code = Stripe price ID. A converter step is added on Northwind's side.

 q3 · to Northwind's finance controller
Does an authorized Lakeside payment plan count as payment, so shipping can start? If yes, Northwind would mark the Stripe invoice paid "out of band" (a real Stripe feature), which fires the same "invoice paid" event its procedure already waits for. Answer: yes, provided the plan is authorized and its total matches the invoice. The loop from Stage 3 disappears.

 q4 · to Lakeside · default accepted
Your last step needs the loan ID, which Northwind never sees. Include it in the payment plan you return? Answer: yes.

 q5 · to Northwind · default accepted
If Lakeside declines, what happens to the invoice? Answer: fall back to card payment.

 Technical detail
 Guardrails produced: q1 → alignment_decision keep_both + field_allowlist on h1; q2 → adapter_node A.adapt_basket_for_lender; q3 → adapter_node A.mark_invoice_paid_out_of_band with precondition status == authorized AND totalLoanAmount == invoice.total; q4 → allowlist on h2 adds finalizedPaymentPlan.loanTransactionId; q5 → compensation_edge from the opaque lender node to A.collect_payment on lender.declined. Answers stored with org, user, time and hash; signed into the contract.

 STAGE 5 The joint procedure

 Read left to right: the shop builds and locks the invoice, converts the basket into the lender's shape and sends it with the customer's name and email. The lender runs its whole loan process privately and returns either "declined" (the shop takes a card instead) or the authorized plan. The shop checks the plan matches the invoice, marks the invoice paid, gets manager approval if the order is $10,000 or more, ships, and tells the lender "delivered" with the loan ID. The lender switches the loan on.

 ORG_A Northwind · executor of every node in this lane
 ORG_B Lakeside · executor of every node in this lane

 create_customer
 create_invoice
 add_items
 finalize_invoice
 adapt_basketADAPTER · q2
 mark_paid_OOBADAPTER · q3
 await_invoice_paid
 fulfil_order
 END

 collect_payment
 approve ≥ $10kHUMAN_GATE

 < $10k
 ≥ $10k
 approval.granted

 card paid → invoice.paid

 checkLoanCanBeProvided

 private chain
 getTermsAndConditions
 createCustomer
 initiateBnplTransaction
 authenticate + authorize
 retrieveFinalizedPaymentPlan
 updateOrderStatus
 END
 END_not_eligible

 h1 · basket + customerallowlist: name, email · q1

 h2 · finalizedPaymentPlanstatus, loanTransactionId, total · q4

 h3 · fulfilled + loanTransactionIdguard: total < $10k OR approval.granted · POL-01

 lender.declined → card fallback · q5

 22 nodes, 28 edges, three boundary edges (h1, h2, h3) and one failure edge. The two violet adapters are the only nodes that did not exist in either submission; each carries an origin pointing at the question that created it. Lakeside's private chain is shown here because the mediator sees it; Northwind's projection will show it as one opaque box.

 Technical detail
 22 nodes, 28 edges. Two adapter nodes, each with an origin pointing at the question that created it. Three boundary edges: h1 A.adapt_basket_for_lender → B.checkLoanCanBeProvided (allowlist: invoice_id, products, customer.name, customer.email); h2 B.retrieveFinalizedPaymentPlan → A.mark_invoice_paid_out_of_band (status, loanTransactionId, totalLoanAmount); h3 A.fulfil_order → B.updateOrderStatus (loanTransactionId, order_status, invoice_total; guard invoice_total < USD 10000 OR approval.granted). One failure edge B.END_not_eligible → A.collect_payment.

 STAGE 6 The certificate

 The mediator re-runs every check on the joined procedure and issues a signed certificate: 24 checks, all passing or explicitly "guarded" (a condition that will be tested at run time because it cannot be guaranteed in advance). Twenty of the checks can be re-run by either company on its own slice; four can only be done by the mediator and are listed as such. Both companies re-run their twenty, confirm the same results, and sign.

 Technical detail
 Local: STR-01..04, IO-01 ×3, IO-02, PRE-01..02, PRE-03 guarded on h2 (lender postcondition is only statusCode == 200) and h3 (approval flag set by a human at run time), POL-01..02, TOOL-01..03 (gated tool wms.create_shipment), DUP-01, FAIL-01, CONF-01 (4 dry-run payloads per edge). Mediator-only: ALN-01, DUP-02, PRIV-01 ×2. Certificate cert_bnpl_v1_0001, contract ctr_northwind_lakeside_bnpl v1, signed over h_A, h_B, h_alignments, h_answers, h_merge = sha256:d8837b71…, h_projection_A/B, h_boundary_interface. Hashes in the repo are real SHA-256 digests of the fixtures; signatures are placeholders.

 STAGE 7 What each side actually gets

 Northwind receives its own steps, the two converter steps it agreed to, and one opaque box labelled "Lakeside" that says what to send it, what comes back, and that it can end in "completed" or "declined". It never sees the loan process or the lender's tools. Lakeside receives its own steps and one opaque box labelled "Northwind" that sends a basket, receives a plan, and later sends "delivered" under a condition. It never learns that Northwind uses Stripe or that a manager approves large orders; it only sees the condition. Each company checks four simple things about its slice by itself, then signs. The contract goes live.

 Technical detail
 Projection A: 13 nodes (10 own + 2 adapters + 1 opaque), INV-1..4 pass. Projection B: 11 nodes (10 own + 1 opaque), INV-1..4 pass. Boundary interface = three edge schemas + allowlists + guards. Contract status active at 15:03:44 once both signatures are present.

 STAGE 8 Running it, twice

 Run 1: a $2,780 order goes through

 Shop → Lenderh1 · basket + name + email
Lender checks: contract active, edge known, fields allowed, conditions met. Replies: two eligible items, $2,780.

 Lender, privately: terms, eligibility (eligible), open loan, customer authorizes, retrieve plan. The shop sees none of this.

 Lender → Shoph2 · plan: authorized, loan ID, $2,780
Shop checks the plan is authorized and the total matches, marks the invoice paid, and because $2,780 is under $10,000 ships without approval.

 Shop → Lenderh3 · delivered + loan ID + $2,780
Lender checks the loan ID exists, status is "delivered", and the amount is under the threshold. Switches the loan on.

 Run 2: a $12,400 order where the shop's automation skips the manager

 Shop → Lenderh3 · delivered + loan ID + $12,400 · no approval attached
Lender's check fails: $12,400 is over the threshold and there is no approval. It refuses the handoff. The contract is flagged for that edge, the shop's agent escalates to a person, the mediator re-examines and finds the condition was right and the shop's automation was wrong. After a manager approves and the message is re-sent with the approval attached, the contract is restored.

 The point of run 2: a rule that lived in the shop's own prose became a condition the lender enforces, without the lender knowing why the rule exists or how approval works.

 Technical detail
metadata.pmp: {contract_id, contract_version: 1, certificate_id, edge_id: "edge:A.fulfil_order->B.updateOrderStatus",
 sender_node, receiver_node, run_id: "run_0002", seq: 3, guard_results: [{check:"PRE-03", result:false}]}
parts[0].data: {"loanTransactionId":"lt_9b12e0","order_status":"fulfilled","invoice_total":{"currency":"USD","amount":12400.0}}
reply: {"state":"rejected","reason":"precondition_failed",
 "failing_term":{"or":[{"op":"Receiver order of checks: contract active and signed → edge in own projection → payload matches boundary schema and allowlist → preconditions. On precondition_failed: merge_log patch at rung 0, contract suspect for the edge, sender follows its ON_FAILURE edge (escalate → A2A input-required), mediator re-validates. Idempotency key (contract_id, run_id, edge_id, seq).

 STAGE 9 Changing it without starting over

 Every condition, allowed-field list, converter and question in the contract records what it was built from. So when a company changes something, the system looks up what depends on it and rebuilds only that. How much work a change causes depends on what kind of thing changed and on its direction, and the direction is the part that is easy to misread.

 Tightening means fewer situations get through than before: a new condition, a stricter threshold, an extra required field. The risk is that something the partner used to guarantee is no longer enough, so the checks that compare "what the sender guarantees" against "what the receiver requires" are re-run, and if a field is now missing, someone is asked to supply it. Nobody's earlier answer becomes wrong; it may just become insufficient.

 Loosening means more situations get through, or a step now does more than it used to: a higher threshold, a removed condition, a step that only used to check something and now also records or changes something. Nothing breaks structurally, which is exactly why it is the risky direction. Earlier answers were given assuming the stricter world. So the questions whose answers were built on the loosened element are re-asked, with the old answer shown as the default.

 Reshaping means names or data shapes change: a step is renamed or split, a field changes type. This is the only direction that can disturb the step matching from Stage 2, because matching was built from names, descriptions and data shapes and from nothing else.

 Note that direction is judged from the merged procedure's point of view, not the company's. Raising Northwind's approval threshold from $10,000 to $15,000 feels like a loosening to Northwind (fewer approvals) and it is one for the joint procedure: more orders ship without a manager, which Lakeside's guard now also allows. Lakeside's eligibility check starting to leave a credit inquiry is loosening too, even though no threshold moved: the step now does more, and Northwind's answer about what to send it assumed it only checked. Five changes, from cheapest to most involved:

 Change | Kind | What is rebuilt | Who is asked | Who signs | 

 Northwind shortens invoice terms from 30 to 14 days | context policy · tightening, but internal: nothing at the boundary was built from it | Northwind's slice only | nobody | Northwind only; live on signing | 

 Northwind raises the approval threshold to $15,000 | context policy · loosening: more orders ship without a manager | the h3 condition Lakeside enforces | nobody: no earlier answer was built on the threshold | both (Lakeside's slice changed) | 

 Lakeside adds "ship within 30 days of authorization" | rule on a handoff step · tightening: a new condition the shop must satisfy | h2 and h3 field lists and their checks; the step match is not redone | one field question each way, both with "yes" defaults, because the rule needs two fields that don't cross yet | both | 

 Lakeside's eligibility check starts leaving a credit inquiry | tool access · loosening: the step now does more than check | the h1 allowed-field list | q1 re-asked to Northwind with the old answer as default, because q1 assumed the step only checked | both | 

 Northwind's finance controller tightens q3: only count the plan as paid once the first installment is collected | an earlier answer · tightening: fewer plans count as paid | the "mark paid" converter, its condition, the h2 field list | one question to Lakeside, because the stricter condition needs a field Lakeside doesn't send yet | both | 

 Adding and removing things

 Changes are not only edits. Companies add and delete steps, fields, outcomes and tools. Adding or removing a constraint (a rule, a policy, a required field) is just tightening or loosening and follows the rules above. Adding or removing structure (a step, an edge, an output field, a way the procedure can end, a tool) always re-runs the structural checks, and then depends on where the element sits: inside one company it changes only that company's slice, though the mediator still confirms that no guarantee the partner relies on has disappeared; on the boundary it is a small new merge grafted onto the old one, or the closing of questions that were about the removed piece.

 Change | Operation, where | What happens | 

 Lakeside inserts a fraud screen before opening the loan, routing failures to its existing "not eligible" exit | add internal step | Lakeside's slice only; reachability re-checked; no new outcome, so Northwind's decline fallback (q5) still covers it. Lakeside signs. | 

 Same, but failures get a new outcome "fraud hold, retry in 24 h" | add outcome on the boundary | Breaks q5's premise that Lakeside ends only in completed or declined; q5 re-asked to Northwind with a proposed default. Both sign. | 

 Lakeside deletes the terms-and-conditions step | remove internal step | Own slice; nothing was built from it. Lakeside signs. | 

 Northwind deletes the manager-approval gate but keeps the $10,000 policy | remove internal step (loosening by deletion) | Northwind's own validation fails first: shipping is a side-effecting tool no longer gated on the ≥ $10,000 path. Nothing reaches Lakeside until Northwind fixes its graph. | 

 Lakeside stops returning the plan total | remove output field on a handoff step | Northwind's "mark paid" condition reads it: boundary check fails and q3's premise breaks. Because one side's removal contradicts the other side's answer, both are asked, as a conflict rather than a follow-up. | 

 Northwind adds a "partial shipment" step that notifies the lender of split deliveries | add handoff step | New boundary edge h4: matched against Lakeside's graph, given a schema and allowlist, two or three fresh questions. Everything on h1–h3 is carried. This is the upper bound: about the cost of one edge of the original merge. | 

 The cost of a change is the size of what was built from it, not the size of the joint procedure. And the people who get asked are exactly the people whose earlier answers depended on what changed.

 Technical detail
 Republish as graph-delta; reverse lookup over derivations.inputs.element_id; rebuild in dependency order; rebuild_fn = human:<org> becomes a re-asked question with the previous answer as default; only rebuilt check_results re-run; signatures required only where a projection hash changed. Alignment inputs exclude rules and policies, so row 3 reuses fulfil_order ~ updateOrderStatus. Change classes and certificate chain: spec §9; element taxonomy and worked table: spec §9.5.