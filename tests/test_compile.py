"""Stage 1 contract from mock/STAGES.md. Pure: no Atlas, no LLM."""
import json

import pytest

from pmp import spec
from pmp.compile import (arazzo_condition, compile_all, compile_lakeside, compile_northwind, parse_skill,
                         to_ast, INPUTS)


@pytest.fixture(scope="module")
def compiled():
    subs, findings = compile_all()
    return {s["org_id"]: s for s in subs}, findings


# ---- Northwind ---------------------------------------------------------------

def test_northwind_shape(compiled):
    subs, _ = compiled
    a = subs["A"]
    assert len(a["nodes"]) == 10
    assert len(a["transitions"]) == 11
    assert sum(n["kind"] == "human_gate" for n in a["nodes"]) == 1
    assert a["hash"].startswith("sha256:")
    assert a["org_name"] == "northwind"


def test_northwind_handoff_visibility(compiled):
    a = compiled[0]["A"]
    out = {n["name"] for n in a["nodes"] if n["visibility"] == "handoff_out"}
    assert out == {"finalize_invoice", "fulfil_order"}
    assert {h["id"] for h in a["handoffs"]} == {"H1_request_financing", "H2_receive_plan", "H3_report_fulfilled"}


def test_northwind_lint02_policy_on_gate(compiled):
    subs, findings = compiled
    gate = next(n for n in subs["A"]["nodes"] if n["kind"] == "human_gate")
    assert gate["name"] == "approve_large_order"
    assert gate["policy"]["requires_human_approval"] is True
    assert gate["policy"]["max_amount"] == {"currency": "USD", "amount": 10000}
    lint02 = [f for f in findings if f["check"] == "LINT-02"]
    assert len(lint02) == 1 and lint02[0]["owner_org"] == "A"
    assert "10,000" in lint02[0]["detail"]


def test_northwind_fields_are_typed_from_the_data_model(compiled):
    a = compiled[0]["A"]
    fin = next(n for n in a["nodes"] if n["name"] == "finalize_invoice")
    by_path = {f["path"]: f for f in fin["outputs"]}
    assert by_path["invoice.total_minor"]["unit"] == "cents"
    assert by_path["line_item.unit_amount_minor"]["unit"] == "cents"     # line_item.* expanded
    assert by_path["line_item.price_id"]["type"] == "string"
    assert by_path["customer.email"] == {"path": "customer.email", "type": "string", "sensitivity": "pii", "may_cross": "ask"}
    assert fin["tool"] == {"server": "stripe", "name": "invoices.finalize", "effect": "side_effect", "reversible": False}


def test_northwind_primary_tool_is_the_money_one(compiled):
    a = compiled[0]["A"]
    pay = next(n for n in a["nodes"] if n["name"] == "collect_payment")
    assert pay["tool"]["name"] == "invoices.pay" and pay["tool"]["money"] is True
    assert {t["name"] for t in pay["tools"]} == {"invoices.send", "invoices.pay"}


def test_northwind_edges(compiled):
    a = compiled[0]["A"]
    edges = {(t["from"], t["to"]) for t in a["transitions"]}
    assert ("A.await_invoice_paid", "A.collect_payment") in edges          # payment_failed -> retry
    assert ("A.approve_large_order", "A.fulfil_order") in edges
    assert ("A.fulfil_order", "A.T_fulfilled") in edges
    assert not any(t["to"].startswith("H") for t in a["transitions"])    # handoff targets are not local edges
    retry = next(t for t in a["transitions"] if (t["from"], t["to"]) == ("A.await_invoice_paid", "A.collect_payment"))
    assert retry["condition"] == {"op": "eq", "args": ["webhook.event", "invoice.payment_failed"]}


# ---- Lakeside ----------------------------------------------------------------

def test_lakeside_shape(compiled):
    b = compiled[0]["B"]
    assert len(b["nodes"]) == 10
    assert len(b["transitions"]) == 12
    assert b["hash"].startswith("sha256:")
    kinds = {n["kind"] for n in b["nodes"]}
    assert kinds == {"action", "decision", "terminal"}
    assert sum(n["kind"] == "decision" for n in b["nodes"]) == 2
    assert sum(n["kind"] == "terminal" for n in b["nodes"]) == 1


