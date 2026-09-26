"""Stage 4 contract: 5 questions, 3 defaults, guardrails applied to merged v2. Offline."""
import copy
import json

import pytest

from pmp import decide, merge, spec, validate as v
from pmp.align import load_fixture
from pmp.compile import compile_all

USE_CASE = "bnpl_checkout_v1"


@pytest.fixture(scope="module")
def world():
    subs, _ = compile_all(USE_CASE)
    als = load_fixture(USE_CASE)
    merged1 = merge.build(subs, als, USE_CASE, 1)
    findings = v.findings(v.run(merged1, subs, als, [], db=None))
    answers = decide.load_answers(decide.ANSWERS_FIXTURE)
    merged2, decisions, questions, als = decide.build_v2(merged1, findings, subs, als, answers)
    return dict(subs=subs, als=als, merged1=merged1, findings=findings, merged2=merged2, decisions=decisions, questions=questions)


def test_five_questions_three_defaults(world):
    qs = world["questions"]
    assert [q["_id"] for q in qs] == ["q1", "q2", "q3", "q4", "q5"]
    assert [q["trigger"] for q in qs] == ["DUP-02", "IO-01", "STR-03", "PRE-01", "FAIL-01"]
    assert sum(q["accepted_default"] for q in qs) == 3
    assert [q["accepted_default"] for q in qs] == [False, True, False, True, True]
    for q in qs:
        assert spec.errors(q, "question") == [], q["_id"]
        assert q["answer"] and q["answered_by"] and q["ts"]


def test_routing(world):
    r = {q["_id"]: (q["to_org"], q["to_role"]) for q in world["questions"]}
    assert r["q1"] == ("A", "ops.lead")
    assert r["q2"] == ("A", "ops.lead")
    assert r["q3"] == ("A", "finance_controller")
    assert r["q4"] == ("B", "platform.lead")
    assert r["q5"] == ("A", "ops.lead")
    assert world["questions"][0]["triggers"] == ["DUP-02", "IO-02"]        # IO-02 folded into q1


def test_question_texts(world):
    t = {q["_id"]: q["text"] for q in world["questions"]}
    assert t["q1"].startswith('Your "create_customer" (Create the Stripe Customer with name and email) and Lakeside\'s "createCustomer"')
    assert "may the customer's name and email be sent to Lakeside" in t["q1"]
    assert "Who converts, and what is productCode?" in t["q2"]
    assert "Does an authorized payment plan from Lakeside count as payment" in t["q3"]
    assert "paid_out_of_band=true" in t["q3"]
    assert t["q4"] == "updateOrderStatus needs loanTransactionId, which Northwind never sees. May Lakeside include it in the finalizedPaymentPlan it returns, so Northwind can echo it back?"
    assert t["q5"].startswith("If Lakeside declines the loan (")
    assert "what should Northwind do with the finalized invoice?" in t["q5"]


def test_defaults(world):
    d = {q["_id"]: q["default"] for q in world["questions"]}
    assert d["q1"] == "keep_both; send customer.name, customer.email; never customer_id, payment_method"
    assert d["q2"] == "Northwind converts; purchaseAmount.amount = unit_amount_minor × quantity / 100; productCode = price_id"
    assert d["q3"] == "yes, provided (finalizedPaymentPlan.status == 'authorized' AND totalLoanAmount == 'invoice.total')"
    assert d["q4"] == "yes"
    assert d["q5"] == "fall back to card payment (collect_payment)"


def test_no_confidential_notes_in_any_question(world):
    notes = [n["confidential_notes"] for s in world["subs"] for n in s["nodes"] if n.get("confidential_notes")]
    assert len(notes) >= 4
    for q in world["questions"]:
        blob = json.dumps(q)
        for note in notes:
            assert note not in blob, q["_id"]
        for secret in ("risk band", "Authentication vendor", "Customer IDs are reused"):
            assert secret not in blob


