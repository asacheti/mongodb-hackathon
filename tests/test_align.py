"""Stage 2 contract from mock/STAGES.md. Offline: embedding, vector search and LLM are faked from the
fixture in mock/stages/2_alignments.json. One live test (-m live) hits Atlas + the LLM provider."""
import json
import math

import pytest

from pmp import align as al
from pmp import spec
from pmp.compile import compile_all

USE_CASE = "bnpl_checkout_v1"
EXPECTED = {  # (a_name, b_name): (sigma, proposal, confidence)
    ("create_customer", "createCustomer"): (0.93, "distinct", 0.88),
    ("finalize_invoice", "checkLoanCanBeProvided"): (0.34, "provides_input", 0.90),
    ("await_invoice_paid", "retrieveFinalizedPaymentPlan"): (0.41, "relate", 0.62),
    ("fulfil_order", "updateOrderStatus"): (0.71, "provides_input", 0.86),
    ("collect_payment", "END_not_eligible"): (0.12, "on_failure", 0.60),
}


@pytest.fixture(scope="module")
def subs():
    return compile_all(USE_CASE)[0]


@pytest.fixture(scope="module")
def fixture_rows():
    return json.loads(al.FIXTURE.read_text())["alignments"]


class Fakes:
    """Deterministic stand-ins. Search returns the fixture partner (twice, to exercise dedupe) plus two
    distractors; the LLM returns the fixture proposal for known pairs and a low-confidence 'distinct'
    for everything else, and garbage on its very first call to exercise the retry."""

    def __init__(self, rows, sub_b):
        self.partner = {r["a"]: r["b"] for r in rows}
        self.proposal = {(r["a"], r["b"]): r for r in rows}
        self.b_ids = [n["_id"] for n in sub_b["nodes"]]
        self.calls = {"embed": 0, "search": 0, "llm": 0}
        self.garbage_once = True

    def embed(self, texts):
        self.calls["embed"] += 1
        return [[math.sin(i + j / 7) for j in range(8)] for i, _ in enumerate(texts)]

    def search(self, vec, org_id, use_case, limit):
        self.calls["search"] += 1
        assert org_id == "B" and use_case == USE_CASE and limit == 5
        distractors = [b for b in self.b_ids if b not in self.partner.values()][:2]
        return [(b, 0.5) for b in distractors] + [(b, 0.9) for b in self.b_ids if b in self.partner.values()][:3] * 2

    def llm(self, prompt):
        self.calls["llm"] += 1
        if self.garbage_once:
            self.garbage_once = False
            return "not json at all"
        ctx = prompt.split("Step A (org A):")[1].split("\nYour previous answer")[0]
        a = json.loads(ctx.split("Step B (org B):")[0])["id"]
        b = json.loads(ctx.split("Step B (org B):")[1])["id"]
        r = self.proposal.get((a, b))
        if r:
            return json.dumps({k: r[k] for k in ("sigma", "iota", "tau", "proposal", "confidence", "rationale")})
        return json.dumps({"sigma": 0.2, "iota": "incompatible", "tau": "x/y", "proposal": "distinct",
                           "confidence": 0.3, "rationale": "unrelated"})


@pytest.fixture(scope="module")
def result(subs, fixture_rows):
    sub_b = next(s for s in subs if s["org_id"] == "B")
    fakes = Fakes(fixture_rows, sub_b)
    rows = al.align(subs, USE_CASE, embed=fakes.embed, search=fakes.search, llm=fakes.llm, store=None, workers=2)
    return rows, fakes


# ---- the Stage 2 contract ----------------------------------------------------

def test_five_expected_pairs_with_proposals(result):
    rows, _ = result
    got = {(r["a_name"], r["b_name"]): r for r in rows}
    assert set(got) == set(EXPECTED)
    for key, (sigma, proposal, conf) in EXPECTED.items():
        r = got[key]
        assert r["proposal"] == proposal, key
        assert abs(r["sigma"] - sigma) <= 0.1, key
        assert abs(r["confidence"] - conf) <= 0.15, key


def test_nothing_confirmed_yet_and_docs_validate(result):
    rows, _ = result
    for r in rows:
        assert r["confirmed_by"] is None
        assert r["use_case_id"] == USE_CASE
        assert r["_id"] == f"{USE_CASE}:{r['a']}~{r['b']}"
        assert spec.errors(r, "alignment") == []


def test_low_confidence_filtered_and_pairs_deduped(result):
    rows, fakes = result
    assert all(r["confidence"] >= al.MIN_CONFIDENCE for r in rows)
    assert all(r["proposal"] != "distinct" or r["sigma"] >= al.KEEP_DISTINCT_SIGMA for r in rows)
    assert al.cosine_from_atlas(0.8) == pytest.approx(0.6) and al.cosine_from_atlas(0.3) == 0.0
    assert len({(r["a"], r["b"]) for r in rows}) == len(rows)
    # the duplicate candidate ids from search must not have caused duplicate LLM calls either
    assert fakes.calls["embed"] == 1 and fakes.calls["search"] == 8
    assert fakes.calls["llm"] == len(set(al_pairs(fakes))) + 1  # +1 for the retry after garbage


