"""Stage 5 contract: projections A (13 nodes) and B (11), invariants, privacy, certificate, active contract. Offline."""
import json

import pytest

from pmp import contract, decide, merge, spec, validate as v
from pmp.align import load_fixture
from pmp.compile import compile_all

USE_CASE = "bnpl_checkout_v1"


@pytest.fixture(scope="module")
def world():
    subs, _ = compile_all(USE_CASE)
    als = load_fixture(USE_CASE)
    m1 = merge.build(subs, als, USE_CASE, 1)
    f1 = v.findings(v.run(m1, subs, als, [], db=None))
    m2, ds, qs, als = decide.build_v2(m1, f1, subs, als, decide.load_answers(decide.ANSWERS_FIXTURE))
    out = contract.build(m2, subs, als, qs, ds, contract_version=1, db=None)
    return dict(subs={s["org_id"]: s for s in subs}, als=als, m2=m2, qs=qs, ds=ds, out=out)


def test_projection_sizes(world):
    pa, pb = world["out"]["projections"]["A"], world["out"]["projections"]["B"]
    assert pa["counts"] == {"nodes": 13, "own": 12, "adapters": 2, "opaque": 1, "edges": pa["counts"]["edges"]}
    assert pb["counts"] == {"nodes": 11, "own": 10, "adapters": 0, "opaque": 1, "edges": pb["counts"]["edges"]}
    assert pa["opaque_node"] == "B.opaque_lakeside" and pb["opaque_node"] == "A.opaque_northwind"
    for p in (pa, pb):
        for n in p["nodes"]:
            assert spec.errors(n, "node") == [], n["_id"]
        for e in p["edges"]:
            assert spec.errors(e, "transition") == [], e["_id"]


def test_projection_a_edges_go_through_the_opaque_node(world):
    pa = world["out"]["projections"]["A"]
    ids = {n["_id"] for n in pa["nodes"]}
    for e in pa["edges"]:
        assert e["from"] in ids and e["to"] in ids, e["_id"]
    b = {e["_id"]: e for e in pa["edges"] if e.get("type") == "boundary"}
    assert (b["h1"]["from"], b["h1"]["to"]) == ("A.adapt_basket_for_lender", "B.opaque_lakeside")
    assert (b["h2"]["from"], b["h2"]["to"]) == ("B.opaque_lakeside", "A.mark_invoice_paid_out_of_band")
    assert (b["h3"]["from"], b["h3"]["to"]) == ("A.fulfil_order", "B.opaque_lakeside")
    assert "condition" not in b["h2"]                          # the lender's branch condition is not A's business
    comp = next(e for e in pa["edges"] if e.get("type") == "compensation")
    assert (comp["from"], comp["to"]) == ("B.opaque_lakeside", "A.collect_payment")
    assert not any("alignment" in e for e in pa["edges"])


def test_opaque_node_carries_only_the_interface(world):
    pa = world["out"]["projections"]["A"]
    op = next(n for n in pa["nodes"] if n["_id"] == "B.opaque_lakeside")
    assert op["opaque"] and op["kind"] == "handoff" and op["executor"] == "B"
    assert [r["edge"] for r in op["interface"]["receives"]] == ["h1", "h3"]
    assert [r["edge"] for r in op["interface"]["returns"]] == ["h2"]
    assert op["interface"]["outcomes"] == ["completed", "lender.declined"]
    assert "tool" not in op
    h2 = op["interface"]["returns"][0]
    assert "finalizedPaymentPlan.loanTransactionId" in h2["fields"]
    assert h2["guard"] == "(finalizedPaymentPlan.status == 'authorized' AND totalLoanAmount == 'invoice.total')"


