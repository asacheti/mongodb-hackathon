---
# Standard skill fields (Northwind's own agent uses these)
name: invoice-and-fulfil
description: >
  Take a checkout basket to a paid, fulfilled order using Stripe Invoicing.
  Create the customer and invoice, finalize, collect payment (card or a
  partner's financing), wait for invoice.paid, then ship. Large orders need
  a manager's approval before shipping.
allowed-tools: stripe:customers.create stripe:invoices.create
  stripe:invoiceitems.create stripe:invoices.finalize stripe:invoices.send
  stripe:invoices.pay stripe:webhooks.listen wms:create_shipment
  northwind:approvals.request

# Identity and versioning
org_id: northwind
use_case_id: bnpl_checkout_v1
procedure_version: 2.4.0
supersedes: 2.3.1
effective_from: 2026-09-01
owner: { team: order-operations, contact: ops.lead@northwind.example }
approvers: [ops.lead, finance.controller, privacy.lead]

# Merge metadata
partner_roles: [lender]
standards: [ISO-4217, ISO-3166, stripe-invoicing-api-2026]
confidentiality: internal
languages: [en]
jurisdictions: [US]
---

# 2. Purpose and scope
trigger: customer completes a checkout basket and chooses "pay later" or "invoice"
outcomes: [fulfilled, paid_by_card_fallback, cancelled_unpaid, awaiting_approval]
out_of_scope: [returns, refunds after shipment, subscriptions]
success_measure: >
  order shipped within 1 business day of payment confirmation;
  no shipment before invoice.paid or an authorized financing plan

# 3. Roles
roles:
  ops_agent:          { kind: agent, may: [create customer, create and finalize invoice, request financing, start shipment] }
  ops_manager:        { kind: human, may: [approve orders >= USD 10000] }
  finance_controller: { kind: human, may: [define what counts as paid, approve out-of-band payment marking] }
  privacy_lead:       { kind: human, may: [approve data sharing rules] }
  lender:             { kind: external_partner }

# 4. Glossary and code lists
terms:
  invoice: "Stripe Invoice object; monetary values are immutable after finalize."
  unit_amount: "Stripe line-item price in minor units (cents) for the invoice currency."
  price_id: "Stripe Price ID for the product; stable across orders."
  paid_out_of_band: "Marking a Stripe invoice paid when money arrived outside Stripe (POST /v1/invoices/{id}/pay with paid_out_of_band=true). Fires invoice.paid."
code_lists:
  invoice_status:
    draft:         "Created, editable."
    open:          "Finalized, awaiting payment."
    paid:          "Payment received or marked paid out of band."
    uncollectible: "Given up on."
    void:          "Cancelled."
  fulfilment_state: { pending: "Not shipped.", shipped: "Shipment created in WMS.", held: "Awaiting manager approval." }

# 5. Data model
entities:
  invoice:
    fields:
      invoice_id:       { type: string, pattern: "in_[A-Za-z0-9]+", source: stripe:invoices.create }
      currency:         { type: enum, values: [USD] }
      total_minor:      { type: integer, unit: cents, currency: USD }
      status:           { type: enum, values: [draft, open, paid, uncollectible, void] }
      days_until_due:   { type: integer, default: 30 }
  line_item:
    fields:
      price_id:         { type: string, pattern: "price_[A-Za-z0-9]+" }
      quantity:         { type: integer, min: 1 }
      unit_amount_minor:{ type: integer, unit: cents, currency: USD }
  customer:
    fields:
      customer_id:      { type: string, pattern: "cus_[A-Za-z0-9]+", sensitivity: internal, may_cross: never }
      name:             { type: string, sensitivity: pii, may_cross: ask }
      email:            { type: string, sensitivity: pii, may_cross: ask }
      payment_method:   { type: string, sensitivity: financial, may_cross: never }
      shipping_address: { type: address, sensitivity: pii, may_cross: ask }
    examples: [{ name: "Jenny Rosen", email: "jenny.rosen@example.com" }]
  approval:
    fields:
      granted:          { type: boolean }
      approver:         { type: string, sensitivity: internal, may_cross: never }

# 6. Tools
tools:
  stripe:customers.create:   { effect: side_effect, reversible: partial, idempotent: true }
  stripe:invoices.create:    { effect: side_effect, reversible: true }
  stripe:invoiceitems.create:{ effect: side_effect, reversible: true, limits: { max_items: 250 } }
  stripe:invoices.finalize:  { effect: side_effect, reversible: false, note: "amounts immutable afterwards" }
  stripe:invoices.send:      { effect: side_effect, reversible: false }
  stripe:invoices.pay:       { effect: side_effect, reversible: false, money: true, idempotent: true }
  stripe:webhooks.listen:    { effect: verify, returns: [invoice.paid, invoice.payment_failed] }
  northwind:approvals.request:{ effect: side_effect, reversible: true, human: true }
  wms:create_shipment:       { effect: side_effect, reversible: partial, requires_gate: "ops_manager if total_minor >= 1000000" }

# 7. Steps

## A1. Create a customer
Create the Stripe Customer with name and email. This is a billing record, not an identity check.
- id: A1
  name: create_customer
  type: action
  visibility: internal
  role: ops_agent
  inputs: [customer.name, customer.email]
  outputs: [customer.customer_id]
  tools: [stripe:customers.create]
  postconditions: { exists: customer.customer_id }
  branches: [{ otherwise: A2 }]
  on_failure: { retry: 2, then: escalate_to: ops_manager }
  obligation: { required: true, reason: "Stripe requires a Customer to invoice" }
  dedupe: never
  confidential_notes: "Customer IDs are reused across orders; never expose the ID."

## A2. Create an invoice
Create the invoice with collection_method=send_invoice and days_until_due=30.
- id: A2
  name: create_invoice
  type: action
  visibility: internal
  role: ops_agent
  inputs: [customer.customer_id]
  outputs: [invoice.invoice_id, invoice.status]
  tools: [stripe:invoices.create]
  postconditions: { eq: [invoice.status, draft] }
  branches: [{ otherwise: A3 }]

## A3. Add invoice items
One invoice item per basket line. Stripe caps an invoice at 250 items.
- id: A3
  name: add_items
  type: action
  visibility: internal
  role: ops_agent
  inputs: [invoice.invoice_id, line_item.*]
  outputs: [invoice.total_minor]
  tools: [stripe:invoiceitems.create]
  preconditions: { lte: [line_item.count, 250] }
  branches: [{ otherwise: A4 }]

## A4. Finalize the invoice
After this the amounts cannot change. This is the point where a financing partner can be asked to cover the total.
- id: A4
  name: finalize_invoice
  type: action
  visibility: handoff_out
  role: ops_agent
  inputs: [invoice.invoice_id]
  outputs: [invoice.status, invoice.total_minor, line_item.*, customer.name, customer.email]
  tools: [stripe:invoices.finalize]
  postconditions: { eq: [invoice.status, open] }
  branches:
    - when: { eq: [checkout.payment_choice, pay_later] }  then: H1_request_financing
    - otherwise: A5
  obligation: { required: true, reason: "amounts must be locked before any payment" }
  dedupe: never

## A5. Collect payment
Card payment through the Payment Element, or send the invoice for later payment. Also the fallback when a financing partner declines.
- id: A5
  name: collect_payment
  type: action
  visibility: internal
  role: ops_agent
  inputs: [invoice.invoice_id, customer.payment_method]
  outputs: [invoice.status]
  tools: [stripe:invoices.send, stripe:invoices.pay]
  branches: [{ otherwise: A6 }]
  on_failure: { retry: 1, then: escalate_to: ops_manager }

## A6. Wait for invoice.paid
Never ship before invoice.paid. On invoice.payment_failed, offer another attempt.
- id: A6
  name: await_invoice_paid
  type: wait
  visibility: internal
  role: ops_agent
  inputs: [invoice.invoice_id]
  outputs: [invoice.status]
  tools: [stripe:webhooks.listen]
  timeout: 30d
  branches:
    - when: { eq: [invoice.status, paid] }
      then: A7
    - when: { eq: [webhook.event, invoice.payment_failed] }
      then: A5
    - otherwise: T_cancelled_unpaid

## A7. Approval for large orders
Orders of USD 10,000 or more need a manager's approval before shipping. Company policy, not negotiable.
- id: A7
  name: approve_large_order
  type: human_gate
  visibility: internal
  role: ops_manager
  inputs: [invoice.total_minor]
  outputs: [approval.granted]
  tools: [northwind:approvals.request]
  preconditions: { gte: [invoice.total_minor, 1000000] }
  branches:
    - when: { eq: [approval.granted, true] } then: A8
    - otherwise: T_cancelled_unpaid
  obligation: { required: true, reason: "finance policy FP-7" }
  dedupe: never

## A8. Fulfil the order
Create the shipment. If the order was financed, tell the partner the order is fulfilled so the loan activates.
- id: A8
  name: fulfil_order
  type: action
  visibility: handoff_out
  role: ops_agent
  inputs: [invoice.invoice_id, customer.shipping_address, approval.granted]
  outputs: [fulfilment_state, shipment_id]
  tools: [wms:create_shipment]
  preconditions:
    and:
      - { eq: [invoice.status, paid] }
      - { or: [{ lt: [invoice.total_minor, 1000000] }, { eq: [approval.granted, true] }] }
  branches:
    - when: { eq: [checkout.payment_choice, pay_later] } then: H3_report_fulfilled
    - otherwise: T_fulfilled

## Terminals
- id: T_fulfilled
  type: terminal
  outcome: fulfilled
- id: T_cancelled_unpaid
  type: terminal
  outcome: cancelled_unpaid

# 8. Handoffs
handoffs:
  - id: H1_request_financing
    step: A4
    direction: out
    to_role: lender
    purpose: "Ask the lender to finance the finalized basket for this customer."
    payload:
      basket:
        - { product_ref: { from: line_item.price_id }, quantity: { from: line_item.quantity },
            amount: { from: line_item.unit_amount_minor, convert: to_major_units, currency: USD } }
      total:            { from: invoice.total_minor, convert: to_major_units, currency: USD }
      customer_name:    { from: customer.name }
      customer_email:   { from: customer.email }
    never_send: [customer.customer_id, customer.payment_method, invoice.invoice_id, approval.*]
    expects_back:
      plan_status: { values: [authorized, declined, pending_customer_action] }
      plan_total:  { type: money, currency: USD }
      loan_reference: { type: string, optional: true }
    sla_expected: { final: 24h, interim: none, negotiable: true }
    retries: { max: 2, backoff: exponential }

  - id: H2_receive_plan
    step: A6
    direction: in
    from_role: lender
    purpose: "Receive the lender's financing decision."
    accept:
      plan_status: { required: true }
      plan_total:  { required: true, must_equal: invoice.total }
      loan_reference: { optional: true }
    on:
      authorized: "mark the invoice paid out of band (finance_controller rule), continue to A6 as paid"
      declined:   "fall back to A5 card payment"

  - id: H3_report_fulfilled
    step: A8
    direction: out
    to_role: lender
    purpose: "Tell the lender the order shipped so the financing activates."
    payload:
      loan_reference: { from: handoff.H2.loan_reference }
      fulfilled_at:   { from: shipment.created_at }
    never_send: [shipment_id, customer.shipping_address, approval.*]
    expects_back: { ack: { values: [activated, rejected] } }
    sla_expected: { final: 1h, negotiable: true }

# 9. Policies
policies:
  large_order_approval: { rule: { gte: [invoice.total_minor, 1000000] }, requires: ops_manager, negotiable: false }
  ship_only_when_paid:  { rule: { eq: [invoice.status, paid] }, negotiable: false }
  financing_counts_as_paid:
    rule: { and: [{ eq: [plan_status, authorized] }, { eq: [plan_total, invoice.total] }] }
    owner: finance_controller
    negotiable: true
  data_residency: { region: US, negotiable: false }
  retention: { invoice_records: 7y }

# 10. Exceptions, failure and compensation
exceptions:
  - case: lender_declined
    action: fall back to card payment (A5); keep the finalized invoice
  - case: lender_no_response_after_sla
    action: send invoice for card payment; note financing as expired
  - case: payment_failed
    action: offer another attempt; after 2 failures send invoice by email
  - case: shipment_created_then_loan_rejected
    action: cancel shipment if not dispatched; else collect by card

# 11. Timing and service levels
timing:
  invoice_due: 30 days from finalize
  financing_decision_expected: 24h
  ship_after_paid: 1 business day
  approval_turnaround: 4h

# 12. Evidence and audit
evidence:
  produces: [stripe_event_log, approval_record, shipment_record]
  accepts_from_partner:
    financing_authorized: { requires_all: [plan_status, plan_total], plus: loan_reference }
  audit_log: every decision with actor, time, inputs hash

# 13. Merge preferences
merge_preferences:
  auto_resolve: [unit_conversion, field_rename, extend_from_own_tools]
  always_ask: [anything touching money, pii, what counts as paid]
  default_owner_for_adapters: self
  dedupe_allowed_for: [effect: verify, obligation: none]
  question_routing:
    pii: privacy_lead
    money: finance_controller
    operations: ops.lead
  auto_countersign: { allowed_for: [adapter_change_from_own_catalog], max_per_week: 5 }

# 14. Test cases
tests:
  - name: financed_happy_path
    input: { invoice.total_minor: 278000, checkout.payment_choice: pay_later }
    partner_returns: { plan_status: authorized, plan_total: 2780.00, loan_reference: "ln_1" }
    expect: { invoice.status: paid, fulfilment_state: shipped, outcome: fulfilled }
  - name: lender_declined_fallback
    input: { invoice.total_minor: 278000, checkout.payment_choice: pay_later }
    partner_returns: { plan_status: declined }
    expect: { next_step: A5, outcome: paid_by_card_fallback }
  - name: large_order_needs_approval
    input: { invoice.total_minor: 1250000, checkout.payment_choice: pay_later }
    partner_returns: { plan_status: authorized, plan_total: 12500.00, loan_reference: "ln_2" }
    expect: { gate: A7, fulfilment_state: held_until_approved }

# 15. Change log
changelog:
  - version: 2.4.0
    date: 2026-09-01
    changes: ["financing_counts_as_paid policy added; H1..H3 handoffs declared"]
    affects: [policies.financing_counts_as_paid, A4, A6, A8]
