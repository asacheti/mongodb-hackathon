"""Stage 5: contract + certificate + per-org projections.

1. Re-run the full check registry on merged v2 (stage revalidate). Every result must be pass or guarded.
2. Build projection A and projection B: own nodes (submitted + adapters carrying an origin), own edges,
   the boundary edges with the partner side collapsed into ONE opaque node that carries only the boundary
   interface (allowlisted fields with types, guards, outcomes, declared handoff purposes). Nothing of the
   partner's private chain, tools or notes.
3. Check the local invariants on each projection (INV-1..4) and PRIV-01 on both (mediator-only).
4. Issue the certificate: real sha256 over submissions, alignments, answers, merged graph, both projections
   and the boundary interface; the list of every check; alignments with their confirmation source; questions
   with answer hashes. Signatures are placeholders: the mediator signs first, each org countersigns after
   re-running its local checks.
5. Write `contracts` (status active, version 1; earlier versions superseded) and `projections`.

Deterministic apart from timestamps and the placeholder signatures.
CLI: python -m pmp.contract --use-case bnpl_checkout_v1 [--merged-version 2] [--contract-version 1] [--dry-run]
"""
from __future__ import annotations
import argparse
import copy
import json
import sys

from pmp import spec, validate as v
from pmp.compile import jsonable, sha256
from pmp.decide import now
from pmp.predicate import to_text

DEFAULT_USE_CASE = "bnpl_checkout_v1"
MERGER_VERSION, VALIDATOR_VERSION = "pmp-merge 0.1.0", "pmp-validate 0.1.0"


class ContractBlocked(RuntimeError):
    pass


# --------------------------------------------------------------------------- boundary interface

def _typed(paths: list[str], sender_fields: list[dict]) -> list[dict]:
    by_path = {f["path"]: f for f in sender_fields}
    by_leaf = {f["path"].split(".")[-1]: f for f in sender_fields}
    out = []
    for p in paths:
        f = by_path.get(p) or by_leaf.get(p.split(".")[-1]) or {"path": p, "type": "string"}
        d = {"path": p, "type": f.get("type", "string")}
        for k in ("unit", "currency", "values", "sensitivity"):
            if f.get(k):
                d[k] = f[k]
        out.append(d)
    return out


def boundary_interface(ctx: v.Ctx) -> dict:
    """The one artefact both orgs see in full: per edge, what crosses and under which guard."""
    edges = []
    for e in ctx.boundary:
        allowed = v.effective_allowlist(e)
        gp = v.guard_predicate(ctx, e)
        edges.append({
            "edge": e["_id"], "direction": e["direction"], "from_org": e["direction"][0], "to_org": e["direction"][-1],
            "fields": _typed(allowed, e["sender_fields"]),
            "allowlist": allowed,
            "guard": gp, "guard_text": to_text(gp) if gp else None,
            "policy": {k: val for k, val in (e.get("guard") or {}).items() if k != "predicate"} or None,
            "purpose_out": (e.get("handoff_out") or {}).get("purpose"),
            "purpose_in": (e.get("handoff_in") or {}).get("purpose"),
            "condition": e.get("condition"),
        })
    failures = []
    for e in ctx.edges:
        if e.get("type") in v.FAILURE_EDGE_TYPES and ctx.nodes[e["from"]]["org_id"] != ctx.nodes[e["to"]]["org_id"]:
            on = (e.get("condition") or {}).get("args", [None, None])[1]
            f_org, t_org = ctx.nodes[e["from"]]["org_id"], ctx.nodes[e["to"]]["org_id"]
            # the failure terminal is partner-visible by construction; the fallback target is the other org's private step
            failures.append({"edge": f"{e['from']}->{t_org}", "from": e["from"], "from_org": f_org, "to_org": t_org,
                             "on": on, "outcome": ctx.nodes[e["from"]].get("outcome")})
    return {"edges": edges, "failure_edges": failures}


# --------------------------------------------------------------------------- projections

def _opaque_id(partner: str, subs: dict) -> str:
    return f"{partner}.opaque_{subs[partner].get('org_name', partner)}"


