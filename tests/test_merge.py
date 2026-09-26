"""Stage 3a contract: deterministic union with boundary edges h1..h3. Offline."""
import pytest

from pmp import merge, spec
from pmp.align import load_fixture
from pmp.compile import compile_all

USE_CASE = "bnpl_checkout_v1"


@pytest.fixture(scope="module")
def merged():
    subs, _ = compile_all(USE_CASE)
    return merge.build(subs, load_fixture(USE_CASE), USE_CASE, 1)


def test_union_carries_everything(merged):
    assert merged["_id"] == f"{USE_CASE}:v1" and merged["version"] == 1 and merged["doc_type"] == "graph"
    assert len(merged["nodes"]) == 20
    assert len(merged["edges"]) == 11 + 12 + 3
    assert {n["org_id"] for n in merged["nodes"]} == {"A", "B"}
    for n in merged["nodes"]:
        assert spec.errors(n, "node") == []
    for e in merged["edges"]:
        assert spec.errors(e, "transition") == []


def test_boundary_edges_h1_h2_h3(merged):
    b = {e["_id"]: e for e in merged["boundary_edges"]}
    assert list(b) == ["h1", "h2", "h3"]
    assert (b["h1"]["from"], b["h1"]["to"], b["h1"]["direction"]) == ("A.finalize_invoice", "B.checkLoanCanBeProvided", "A->B")
    assert (b["h2"]["from"], b["h2"]["to"], b["h2"]["direction"]) == ("B.retrieveFinalizedPaymentPlan", "A.await_invoice_paid", "B->A")
    assert (b["h3"]["from"], b["h3"]["to"], b["h3"]["direction"]) == ("A.fulfil_order", "B.updateOrderStatus", "A->B")
    assert b["h1"]["handoff_out"]["id"] == "H1_request_financing" and b["h1"]["handoff_in"]["id"] == "L1_receive_basket"
    assert b["h2"]["handoff_out"]["id"] == "L2_return_plan" and b["h2"]["handoff_in"]["id"] == "H2_receive_plan"
    assert b["h3"]["handoff_out"]["id"] == "H3_report_fulfilled" and b["h3"]["handoff_in"]["id"] == "L3_receive_fulfilment"
    assert b["h1"]["condition"] == {"op": "eq", "args": ["checkout.payment_choice", "pay_later"]}
    for e in b.values():
        assert e["type"] == "boundary" and e["org_id"] == "M"
        assert e["allowlist"] is None and e["guard"] is None and e["adapter"] is None
    assert {f["path"] for f in b["h1"]["sender_fields"]} >= {"line_item.unit_amount_minor", "line_item.price_id", "customer.email"}
    assert {f["path"] for f in b["h1"]["receiver_fields"]} >= {"products.purchaseAmount", "products.productCode"}


def test_on_failure_is_a_candidate_not_an_edge(merged):
    assert merged["failure_candidates"] == [{
        "from": "B.END_not_eligible", "to": "A.collect_payment",
        "alignment": f"{USE_CASE}:A.collect_payment~B.END_not_eligible",
        "rationale": merged["failure_candidates"][0]["rationale"]}]
    assert not any(e["from"] == "B.END_not_eligible" for e in merged["edges"])
    assert merged["merge_candidates"] == []
    assert len(merged["alignments_used"]) == 4      # distinct is not used by the merge


def test_deterministic_and_hashes(merged):
    subs, _ = compile_all(USE_CASE)
    again = merge.build(subs, load_fixture(USE_CASE), USE_CASE, 1)
    assert again["hash"] == merged["hash"]
    assert merged["inputs"]["h_A"].startswith("sha256:") and merged["inputs"]["h_alignments"].startswith("sha256:")
    assert merged["inputs"]["h_A"] == next(s["hash"] for s in subs if s["org_id"] == "A")


def test_adjacency_docs(merged):
    adj = {d["node_id"]: d for d in merge.adjacency(merged)}
    assert len(adj) == 20 and all(d["doc_type"] == "node" and d["version"] == 1 for d in adj.values())
    assert "B.checkLoanCanBeProvided" in adj["A.finalize_invoice"]["next"]      # crosses h1
    assert adj["A.T_fulfilled"]["terminal"] and adj["B.END_not_eligible"]["terminal"]
    assert adj["B.updateOrderStatus"]["terminal"]                             # last step carries the outcome
    assert adj["A.create_customer"]["next"] == ["A.create_invoice"]


def test_relate_alignment_still_pairs_declared_handoffs(merged):
    """h2 comes from the await_invoice_paid ~ retrieveFinalizedPaymentPlan row even though it is 'relate':
    A declared H2 (in) at that step and B declared L2 (out)."""
    h2 = next(e for e in merged["boundary_edges"] if e["_id"] == "h2")
    assert h2["proposal"] == "relate"
