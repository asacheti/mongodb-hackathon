"""Stage 3a: deterministic union of the two submissions over the alignment record.

Same submissions + same alignments = same merged graph, byte for byte. No LLM.

- Every node of both orgs is carried (the mediator sees everything; projections filter later).
- Every local transition is carried.
- Boundary edges h1..hn are created for provides_input / relate alignments whose two nodes form a
  handoff-out -> handoff-in pairing (from visibility tags and the orgs' declared handoffs). They are
  numbered along org A's flow order. Nothing crosses them yet: allowlist, guard and adapter are null
  until decide (Stage 4) fills them from answers.
- on_failure alignments are recorded as failure_candidates, not edges: a compensation edge is a
  customer-facing decision, so FAIL-01 must raise it and a human (or a signed default) must add it.
- merge alignments are recorded as merge_candidates (none in this use case).

Atlas layout in `merged`: one graph doc {_id "<use_case>:v<version>", doc_type "graph", ...} plus one
adjacency doc per node ({doc_type "node", node_id, next: [...]}) so STR-01 can run $graphLookup.

CLI: python -m pmp.merge --use-case bnpl_checkout_v1 [--version 1] [--dry-run] [--alignments-from-fixture]
"""
from __future__ import annotations
import argparse
import copy
import sys

from pmp import spec
from pmp.compile import jsonable, sha256

DEFAULT_USE_CASE = "bnpl_checkout_v1"
BOUNDARY_PROPOSALS = {"provides_input", "relate"}


def handoffs_by_node(sub: dict) -> dict[str, list[dict]]:
    """A resolved handoff ids onto nodes at compile time; B's profile names the step. Handle both."""
    out: dict[str, list[dict]] = {}
    for n in sub["nodes"]:
        mine = set(n.get("handoffs", []))
        decls = [h for h in sub.get("handoffs", []) if h.get("id") in mine or h.get("step") == n["name"]]
        if decls:
            out[n["_id"]] = decls
    return out


def _directions(node: dict, decls: list[dict]) -> set[str]:
    d = {"out"} if node["visibility"] == "handoff_out" else {"in"} if node["visibility"] == "handoff_in" else set()
    return d | {h["direction"] for h in decls if h.get("direction") in ("in", "out")}


def pair_handoffs(a: dict, b: dict, ha: list[dict], hb: list[dict]) -> tuple[dict, dict, dict | None, dict | None] | None:
    """(sender, receiver, out_decl, in_decl) if the two nodes form a handoff-out -> handoff-in pairing."""
    da, db_ = _directions(a, ha), _directions(b, hb)
    if "out" in da and "in" in db_:
        return a, b, next((h for h in ha if h.get("direction") == "out"), None), next((h for h in hb if h.get("direction") == "in"), None)
    if "out" in db_ and "in" in da:
        return b, a, next((h for h in hb if h.get("direction") == "out"), None), next((h for h in ha if h.get("direction") == "in"), None)
    return None


def _decl_summary(h: dict | None) -> dict | None:
    if not h:
        return None
    out = {"id": h.get("id"), "direction": h.get("direction"), "purpose": h.get("purpose")}
    for k in ("payload", "accept", "expects_back", "returns", "never_send", "never_accept", "to_role", "from_role"):
        if k in h:
            out[k] = h[k]
    return out


def boundary_edge(use_case: str, hid: str, sender: dict, receiver: dict, out_decl: dict | None, in_decl: dict | None,
                  alignment: dict) -> dict:
    cond = None
    if out_decl:
        cond = next((br.get("when") for br in sender.get("branches", []) if br.get("then") == out_decl.get("id")), None)
    e = {"_id": hid, "org_id": "M", "use_case_id": use_case, "from": sender["_id"], "to": receiver["_id"],
         "type": "boundary", "direction": f"{sender['org_id']}->{receiver['org_id']}",
         "alignment": alignment["_id"], "proposal": alignment["proposal"],
         "handoff_out": _decl_summary(out_decl), "handoff_in": _decl_summary(in_decl),
         "sender_fields": copy.deepcopy(sender.get("outputs", [])),
         "receiver_fields": copy.deepcopy(receiver.get("inputs", [])),
         "allowlist": None, "guard": None, "adapter": None,
         "guidance": f"{sender['name']} -> {receiver['name']}: "
                     + ((out_decl or {}).get("purpose") or (in_decl or {}).get("purpose") or alignment.get("rationale", ""))}
    if cond:
        e["condition"] = cond
    return e