def project(merged: dict, org: str, subs: dict[str, dict], interface: dict, contract_version: int) -> dict:
    partner = "B" if org == "A" else "A"
    pname = subs[partner].get("org_name", partner).capitalize()
    opaque = _opaque_id(partner, subs)
    use_case = merged["use_case_id"]
    own = [copy.deepcopy(n) for n in merged["nodes"] if n["org_id"] == org]
    for n in own:
        n["executor"] = org
    ins = [e for e in interface["edges"] if e["to_org"] == partner]
    outs = [e for e in interface["edges"] if e["from_org"] == partner]
    fails = [f for f in interface["failure_edges"] if f["from_org"] == partner]
    outcomes = ["completed"] + [f["on"] for f in fails if f.get("on")]
    opaque_node = {
        "_id": opaque, "org_id": partner, "use_case_id": use_case, "name": f"{pname} (opaque)", "kind": "handoff",
        "visibility": "handoff_in", "executor": partner, "opaque": True,
        "inputs": [f for e in ins for f in e["fields"]], "outputs": [f for e in outs for f in e["fields"]],
        "interface": {"receives": [{"edge": e["edge"], "fields": e["allowlist"], "guard": e["guard_text"], "purpose": e["purpose_in"]} for e in ins],
                      "returns": [{"edge": e["edge"], "fields": e["allowlist"], "guard": e["guard_text"], "purpose": e["purpose_out"]} for e in outs],
                      "outcomes": outcomes},
        "text": (f"{pname}: opaque partner procedure. Receives {', '.join(e['edge'] for e in ins) or 'nothing'}; returns "
                 f"{', '.join(e['edge'] for e in outs) or 'nothing'}; terminal outcomes {' | '.join(outcomes)}."),
    }
    nodes = own + [opaque_node]
    own_ids = {n["_id"] for n in own}
    edges = []
    for e in merged["edges"]:
        f_own, t_own = e["from"] in own_ids, e["to"] in own_ids
        if f_own and t_own:
            edges.append(copy.deepcopy(e))
        elif f_own or t_own:
            pe = {k: copy.deepcopy(val) for k, val in e.items()
                  if k not in ("alignment", "proposal", "handoff_out", "handoff_in", "sender_fields", "receiver_fields", "adapter", "guidance", "rationale")}
            pe["from"], pe["to"] = (e["from"] if f_own else opaque), (e["to"] if t_own else opaque)
            if e.get("type") == "boundary":
                ie = next(x for x in interface["edges"] if x["edge"] == e["_id"])
                pe["allowlist"], pe["fields"] = ie["allowlist"], ie["fields"]
                pe["guard"] = ie["guard"]
                pe["policy"] = ie["policy"]
                decl = e.get("handoff_out") if f_own else e.get("handoff_in")
                pe["purpose"] = (decl or {}).get("purpose")
                if e.get("adapter") in own_ids:
                    pe["adapter"] = e["adapter"]
                if f_own:
                    pe["sender_fields"] = copy.deepcopy(e["sender_fields"])
                else:
                    pe["receiver_fields"] = copy.deepcopy(e["receiver_fields"])
                    pe.pop("condition", None)      # the partner's branch condition is its own business
                pe["guidance"] = f"{'send' if f_own else 'receive'} {e['_id']}: {pe['purpose'] or ''}".strip()
            else:
                pe["_id"] = f"{pe['from']}->{pe['to']}"
                pe["guidance"] = f"partner outcome {(e.get('condition') or {}).get('args', ['', ''])[1]} -> {pe['to']}"
            edges.append(pe)
    for n in nodes:
        spec.validate(n, "node")
    for e in edges:
        spec.validate(e, "transition")
    proj = jsonable({"_id": f"{use_case}:{org}:v{contract_version}", "org_id": org, "use_case_id": use_case,
                     "contract_version": contract_version, "merged_version": merged["version"],
                     "opaque_node": opaque, "nodes": nodes, "edges": edges, "boundary_interface": interface,
                     "counts": {"nodes": len(nodes), "own": len(own), "adapters": sum(1 for n in own if n.get("origin")), "opaque": 1,
                                "edges": len(edges)}})
    proj["hash"] = sha256({k: proj[k] for k in ("nodes", "edges", "boundary_interface")})
    return proj


