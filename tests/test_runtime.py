"""Stage 6: in-process runtime, no network, no Atlas (in-memory backend). Run 0001 completes with h1, h2, h3
accepted; run 0002 yields exactly one MISSING_FIELDS rejection and a contract version 2, then completes."""
import pytest

from pmp import db, spec
from pmp.runtime import a2a, mediator
from pmp.runtime.agent import Agent, apply_transform, load_fixtures, pred_ctx
from pmp.runtime.run import InProcessRuntime, bootstrap, republish

USE_CASE = "bnpl_checkout_v1"


@pytest.fixture(scope="module")
def world():
    store = db.use_memory()
    contract = bootstrap(USE_CASE)
    rt = InProcessRuntime(USE_CASE)
    r1 = rt.run("0001")
    r2 = rt.run("0002")
    yield dict(store=store, contract=contract, rt=rt, r1=r1, r2=r2)
    db.use_atlas()


def test_run_0001_completes_with_three_handoffs(world):
    r = world["r1"]
    assert r["A"]["status"] == "completed" and r["A"]["outcome"] == "fulfilled"
    assert r["B"]["status"] == "completed" and r["B"]["outcome"] == "loan_activated"
    sent = [(h["edge"], h["status"]) for h in r["A"]["handoffs"] if h["direction"] == "sent"]
    assert sent == [("h1", "accepted"), ("h3", "accepted")]
    recv = [(h["edge"], h["status"]) for h in r["A"]["handoffs"] if h["direction"] == "received"]
    assert recv == [("h2", "accepted")]
    assert r["rejections"] == [] and r["contract_version"] == 1 and not r["patched"]
    seqs = sorted((h["run"], h["seq"], h["edge"]) for h in db.col("handoffs").find({"use_case_id": USE_CASE, "run": "0001", "role": "sender"}))
    assert seqs == [("0001", 1, "h1"), ("0001", 2, "h2"), ("0001", 3, "h3")]


def test_run_0001_state_and_order(world):
    a = world["rt"].agents["A"].runs["0001"]
    b = world["rt"].agents["B"].runs["0001"]
    assert a["done"][:5] == ["A.create_customer", "A.create_invoice", "A.add_items", "A.finalize_invoice", "A.adapt_basket_for_lender"]
    assert a["done"][5:] == ["A.mark_invoice_paid_out_of_band", "A.await_invoice_paid", "A.approve_large_order", "A.fulfil_order", "A.T_fulfilled"]
    assert b["done"] == ["B.checkLoanCanBeProvided", "B.getCustomerTermsAndConditions", "B.decide_eligibilityCheckRequired", "B.createCustomer",
                         "B.initiateBnplTransaction", "B.decide_redirectAuthToken", "B.authenticateCustomerAndAuthorizeLoan",
                         "B.retrieveFinalizedPaymentPlan", "B.updateOrderStatus"]
    assert a["state"]["invoice"]["status"] == "paid" and a["state"]["loanTransactionId"] == "ln_4Qz9"
    assert a["state"]["products"] == [{"productCode": "price_1S9kNzLq7", "purchaseAmount": {"currency": "USD", "amount": 1980.0}},
                                      {"productCode": "price_1S9kO4Lq7", "purchaseAmount": {"currency": "USD", "amount": 800.0}}]
    assert a["state"]["total"] == {"currency": "USD", "amount": 2780.0}
    gate = next(e for e in a["log"] if e["event"] == "gate")
    assert "not required" in gate["detail"]


def test_h1_payload_is_only_the_allowlist(world):
    h1 = db.col("handoffs").find_one({"use_case_id": USE_CASE, "run": "0001", "edge": "h1", "role": "sender"})
    p = h1["handoff"]["payload"]
    assert set(p) == {"customer", "invoice", "products", "total"}
    assert p["customer"] == {"name": "Jenny Rosen", "email": "jenny.rosen@example.com"}      # never customer_id / payment method
    assert "shipping_address" not in p["customer"] and "invoice_id" not in p["invoice"]
    assert spec.errors(h1["handoff"], "handoff") == []
    meta = h1["handoff"]["metadata"]["pmp"]
    assert meta["contract_id"] == "ctr_northwind_lakeside_bnpl" and meta["contract_version"] == 1 and meta["certificate_id"] == "cert_bnpl_checkout_v1_0001"
    assert (meta["edge"], meta["sender"], meta["receiver"], meta["run"], meta["seq"]) == ("h1", "A", "B", "0001", 1)
    h3 = db.col("handoffs").find_one({"use_case_id": USE_CASE, "run": "0001", "edge": "h3", "role": "sender"})
    assert h3["handoff"]["payload"] == {"fulfilled_at": "2026-09-26T09:14:40Z", "fulfilment_state": "shipped", "loanTransactionId": "ln_4Qz9"}
    assert list(h3["handoff"]["metadata"]["pmp"]["guard_results"].values()) == [True]