def build(submissions: list[dict], alignments: list[dict], use_case: str = DEFAULT_USE_CASE, version: int = 1) -> dict:
    subs = {s["org_id"]: s for s in submissions}
    A, B = subs["A"], subs["B"]
    nodes = copy.deepcopy(A["nodes"]) + copy.deepcopy(B["nodes"])
    edges = copy.deepcopy(A["transitions"]) + copy.deepcopy(B["transitions"])
    by_id = {n["_id"]: n for n in nodes}
    a_order = {n["_id"]: i for i, n in enumerate(A["nodes"])}
    hmap = {**handoffs_by_node(A), **handoffs_by_node(B)}

    boundary, failure_candidates, merge_candidates, used = [], [], [], []
    for al in sorted(alignments, key=lambda x: (a_order.get(x["a"], 99), x["b"])):
        a, b = by_id.get(al["a"]), by_id.get(al["b"])
        if not a or not b:
            continue
        if al["proposal"] == "on_failure":
            failure_candidates.append({"from": b["_id"] if b["kind"] == "terminal" else a["_id"],
                                       "to": a["_id"] if b["kind"] == "terminal" else b["_id"],
                                       "alignment": al["_id"], "rationale": al.get("rationale", "")})
            used.append(al["_id"])
        elif al["proposal"] == "merge":
            merge_candidates.append({"a": a["_id"], "b": b["_id"], "alignment": al["_id"]})
            used.append(al["_id"])
        elif al["proposal"] in BOUNDARY_PROPOSALS:
            pair = pair_handoffs(a, b, hmap.get(a["_id"], []), hmap.get(b["_id"], []))
            if pair:
                boundary.append((pair, al))
                used.append(al["_id"])

    # number boundary edges along org A's flow: h1 is the earliest A node involved
    boundary.sort(key=lambda p: a_order[p[0][0]["_id"] if p[0][0]["org_id"] == "A" else p[0][1]["_id"]])
    boundary_edges = [boundary_edge(use_case, f"h{i + 1}", s, r, o, d, al) for i, ((s, r, o, d), al) in enumerate(boundary)]
    edges += boundary_edges

    for n in nodes:
        spec.validate(n, "node")
    for e in edges:
        spec.validate(e, "transition")

    doc = jsonable({
        "_id": f"{use_case}:v{version}", "doc_type": "graph", "use_case_id": use_case, "version": version,
        "nodes": nodes, "edges": edges, "boundary_edges": boundary_edges,
        "failure_candidates": failure_candidates, "merge_candidates": merge_candidates,
        "alignments_used": used, "decisions_applied": [], "questions_applied": [],
        "inputs": {"h_A": A["hash"], "h_B": B["hash"],
                   "h_alignments": sha256(sorted(({k: al.get(k) for k in ("a", "b", "sigma", "iota", "tau", "proposal", "confidence")}
                                                  for al in alignments), key=lambda d: (d["a"], d["b"])))},
    })
    doc["hash"] = sha256({k: doc[k] for k in ("nodes", "edges", "boundary_edges", "failure_candidates", "merge_candidates", "inputs")})
    return doc


def adjacency(doc: dict) -> list[dict]:
    """One doc per node for $graphLookup: node_id -> next[]."""
    nxt: dict[str, list[str]] = {n["_id"]: [] for n in doc["nodes"]}
    for e in doc["edges"]:
        if e["from"] in nxt:
            nxt[e["from"]].append(e["to"])
    return [{"_id": f"{doc['_id']}:{n['_id']}", "doc_type": "node", "use_case_id": doc["use_case_id"],
             "version": doc["version"], "node_id": n["_id"], "org_id": n["org_id"], "kind": n["kind"],
             "terminal": n["kind"] == "terminal" or bool(n.get("outcome")), "next": sorted(set(nxt[n["_id"]]))}
            for n in doc["nodes"]]


def write(doc: dict) -> None:
    from pmp import db
    col = db.col("merged")
    col.replace_one({"_id": doc["_id"]}, doc, upsert=True)
    col.delete_many({"use_case_id": doc["use_case_id"], "version": doc["version"], "doc_type": "node"})
    col.insert_many(adjacency(doc))


def load(use_case: str, version: int) -> dict:
    from pmp import db
    doc = db.col("merged").find_one({"_id": f"{use_case}:v{version}", "doc_type": "graph"})
    if not doc:
        raise SystemExit(f"no merged graph {use_case} v{version}; run pmp.merge first")
    return doc


def summary(doc: dict) -> str:
    lines = [f"merged {doc['_id']}: {len(doc['nodes'])} nodes, {len(doc['edges'])} edges, "
             f"{len(doc['boundary_edges'])} boundary, {len(doc['failure_candidates'])} failure candidate(s), hash {doc['hash'][:19]}…"]
    for e in doc["boundary_edges"]:
        lines.append(f"  {e['_id']}  {e['from']} -> {e['to']}  ({e['direction']}, {e['proposal']})")
    for f in doc["failure_candidates"]:
        lines.append(f"  failure candidate  {f['from']} -> {f['to']}  (not an edge yet: FAIL-01 decides)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Stage 3a: merge the two submissions over the alignments.")
    p.add_argument("--use-case", default=DEFAULT_USE_CASE)
    p.add_argument("--version", type=int, default=1)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--alignments-from-fixture", action="store_true", help="use mock/stages/2_alignments.json instead of Atlas")
    a = p.parse_args(argv)
    from pmp import db
    subs = list(db.col("submissions").find({"use_case_id": a.use_case}))
    if a.alignments_from_fixture:
        from pmp.align import load_fixture
        als = load_fixture(a.use_case)
    else:
        als = list(db.col("alignments").find({"use_case_id": a.use_case}))
    if len(subs) != 2 or not als:
        raise SystemExit(f"need 2 submissions and >0 alignments (have {len(subs)}, {len(als)}); run compile + align first")
    doc = build(subs, als, a.use_case, a.version)
    print(summary(doc))
    if not a.dry_run:
        write(doc)
        print(f"wrote {doc['_id']} + {len(doc['nodes'])} adjacency docs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
