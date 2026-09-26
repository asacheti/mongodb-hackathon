"""Stage 2: align steps across the two vocabularies. The ONLY module that calls an LLM.

1. Embed every node's `text` (text-embedding-3-small, 1536 dims) into `steps_vec`.
2. For each A node that is handoff-visible, declares a handoff, or has a side-effecting tool, run
   $vectorSearch on `steps_vec_idx` filtered to org B (numCandidates 50, limit 5). B's own
   handoff-visible nodes are always added as candidates: a decline terminal is never "similar"
   to anything by embedding, yet it is exactly what the merchant has to react to.
3. For each candidate pair ask the LLM for strict JSON {sigma, iota, tau, proposal, confidence,
   rationale}; retry once on invalid JSON. Keep confidence >= 0.5, dedupe by (a, b).
4. Write `alignments` with confirmed_by null. Nothing here is a decision.

CLI: python -m pmp.align --use-case bnpl_checkout_v1 [--dry-run] [--from-fixture]
"""
from __future__ import annotations
import argparse
import difflib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from pmp import spec

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "mock" / "stages" / "2_alignments.json"
DEFAULT_USE_CASE = "bnpl_checkout_v1"
VECTOR_INDEX = "steps_vec_idx"
PROPOSALS = ("distinct", "provides_input", "relate", "on_failure", "merge")
MIN_CONFIDENCE = 0.5
KEEP_DISTINCT_SIGMA = 0.6   # a "distinct" proposal is only worth recording for a near-duplicate pair
NEAR_DUPLICATE_NAME = 0.8   # name similarity at/above this floors sigma (create_customer ~ createCustomer)
SEARCH_LIMIT, NUM_CANDIDATES = 5, 50

EmbedFn = Callable[[list[str]], list[list[float]]]
SearchFn = Callable[[list[float], str, str, int], list[tuple[str, float]]]
LlmFn = Callable[[str], str]
StoreFn = Callable[[list[dict]], None]


# --------------------------------------------------------------------------- models / providers

def _openrouter() -> bool:
    return "openrouter" in os.environ.get("OPENAI_BASE_URL", "")


def embed_model() -> str:
    return os.environ.get("PMP_EMBED_MODEL") or ("openai/" if _openrouter() else "") + "text-embedding-3-small"


def llm_model() -> str:
    # gpt-4o-mini missed the two handoff pairs and echoed n/a effects; gpt-4.1 reproduces Stage 2 for ~$0.25 a run
    return os.environ.get("PMP_LLM_MODEL") or ("openai/" if _openrouter() else "") + "gpt-4.1"


def _client():
    from openai import OpenAI
    return OpenAI()


def openai_embed(texts: list[str]) -> list[list[float]]:
    r = _client().embeddings.create(model=embed_model(), input=texts)
    return [d.embedding for d in sorted(r.data, key=lambda d: d.index)]


def openai_llm(prompt: str) -> str:
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = _client().chat.completions.create(
                model=llm_model(), temperature=0, response_format={"type": "json_object"},
                messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}])
            return r.choices[0].message.content or ""
        except Exception as e:  # rate limit / transient
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"LLM call failed after retries: {last}")


def atlas_search(vec: list[float], org_id: str, use_case: str, limit: int = SEARCH_LIMIT) -> list[tuple[str, float]]:
    from pmp import db
    pipeline = [
        {"$vectorSearch": {"index": VECTOR_INDEX, "path": "embedding", "queryVector": vec,
                           "numCandidates": NUM_CANDIDATES, "limit": limit,
                           "filter": {"org_id": org_id, "use_case_id": use_case}}},
        {"$project": {"_id": 1, "score": {"$meta": "vectorSearchScore"}}},
    ]
    return [(d["_id"], float(d["score"])) for d in db.col("steps_vec").aggregate(pipeline)]


def atlas_store(vec_docs: list[dict]) -> None:
    """Replace this use case's vectors and block until the search index has caught up."""
    from pmp import db
    use_case = vec_docs[0]["use_case_id"]
    col = db.col("steps_vec")
    col.delete_many({"use_case_id": use_case})
    col.insert_many(vec_docs)
    probe = next(d for d in vec_docs if d["org_id"] == "B")
    want = min(SEARCH_LIMIT, sum(d["org_id"] == "B" for d in vec_docs))
    deadline = time.time() + 120
    while time.time() < deadline:
        if len(atlas_search(probe["embedding"], "B", use_case)) >= want:
            return
        time.sleep(2)
    raise TimeoutError("steps_vec_idx did not index the new vectors in time")


# --------------------------------------------------------------------------- contexts + prompt