def invariants(proj: dict, sub: dict, merged: dict) -> list[dict]:
    org, opaque = proj["org_id"], proj["opaque_node"]
    own = [n for n in proj["nodes"] if n["_id"] != opaque]
    adapters = [n for n in own if n.get("origin")]
    res = []
    bad = [n["_id"] for n in own if n["org_id"] != org]
    res.append(v.result("INV-1", "fail" if bad else "pass", f"projection_{org}",
                        f"foreign nodes {bad}" if bad else f"own nodes only; {len(adapters)} adapter nodes, origin {', '.join(sorted({a['origin'] for a in adapters})) or 'none'}"))
    bad = [n["_id"] for n in own if n.get("executor") != org]
    res.append(v.result("INV-2", "fail" if bad else "pass", f"projection_{org}", f"executor mismatch {bad}" if bad else "own executor for every node"))
    submitted = {n["_id"] for n in sub["nodes"]}
    gone = sorted(submitted - {n["_id"] for n in own})
    res.append(v.result("INV-3", "fail" if gone else "pass", f"projection_{org}", f"removed {gone}" if gone else "nothing removed; 0 merged-away nodes"))
    servers = {t["ref"].split(":")[0].split(".")[0] for t in sub.get("tools", [])}
    used = sorted({n["tool"]["server"] for n in own if n.get("tool")})
    bad = [s for s in used if s not in servers]
    res.append(v.result("INV-4", "fail" if bad else "pass", f"projection_{org}", f"foreign tools {bad}" if bad else "own tools: " + ", ".join(used)))
    return res


def priv_01(proj: dict, partner_sub: dict, merged: dict) -> dict:
    """Mediator-only: nothing of the partner's private chain leaks into this projection."""
    blob = json.dumps(proj, sort_keys=True)
    partner_nodes = [n for n in merged["nodes"] if n["org_id"] == partner_sub["org_id"]]
    forbidden: dict[str, str] = {}
    for n in partner_nodes:
        if n["visibility"] == "internal" or n.get("origin"):
            forbidden[n["name"]] = "internal node name"
            forbidden[n["text"]] = "internal node text"
        if n.get("confidential_notes"):
            forbidden[n["confidential_notes"]] = "confidential_notes"
        if n.get("tool"):
            forbidden[f"{n['tool']['server']}.{n['tool']['name']}"] = "tool"
    forbidden.update({t["ref"]: "tool catalog" for t in partner_sub.get("tools", [])})
    leaks = sorted({why + ": " + s[:40] for s, why in forbidden.items() if s and s in blob})
    return v.result("PRIV-01", "fail" if leaks else "pass", f"projection_{proj['org_id']}",
                    ("leaks: " + "; ".join(leaks)) if leaks else "no cross-org content beyond the opaque interface node",
                    locality="mediator", **({"rung": 6, "stakes": "high"} if leaks else {}))


# --------------------------------------------------------------------------- certificate + contract

def confirm_alignments(ctx: v.Ctx, alignments: list[dict]) -> None:
    for al in alignments:
        if not al.get("confirmed_by"):
            src = v._confirmation(ctx, al)
            if src:
                al["confirmed_by"] = src


