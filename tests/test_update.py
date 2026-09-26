"""Stage 8 (design doc §6): Northwind raises its approval threshold 10,000 -> 15,000. Only what was built from it moves:
the h3 boundary guard and the gate condition are rebuilt, a handful of checks re-run, everything else is carried,
nobody is asked anything, both orgs re-sign because both visible slices changed. Offline, in-memory."""
import pytest

from pmp import db, spec, update
from pmp.predicate import to_text
from pmp.runtime.run import InProcessRuntime, bootstrap

USE_CASE = "bnpl_checkout_v1"


@pytest.fixture(scope="module")
def world():
    db.use_memory()
    bootstrap(USE_CASE)
    rt = InProcessRuntime(USE_CASE)
    rt.run("0001"); rt.run("0002")                       # contract v2 exists (runtime patch)
    before = db.col("contracts").find_one({"use_case_id": USE_CASE, "status": "active"})
    report = update.apply_update("A", "large_order_approval", 15000, use_case=USE_CASE)
    yield dict(before=before, r=report, rt=rt)
    db.use_atlas()


def test_index_records_what_everything_was_built_from(world):
    els, ds = world["r"]["elements"], world["r"]["derivations"]
    kinds = {e["kind"] for e in els}
    assert kinds == {"node", "schema", "rule", "tool_binding", "policy", "edge"}
    by = {d["derived_id"]: d for d in ds}
    assert by["guardrail:d1:policy_value:h3"]["rebuild_fn"] == "lattice"
    assert "A.approve_large_order.policy" in by["guardrail:d1:policy_value:h3"]["inputs"]
    assert "A.policy.financing_counts_as_paid" in by["guardrail:q3:predicate:h2"]["inputs"]
    assert set(by["alignment:create_customer~createCustomer"]["inputs"]) == {"A.create_customer", "A.create_customer.io", "B.createCustomer", "B.createCustomer.io"}
    assert "guard:h3" in by["check:PRE-03:h3"]["derived_inputs"]
    assert db.col("elements").count_documents({"use_case_id": USE_CASE}) == len(els)
    assert db.col("derivations").count_documents({"use_case_id": USE_CASE}) > 40


def test_delta_is_a_loosening_of_policy_and_rules(world):
    d = world["r"]["delta"]
    changed = {c["element_id"]: c for c in d["changed"]}
    assert set(changed) == {"A.policy.large_order_approval", "A.approve_large_order.rule", "A.approve_large_order.policy",
                            "A.fulfil_order.rule", "A.fulfil_order.tool", "A.approve_large_order"}
    assert changed["A.policy.large_order_approval"]["direction"] == "loosen"
    assert changed["A.approve_large_order.policy"]["direction"] == "loosen"
    assert d["added"] == [] and d["removed"] == [] and d["delta_hash"].startswith("sha256:")
    assert world["r"]["change"]["old"] == 10000 and world["r"]["change"]["new"] == 15000 and world["r"]["class"] == 2


def test_only_what_was_built_from_it_is_rebuilt(world):
    r = world["r"]
    rebuilt = {x["derived_id"]: x for x in r["rebuilt"]}
    assert set(rebuilt) == {"guardrail:d1:policy_value:h3", "guard:h3", "gate:A.approve_large_order"}
    assert rebuilt["guardrail:d1:policy_value:h3"]["result"]["max_amount"] == {"currency": "USD", "amount": 15000}
    assert rebuilt["guard:h3"]["result"] == "(invoice.total_minor < 1500000 OR approval.granted == True)"
    carried = set(r["carried_guardrails"])
    assert {"guardrail:q1:allowlist:h1", "guardrail:q3:adapter:A.mark_invoice_paid_out_of_band", "guardrail:q4:allowlist:h2",
            "guardrail:q3:predicate:h2", "guard:h2", "guard:h1"} <= carried