def test_lakeside_handoff_visibility(compiled):
    b = compiled[0]["B"]
    vis = {n["name"]: n["visibility"] for n in b["nodes"]}
    assert vis["checkLoanCanBeProvided"] == "handoff_in"
    assert vis["retrieveFinalizedPaymentPlan"] == "handoff_out"
    assert vis["updateOrderStatus"] == "handoff_in"
    assert vis["END_not_eligible"] == "handoff_out"
    assert vis["createCustomer"] == "internal"


def test_lakeside_lint01_exactly_two(compiled):
    subs, findings = compiled
    lint01 = [f for f in findings if f["check"] == "LINT-01"]
    assert len(lint01) == 2 and all(f["owner_org"] == "B" for f in lint01)
    scopes = [f["scope"] for f in lint01]
    assert scopes == ["steps[4].parameters.redirectAuthToken", "steps[5].parameters.loanTransactionId"]
    assert "itself" in lint01[0]["detail"] and "initiateBnplTransaction" in lint01[0]["detail"]
    assert "derived" in lint01[1]["detail"]


def test_lakeside_lint01_corrections_applied(compiled):
    b = compiled[0]["B"]
    nodes = {n["name"]: n for n in b["nodes"]}
    # self-reference rewired to the real producer
    auth_in = {f["path"]: f for f in nodes["authenticateCustomerAndAuthorizeLoan"]["inputs"]}
    assert auth_in["redirectAuthToken"]["source"] == "$steps.initiateBnplTransaction.outputs.redirectAuthToken"
    assert auth_in["redirectAuthToken"]["sensitivity"] == "secret"
    # undeclared output compiled as derived on its producer, so the later reference resolves without a 3rd finding
    init_out = {f["path"]: f for f in nodes["initiateBnplTransaction"]["outputs"]}
    assert init_out["loanTransactionId"]["derived"] is True
    upd_in = {f["path"] for f in nodes["updateOrderStatus"]["inputs"]}
    assert "loanTransactionId" in upd_in
    assert nodes["updateOrderStatus"]["pre"] == {"op": "exists", "args": ["loanTransactionId"]}


def test_lakeside_receiver_inputs_and_tools(compiled):
    b = compiled[0]["B"]
    chk = next(n for n in b["nodes"] if n["name"] == "checkLoanCanBeProvided")
    by_path = {f["path"]: f for f in chk["inputs"]}
    assert by_path["products.purchaseAmount"] == {"path": "products.purchaseAmount", "type": "money", "unit": "major"}
    assert by_path["products.productCode"]["type"] == "string"
    assert by_path["customer.firstName"]["sensitivity"] == "pii"
    assert chk["tool"] == {"server": "BnplApi", "name": "findEligibleProducts", "effect": "verify"}
    upd = next(n for n in b["nodes"] if n["name"] == "updateOrderStatus")
    assert upd["tool"]["money"] is True and "risk_officer" in upd["tool"]["requires_gate"]


def test_lakeside_edges(compiled):
    b = compiled[0]["B"]
    edges = {(t["from"], t["to"]): t for t in b["transitions"]}
    # three "not eligible" exits collapse into one terminal
    to_end = [e for e in edges if e[1] == "B.END_not_eligible"]
    assert sorted(to_end) == [("B.checkLoanCanBeProvided", "B.END_not_eligible"), ("B.createCustomer", "B.END_not_eligible")]
    assert edges[("B.checkLoanCanBeProvided", "B.END_not_eligible")]["condition"]["op"] == "or"
    # the two internal decision points
    assert ("B.getCustomerTermsAndConditions", "B.decide_eligibilityCheckRequired") in edges
    assert ("B.decide_eligibilityCheckRequired", "B.createCustomer") in edges
    assert ("B.decide_eligibilityCheckRequired", "B.initiateBnplTransaction") in edges
    assert ("B.decide_redirectAuthToken", "B.authenticateCustomerAndAuthorizeLoan") in edges
    assert ("B.decide_redirectAuthToken", "B.retrieveFinalizedPaymentPlan") in edges
    assert edges[("B.decide_redirectAuthToken", "B.retrieveFinalizedPaymentPlan")]["condition"] == \
        {"op": "eq", "args": ["response.body.redirectAuthToken", None]}
    # sequential fallthrough where Arazzo has no onSuccess
    assert ("B.authenticateCustomerAndAuthorizeLoan", "B.retrieveFinalizedPaymentPlan") in edges
    assert ("B.retrieveFinalizedPaymentPlan", "B.updateOrderStatus") in edges


