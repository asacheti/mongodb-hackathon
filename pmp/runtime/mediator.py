"""The mediator at run time. Watches `rejections` (Atlas change stream) and, for each one, plans a patch from the
rejection alone, applies it to a new merged version, re-runs the full check registry, issues contract v+1 (active)
and marks the previous version superseded. Every patch is written to merge_log.

MISSING_FIELDS: search the SENDER org's own submission (tool outputs, node fields, data model) for a producer of
the missing field and extend the adapter on that edge (inputs, outputs, transform, allowlist); insert an adapter
if the edge has none. TYPE_MISMATCH: add a conversion to the adapter. Anything else stays a first-class conflict.

CLI: python -m pmp.runtime.mediator [--use-case ...]   (blocks on the change stream; needs Atlas)
"""
from __future__ import annotations
import argparse
import copy
import re
import sys

from pmp import contract as ct, db, merge
from pmp.compile import jsonable, sha256
from pmp.decide import now

DEFAULT_USE_CASE = "bnpl_checkout_v1"


class Unresolvable(RuntimeError):
    pass


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def find_producer(sub: dict, missing: str) -> dict | None:
    """A field in the sender's own submission that can supply `missing`: same name anywhere (node outputs first,
    then inputs, then the data model), else an address-typed field when a postal code / zip is wanted."""
    leaf = missing.split(".")[-1]
    want = _norm(leaf)
    for where in ("outputs", "inputs"):
        for n in sub["nodes"]:
            for f in n.get(where, []):
                if _norm(f["path"].split(".")[-1]) == want:
                    return {"source": f["path"], "expr": f["path"].split(".")[-1], "found_in": f"{n['_id']} {where}", "field": f}
    for ent, e in sub.get("entities", {}).items():
        for fname, f in e.get("fields", {}).items():
            if _norm(fname) == want:
                return {"source": f"{ent}.{fname}", "expr": fname, "found_in": f"entities.{ent}", "field": {"path": f"{ent}.{fname}", **f}}
    if any(k in want for k in ("postal", "zip")):
        for ent, e in sub.get("entities", {}).items():
            for fname, f in e.get("fields", {}).items():
                if f.get("type") == "address":
                    return {"source": f"{ent}.{fname}", "expr": f"{fname}.postal_code", "found_in": f"entities.{ent} (address)",
                            "field": {"path": f"{ent}.{fname}", **f}}
    return None


def plan_patch(rej: dict, subs: dict[str, dict], merged: dict) -> dict:
    edge = next((e for e in merged["boundary_edges"] if e["_id"] == rej["edge"]), None)
    if not edge:
        raise Unresolvable(f"edge {rej['edge']} not in merged v{merged['version']}")
    sender_org = edge["direction"][0]
    plan = {"code": rej["code"], "edge": edge["_id"], "sender": sender_org, "receiver": rej["receiver"], "ops": [], "unresolved": []}
    adapter_id = edge.get("adapter") if edge.get("adapter", "").startswith(sender_org + ".") else None
    if rej["code"] == "MISSING_FIELDS":
        for m in rej.get("missing", []):
            prod = find_producer(subs[sender_org], m)
            if not prod:
                plan["unresolved"].append(m)
                continue
            f = prod["field"]
            out_field = {"path": m, "type": "string" if f.get("type") == "address" else f.get("type", "string"), "origin": "mediator",
                         **({"sensitivity": f["sensitivity"]} if f.get("sensitivity") else {}), **({"may_cross": f["may_cross"]} if f.get("may_cross") else {})}
            plan["ops"].append({"op": "extend_adapter" if adapter_id else "insert_adapter", "adapter": adapter_id, "edge": edge["_id"],
                                "input": {"path": prod["source"], "type": f.get("type", "string")}, "output": out_field,
                                "transform": {"to": m, "from": [prod["source"]], "expr": prod["expr"]}, "found_in": prod["found_in"],
                                "allowlist": [m]})
    elif rej["code"] == "TYPE_MISMATCH":
        plan["ops"].append({"op": "extend_adapter" if adapter_id else "insert_adapter", "adapter": adapter_id, "edge": edge["_id"],
                            "transform": {"to": rej["field"], "from": [rej["field"]], "expr": f"convert({rej['field'].split('.')[-1]}, {rej.get('expected')})"},
                            "output": {"path": rej["field"], "type": rej.get("expected", "string"), "origin": "mediator"}, "allowlist": [rej["field"]]})
    else:
        plan["unresolved"].append(rej["code"])
    return plan