def test_checks_rerun_vs_carried(world):
    r = world["r"]
    assert set(r["checks_rerun"]) >= {"PRE-03 on h3", "CONF-01 on h3", "POL-01 on h3"}
    assert not any(x.endswith("on h1") or x.endswith("on h2") for x in r["checks_rerun"])
    cert = r["certificate"]
    st = {(c["check"], c["scope"]): c.get("status") for c in cert["checks"]}
    assert st[("PRE-03", "h3")] == "rerun" and st[("IO-01", "h1")] == "carried" and st[("PRE-03", "h2")] == "carried"
    n_local_or_mediator = len([c for c in cert["checks"] if not c["check"].startswith("INV") and c["check"] != "PRIV-01"])
    assert cert["checks_carried"] == r["checks_carried"] == n_local_or_mediator - len(r["checks_rerun"]) > 10
    assert all(c["verdict"] in ("pass", "guarded") for c in cert["checks"])
    assert next(c for c in cert["checks"] if (c["check"], c["scope"]) == ("PRE-03", "h3"))["evidence"].startswith("guard (invoice.total_minor < 1500000")
    assert spec.errors(cert, "certificate") == []


def test_nobody_is_asked_both_resign(world):
    r = world["r"]
    assert r["questions_reasked"] == [] and r["questions_carried"] == ["q1", "q2", "q3", "q4", "q5"]
    assert r["signatures_required"] == ["A", "B"]
    assert r["friction"] == "Lakeside will need to re-sign but not answer anything"
    cert = r["certificate"]
    assert cert["supersedes"] == world["before"]["certificate_id"] and cert["delta"]["direction"] == "loosen" and cert["delta"]["class"] == 2


def test_contract_v3_active_with_new_guard(world):
    c = db.col("contracts").find_one({"use_case_id": USE_CASE, "status": "active"})
    assert c["version"] == world["before"]["version"] + 1 == 3 and c["supersedes"] == 2
    assert c["reason"].startswith("republish: Northwind large_order_approval max_amount 10,000 -> 15,000 (loosen)")
    versions = {x["version"]: x["status"] for x in db.col("contracts").find({"use_case_id": USE_CASE})}
    assert versions == {1: "superseded", 2: "superseded", 3: "active"}
    h3 = next(e for e in c["boundary_edges"] if e["id"] == "h3")
    assert to_text(h3["guard"]) == "(invoice.total_minor < 1500000 OR approval.granted == True)"
    assert h3["policy"]["max_amount"]["amount"] == 15000
    assert spec.errors(c, "contract") == []
    log = db.col("merge_log").find_one({"use_case_id": USE_CASE, "type": "republish"})
    assert log["contract_version"] == 3 and log["questions_reasked"] == []
    sub = db.col("submissions").find_one({"_id": f"{USE_CASE}:A"})
    assert sub["version"] == "2.5.0" and sub["republished"]["new"] == 15000
    pa = db.col("projections").find_one({"_id": f"{USE_CASE}:A:v3"})
    gate = next(n for n in pa["nodes"] if n["_id"] == "A.approve_large_order")
    assert gate["policy"]["max_amount"]["amount"] == 15000 and "15,000" in gate["text"]


def test_runtime_continues_under_v3(world):
    rt = world["rt"]
    rt.reload()
    assert rt.agents["A"].contract["version"] == 3 and rt.agents["B"].contract["version"] == 3
    res = rt.run("0001")                                   # same order again, now under the v3 contract
    assert res["A"]["status"] == "completed" and res["contract_version"] == 3 and res["rejections"] == []
    h3 = db.col("handoffs").find_one({"use_case_id": USE_CASE, "run": "0001", "edge": "h3", "role": "sender"}, sort=[("seq", -1)])
    assert h3["handoff"]["metadata"]["pmp"]["contract_version"] == 3
    assert list(h3["handoff"]["metadata"]["pmp"]["guard_results"]) == ["(invoice.total_minor < 1500000 OR approval.granted == True)"]


def test_tightening_is_classified_and_rebuilds_the_same_things():
    db.use_memory()
    bootstrap(USE_CASE)
    r = update.apply_update("A", "large_order_approval", 5000, use_case=USE_CASE, dry_run=True)
    assert r["change"]["direction"] == "tighten" and r["class"] == 2
    assert {x["derived_id"] for x in r["rebuilt"]} == {"guardrail:d1:policy_value:h3", "guard:h3", "gate:A.approve_large_order"}
    assert r["questions_reasked"] == [] and r["contract"]["version"] == 2
    db.use_atlas()