def test_run_0002_one_rejection_then_contract_v2(world):
    r = world["r2"]
    assert len(r["rejections"]) == 1
    rej = r["rejections"][0]
    assert rej["code"] == "MISSING_FIELDS" and rej["edge"] == "h1" and rej["missing"] == ["customer.postalCode"]
    assert r["patched"] and r["contract_version"] == 2
    assert r["A"]["status"] == "completed" and r["B"]["status"] == "completed"
    versions = {c["version"]: c["status"] for c in db.col("contracts").find({"use_case_id": USE_CASE})}
    assert versions == {1: "superseded", 2: "active"}
    v2 = db.col("contracts").find_one({"_id": f"{USE_CASE}:ctr:v2"})
    assert v2["supersedes"] == 1 and v2["reason"].startswith("MISSING_FIELDS on h1")
    assert "customer.postalCode" in next(e for e in v2["boundary_edges"] if e["id"] == "h1")["allowlist"]
    assert v2["certificate"]["inputs"]["h_B"] != world["contract"]["h_B"]        # Lakeside's republish is in the certificate
    stored = db.col("rejections").find_one({"use_case_id": USE_CASE, "run": "0002"})
    assert spec.errors(stored, "rejection") == [] and stored["receiver"] == "B" and stored["contract_version"] == 1


def test_run_0002_patch_came_from_northwinds_own_catalog(world):
    log = db.col("merge_log").find_one({"use_case_id": USE_CASE, "type": "patch", "status": "applied"})
    op = log["plan"]["ops"][0]
    assert op["op"] == "extend_adapter" and op["adapter"] == "A.adapt_basket_for_lender"
    assert op["input"]["path"] == "customer.shipping_address" and op["found_in"] == "entities.customer (address)"
    assert op["transform"] == {"to": "customer.postalCode", "from": ["customer.shipping_address"], "expr": "shipping_address.postal_code"}
    assert log["countersign"]["A"].startswith("auto")
    m3 = db.col("merged").find_one({"_id": f"{USE_CASE}:v3", "doc_type": "graph"})
    adapter = next(n for n in m3["nodes"] if n["_id"] == "A.adapt_basket_for_lender")
    assert any(f["path"] == "customer.postalCode" for f in adapter["outputs"])
    retry = db.col("handoffs").find_one({"use_case_id": USE_CASE, "run": "0002", "edge": "h1", "role": "sender", "status": "accepted"})
    assert retry["handoff"]["payload"]["customer"]["postalCode"] == "97201" and retry["handoff"]["metadata"]["pmp"]["contract_version"] == 2
    pa = db.col("projections").find_one({"_id": f"{USE_CASE}:A:v2"})
    assert pa["counts"]["nodes"] == 13


