"""Stage 3b contract: exactly the 7 findings of Stage 3 with the stated verdicts, stakes and owners. Offline."""
import pytest

from pmp import merge, spec, validate as v
from pmp.align import load_fixture
from pmp.compile import compile_all

USE_CASE = "bnpl_checkout_v1"

EXPECTED = {  # check: (verdict, scope, rung, stakes, owner)
    "IO-01": ("fail", "h1", 2, "low", "A"),
    "IO-02": ("fail", "h1", 5, "high", "A"),
    "STR-03": ("fail", "merged", 5, "high", "A"),
    "PRE-01": ("fail", "h3", 5, "high", "B"),
    "POL-01": ("pass", "h3", 1, "low", "agent"),
    "FAIL-01": ("fail", "h1", 5, "high", "A"),
    "DUP-02": ("fail", "create_customer ~ createCustomer", 5, "high", "A"),
}


@pytest.fixture(scope="module")
def world():
    subs, _ = compile_all(USE_CASE)
    als = load_fixture(USE_CASE)
    merged = merge.build(subs, als, USE_CASE, 1)
    results = v.run(merged, subs, als, [], db=None)
    return merged, subs, als, results


def test_exactly_the_seven_findings(world):
    _, _, _, results = world
    rows = v.findings(results)
    assert {r["check"] for r in rows} == set(EXPECTED)
    assert len(rows) == 7
    for r in rows:
        verdict, scope, rung, stakes, owner = EXPECTED[r["check"]]
        assert r["verdict"] == verdict, r["check"]
        assert r["scope"] == scope, r["check"]
        assert r["rung"] == rung, r["check"]
        assert r["stakes"] == stakes, r["check"]
        assert r["owner_org"] == owner, r["check"]
        assert r["stage"] == "validate" and r["merged_version"] == 1 and r["resolved_by"] is None
        assert spec.errors(r, "finding") == [], r["_id"]


def test_details_match_stage_3(world):
    d = {r["check"]: r for r in v.findings(world[3])}
    assert "unit_amount_minor" in d["IO-01"]["detail"] and "cents" in d["IO-01"]["detail"]
    assert "purchaseAmount{currency, amount}" in d["IO-01"]["detail"] and "productCode" in d["IO-01"]["detail"] and "price_id" in d["IO-01"]["detail"]
    assert d["IO-02"]["detail"] == "Sender emits customer name and email; no field allowlist declared."
    assert "invoice.status == 'paid'" in d["STR-03"]["detail"] and "updateOrderStatus" in d["STR-03"]["detail"] and "fulfil_order" in d["STR-03"]["detail"]
    assert d["PRE-01"]["detail"].startswith("Receiver precondition path loanTransactionId is not in the edge's boundary schema; A never carries it.")
    assert "A True, B unset -> OR -> True" in d["POL-01"]["detail"] and "USD 10,000" in d["POL-01"]["detail"]
    assert d["POL-01"]["guard"] == {"requires_human_approval": True, "max_amount": {"currency": "USD", "amount": 10000}}
    assert d["FAIL-01"]["detail"] == "No failure path in A for the lender terminal END_not_eligible."
    assert d["FAIL-01"]["rung_from"] == 4 and d["FAIL-01"]["patch"]["to"] == "A.collect_payment"
    assert d["DUP-02"]["detail"].startswith("Near-duplicate pair (σ 0.93) still undecided; both side-effecting.")
    assert d["DUP-02"]["locality"] == "mediator"


def test_passing_checks_are_in_results_but_not_findings(world):
    results = world[3]
    by = {}
    for r in results:
        by.setdefault(r["check"], []).append(r["verdict"])
    assert by["STR-01"] == ["pass"]
    assert by["TOOL-01"] == ["pass"] and by["TOOL-02"] == ["pass"] and by["TOOL-03"] == ["pass"]
    tool03 = next(r for r in results if r["check"] == "TOOL-03")
    assert "wms.create_shipment (human_gate)" in tool03["detail"] and "org policy" in tool03["detail"]
    assert by["IO-01"] == ["fail", "pass", "pass"]         # h1 fails; h2, h3 pass
    assert by["IO-02"] == ["fail", "pass", "pass"]
    assert by["PRE-01"] == ["pass", "pass", "fail"]        # only h3
    assert [r["check"] for r in results] == sorted([r["check"] for r in results], key=[c for c, *_ in v.REGISTRY].index)
    assert by["STR-02"] == ["pass"]
    assert "PRE-03" not in by and "ALN-01" not in by and "CONF-01" not in by      # certificate-time only