def test_lattice_decision_logged_not_asked(world):
    ds = world["decisions"]
    assert len(ds) == 1 and ds[0]["_id"] == "d1" and ds[0]["rule"] == "lattice" and ds[0]["check"] == "POL-01"
    assert spec.errors(ds[0], "decision") == []
    h3 = next(e for e in world["merged2"]["boundary_edges"] if e["_id"] == "h3")
    assert h3["guard"]["requires_human_approval"] is True and h3["guard"]["max_amount"]["amount"] == 10000


def test_merged_v2_guardrails(world):
    m = world["merged2"]
    assert m["_id"] == f"{USE_CASE}:v2" and m["version"] == 2 and m["supersedes"] == 1
    nodes = {n["_id"]: n for n in m["nodes"]}
    assert "A.adapt_basket_for_lender" in nodes and nodes["A.adapt_basket_for_lender"]["origin"] == "q2"
    assert "A.mark_invoice_paid_out_of_band" in nodes and nodes["A.mark_invoice_paid_out_of_band"]["origin"] == "q3"
    assert len(m["nodes"]) == 22
    comp = [e for e in m["edges"] if e.get("type") == "compensation"]
    assert len(comp) == 1 and (comp[0]["from"], comp[0]["to"], comp[0]["origin"]) == ("B.END_not_eligible", "A.collect_payment", "q5")
    b = {e["_id"]: e for e in m["boundary_edges"]}
    assert b["h1"]["from"] == "A.adapt_basket_for_lender" and b["h1"]["adapter"] == "A.adapt_basket_for_lender"
    assert b["h1"]["allowlist"] == ["customer.email", "customer.name"]
    assert b["h2"]["to"] == "A.mark_invoice_paid_out_of_band"
    assert "finalizedPaymentPlan.loanTransactionId" in b["h2"]["allowlist"]
    assert b["h3"]["allowlist"] == ["fulfilled_at", "loanTransactionId"]
    assert {f["path"] for f in nodes["A.fulfil_order"]["outputs"]} >= {"loanTransactionId", "fulfilled_at"}
    assert nodes["A.mark_invoice_paid_out_of_band"]["pre"] == {"op": "and", "args": [
        {"op": "eq", "args": ["finalizedPaymentPlan.status", "authorized"]}, {"op": "eq", "args": ["totalLoanAmount", "invoice.total"]}]}
    assert nodes["A.mark_invoice_paid_out_of_band"]["tool"]["name"] == "invoices.pay"
    assert len(m["edges"]) == 26 + 3        # sender->adapter, adapter->await, compensation
    # the branch condition moved from h1 onto finalize_invoice -> adapter
    assert "condition" not in b["h1"]
    assert next(e for e in m["edges"] if e["_id"] == "A.finalize_invoice->A.adapt_basket_for_lender")["condition"]["args"] == ["checkout.payment_choice", "pay_later"]
    al = next(a for a in world["als"] if a["a_name"] == "create_customer")
    assert al["confirmed_by"] == "q1" and al["decision"] == "keep_both"
    assert len(m["guardrails"]) >= 8 and m["inputs"]["h_answers"].startswith("sha256:")


def test_adapter_schema_for_lender(world):
    a = next(n for n in world["merged2"]["nodes"] if n["_id"] == "A.adapt_basket_for_lender")
    outs = {f["path"]: f for f in a["outputs"]}
    assert outs["products.purchaseAmount"]["unit"] == "major" and outs["products.productCode"]["type"] == "string"
    assert "customer.email" in outs and "line_item.unit_amount_minor" not in outs
    assert {f["path"] for f in a["inputs"]} == {"line_item.unit_amount_minor", "line_item.quantity", "line_item.price_id", "invoice.total_minor"}
    assert a["visibility"] == "handoff_out" and a["org_id"] == "A"


def test_v2_revalidates_clean(world):
    res = v.run(world["merged2"], world["subs"], world["als"], world["questions"], db=None, stage="revalidate")
    bad = [(r["check"], r["scope"], r["detail"]) for r in res if r["verdict"] == "fail"]
    assert bad == []
    assert all(r["stage"] == "revalidate" and r["merged_version"] == 2 for r in res)
    assert {r["check"] for r in res} >= {"IO-01", "IO-02", "STR-01", "STR-03", "PRE-01", "POL-01", "FAIL-01", "DUP-02", "TOOL-01", "TOOL-02", "TOOL-03"}