def al_pairs(fakes):
    """Every (a, b) pair the aligner should have asked about, derived the same way it does."""
    return {(a, b) for a in A_CANDIDATES for b in set(x for x, _ in fakes.search(None, "B", USE_CASE, 5)) | set(B_ALWAYS)}


A_CANDIDATES = ["A.create_customer", "A.create_invoice", "A.add_items", "A.finalize_invoice", "A.collect_payment",
                "A.await_invoice_paid", "A.approve_large_order", "A.fulfil_order"]
B_ALWAYS = ["B.checkLoanCanBeProvided", "B.retrieveFinalizedPaymentPlan", "B.updateOrderStatus", "B.END_not_eligible"]


def test_candidate_selection(subs):
    sub_a = next(s for s in subs if s["org_id"] == "A")
    sub_b = next(s for s in subs if s["org_id"] == "B")
    assert [n["_id"] for n in al.a_candidates(sub_a)] == A_CANDIDATES
    assert al.b_always(sub_b) == B_ALWAYS


def test_node_context_has_neighborhood_and_effects(subs):
    sub_a = next(s for s in subs if s["org_id"] == "A")
    fin = next(n for n in sub_a["nodes"] if n["name"] == "finalize_invoice")
    ctx = al.node_context(fin, sub_a)
    assert ctx["predecessors"] == ["add_items"] and ctx["successors"] == ["collect_payment"]
    assert ctx["effect"] == "side_effect" and ctx["tool"] == "stripe.invoices.finalize"
    assert "invoice.total_minor:integer[cents]" in ctx["outputs"]
    assert "customer.email:string(pii)" in ctx["outputs"]
    assert ctx["handoffs"] == ["H1_request_financing"]
    prompt = al.build_prompt(ctx, ctx)
    assert '"proposal"' in prompt and "Never when either" in prompt and "Tool effects: A = side_effect" in prompt
    assert ctx["declared_handoffs"][0]["purpose"].startswith("Ask the lender")
    assert al.tau(ctx, {"effect": "verify"}) == "side_effect/verify"
    assert al.directions(ctx) == {"out"}
    assert al.can_provide_input(ctx, {"visibility": "handoff_in"}) and not al.can_provide_input(ctx, {"visibility": "internal"})
    assert al.name_similarity("create_customer", "createCustomer") == 1.0
    assert al.name_similarity("fulfil_order", "END_not_eligible") < 0.5


@pytest.mark.parametrize("raw,ok", [
    ('{"sigma": 0.5, "iota": "partial", "tau": "verify/verify", "proposal": "relate", "confidence": 0.7}', True),
    ('{"sigma": 1.5, "iota": "partial", "tau": "a/b", "proposal": "relate", "confidence": 0.7}', False),
    ('{"sigma": 0.5, "iota": "partial", "tau": "a/b", "proposal": "bogus", "confidence": 0.7}', False),
    ('{"sigma": 0.5, "tau": "a/b", "proposal": "relate", "confidence": 0.7}', False),
    ('[]', False),
    ('nope', False),
])
def test_parse_proposal(raw, ok):
    assert (al.parse_proposal(raw) is not None) is ok


def test_propose_retries_once_then_gives_up():
    calls = []
    def bad(prompt):
        calls.append(prompt)
        return "{}"
    assert al.propose({"id": "A.x"}, {"id": "B.y"}, bad) is None
    assert len(calls) == 2 and "previous answer was not a valid JSON" in calls[1]


def test_fixture_loads_and_validates():
    rows = al.load_fixture(USE_CASE)
    assert {(r["a_name"], r["b_name"]) for r in rows} == set(EXPECTED)
    assert "create_customer" in al.table(rows)


def test_models_follow_base_url(monkeypatch):
    monkeypatch.delenv("PMP_EMBED_MODEL", raising=False)
    monkeypatch.delenv("PMP_LLM_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    assert al.embed_model() == "openai/text-embedding-3-small" and al.llm_model() == "openai/gpt-4.1"
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    assert al.embed_model() == "text-embedding-3-small"


# ---- live ------------------------------------------------------------------------

@pytest.mark.live
def test_live_align_against_atlas_and_llm():
    from pmp import db
    assert db.col("submissions").count_documents({"use_case_id": USE_CASE}) == 2, "run pmp.compile first"
    rows = al.run(USE_CASE, dry_run=True)
    got = {(r["a_name"], r["b_name"]): r["proposal"] for r in rows}
    hits = sum(got.get(k) == v[1] for k, v in EXPECTED.items())
    print(al.table(rows))
    assert got.get(("finalize_invoice", "checkLoanCanBeProvided")) == "provides_input"
    assert hits >= 3, f"only {hits}/5 expected proposals reproduced live: {got}"
    assert db.col("steps_vec").count_documents({"use_case_id": USE_CASE}) == 20