def test_stakes_rule(world):
    for r in v.findings(world[3]):
        if r["stakes"] == "low":
            assert r["check"] in {"IO-01", "POL-01"}
            assert not any(w in r["stakes_reason"] for w in ("pii", "customer"))


def test_str01_fails_on_a_dead_end(world):
    merged, subs, als, _ = world
    import copy
    m = copy.deepcopy(merged)
    m["edges"] = [e for e in m["edges"] if e["from"] != "A.approve_large_order"]      # gate now leads nowhere
    res = [r for r in v.run(m, subs, als, [], db=None) if r["check"] == "STR-01"]
    assert res and res[0]["verdict"] == "fail" and res[0]["scope"] == "A.approve_large_order"


def test_str03_clears_once_paid_is_established_upstream(world):
    """Simulating q3's adapter: a node on the financed path that marks the invoice paid removes the cycle."""
    merged, subs, als, _ = world
    import copy
    m = copy.deepcopy(merged)
    m["nodes"].append({"_id": "A.mark_invoice_paid_out_of_band", "org_id": "A", "use_case_id": USE_CASE,
                       "name": "mark_invoice_paid_out_of_band", "kind": "action", "visibility": "internal",
                       "post": {"op": "eq", "args": ["invoice.status", "paid"]}, "text": "mark paid"})
    m["edges"] = [dict(e, to="A.mark_invoice_paid_out_of_band") if e["_id"] == "h2" else e for e in m["edges"]]
    m["edges"].append({"_id": "x", "org_id": "A", "use_case_id": USE_CASE, "from": "A.mark_invoice_paid_out_of_band", "to": "A.await_invoice_paid"})
    m["boundary_edges"] = [dict(e, to="A.mark_invoice_paid_out_of_band") if e["_id"] == "h2" else e for e in m["boundary_edges"]]
    res = [r for r in v.run(m, subs, als, [], db=None) if r["check"] == "STR-03"]
    assert [r["verdict"] for r in res] == ["pass"]


def test_dup02_ignores_feeding_pairs(world):
    merged, subs, als, _ = world
    import copy
    als2 = copy.deepcopy(als)
    next(a for a in als2 if a["a_name"] == "fulfil_order")["sigma"] = 0.9      # live LLM did this once
    res = [r for r in v.run(merged, subs, als2, [], db=None) if r["check"] == "DUP-02"]
    assert [r["scope"] for r in res] == ["create_customer ~ createCustomer"]


def test_dup02_clears_when_confirmed(world):
    merged, subs, als, _ = world
    import copy
    als2 = copy.deepcopy(als)
    next(a for a in als2 if a["a_name"] == "create_customer")["confirmed_by"] = "q1"
    res = [r for r in v.run(merged, subs, als2, [], db=None) if r["check"] == "DUP-02"]
    assert [r["verdict"] for r in res] == ["pass"]


def test_io02_passes_with_allowlist_but_flags_never_fields(world):
    merged, subs, als, _ = world
    import copy
    m = copy.deepcopy(merged)
    h1 = next(e for e in m["boundary_edges"] if e["_id"] == "h1")
    h1["allowlist"] = ["customer.name", "customer.email"]
    res = [r for r in v.run(m, subs, als, [], db=None) if r["check"] == "IO-02" and r["scope"] == "h1"]
    assert res[0]["verdict"] == "pass"
    h1["sender_fields"].append({"path": "customer.customer_id", "type": "string", "sensitivity": "internal", "may_cross": "never"})
    h1["allowlist"].append("customer.customer_id")
    res = [r for r in v.run(m, subs, als, [], db=None) if r["check"] == "IO-02" and r["scope"] == "h1"]
    assert res[0]["verdict"] == "fail"


@pytest.mark.live
def test_live_merge_and_validate_with_graphlookup():
    from pmp import db
    subs = list(db.col("submissions").find({"use_case_id": USE_CASE}))
    als = list(db.col("alignments").find({"use_case_id": USE_CASE})) or load_fixture(USE_CASE)
    for al in als:                      # v1 is validated before any decision: forget confirmations a later stage wrote
        al["confirmed_by"] = None
        al.pop("decision", None)
    merged = merge.build(subs, als, USE_CASE, 1)
    merge.write(merged)
    results = v.run(merged, subs, als, [], db=db)
    str01 = next(r for r in results if r["check"] == "STR-01")
    assert str01["verdict"] == "pass" and "$graphLookup" in str01["detail"]
    rows = v.findings(results)
    assert {r["check"] for r in rows} == set(EXPECTED) and len(rows) == 7