# ---- cross-cutting -------------------------------------------------------------

def test_everything_validates_against_spec(compiled):
    subs, findings = compiled
    for s in subs.values():
        for n in s["nodes"]:
            assert spec.errors(n, "node") == [], n["_id"]
        for t in s["transitions"]:
            assert spec.errors(t, "transition") == [], t["_id"]
    for f in findings:
        assert spec.errors(f, "finding") == [], f["_id"]


def test_confidential_notes_never_in_text(compiled):
    subs, _ = compiled
    for s in subs.values():
        notes = [n["confidential_notes"] for n in s["nodes"] if n.get("confidential_notes")]
        assert notes, f"{s['org_id']} should carry some confidential_notes on nodes"
        for n in s["nodes"]:
            assert n["text"].startswith(n["name"] + ":")
            for note in notes:
                assert note not in n["text"]


def test_deterministic_and_bson_safe(compiled):
    subs, _ = compiled
    again = {s["org_id"]: s for s in compile_all()[0]}
    for org in ("A", "B"):
        assert again[org]["hash"] == subs[org]["hash"]
        json.dumps(subs[org])  # no dates / non-JSON values leaked from YAML
        def no_dotted_keys(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    assert "." not in k, k
                    no_dotted_keys(v)
            elif isinstance(o, list):
                for v in o:
                    no_dotted_keys(v)
        no_dotted_keys(subs[org])


def test_submission_ids_and_hash_scope(compiled):
    subs, _ = compiled
    assert subs["A"]["_id"] == "bnpl_checkout_v1:A" and subs["B"]["_id"] == "bnpl_checkout_v1:B"
    assert subs["A"]["hash"] != subs["B"]["hash"]


# ---- unit: parsers ---------------------------------------------------------------

def test_parse_skill_sections():
    doc = parse_skill(INPUTS / "northwind" / "SKILL.md")
    assert doc["frontmatter"]["org_id"] == "northwind"
    assert [s["id"] for s in doc["steps"]] == [f"A{i}" for i in range(1, 9)]
    assert doc["steps"][0]["prose"].startswith("Create the Stripe Customer")
    assert doc["steps"][0]["on_failure"] == {"retry": 2, "then": {"escalate_to": "ops_manager"}}
    assert [t["id"] for t in doc["terminals"]] == ["T_fulfilled", "T_cancelled_unpaid"]
    assert "stripe:invoiceitems.create" in doc["tools"]


def test_to_ast_shorthand():
    assert to_ast({"eq": ["invoice.status", "paid"]}) == {"op": "eq", "args": ["invoice.status", "paid"]}
    assert to_ast({"exists": "loanTransactionId"}) == {"op": "exists", "args": ["loanTransactionId"]}
    assert to_ast({"and": [{"eq": ["a.b", 1]}, {"or": [{"lt": ["a.c", 2]}, {"exists": "a.d"}]}]}) == {
        "op": "and", "args": [{"op": "eq", "args": ["a.b", 1]},
                              {"op": "or", "args": [{"op": "lt", "args": ["a.c", 2]}, {"op": "exists", "args": ["a.d"]}]}]}
    assert to_ast(None) is None


@pytest.mark.parametrize("cond,expected", [
    ("$statusCode == 200", {"op": "eq", "args": ["response.statusCode", 200]}),
    ("$response.body#/redirectAuthToken != null", {"op": "ne", "args": ["response.body.redirectAuthToken", None]}),
    ("$steps.checkLoanCanBeProvided.outputs.eligibilityCheckRequired == true", {"op": "eq", "args": ["eligibilityCheckRequired", True]}),
    ("$[?count(@.products) > 0]", {"op": "gt", "args": ["response.body.products.count", 0]}),
    ("$statusCode == 200 || $statusCode == 201", {"op": "or", "args": [{"op": "eq", "args": ["response.statusCode", 200]},
                                                                        {"op": "eq", "args": ["response.statusCode", 201]}]}),
])
def test_arazzo_condition(cond, expected):
    assert arazzo_condition(cond) == expected