def _field(f: dict) -> str:
    s = f"{f['path']}:{f['type']}"
    if f.get("unit"):
        s += f"[{f['unit']}]"
    if f.get("sensitivity"):
        s += f"({f['sensitivity']})"
    return s


def _handoff_summary(h: dict) -> dict:
    out = {"id": h.get("id"), "direction": h.get("direction"), "purpose": h.get("purpose")}
    for k in ("payload", "accept", "expects_back", "returns"):
        if k in h:
            out[k] = sorted(h[k]) if isinstance(h[k], dict) else h[k]
    return out


def node_context(node: dict, sub: dict) -> dict:
    names = {n["_id"]: n["name"] for n in sub["nodes"]}
    tool = node.get("tool") or {}
    # A's handoffs were resolved onto nodes at compile time; B's profile declares them by step name
    mine = set(node.get("handoffs", []))
    handoffs = [_handoff_summary(h) for h in sub.get("handoffs", [])
                if h.get("id") in mine or h.get("step") == node["name"]]
    fallback_for = [e["case"] for e in sub.get("exceptions", []) if node["name"] in str(e.get("action", ""))
                    or node.get("source", {}).get("step", "") and f"({node['source']['step']})" in str(e.get("action", ""))]
    return {
        "id": node["_id"], "name": node["name"], "kind": node["kind"], "visibility": node["visibility"],
        "org_role": "merchant" if node["org_id"] == "A" else "lender",
        "text": node["text"],
        "declared_handoffs": handoffs,
        "declared_fallback_for": fallback_for,
        "inputs": [_field(f) for f in node.get("inputs", [])],
        "outputs": [_field(f) for f in node.get("outputs", [])],
        "tool": f"{tool['server']}.{tool['name']}" if tool else None,
        "effect": tool.get("effect", "n/a") if tool else "n/a",
        "money": bool(tool.get("money")),
        "predecessors": sorted(names[t["from"]] for t in sub["transitions"] if t["to"] == node["_id"]),
        "successors": sorted(names[t["to"]] for t in sub["transitions"] if t["from"] == node["_id"]),
        "handoffs": node.get("handoffs", []),
        "outcome": node.get("outcome"),
    }


SYSTEM = ("You align steps from two organizations' private procedures so a mediator can merge them. "
          "You see one step from org A (a merchant) and one from org B (a lender). Answer with one JSON object only.")

PROMPT = """Use case: a merchant (org A, Stripe invoicing) and a lender (org B, buy-now-pay-later loans) are merging
their procedures for financed checkout. Compare step A and step B and return a JSON object with exactly these keys:

- "sigma": number 0..1, how similar the two steps are by name + description (what they are about), regardless
   of whether they mean the same thing. Two steps both called "create customer" score high even if one is a
   billing record and the other an eligibility check. An embedding similarity hint is given when available.
- "iota": input/output compatibility: "compatible" | "partial" | "incompatible" | "missing:<field>" | "n/a".
   "partial" when the same information crosses in different units or field names. "missing:<field>" when B
   needs one specific field that A never produces (name it). "n/a" when either step is a terminal.
- "proposal": exactly one of
   "provides_input": what A produces is what B consumes to start, or what B returns is what A waits for.
      Decisive evidence: A declares a handoff OUT (to the lender) and B declares a handoff IN (from the merchant)
      about the same object (a basket, a fulfilment report, a plan), or vice versa. Different units or field
      names do NOT change this; record them in iota as "partial" and still propose provides_input.
   "on_failure": B is a decline / failure terminal and A is the step the merchant runs INSTEAD as the fallback.
      Only when A's own description or declared_fallback_for says it is that fallback. Do not propose it for
      steps that merely precede the failure.
   "relate": rare. Both steps observe the same event from two vantage points (e.g. one waits for "paid", the
      other retrieves a finalized plan), both keep running, neither feeds the other directly, and both are
      pure or verify steps. A step that merely happens before or after the other in the joint flow is "distinct".
   "merge": same meaning AND both tool effects are pure or verify, so one run can serve both. Never when either
      effect is side_effect.
   "distinct": unrelated, or same-looking but different meaning; both stay as they are.
- "confidence": number 0..1 for the proposal.
- "rationale": one sentence.

Tool effects: A = {a_effect}, B = {b_effect}.{hint}

Step A (org A):
{a}

Step B (org B):
{b}
"""


def cosine_from_atlas(score: float) -> float:
    """Atlas vectorSearchScore for cosine is (1 + cos) / 2; unrelated text sits near 0.6. Map back to cos, clipped to 0..1."""
    return max(0.0, min(1.0, 2 * score - 1))