def apply_patch(merged: dict, plan: dict, subs: dict[str, dict], version: int) -> dict:
    m = copy.deepcopy(merged)
    m["_id"], m["version"], m["supersedes"] = f"{m['use_case_id']}:v{version}", version, merged["version"]
    nodes = {n["_id"]: n for n in m["nodes"]}
    edge = next(e for e in m["boundary_edges"] if e["_id"] == plan["edge"])
    for op in plan["ops"]:
        if op["op"] == "insert_adapter":
            sender = nodes[edge["from"]]
            adapter = {"_id": f"{plan['sender']}.adapt_{edge['_id']}_patch", "org_id": plan["sender"], "use_case_id": m["use_case_id"],
                       "name": f"adapt_{edge['_id']}_patch", "kind": "action", "visibility": "handoff_out", "origin": "mediator",
                       "inputs": [], "outputs": copy.deepcopy(sender.get("outputs", [])), "transform": [],
                       "text": f"adapt_{edge['_id']}_patch: adapter inserted by the mediator after a runtime rejection on {edge['_id']}."}
            m["nodes"].append(adapter)
            nodes[adapter["_id"]] = adapter
            m["edges"].append({"_id": f"{sender['_id']}->{adapter['_id']}", "org_id": plan["sender"], "use_case_id": m["use_case_id"],
                               "from": sender["_id"], "to": adapter["_id"], "type": "normal", "origin": "mediator", "guidance": "patch adapter"})
            for x in [e for e in m["edges"] if e["_id"] == edge["_id"]] + [edge]:
                x["from"], x["adapter"] = adapter["_id"], adapter["_id"]
            op["adapter"] = adapter["_id"]
        adapter = nodes[op["adapter"]]
        if op.get("input") and op["input"]["path"] not in {f["path"] for f in adapter.get("inputs", [])}:
            adapter.setdefault("inputs", []).append(op["input"])
        if op["output"]["path"] not in {f["path"] for f in adapter.get("outputs", [])}:
            adapter.setdefault("outputs", []).append(op["output"])
        adapter.setdefault("transform", []).append(op["transform"])
        adapter["text"] += f" Extended by the mediator: {op['transform']['to']} = {op['transform']['expr']} (from {op.get('found_in', 'own catalog')})."
        for x in [e for e in m["edges"] if e["_id"] == edge["_id"]] + [edge]:
            x["sender_fields"] = copy.deepcopy(adapter["outputs"])
            x["allowlist"] = sorted(set(x.get("allowlist") or []) | set(op["allowlist"]))
    # the receiver's republished node definition (e.g. a newly required input) enters the merged graph
    recv_sub = subs[plan["receiver"]]
    fresh = next((n for n in recv_sub["nodes"] if n["_id"] == edge["to"]), None)
    if fresh:
        nodes[edge["to"]]["inputs"] = copy.deepcopy(fresh.get("inputs", []))
        for x in [e for e in m["edges"] if e["_id"] == edge["_id"]] + [edge]:
            x["receiver_fields"] = copy.deepcopy(fresh.get("inputs", []))
    m["patches"] = m.get("patches", []) + [plan]
    m["inputs"]["h_A"], m["inputs"]["h_B"] = subs["A"]["hash"], subs["B"]["hash"]
    m = jsonable(m)
    m["hash"] = sha256({k: m[k] for k in ("nodes", "edges", "boundary_edges", "failure_candidates", "merge_candidates", "inputs")})
    return m


def handle_rejection(rej: dict, use_case: str = DEFAULT_USE_CASE) -> dict:
    """Plan -> patch -> revalidate -> contract v+1 active, previous superseded -> merge_log. Returns the new contract."""
    current = db.col("contracts").find_one({"use_case_id": use_case, "status": {"$in": ["active", "suspect"]}}, sort=[("version", -1)])
    if not current:
        raise Unresolvable("no contract to patch")
    merged = db.col("merged").find_one({"use_case_id": use_case, "doc_type": "graph"}, sort=[("version", -1)])
    subs = {s["org_id"]: s for s in db.col("submissions").find({"use_case_id": use_case})}
    als = list(db.col("alignments").find({"use_case_id": use_case}))
    qs = list(db.col("merge_questions").find({"use_case_id": use_case}).sort("_id", 1))
    ds = list(db.col("merge_log").find({"use_case_id": use_case, "type": "decision"}))
    plan = plan_patch(rej, subs, merged)
    if plan["unresolved"]:
        db.col("merge_log").insert_one({"_id": f"{use_case}:patch:{now()}", "use_case_id": use_case, "type": "patch", "rejection": rej,
                                        "plan": plan, "status": "unresolved", "ts": now()})
        raise Unresolvable(f"cannot resolve {plan['unresolved']} from {plan['sender']}'s own catalog")
    new_merged = apply_patch(merged, plan, subs, merged["version"] + 1)
    merge.write(new_merged)
    out = ct.build(new_merged, list(subs.values()), als, qs, ds, contract_version=current["version"] + 1,
                   db=None if db.in_memory() else db, supersedes=current["version"],
                   reason=f"{rej['code']} on {rej['edge']}: {', '.join(rej.get('missing') or [rej.get('field') or ''])}")
    ct.write(out, als)
    new = out["contract"]
    countersign = {"A": "auto (adapter_change_from_own_catalog allowed by A's merge_preferences)" if plan["sender"] == "A" else "platform.lead (simulated)",
                   "B": "risk.officer (simulated; B never auto-countersigns)" if plan["sender"] == "A" else "auto"}
    db.col("merge_log").insert_one({"_id": f"{use_case}:patch:v{new['version']}", "use_case_id": use_case, "type": "patch", "rejection": rej,
                                    "plan": plan, "merged_version": new_merged["version"], "contract_version": new["version"],
                                    "supersedes": current["version"], "countersign": countersign, "status": "applied", "ts": now()})
    return new


def watch(use_case: str = DEFAULT_USE_CASE) -> None:
    print(f"mediator: watching rejections for {use_case} (change stream)")
    with db.col("rejections").watch([{"$match": {"operationType": "insert"}}]) as stream:
        for change in stream:
            rej = change["fullDocument"]
            if rej.get("use_case_id") != use_case:
                continue
            print(f"mediator: rejection {rej['code']} on {rej['edge']} run {rej['run']} seq {rej['seq']}")
            try:
                new = handle_rejection(rej, use_case)
                print(f"mediator: contract v{new['version']} active (supersedes v{new['supersedes']}): {new['reason']}")
            except Unresolvable as e:
                print(f"mediator: {e}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="PMP mediator: patch contracts from runtime rejections.")
    p.add_argument("--use-case", default=DEFAULT_USE_CASE)
    a = p.parse_args(argv)
    try:
        watch(a.use_case)
    except KeyboardInterrupt:
        print("mediator: stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