def build(merged: dict, submissions: list[dict], alignments: list[dict], questions: list[dict], decisions: list[dict],
          *, contract_version: int = 1, db=None, supersedes: int | None = None, reason: str = "initial",
          results: list[dict] | None = None, cert_extra: dict | None = None) -> dict:
    """`results`: precomputed registry results (an incremental update passes re-run + carried results);
    `cert_extra`: extra certificate fields (delta, checks re-run vs carried, signatures required, ...)."""
    subs = {s["org_id"]: s for s in submissions}
    use_case = merged["use_case_id"]
    ctx = v.Ctx(merged, subs, alignments, questions, db, "revalidate")
    confirm_alignments(ctx, alignments)
    if results is None:
        results = v.run(merged, submissions, alignments, questions, db=db, stage="revalidate")
    interface = boundary_interface(ctx)
    projections = {org: project(merged, org, subs, interface, contract_version) for org in ("A", "B")}
    inv = {org: invariants(projections[org], subs[org], merged) for org in ("A", "B")}
    priv = {org: priv_01(projections[org], subs["B" if org == "A" else "A"], merged) for org in ("A", "B")}
    for org in ("A", "B"):
        projections[org]["invariants"] = inv[org]
        projections[org]["privacy"] = priv[org]
    extra = [r for org in ("A", "B") for r in inv[org]] + [priv["A"], priv["B"]]
    failed = [r for r in results + extra if r["verdict"] == "fail"]
    if failed:
        raise ContractBlocked("contract blocked by: " + "; ".join(f"{r['check']} {r['scope']}: {r['detail']}" for r in failed))

    contract_id = f"ctr_{subs['A'].get('org_name', 'A')}_{subs['B'].get('org_name', 'B')}_{use_case.split('_')[0]}"
    cert_id = f"cert_{use_case}_{contract_version:04d}"
    h_iface = sha256(interface)
    inputs = {"h_A": subs["A"]["hash"], "h_B": subs["B"]["hash"], "h_alignments": merged["inputs"]["h_alignments"],
              "h_answers": merged["inputs"].get("h_answers", sha256([])), "h_merge": merged["hash"],
              "h_projection_A": projections["A"]["hash"], "h_projection_B": projections["B"]["hash"], "h_boundary_interface": h_iface}
    checks = [{"check": r["check"], "scope": r["scope"], "verdict": r["verdict"], "locality": r["locality"], "evidence": r["detail"],
               **({"status": r["status"]} if r.get("status") else {})}
              for r in results + extra]
    issued = now()
    n_local = sum(c["locality"] == "local" for c in checks)
    cert = {
        "certificate_id": cert_id, "use_case_id": use_case, "contract_id": contract_id, "contract_version": contract_version,
        "inputs": inputs, "checks": checks,
        "alignments": [{"_id": al["_id"], "a": al["a"], "b": al["b"], "proposal": al["proposal"], "sigma": al["sigma"],
                        "confirmed_by": al.get("confirmed_by"), "decision": al.get("decision")} for al in alignments],
        "decisions": [{"_id": d["_id"], "rule": d["rule"], "finding": d["finding"]} for d in decisions],
        "questions": [{"question_id": q["_id"], "trigger": q["trigger"], "to_org": q["to_org"],
                       "answered_by": {"org": q["answered_by"]["org"], "user": q["answered_by"]["user"]},
                       "accepted_default": q.get("accepted_default", False), "answer_hash": sha256(q.get("answer_data", q["answer"])),
                       "guardrails": [g["type"] for g in q.get("guardrails", [])]} for q in questions],
        "merger_version": MERGER_VERSION, "validator_version": VALIDATOR_VERSION,
        **(cert_extra or {}),
        "issued_at": issued,
        "signatures": {"mediator": {"alg": "Ed25519", "kid": "mediator-2026-09", "signed_at": issued, "sig": "MOCK"},
                       "A": {"alg": "Ed25519", "kid": f"{subs['A'].get('org_name', 'A')}-k1", "signed_at": now(), "sig": "MOCK",
                             "local_checks_rerun": n_local, "invariants": [r["verdict"] for r in inv["A"]]},
                       "B": {"alg": "Ed25519", "kid": f"{subs['B'].get('org_name', 'B')}-k1", "signed_at": now(), "sig": "MOCK",
                             "local_checks_rerun": n_local, "invariants": [r["verdict"] for r in inv["B"]]}},
    }
    spec.validate(cert, "certificate")
    contract = {
        "_id": f"{use_case}:ctr:v{contract_version}", "use_case_id": use_case, "contract_id": contract_id, "version": contract_version,
        "status": "active", "certificate_id": cert_id, **inputs,
        "boundary_edges": [{"id": e["edge"], "from": next(x["from"] for x in merged["boundary_edges"] if x["_id"] == e["edge"]),
                            "to": next(x["to"] for x in merged["boundary_edges"] if x["_id"] == e["edge"]),
                            "direction": e["direction"], "allowlist": e["allowlist"], "schema": {"fields": e["fields"]},
                            **({"guard": e["guard"]} if e["guard"] else {}), "policy": e["policy"]} for e in interface["edges"]],
        "failure_edges": interface["failure_edges"],
        "merged_version": merged["version"], "reason": reason, "supersedes": supersedes, "issued_at": issued,
        "signatures": cert["signatures"], "certificate": cert,
    }
    spec.validate(contract, "contract")
    return {"contract": jsonable(contract), "certificate": jsonable(cert), "projections": jsonable(projections),
            "results": results + extra, "interface": interface}