def build_prompt(a_ctx: dict, b_ctx: dict, vector_score: float | None = None) -> str:
    hint = (f" Embedding similarity hint (0 = unrelated, 1 = identical text): {cosine_from_atlas(vector_score):.2f}."
            if vector_score is not None else "")
    return PROMPT.format(a=json.dumps(a_ctx, indent=1), b=json.dumps(b_ctx, indent=1),
                         a_effect=a_ctx.get("effect", "n/a"), b_effect=b_ctx.get("effect", "n/a"), hint=hint)


def name_similarity(a: str, b: str) -> float:
    """Deterministic floor for sigma: create_customer vs createCustomer -> 1.0."""
    norm = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())
    return round(difflib.SequenceMatcher(None, norm(a), norm(b)).ratio(), 2)


def directions(ctx: dict) -> set[str]:
    """Which way this node can hand work across the boundary, from its visibility tag and declared handoffs."""
    out = {"out"} if ctx.get("visibility") == "handoff_out" else {"in"} if ctx.get("visibility") == "handoff_in" else set()
    out |= {h["direction"] for h in ctx.get("declared_handoffs", []) if h.get("direction") in ("in", "out")}
    return out


def can_provide_input(a_ctx: dict, b_ctx: dict) -> bool:
    """Input crosses the boundary only from a handoff-out node into a handoff-in node, in either direction."""
    da, db_ = directions(a_ctx), directions(b_ctx)
    return ("out" in da and "in" in db_) or ("out" in db_ and "in" in da)


def tau(a_ctx: dict, b_ctx: dict) -> str:
    """Deterministic: the two tool effect classes. Terminals and decisions have no tool -> n/a."""
    return f"{a_ctx.get('effect', 'n/a')}/{b_ctx.get('effect', 'n/a')}"