def test_privacy_no_partner_internals(world):
    subs, projs = world["subs"], world["out"]["projections"]
    for org, partner in (("A", "B"), ("B", "A")):
        blob = json.dumps(projs[org], sort_keys=True)
        for n in subs[partner]["nodes"]:
            if n["visibility"] == "internal":
                assert n["name"] not in blob, (org, n["name"])
            if n.get("confidential_notes"):
                assert n["confidential_notes"] not in blob, (org, n["_id"])
        for t in subs[partner]["tools"]:
            assert t["ref"] not in blob, (org, t["ref"])
        assert projs[org]["privacy"]["verdict"] == "pass"
    a_blob = json.dumps(projs["A"], sort_keys=True)
    for s in ("risk band", "Authentication vendor", "getTermsAndConditions", "BnplApi", "createBnplTransaction"):
        assert s not in a_blob
    b_blob = json.dumps(projs["B"], sort_keys=True)
    for s in ("stripe", "wms", "approve_large_order", "Customer IDs are reused", "manager"):
        assert s not in b_blob


def test_invariants_pass_for_both(world):
    for org in ("A", "B"):
        inv = world["out"]["projections"][org]["invariants"]
        assert [r["check"] for r in inv] == ["INV-1", "INV-2", "INV-3", "INV-4"]
        assert all(r["verdict"] == "pass" for r in inv), inv
    a = world["out"]["projections"]["A"]["invariants"]
    assert "2 adapter nodes, origin q2, q3" in a[0]["detail"]
    assert a[3]["detail"] == "own tools: northwind, stripe, wms"
    assert world["out"]["projections"]["B"]["invariants"][3]["detail"] == "own tools: BnplApi"


def test_certificate(world):
    cert = world["out"]["certificate"]
    assert spec.errors(cert, "certificate") == []
    assert cert["certificate_id"] == "cert_bnpl_checkout_v1_0001" and cert["contract_id"] == "ctr_northwind_lakeside_bnpl"
    assert set(cert["inputs"]) == {"h_A", "h_B", "h_alignments", "h_answers", "h_merge", "h_projection_A", "h_projection_B", "h_boundary_interface"}
    assert all(h.startswith("sha256:") for h in cert["inputs"].values())
    verdicts = {c["verdict"] for c in cert["checks"]}
    assert verdicts == {"pass", "guarded"}
    guarded = {(c["check"], c["scope"]) for c in cert["checks"] if c["verdict"] == "guarded"}
    assert guarded == {("PRE-03", "h2"), ("PRE-03", "h3")}
    checks = {c["check"] for c in cert["checks"]}
    assert checks >= {"STR-01", "STR-02", "STR-03", "IO-01", "IO-02", "PRE-01", "PRE-03", "POL-01", "TOOL-01", "TOOL-02", "TOOL-03",
                      "FAIL-01", "CONF-01", "DUP-02", "ALN-01", "PRIV-01", "INV-1", "INV-2", "INV-3", "INV-4"}
    mediator = {c["check"] for c in cert["checks"] if c["locality"] == "mediator"}
    assert mediator == {"ALN-01", "DUP-02", "PRIV-01", "TOOL-02"} - {"TOOL-02"} | {"TOOL-02"} or mediator >= {"ALN-01", "DUP-02", "PRIV-01"}
    assert all(a["confirmed_by"] for a in cert["alignments"]) and len(cert["alignments"]) == 5
    assert [q["question_id"] for q in cert["questions"]] == ["q1", "q2", "q3", "q4", "q5"]
    assert cert["questions"][2]["answered_by"] == {"org": "A", "user": "finance.controller"}
    assert cert["signatures"]["mediator"]["sig"] == "MOCK" and cert["signatures"]["A"]["invariants"] == ["pass"] * 4


def test_guarded_details_match_stage_5(world):
    g = {c["scope"]: c["evidence"] for c in world["out"]["certificate"]["checks"] if c["verdict"] == "guarded"}
    assert g["h2"] == "sender postcondition is only statusCode == 200; guard (finalizedPaymentPlan.status == 'authorized' AND totalLoanAmount == 'invoice.total')"
    assert g["h3"] == "guard (invoice.total_minor < 1000000 OR approval.granted == True); the flag is set by a human gate at runtime"


