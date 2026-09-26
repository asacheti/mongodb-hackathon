"""Precompute the whole demo once and save it: every stage's results and the model outputs, in one JSON the
walkthrough page reads. Nothing is recomputed at demo time.

Alignments are taken from Atlas when they exist there (the live LLM output, rationales included); otherwise
the fixture. Everything downstream is deterministic and runs in memory: merge, validate, decide (with the
Stage 4 answers), contract, run 0001, run 0002 with the mediator repair, and the §6 update.

CLI: python -m pmp.snapshot [--out ui/demo.json] [--fixture-align]
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from pmp import align, compile as cp, contract as ct, db, decide, merge, update, validate as v
from pmp.predicate import to_text
from pmp.runtime.run import InProcessRuntime

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "ui" / "demo.json"
USE_CASE = "bnpl_checkout_v1"
ORGS = {"A": {"name": "Northwind Outdoor", "short": "Northwind", "role": "merchant", "format": "SKILL.md · typed YAML steps",
              "file": "mock/inputs/northwind/SKILL.md", "source": {"label": "Stripe, Integrate with the Invoicing API",
                                                                     "url": "https://docs.stripe.com/invoicing/integration"}},
        "B": {"name": "Lakeside BNPL", "short": "Lakeside", "role": "lender", "format": "Arazzo 1.0.0 workflow + merge-profile sidecar",
              "file": "mock/inputs/lakeside/bnpl-arazzo.yaml", "source": {"label": "OpenAPI Initiative, Arazzo examples/1.0.0/bnpl-arazzo.yaml",
                                                                          "url": "https://github.com/OAI/Arazzo-Specification/blob/main/examples/1.0.0/bnpl-arazzo.yaml"}}}


def _excerpt(path: Path, start_pat: str, lines: int) -> str:
    text = path.read_text().splitlines()
    i = next((k for k, l in enumerate(text) if re.search(start_pat, l)), 0)
    return "\n".join(text[i:i + lines])


def _node(n: dict) -> dict:
    t = n.get("tool") or {}
    return {"id": n["_id"], "org": n["org_id"], "name": n["name"], "kind": n["kind"], "visibility": n["visibility"], "origin": n.get("origin"),
            "tool": f"{t['server']}.{t['name']}" if t else None, "effect": t.get("effect") if t else None, "money": bool(t.get("money")),
            "pre": to_text(n["pre"]) if n.get("pre") else None, "post": to_text(n["post"]) if n.get("post") else None,
            "inputs": [f["path"] for f in n.get("inputs", [])], "outputs": [f["path"] for f in n.get("outputs", [])],
            "policy": n.get("policy"), "outcome": n.get("outcome"), "text": n["text"], "transform": n.get("transform")}


def _edge(e: dict) -> dict:
    return {"id": e["_id"], "from": e["from"], "to": e["to"], "type": e.get("type", "normal"), "condition": to_text(e["condition"]) if e.get("condition") else None,
            "origin": e.get("origin"), "direction": e.get("direction")}


def _finding(f: dict) -> dict:
    return {k: f.get(k) for k in ("check", "verdict", "scope", "detail", "rung", "rung_from", "stakes", "stakes_reason", "owner_org", "locality", "resolved_by")}


def live_alignments() -> list[dict] | None:
    try:
        db.use_atlas()
        from pymongo import MongoClient
        db.client.cache_clear()
        rows = list(db.col("alignments").find({"use_case_id": USE_CASE}))
        if len(rows) >= 5:
            for r in rows:
                r["confirmed_by"] = None
                r.pop("decision", None)
            return rows
    except Exception:
        pass
    return None


def build(fixture_align: bool = False) -> dict:
    als_live = None if fixture_align else live_alignments()
    store = db.use_memory()
    subs, cfind = cp.compile_all(USE_CASE)
    cp.write(subs, cfind, USE_CASE)
    als = als_live or align.load_fixture(USE_CASE)
    align.write(als, USE_CASE)
    m1 = merge.build(subs, als, USE_CASE, 1)
    merge.write(m1)
    results1 = v.run(m1, subs, als, [], db=None)
    f1 = v.findings(results1)
    v.write(f1, USE_CASE, 1, "validate")
    answers = decide.load_answers(decide.ANSWERS_FIXTURE)
    m2, ds, qs, als = decide.build_v2(m1, f1, subs, als, answers)
    decide.write(m2, ds, qs, als, f1)
    out1 = ct.build(m2, subs, als, qs, ds, contract_version=1, db=None)
    ct.write(out1, als)
    els, drv = update.build_index(m2, {s["org_id"]: s for s in subs}, qs, ds, out1["certificate"]["checks"], als)
    update.write_index(els, drv, USE_CASE)

    rt = InProcessRuntime(USE_CASE)
    r1 = rt.run("0001")
    logs1 = {o: list(rt.agents[o].runs["0001"]["log"]) for o in ("A", "B")}
    r2 = rt.run("0002")
    logs2 = {o: list(rt.agents[o].runs["0002"]["log"]) for o in ("A", "B")}
    c2 = db.col("contracts").find_one({"_id": f"{USE_CASE}:ctr:v2"})
    patch = db.col("merge_log").find_one({"use_case_id": USE_CASE, "type": "patch", "status": "applied"})
    rej = db.col("rejections").find_one({"use_case_id": USE_CASE})
    handoffs = list(db.col("handoffs").find({"use_case_id": USE_CASE, "role": "sender"}).sort([("run", 1), ("seq", 1)]))

    rep = update.apply_update("A", "large_order_approval", 15000, use_case=USE_CASE)
    rt.reload()
    r3 = rt.run("0001")
    logs3 = {o: list(rt.agents[o].runs["0001"]["log"]) for o in ("A", "B")}

    by_org = {s["org_id"]: s for s in subs}
    snap = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "use_case": USE_CASE,
        "models": {"embedding": align.embed_model(), "llm": align.llm_model(), "alignments_source": "atlas-live" if als_live else "fixture"},
        "input": {"orgs": {}},
        "merge": {}, "interview": {}, "contract": {}, "run": {}, "update": {},
    }
    for org, s in by_org.items():
        meta = ORGS[org]
        snap["input"]["orgs"][org] = {
            **meta, "version": s["version"], "hash": s["hash"], "procedure": s.get("procedure"), "description": (s.get("description") or "").strip(),
            "excerpt": _excerpt(ROOT / meta["file"], r"^## A4\. " if org == "A" else r"stepId: checkLoanCanBeProvided", 14 if org == "A" else 24),
            "counts": {"nodes": len(s["nodes"]), "edges": len(s["transitions"]), "human_gates": sum(n["kind"] == "human_gate" for n in s["nodes"]),
                       "handoff_visible": sum(n["visibility"] != "internal" for n in s["nodes"]), "tools": len(s.get("tools", []))},
            "nodes": [_node(n) for n in s["nodes"]], "edges": [_edge(e) for e in s["transitions"]],
            "handoffs": [{"id": h["id"], "direction": h["direction"], "purpose": h.get("purpose")} for h in s.get("handoffs", [])],
            "policies": {k: (to_text(p["rule"]) if isinstance(p, dict) and p.get("rule") else str(p)) for k, p in s.get("policies", {}).items()},
            "lint": [_finding(f) for f in cfind if f["owner_org"] == org],
            "confidential_notes_count": sum(1 for n in s["nodes"] if n.get("confidential_notes")),
        }
    snap["merge"] = {
        "alignments": [{k: a.get(k) for k in ("a_name", "b_name", "sigma", "iota", "tau", "proposal", "confidence", "rationale", "vector_score")} for a in als],
        "merged": {"version": 1, "hash": m1["hash"], "counts": {"nodes": len(m1["nodes"]), "edges": len(m1["edges"]), "boundary": len(m1["boundary_edges"])},
                   "nodes": [_node(n) for n in m1["nodes"]], "edges": [_edge(e) for e in m1["edges"]],
                   "boundary": [{"id": e["_id"], "from": e["from"], "to": e["to"], "direction": e["direction"], "proposal": e["proposal"],
                                 "purpose_out": (e.get("handoff_out") or {}).get("purpose"), "purpose_in": (e.get("handoff_in") or {}).get("purpose"),
                                 "condition": to_text(e["condition"]) if e.get("condition") else None,
                                 "sender_fields": [f["path"] for f in e["sender_fields"]], "receiver_fields": [f["path"] for f in e["receiver_fields"]]}
                                for e in m1["boundary_edges"]],
                   "failure_candidates": m1["failure_candidates"], "inputs": m1["inputs"]},
        "findings": [_finding(f) for f in f1],
        "all_results": [{"check": r["check"], "scope": r["scope"], "verdict": r["verdict"]} for r in results1],
    }
    snap["interview"] = {
        "decisions": [{"id": d["_id"], "check": d["check"], "scope": d["scope"], "rule": d["rule"], "patch": d["patch"]} for d in ds],
        "questions": [{k: q.get(k) for k in ("_id", "trigger", "triggers", "to_org", "to_role", "text", "options", "default", "answer", "answered_by",
                                             "accepted_default", "ts", "guardrails")} for q in qs],
        "merged_v2": {"hash": m2["hash"], "counts": {"nodes": len(m2["nodes"]), "edges": len(m2["edges"])},
                      "adapters": [_node(n) for n in m2["nodes"] if n.get("origin")],
                      "new_edges": [_edge(e) for e in m2["edges"] if e.get("origin")],
                      "boundary": [{"id": e["_id"], "from": e["from"], "to": e["to"], "allowlist": e.get("allowlist"), "adapter": e.get("adapter"),
                                    "guard": {k: (to_text(x) if k == "predicate" else x) for k, x in (e.get("guard") or {}).items()} or None}
                                   for e in m2["boundary_edges"]],
                      "guardrails": m2["guardrails"], "inputs": m2["inputs"]},
    }
    cert = out1["certificate"]
    snap["contract"] = {
        "certificate": {**{k: cert[k] for k in ("certificate_id", "contract_id", "contract_version", "inputs", "checks", "issued_at", "signatures",
                                                 "merger_version", "validator_version")},
                        "alignments": cert["alignments"], "questions": cert["questions"]},
        "contract": {"id": out1["contract"]["contract_id"], "version": 1, "status": "active", "issued_at": out1["contract"]["issued_at"],
                     "boundary_edges": [{**{k: e[k] for k in ("id", "direction", "from", "to", "allowlist")}, "guard": to_text(e["guard"]) if e.get("guard") else None,
                                         "policy": e.get("policy"), "fields": e["schema"]["fields"]} for e in out1["contract"]["boundary_edges"]],
                     "failure_edges": out1["contract"]["failure_edges"],
                     "hashes": {k: out1["contract"][k] for k in ("h_A", "h_B", "h_alignments", "h_answers", "h_merge", "h_projection_A", "h_projection_B", "h_boundary_interface")}},
        "projections": {o: {"counts": p["counts"], "hash": p["hash"], "nodes": [_node(n) if not n.get("opaque") else {**_node(n), "opaque": True, "interface": n["interface"]} for n in p["nodes"]],
                            "edges": [_edge(e) for e in p["edges"]], "opaque": next(({"id": n["_id"], "name": n["name"], "text": n["text"], "interface": n["interface"]}
                                                                                   for n in p["nodes"] if n.get("opaque")), None),
                            "invariants": [{"check": r["check"], "verdict": r["verdict"], "detail": r["detail"]} for r in p["invariants"]],
                            "privacy": {"verdict": p["privacy"]["verdict"], "detail": p["privacy"]["detail"]}}
                        for o, p in out1["projections"].items()},
    }

    def run_block(rid, res, logs, title):
        ev = sorted([{**e} for o in ("A", "B") for e in logs[o]], key=lambda e: (e.get("n", 0), e["ts"]))
        hs = [{"seq": h["seq"], "edge": h["edge"], "sender": h["sender"], "receiver": h["receiver"], "status": h["status"], "ts": h["ts"],
               "payload": h["handoff"]["payload"], "pmp": h["handoff"]["metadata"]["pmp"],
               "rejection": (h.get("response") or {}).get("rejection")} for h in handoffs if h["run"] == rid]
        return {"title": title, "inputs": rt.fixtures[rid]["inputs"], "events": ev, "handoffs": hs,
                "A": {k: res["A"].get(k) for k in ("status", "outcome", "done")}, "B": res["B"], "rejections": res["rejections"],
                "contract_version": res["contract_version"], "runtime_events": [e for e in rt.events if rid in e["event"] or "rejection" in e["event"] or "mediator" in e["event"] or "republished" in e["event"]]}

    snap["run"] = {
        "0001": run_block("0001", r1, logs1, rt.fixtures["0001"]["title"]),
        "0002": {**run_block("0002", r2, logs2, rt.fixtures["0002"]["title"]),
                 "republish": rt.fixtures["0002"]["republish"], "rejection": {k: rej.get(k) for k in ("code", "edge", "run", "seq", "receiver", "missing", "expected", "contract_version", "ts")} if rej else None,
                 "patch": {"plan": patch["plan"], "countersign": patch["countersign"], "merged_version": patch["merged_version"], "contract_version": patch["contract_version"], "ts": patch["ts"]} if patch else None,
                 "contract_v2": {"version": c2["version"], "status": "active", "reason": c2["reason"], "supersedes": c2["supersedes"], "issued_at": c2["issued_at"],
                                 "h1_allowlist": next(e["allowlist"] for e in c2["boundary_edges"] if e["id"] == "h1"), "h_B": c2["h_B"],
                                 "checks": len(c2["certificate"]["checks"])} if c2 else None},
        "contracts": [{k: c.get(k) for k in ("version", "status", "reason", "supersedes", "issued_at", "certificate_id")}
                      for c in db.col("contracts").find({"use_case_id": USE_CASE}).sort("version", 1)],
    }
    # §6 update: the report plus the slice of the derivation graph the design doc draws
    hit = rep["affected"]
    rebuilt_ids = {x["derived_id"] for x in rep["rebuilt"]}
    drv_by = {d["derived_id"]: d for d in rep["derivations"]}
    el_by = {e["element_id"]: e for e in rep["elements"]}
    changed_ids = {c["element_id"] for c in rep["delta"]["changed"]}
    show_elements = ["A.policy.large_order_approval", "A.fulfil_order.rule", "A.fulfil_order.tool", "B.updateOrderStatus.rule", "B.checkLoanCanBeProvided.tool", "answer.q3"]
    show_derived = ["guardrail:d1:policy_value:h3", "gate:A.approve_large_order", "guardrail:q1:allowlist:h1", "guardrail:q3:adapter:A.mark_invoice_paid_out_of_band",
                    "guardrail:q4:allowlist:h3", "guardrail:q3:predicate:h2"]
    show_outputs = ["check:PRE-03:h3", "check:CONF-01:h3", "projection:B", "projection:A", "check:IO-01:h1", "check:PRE-03:h2"]
    labels = {"A.policy.large_order_approval": "A policy · max_amount", "A.fulfil_order.rule": "A rule · fulfil_order precondition", "A.fulfil_order.tool": "A tool · wms.create_shipment",
              "B.updateOrderStatus.rule": "B rule · updateOrderStatus precondition", "B.checkLoanCanBeProvided.tool": "B tool · findEligibleProducts (verify)", "answer.q3": "A answer · q3 \"what counts as paid\""}
    def status_of(did):
        if did in rebuilt_ids: return "rebuilt"
        if did.startswith("check:") and any(did == f"check:{x.replace(' on ', ':')}" for x in rep["checks_rerun"]): return "rerun"
        if did.startswith("projection:") and did.split(":")[1] in rep["signatures_required"]: return "resigned"
        if did in hit: return "rebuilt"
        return "carried"
    graph = {
        "elements": [{"id": e, "label": labels.get(e, e), "kind": el_by[e]["kind"] if e in el_by else "answer", "org": e.split(".")[0] if not e.startswith("answer") else "A",
                      "changed": e in changed_ids, "old": next((c["old"] for c in rep["delta"]["changed"] if c["element_id"] == e), None),
                      "new": next((c["new"] for c in rep["delta"]["changed"] if c["element_id"] == e), None)} for e in show_elements],
        "derived": [{"id": d, "label": drv_by[d]["label"] if d in drv_by else d, "rebuild_fn": drv_by[d]["rebuild_fn"] if d in drv_by else "", "status": status_of(d),
                     "inputs": [i for i in (drv_by[d]["inputs"] if d in drv_by else []) if i in show_elements],
                     "result": next((x["result"] for x in rep["rebuilt"] if x["derived_id"] == d), None)} for d in show_derived],
        "outputs": [{"id": o, "label": (drv_by[o]["label"] if o in drv_by else o).replace("projection ", "Projection "), "status": status_of(o),
                     "inputs": [i for i in (drv_by[o]["inputs"] if o in drv_by else []) if i in show_elements],
                     "derived_inputs": [i for i in (drv_by[o]["derived_inputs"] if o in drv_by else []) if i in show_derived]
                                       + ([d for d in show_derived if d.endswith(":h3") or d.startswith("gate:")] if o.startswith("projection") and status_of(o) == "resigned" else [])}
                    for o in show_outputs],
    }
    cert3 = rep["certificate"]
    snap["update"] = {
        "change": rep["change"], "delta": rep["delta"], "class": rep["class"], "direction_note": rep["direction_note"],
        "rebuilt": rep["rebuilt"], "carried_guardrails": rep["carried_guardrails"], "checks_rerun": rep["checks_rerun"], "checks_carried": rep["checks_carried"],
        "projections_affected": rep["projections_affected"], "questions_reasked": rep["questions_reasked"], "questions_carried": rep["questions_carried"],
        "signatures_required": rep["signatures_required"], "friction": rep["friction"], "graph": graph,
        "certificate": {"certificate_id": cert3["certificate_id"], "supersedes": cert3["supersedes"], "delta_hash": cert3["delta"]["delta_hash"],
                        "checks": [{k: c.get(k) for k in ("check", "scope", "verdict", "status", "locality")} for c in cert3["checks"]],
                        "signatures": cert3["signatures"], "inputs": cert3["inputs"], "issued_at": cert3["issued_at"]},
        "contract_v3": {"version": rep["contract"]["version"], "status": rep["contract"]["status"], "reason": rep["contract"]["reason"], "supersedes": rep["contract"]["supersedes"],
                        "h3": next({"allowlist": e["allowlist"], "guard": to_text(e["guard"]), "policy": e["policy"]} for e in rep["contract"]["boundary_edges"] if e["id"] == "h3")},
        "index_counts": {"elements": len(rep["elements"]), "derivations": len(rep["derivations"])},
        "rerun_under_v3": {"A": {k: r3["A"].get(k) for k in ("status", "outcome")}, "contract_version": r3["contract_version"], "rejections": r3["rejections"],
                           "guard_results": next((h["handoff"]["metadata"]["pmp"]["guard_results"] for h in db.col("handoffs").find({"use_case_id": USE_CASE, "run": "0001", "edge": "h3", "role": "sender"}).sort("seq", -1)), {}),
                           "events": [e for o in ("A", "B") for e in logs3[o] if e["event"] in ("gate", "handoff_sent", "handoff_accepted", "completed")]},
        "table": [
            {"change": "Tighten", "internal_rule": "own slice only; owner signs", "handoff_rule": "precondition checks re-run; maybe one field question; both sign",
             "policy": "guard rebuilt if the merged value moves; both sign, no questions", "answer": "derived guardrail rebuilt; maybe one question", "tool": "own checks only"},
            {"change": "Loosen", "internal_rule": "same", "handoff_rule": "same, plus re-ask questions built on this rule",
             "policy": "guard rebuilt; re-ask questions built on this policy (usually none)", "answer": "same", "tool": "a step going from checks to does something re-asks what may be sent to it"},
            {"change": "Reshape", "internal_rule": "own slice only", "handoff_rule": "handoff contract rebuilt; step matching may re-run; both sign", "policy": "n/a", "answer": "n/a", "tool": "catalog and tool checks only"},
        ],
    }
    db.use_atlas()
    return json.loads(json.dumps(snap, default=str))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Precompute the demo into one JSON.")
    p.add_argument("--out", type=Path, default=OUT)
    p.add_argument("--fixture-align", action="store_true")
    a = p.parse_args(argv)
    snap = build(a.fixture_align)
    a.out.write_text(json.dumps(snap, indent=1) + "\n")
    print(f"wrote {a.out} ({a.out.stat().st_size // 1024} KB); alignments from {snap['models']['alignments_source']}; "
          f"update: {snap['update']['friction']}; checks re-run {snap['update']['checks_rerun']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