def test_findings_marked_resolved(world):
    r = {f["check"]: f["resolved_by"] for f in world["findings"]}
    assert r == {"IO-01": "q2", "IO-02": "q1", "STR-03": "q3", "PRE-01": "q4", "POL-01": "d1", "FAIL-01": "q5", "DUP-02": "q1"}


def test_all_defaults_path_and_determinism(world):
    subs, als1 = world["subs"], load_fixture(USE_CASE)
    m1 = merge.build(subs, als1, USE_CASE, 1)
    f1 = v.findings(v.run(m1, subs, als1, [], db=None))
    m2, ds, qs, _ = decide.build_v2(m1, f1, subs, als1, {})
    assert all(q["accepted_default"] for q in qs) and len(qs) == 5
    nodes = {n["_id"] for n in m2["nodes"]}
    assert {"A.adapt_basket_for_lender", "A.mark_invoice_paid_out_of_band"} <= nodes
    assert v.run(m2, subs, als1, qs, db=None) and not [r for r in v.run(m2, subs, als1, qs, db=None) if r["verdict"] == "fail"]
    # same inputs -> same graph (timestamps live only in the question docs, not in the hash)
    als2 = load_fixture(USE_CASE)
    m2b, _, _, _ = decide.build_v2(merge.build(subs, als2, USE_CASE, 1), v.findings(v.run(m1, subs, als2, [], db=None)), subs, als2, {})
    assert m2b["hash"] == m2["hash"]


def test_alternative_answers(world):
    subs, als = world["subs"], load_fixture(USE_CASE)
    m1 = merge.build(subs, als, USE_CASE, 1)
    f1 = v.findings(v.run(m1, subs, als, [], db=None))
    m2, _, qs, _ = decide.build_v2(m1, f1, subs, als, {"IO-01": {"answer": "receiver_converts"}, "FAIL-01": {"answer": "cancel the invoice"}, "STR-03": {"answer": "no"}})
    nodes = {n["_id"] for n in m2["nodes"]}
    assert "B.adapt_h1_inbound" in nodes and "A.mark_invoice_paid_out_of_band" not in nodes
    comp = next(e for e in m2["edges"] if e.get("type") == "compensation")
    assert comp["to"] == "A.T_cancelled_unpaid"
    res = v.run(m2, subs, als, qs, db=None)
    assert [r["check"] for r in res if r["verdict"] == "fail"] == ["STR-03"]      # the cycle stays a first-class conflict


@pytest.mark.live
def test_live_decide_from_atlas():
    from pmp import db
    m1 = merge.load(USE_CASE, 1)
    subs = list(db.col("submissions").find({"use_case_id": USE_CASE}))
    als = list(db.col("alignments").find({"use_case_id": USE_CASE}))
    findings = list(db.col("findings").find({"use_case_id": USE_CASE, "stage": "validate", "merged_version": 1}))
    m2, ds, qs, als = decide.build_v2(m1, findings, subs, als, decide.load_answers(decide.ANSWERS_FIXTURE))
    assert len(qs) == 5 and {n["_id"] for n in m2["nodes"]} >= {"A.adapt_basket_for_lender", "A.mark_invoice_paid_out_of_band"}
    decide.write(m2, ds, qs, als, findings)
    assert db.col("merge_questions").count_documents({"use_case_id": USE_CASE}) == 5
    assert db.col("merged").find_one({"_id": f"{USE_CASE}:v2", "doc_type": "graph"})
    assert db.col("alignments").count_documents({"use_case_id": USE_CASE, "confirmed_by": "q1"}) == 1
    res = v.run(m2, subs, als, qs, db=db, stage="revalidate")
    assert not [r for r in res if r["verdict"] == "fail"]