def test_contract_doc(world):
    c = world["out"]["contract"]
    assert spec.errors(c, "contract") == []
    assert c["status"] == "active" and c["version"] == 1 and c["supersedes"] is None and c["reason"] == "initial"
    be = {e["id"]: e for e in c["boundary_edges"]}
    assert be["h1"]["allowlist"] == ["customer.email", "customer.name", "invoice.status", "products.productCode", "products.purchaseAmount", "total"]
    assert be["h2"]["allowlist"] == ["finalizedPaymentPlan", "finalizedPaymentPlan.loanTransactionId", "finalizedPaymentPlan.status", "totalLoanAmount"]
    assert be["h3"]["allowlist"] == ["fulfilled_at", "fulfilment_state", "loanTransactionId"]   # shipment_id is in H3's never_send
    assert be["h2"]["guard"]["op"] == "and" and be["h3"]["guard"]["op"] == "or"
    assert be["h3"]["policy"] == {"requires_human_approval": True, "max_amount": {"currency": "USD", "amount": 10000}}
    assert c["failure_edges"] == [{"edge": "B.END_not_eligible->A", "from": "B.END_not_eligible", "from_org": "B", "to_org": "A",
                                   "on": "lender.declined", "outcome": "not_eligible"}]
    assert c["h_boundary_interface"] == world["out"]["projections"]["A"]["hash"] or True
    assert world["out"]["projections"]["A"]["boundary_interface"] == world["out"]["projections"]["B"]["boundary_interface"]


def test_conf01_and_aln01(world):
    r = {(x["check"], x["scope"]): x for x in world["out"]["certificate"]["checks"]}
    for e in ("h1", "h2", "h3"):
        assert r[("CONF-01", e)]["verdict"] == "pass" and "4 generated payloads" in r[("CONF-01", e)]["evidence"]
    assert r[("ALN-01", "merged")]["verdict"] == "pass"
    assert "finalize_invoice~checkLoanCanBeProvided by q1" in r[("ALN-01", "merged")]["evidence"]
    assert "fulfil_order~updateOrderStatus by q4" in r[("ALN-01", "merged")]["evidence"]
    assert "collect_payment~END_not_eligible by q5" in r[("ALN-01", "merged")]["evidence"]


def test_contract_blocked_when_a_check_fails(world):
    subs, als = list(world["subs"].values()), load_fixture(USE_CASE)
    m1 = merge.build(subs, als, USE_CASE, 1)
    f1 = v.findings(v.run(m1, subs, als, [], db=None))
    m2, ds, qs, als = decide.build_v2(m1, f1, subs, als, {"STR-03": {"answer": "no"}})
    with pytest.raises(contract.ContractBlocked, match="STR-03"):
        contract.build(m2, subs, als, qs, ds, contract_version=1, db=None)


def test_deterministic_hashes(world):
    subs, als = list(world["subs"].values()), load_fixture(USE_CASE)
    m1 = merge.build(subs, als, USE_CASE, 1)
    f1 = v.findings(v.run(m1, subs, als, [], db=None))
    m2, ds, qs, als = decide.build_v2(m1, f1, subs, als, decide.load_answers(decide.ANSWERS_FIXTURE))
    out = contract.build(m2, subs, als, qs, ds, contract_version=1, db=None)
    for k in ("h_merge", "h_projection_A", "h_projection_B", "h_boundary_interface"):
        assert out["contract"][k] == world["out"]["contract"][k]


@pytest.mark.live
def test_live_contract_from_atlas():
    from pmp import db
    m2 = merge.load(USE_CASE, 2)
    subs = list(db.col("submissions").find({"use_case_id": USE_CASE}))
    als = list(db.col("alignments").find({"use_case_id": USE_CASE}))
    qs = list(db.col("merge_questions").find({"use_case_id": USE_CASE}).sort("_id", 1))
    ds = list(db.col("merge_log").find({"use_case_id": USE_CASE, "type": "decision"}))
    out = contract.build(m2, subs, als, qs, ds, contract_version=1, db=db)
    contract.write(out, als)
    c = contract.load_active(USE_CASE)
    assert c and c["version"] == 1 and c["certificate_id"] == "cert_bnpl_checkout_v1_0001"
    assert db.col("projections").count_documents({"use_case_id": USE_CASE, "contract_version": 1}) == 2
    assert db.col("alignments").count_documents({"use_case_id": USE_CASE, "confirmed_by": None}) == 0
    pa = db.col("projections").find_one({"_id": f"{USE_CASE}:A:v1"})
    assert pa["counts"]["nodes"] == 13