def write(out: dict, alignments: list[dict]) -> None:
    from pmp import db
    c = out["contract"]
    db.col("contracts").update_many({"use_case_id": c["use_case_id"], "version": {"$lt": c["version"]}, "status": {"$in": ["active", "suspect"]}},
                                    {"$set": {"status": "superseded"}})
    db.col("contracts").replace_one({"_id": c["_id"]}, c, upsert=True)
    for org, p in out["projections"].items():
        db.col("projections").replace_one({"_id": p["_id"]}, p, upsert=True)
    for al in alignments:
        db.col("alignments").update_one({"_id": al["_id"]}, {"$set": {"confirmed_by": al.get("confirmed_by")}})
    rows = v.findings([r for r in out["results"] if r["check"] not in ("INV-1", "INV-2", "INV-3", "INV-4", "PRIV-01")])
    v.write(rows, c["use_case_id"], c["merged_version"], "revalidate")


def load_active(use_case: str) -> dict | None:
    from pmp import db
    return db.col("contracts").find_one({"use_case_id": use_case, "status": "active"}, sort=[("version", -1)])


def summary(out: dict) -> str:
    c, cert, projs = out["contract"], out["certificate"], out["projections"]
    lines = [f"certificate {cert['certificate_id']}: {len(cert['checks'])} checks "
             f"({sum(x['verdict'] == 'pass' for x in cert['checks'])} pass, {sum(x['verdict'] == 'guarded' for x in cert['checks'])} guarded; "
             f"{sum(x['locality'] == 'local' for x in cert['checks'])} local, {sum(x['locality'] == 'mediator' for x in cert['checks'])} mediator-only)"]
    for x in cert["checks"]:
        if x["verdict"] == "guarded" or x["check"].startswith(("INV", "PRIV", "ALN", "CONF")):
            lines.append(f"  {x['check']:<8} {x['scope']:<14} {x['verdict']:<8} {x['locality']:<8} {x['evidence'][:110]}")
    for org, p in projs.items():
        lines.append(f"projection {org}: {p['counts']['nodes']} nodes ({p['counts']['own']} own incl. {p['counts']['adapters']} adapters + 1 opaque), "
                     f"{p['counts']['edges']} edges, hash {p['hash'][:19]}…")
    for e in c["boundary_edges"]:
        lines.append(f"  {e['id']} {e['direction']}  allowlist {e['allowlist']}" + (f"  guard {to_text(e['guard'])}" if e.get("guard") else ""))
    lines.append(f"contract {c['contract_id']} v{c['version']} is {c['status']} as of {c['signatures']['B']['signed_at']}; "
                 f"h_merge {c['h_merge'][:19]}… h_boundary_interface {c['h_boundary_interface'][:19]}…")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Stage 5: revalidate merged v2, issue certificate, contract and projections.")
    p.add_argument("--use-case", default=DEFAULT_USE_CASE)
    p.add_argument("--merged-version", type=int, default=2)
    p.add_argument("--contract-version", type=int, default=1)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    from pmp import db, merge
    merged = merge.load(a.use_case, a.merged_version)
    subs = list(db.col("submissions").find({"use_case_id": a.use_case}))
    als = list(db.col("alignments").find({"use_case_id": a.use_case}))
    qs = list(db.col("merge_questions").find({"use_case_id": a.use_case}).sort("_id", 1))
    ds = list(db.col("merge_log").find({"use_case_id": a.use_case, "type": "decision"}).sort("_id", 1))
    try:
        out = build(merged, subs, als, qs, ds, contract_version=a.contract_version, db=db)
    except ContractBlocked as e:
        print(e)
        return 1
    print(summary(out))
    if not a.dry_run:
        write(out, als)
        print("wrote contract, 2 projections, revalidate findings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