def parse_proposal(raw: str) -> dict | None:
    try:
        d = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("proposal") not in PROPOSALS:
        return None
    try:
        sigma, conf = float(d["sigma"]), float(d["confidence"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= sigma <= 1 and 0 <= conf <= 1) or not isinstance(d.get("iota"), str):
        return None
    return {"sigma": round(sigma, 2), "iota": d["iota"], "tau": str(d.get("tau", "")), "proposal": d["proposal"],
            "confidence": round(conf, 2), "rationale": str(d.get("rationale", ""))}


def propose(a_ctx: dict, b_ctx: dict, llm: LlmFn, vector_score: float | None = None) -> dict | None:
    prompt = build_prompt(a_ctx, b_ctx, vector_score)
    p = parse_proposal(llm(prompt))
    if p is None:
        p = parse_proposal(llm(prompt + "\nYour previous answer was not a valid JSON object with the required keys. "
                                        "Return only the JSON object."))
    if p:
        p["tau"] = tau(a_ctx, b_ctx)
        ns = name_similarity(a_ctx.get("name", ""), b_ctx.get("name", ""))
        if ns >= NEAR_DUPLICATE_NAME:  # only a genuinely shared name floors sigma; random names score ~0.3
            p["sigma"] = max(p["sigma"], ns)
        if p["proposal"] == "merge" and "side_effect" in p["tau"]:
            p["proposal"], p["rationale"] = "relate", p["rationale"] + " (merge downgraded: a side-effecting step never merges)"
        if p["proposal"] == "relate" and "side_effect" in p["tau"]:
            p["proposal"], p["rationale"] = "distinct", p["rationale"] + " (relate downgraded: cousins are verify/pure steps)"
        if p["proposal"] == "provides_input" and not can_provide_input(a_ctx, b_ctx):
            p["proposal"], p["rationale"] = "distinct", p["rationale"] + " (provides_input downgraded: no handoff-out -> handoff-in pairing)"
    return p


# --------------------------------------------------------------------------- selection

def a_candidates(sub_a: dict) -> list[dict]:
    """A nodes worth aligning: handoff-visible, declaring a handoff, or side-effecting."""
    return [n for n in sub_a["nodes"]
            if n["visibility"] != "internal" or n.get("handoffs") or (n.get("tool") or {}).get("effect") == "side_effect"]


def b_always(sub_b: dict) -> list[str]:
    return [n["_id"] for n in sub_b["nodes"] if n["visibility"] != "internal"]


# --------------------------------------------------------------------------- pipeline

def align(submissions: list[dict], use_case: str = DEFAULT_USE_CASE, *, embed: EmbedFn, search: SearchFn, llm: LlmFn,
          store: StoreFn | None = None, min_conf: float = MIN_CONFIDENCE, workers: int = 6) -> list[dict]:
    subs = {s["org_id"]: s for s in submissions}
    sub_a, sub_b = subs["A"], subs["B"]
    all_nodes = [(n, sub_a) for n in sub_a["nodes"]] + [(n, sub_b) for n in sub_b["nodes"]]
    vectors = embed([n["text"] for n, _ in all_nodes])
    vec_docs = [{"_id": n["_id"], "org_id": n["org_id"], "use_case_id": use_case, "name": n["name"],
                 "text": n["text"], "embedding": v} for (n, _), v in zip(all_nodes, vectors)]
    by_id = {d["_id"]: d for d in vec_docs}
    if store:
        store(vec_docs)

    b_nodes = {n["_id"]: n for n in sub_b["nodes"]}
    pairs: list[tuple[dict, dict, float | None]] = []
    seen: set[tuple[str, str]] = set()
    for a in a_candidates(sub_a):
        hits = dict(search(by_id[a["_id"]]["embedding"], "B", use_case, SEARCH_LIMIT))
        for b_id in list(hits) + [x for x in b_always(sub_b) if x not in hits]:
            if (a["_id"], b_id) in seen or b_id not in b_nodes:
                continue
            seen.add((a["_id"], b_id))
            pairs.append((a, b_nodes[b_id], hits.get(b_id)))

    def work(item):
        a, b, score = item
        p = propose(node_context(a, sub_a), node_context(b, sub_b), llm, score)
        return (a, b, score, p)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        results = list(ex.map(work, pairs))

    best: dict[tuple[str, str], dict] = {}
    for a, b, score, p in results:
        if not p or p["confidence"] < min_conf:
            continue
        if p["proposal"] == "distinct" and p["sigma"] < KEEP_DISTINCT_SIGMA:
            continue  # unrelated pair: nothing to record
        doc = {"_id": f"{use_case}:{a['_id']}~{b['_id']}", "use_case_id": use_case,
               "a": a["_id"], "b": b["_id"], "a_name": a["name"], "b_name": b["name"], **p,
               "vector_score": round(score, 4) if score is not None else None, "confirmed_by": None}
        spec.validate(doc, "alignment")
        key = (doc["a"], doc["b"])
        if key not in best or doc["confidence"] > best[key]["confidence"]:
            best[key] = doc
    order = {n["_id"]: i for i, (n, _) in enumerate(all_nodes)}
    return sorted(best.values(), key=lambda d: (order[d["a"]], order[d["b"]]))


def write(alignments: list[dict], use_case: str) -> None:
    from pmp import db
    db.col("alignments").delete_many({"use_case_id": use_case})
    if alignments:
        db.col("alignments").insert_many(alignments)


def load_fixture(use_case: str = DEFAULT_USE_CASE) -> list[dict]:
    rows = json.loads(FIXTURE.read_text())["alignments"]
    for r in rows:
        r["use_case_id"] = use_case
        r["_id"] = f"{use_case}:{r['a']}~{r['b']}"
        spec.validate(r, "alignment")
    return rows


def table(rows: list[dict]) -> str:
    head = f"{'A step':<22} {'B step':<32} {'σ':>5} {'ι':<26} {'τ':<24} {'proposal':<15} {'conf':>5}"
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append(f"{r['a_name']:<22} {r['b_name']:<32} {r['sigma']:>5.2f} {r['iota']:<26} {r['tau']:<24} "
                     f"{r['proposal']:<15} {r['confidence']:>5.2f}")
    return "\n".join(lines)


def run(use_case: str = DEFAULT_USE_CASE, *, dry_run: bool = False, workers: int = 6) -> list[dict]:
    from pmp import db
    subs = list(db.col("submissions").find({"use_case_id": use_case}))
    if len(subs) != 2:
        raise SystemExit(f"expected 2 submissions for {use_case}, found {len(subs)}; run pmp.compile first")
    rows = align(subs, use_case, embed=openai_embed, search=atlas_search, llm=openai_llm, store=atlas_store,
                 workers=workers)
    if not dry_run:
        write(rows, use_case)
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Stage 2: propose step alignments between org A and org B.")
    p.add_argument("--use-case", default=DEFAULT_USE_CASE)
    p.add_argument("--dry-run", action="store_true", help="embed + search + propose, print the table, write nothing to `alignments`")
    p.add_argument("--from-fixture", action="store_true", help="skip the LLM; load mock/stages/2_alignments.json")
    p.add_argument("--workers", type=int, default=6)
    a = p.parse_args(argv)
    t0 = time.time()
    if a.from_fixture:
        rows = load_fixture(a.use_case)
        if not a.dry_run:
            write(rows, a.use_case)
    else:
        rows = run(a.use_case, dry_run=a.dry_run, workers=a.workers)
    print(table(rows))
    print(f"{len(rows)} alignments ({'dry run' if a.dry_run else 'written'}) in {time.time() - t0:.1f}s; "
          f"models {embed_model()} / {llm_model()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