def test_receiver_checks_in_order(world):
    rt = world["rt"]
    b = rt.agents["B"]
    h = db.col("handoffs").find_one({"use_case_id": USE_CASE, "run": "0001", "edge": "h1", "role": "sender"})["handoff"]
    node = b.nodes["B.checkLoanCanBeProvided"]
    contract = b.fresh_contract()
    ok = a2a.receiver_check(h, "B", USE_CASE, contract, b.projection, node, b.required_inputs(node), {})
    assert ok is None or ok["code"] == "CONTRACT_NOT_ACTIVE"        # v1 handoff replayed against v2 = stale contract
    stale = a2a.receiver_check(h, "B", USE_CASE, contract, b.projection, node, [], {})
    assert stale and stale["code"] == "CONTRACT_NOT_ACTIVE"
    fresh = a2a.build_handoff(contract, "h1", "A", "B", "0009", 1, h["payload"])
    assert a2a.receiver_check(fresh, "B", USE_CASE, dict(contract, status="suspect"), b.projection, node, [], {})["code"] == "CONTRACT_NOT_ACTIVE"
    bad_edge = a2a.build_handoff(contract, "h3", "A", "B", "0009", 1, h["payload"])
    assert a2a.receiver_check(bad_edge, "B", USE_CASE, contract, b.projection, node, [], {})["code"] == "EDGE_NOT_IN_PROJECTION"
    leak = a2a.build_handoff(contract, "h1", "A", "B", "0009", 1, {**h["payload"], "customer": {**h["payload"]["customer"], "customer_id": "cus_1"}})
    r = a2a.receiver_check(leak, "B", USE_CASE, contract, b.projection, node, [], {})
    assert r["code"] == "ALLOWLIST_VIOLATION" and r["field"] == "customer.customer_id"
    wrong_type = a2a.build_handoff(contract, "h1", "A", "B", "0009", 1, {**h["payload"], "total": 2780})
    assert a2a.receiver_check(wrong_type, "B", USE_CASE, contract, b.projection, node, [], {})["code"] == "TYPE_MISMATCH"
    a = rt.agents["A"]
    h2 = db.col("handoffs").find_one({"use_case_id": USE_CASE, "run": "0001", "edge": "h2", "role": "sender"})["handoff"]
    mp = a.nodes["A.mark_invoice_paid_out_of_band"]
    declined = a2a.build_handoff(a.fresh_contract(), "h2", "B", "A", "0009", 2, {**h2["payload"], "finalizedPaymentPlan": {**h2["payload"]["finalizedPaymentPlan"], "status": "declined"}})
    r = a2a.receiver_check(declined, "A", USE_CASE, a.fresh_contract(), a.projection, mp, [], {"invoice": {"total": 2780.0}})
    assert r["code"] == "PRECONDITION_FAILED" and r["got"]["finalizedPaymentPlan.status"] == "declined"


def test_transform_and_payload_helpers():
    state = {"line_item": [{"price_id": "p1", "quantity": 2, "unit_amount_minor": 150}], "invoice": {"total_minor": 300, "currency": "EUR"},
             "customer": {"shipping_address": {"postal_code": "10115"}}}
    apply_transform(state, {"to": "products.purchaseAmount.amount", "from": ["line_item.unit_amount_minor", "line_item.quantity"], "expr": "unit_amount_minor × quantity / 100"}, "EUR")
    apply_transform(state, {"to": "products.productCode", "from": ["line_item.price_id"], "expr": "price_id"})
    apply_transform(state, {"to": "total", "from": ["invoice.total_minor"], "expr": "total_minor / 100"})
    apply_transform(state, {"to": "customer.postalCode", "from": ["customer.shipping_address"], "expr": "shipping_address.postal_code"})
    assert state["products"] == [{"purchaseAmount": {"amount": 3.0, "currency": "EUR"}, "productCode": "p1"}]
    assert state["total"] == 3.0 and state["customer"]["postalCode"] == "10115"
    assert pred_ctx(state)["line_item"]["count"] == 1
    payload = a2a.build_payload(state, ["products.productCode", "products.purchaseAmount", "customer.postalCode", "total"])
    assert payload == {"products": [{"productCode": "p1", "purchaseAmount": {"amount": 3.0, "currency": "EUR"}}], "customer": {"postalCode": "10115"}, "total": 3.0}
    flat = a2a.flatten(payload)
    assert flat["products.productCode"] == ["p1"] and flat["products.purchaseAmount.amount"] == [3.0] and flat["total"] == 3.0
    assert a2a.allowed("products.purchaseAmount.currency", ["products.purchaseAmount"]) and not a2a.allowed("customer.email", ["customer.name"])


def test_mediator_plan_is_unresolvable_for_unknown_fields(world):
    subs = {s["org_id"]: s for s in db.col("submissions").find({"use_case_id": USE_CASE})}
    merged = db.col("merged").find_one({"use_case_id": USE_CASE, "doc_type": "graph"}, sort=[("version", -1)])
    rej = {"code": "MISSING_FIELDS", "edge": "h1", "receiver": "B", "missing": ["customer.bloodType"]}
    plan = mediator.plan_patch(rej, subs, merged)
    assert plan["unresolved"] == ["customer.bloodType"] and plan["ops"] == []
    assert mediator.find_producer(subs["A"], "customer.email")["source"] == "customer.email"
