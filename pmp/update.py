"""Stage 8: change one thing without redoing everything (design doc §6).

Every guardrail, check result, alignment and projection records what it was built from: the derivation
index (`elements` + `derivations`). A republish is a graph delta over elements (rule | policy | tool_binding |
schema | node | edge), classified by direction (tighten | loosen | reshape) and class (0 cosmetic, 1 internal,
2 boundary, 3 trust-relevant). The update is a reverse lookup:

  find everything built from the changed elements -> rebuild just that -> re-run just the checks that read
  it -> re-ask only the questions whose inputs moved (old answer as the default) -> require a new signature
  only from an org whose visible slice changed.

Certificate n+1 lists supersedes, the delta hash, checks re-run vs carried, questions re-asked and the
signatures required. Alignment inputs are limited to name, description, I/O and neighbourhood, so a rule
or policy edit never re-runs the aligner. In-flight runs finish on their version.

CLI: python -m pmp.update --use-case bnpl_checkout_v1 --org A --policy large_order_approval --max-amount 15000 [--dry-run]
"""
from __future__ import annotations
import argparse
import copy
import json
import re
import sys
from typing import Any

from pmp import contract as ct, db, merge, validate as v
from pmp.compile import jsonable, sha256
from pmp.decide import now, ORG_NAMES
from pmp.predicate import to_text

DEFAULT_USE_CASE = "bnpl_checkout_v1"
EDGE_CHECKS = {"IO-01", "IO-02", "PRE-01", "PRE-03", "POL-01", "CONF-01", "FAIL-01"}
DIRECTION_TEXT = {"tighten": "tightening can make an old guarantee insufficient: checks re-run",
                  "loosen": "loosening is the trust-sensitive direction: questions that assumed the stricter world would be re-asked",
                  "reshape": "reshaping changes names or data shapes: step matching may re-run"}


# --------------------------------------------------------------------------- elements

def _el(use_case: str, element_id: str, kind: str, org: str, host: str, content: Any, summary: str = "") -> dict:
    return {"_id": f"{use_case}:{element_id}", "use_case_id": use_case, "element_id": element_id, "kind": kind, "org_id": org,
            "host": host, "hash": sha256(content), "summary": summary[:160]}


def elements_of(sub: dict) -> list[dict]:
    """Typed-in things, per org: one element per node facet, edge, policy and declared handoff."""
    org, uc = sub["org_id"], sub["use_case_id"]
    els = []
    for n in sub["nodes"]:
        nid = n["_id"]
        els.append(_el(uc, nid, "node", org, nid, {"name": n["name"], "text": n["text"], "kind": n["kind"], "visibility": n["visibility"]}, n["name"]))
        els.append(_el(uc, f"{nid}.io", "schema", org, nid, {"inputs": n.get("inputs", []), "outputs": n.get("outputs", [])},
                       f"{len(n.get('inputs', []))} in / {len(n.get('outputs', []))} out"))
        if n.get("pre") or n.get("post") or n.get("branches"):
            els.append(_el(uc, f"{nid}.rule", "rule", org, nid, {"pre": n.get("pre"), "post": n.get("post"), "branches": n.get("branches")},
                           f"pre {to_text(n.get('pre'))}"))
        if n.get("tool"):
            els.append(_el(uc, f"{nid}.tool", "tool_binding", org, nid, n["tool"], f"{n['tool']['server']}.{n['tool']['name']} ({n['tool']['effect']})"))
        if n.get("policy"):
            els.append(_el(uc, f"{nid}.policy", "policy", org, nid, n["policy"], json.dumps(n["policy"].get("max_amount") or n["policy"], sort_keys=True)))
    for e in sub["transitions"]:
        els.append(_el(uc, e["_id"], "edge", org, e["_id"], {"from": e["from"], "to": e["to"], "condition": e.get("condition")}, to_text(e.get("condition"))))
    for name, pol in (sub.get("policies") or {}).items():
        els.append(_el(uc, f"{org}.policy.{name}", "policy", org, name, pol, to_text(pol.get("rule")) if isinstance(pol, dict) and pol.get("rule") else str(pol)[:80]))
    for h in sub.get("handoffs", []):
        els.append(_el(uc, f"{org}.handoff.{h['id']}", "schema", org, h["id"], h, h.get("purpose", "")))
    return els


# --------------------------------------------------------------------------- derivations

def _d(use_case: str, derived_id: str, kind: str, rebuild_fn: str, inputs: list[str], derived_inputs: list[str] | None = None,
       built_by: str | None = None, output: Any = None, label: str = "") -> dict:
    return {"_id": f"{use_case}:{derived_id}", "use_case_id": use_case, "derived_id": derived_id, "kind": kind, "rebuild_fn": rebuild_fn,
            "inputs": inputs, "derived_inputs": derived_inputs or [], "built_from": list(inputs) + list(derived_inputs or []),
            "built_by": built_by, "output_hash": sha256(output) if output is not None else None, "label": label}


def derivations_of(merged: dict, subs: dict[str, dict], questions: list[dict], decisions: list[dict], cert_checks: list[dict],
                   alignments: list[dict]) -> list[dict]:
    """What every guardrail, guard, check, alignment and projection was built from."""
    uc = merged["use_case_id"]
    nodes = {n["_id"]: n for n in merged["nodes"]}
    edges = {e["_id"]: e for e in merged["boundary_edges"]}
    ctx = v.Ctx(merged, subs, alignments, questions, None, "revalidate")
    ds: list[dict] = []

    def facets(nid: str, *kinds: str) -> list[str]:
        n = nodes.get(nid)
        if not n:
            return []
        out = []
        for k in kinds:
            if k == "node":
                out.append(nid)
            elif k == "io":
                out.append(f"{nid}.io")
            elif k == "rule" and (n.get("pre") or n.get("post") or n.get("branches")):
                out.append(f"{nid}.rule")
            elif k == "tool" and n.get("tool"):
                out.append(f"{nid}.tool")
            elif k == "policy" and n.get("policy"):
                out.append(f"{nid}.policy")
        return out

    def original(nid: str) -> str:
        """An adapter stands in for the submitted node it wraps."""
        n = nodes.get(nid) or {}
        if not n.get("origin"):
            return nid
        for e in merged["edges"]:
            if e["to"] == nid and not nodes.get(e["from"], {}).get("origin"):
                return e["from"]
        return nid

    # guardrails from answers and rule decisions
    guard_ids_by_edge: dict[str, list[str]] = {}
    for g in merged.get("guardrails", []):
        q = next((x for x in questions if x["_id"] == g.get("question")), None)
        edge = edges.get(g.get("edge") or "")
        by = g.get("question") or g.get("decision")
        inputs = [f"answer.{q['_id']}"] if q else []
        if g["type"] == "policy_value" and edge:
            s, r = nodes[edge["from"]], nodes[edge["to"]]
            inputs = facets(original(s["_id"]), "policy") + [x for gate in ctx.gates_before(s["_id"]) for x in facets(gate["_id"], "policy")] \
                     + facets(r["_id"], "policy") + [f"{s['org_id']}.policy.{k}" for k, p in subs[s["org_id"]].get("policies", {}).items() if "approval" in k]
            did, label = f"guardrail:{by}:policy_value:{edge['_id']}", f"{edge['_id']} boundary guard (lattice)"
        elif g["type"] == "allowlist" and edge:
            inputs += facets(original(edge["from"]), "io") + [f"{nodes[edge['from']]['org_id']}.handoff.{(edge.get('handoff_out') or {}).get('id')}"]
            did, label = f"guardrail:{by}:allowlist:{edge['_id']}", f"{edge['_id']} allowlist ({by})"
        elif g["type"] == "adapter_node" and edge:
            nid = (g.get("node") or {}).get("_id", "")
            inputs += facets(original(edge["from"]), "io") + facets(edge["to"] if not nodes.get(edge["to"], {}).get("origin") else original(edge["to"]), "io")
            if "paid" in nid:
                inputs += [f"{nodes[edge['to']]['org_id']}.policy.{k}" for k in subs[nodes[edge["to"]]["org_id"]].get("policies", {}) if "paid" in k]
                inputs += [f"{nodes[edge['from']]['org_id']}.handoff.{(edge.get('handoff_out') or {}).get('id')}"]
            did, label = f"guardrail:{by}:adapter:{nid}", f"{nid.split('.')[-1]} adapter ({by})"
        elif g["type"] == "predicate" and edge:
            inputs += [f"{nodes[edge['to']]['org_id']}.policy.{k}" for k in subs[nodes[edge["to"]]["org_id"]].get("policies", {}) if "paid" in k]
            inputs += [f"{nodes[edge['from']]['org_id']}.handoff.{(edge.get('handoff_out') or {}).get('id')}"]
            did, label = f"guardrail:{by}:predicate:{edge['_id']}", f"{edge['_id']} runtime guard ({by})"
        elif g["type"] == "compensation_edge":
            inputs += facets(g.get("from", ""), "node") + facets(g.get("to", ""), "node")
            did, label = f"guardrail:{by}:compensation_edge", f"compensation edge {g.get('on')} ({by})"
        elif g["type"] == "alignment_decision":
            al = next((a for a in alignments if a["_id"] == g.get("value")), None)
            inputs += (facets(al["a"], "node", "io") + facets(al["b"], "node", "io")) if al else []
            did, label = f"guardrail:{by}:alignment_decision", f"keep-both decision ({by})"
        else:
            did, label = f"guardrail:{by}:{g['type']}", g["type"]
        ds.append(_d(uc, did, "guardrail", {"policy_value": "lattice", "allowlist": "allowlist", "adapter_node": "adapter", "predicate": "guard",
                                             "compensation_edge": "guard", "alignment_decision": f"human:{q['to_org']}" if q else "human"}.get(g["type"], "guard"),
                     sorted(set(inputs)), built_by=by, output=g, label=label))
        if edge:
            guard_ids_by_edge.setdefault(edge["_id"], []).append(did)

    # runtime guards and gate conditions (derived from rules + policy guards)
    for eid, e in edges.items():
        gp = v.guard_predicate(ctx, e)
        s = nodes[e["from"]]
        inputs = facets(original(s["_id"]), "rule") + [x for gate in ctx.gates_before(s["_id"]) for x in facets(gate["_id"], "rule", "policy")]
        ds.append(_d(uc, f"guard:{eid}", "guard", "guard", sorted(set(inputs)), [d for d in guard_ids_by_edge.get(eid, [])], output=gp,
                     label=f"{eid} guard: {to_text(gp) if gp else 'none'}"))
    for n in merged["nodes"]:
        if n["kind"] == "human_gate":
            deps = [x["_id"] for x in merged["nodes"] if any(e["from"] == n["_id"] and e["to"] == x["_id"] for e in merged["edges"])]
            inputs = facets(n["_id"], "rule", "policy") + [x for d_ in deps for x in facets(d_, "rule")] \
                     + [f"{n['org_id']}.policy.{k}" for k in subs[n["org_id"]].get("policies", {}) if "approval" in k]
            ds.append(_d(uc, f"gate:{n['_id']}", "gate", "guard", sorted(set(inputs)), output={"pre": n.get("pre"), "policy": n.get("policy")},
                         label=f"{n['name']} gate condition"))

    # alignments: name, description, I/O and neighbourhood only
    for al in alignments:
        ds.append(_d(uc, f"alignment:{al['a_name']}~{al['b_name']}", "alignment", "align", facets(al["a"], "node", "io") + facets(al["b"], "node", "io"),
                     output={k: al.get(k) for k in ("sigma", "iota", "tau", "proposal")}, label=f"{al['a_name']} ~ {al['b_name']}"))

    # checks: edge-scoped read the two endpoint nodes and the guardrails on the edge; graph-scoped read what they inspect
    for c in cert_checks:
        chk, scope = c["check"], c["scope"]
        inputs: list[str] = []
        derived: list[str] = []
        if scope in edges:
            e = edges[scope]
            inputs = facets(original(e["from"]), "io", "rule", "policy", "tool") + facets(original(e["to"]), "io", "rule", "policy", "tool") \
                     + [x for gate in ctx.gates_before(e["from"]) for x in facets(gate["_id"], "rule", "policy")]
            derived = guard_ids_by_edge.get(scope, []) + [f"guard:{scope}"]
        elif chk.startswith("STR"):
            inputs = [t["_id"] for s_ in subs.values() for t in s_["transitions"]]
            derived = [d["derived_id"] for d in ds if d["kind"] == "guardrail" and "compensation" in d["derived_id"]]
        elif chk.startswith("TOOL"):
            inputs = [x for n in merged["nodes"] for x in facets(n["_id"], "tool", "policy")] + [f"{o}.policy.{k}" for o, s_ in subs.items() for k in s_.get("policies", {}) if "approval" in k]
        elif chk in ("DUP-02", "ALN-01"):
            derived = [d["derived_id"] for d in ds if d["kind"] == "alignment"] + [d["derived_id"] for d in ds if "alignment_decision" in d["derived_id"]]
        elif chk.startswith("INV") or chk == "PRIV-01":
            derived = [f"projection:{scope[-1]}"]
        ds.append(_d(uc, f"check:{chk}:{scope}", "check", "check", sorted(set(inputs)), sorted(set(derived)), output={"verdict": c["verdict"], "evidence": c["evidence"]},
                     label=f"{chk} on {scope}"))

    # boundary interface and projections
    ds.append(_d(uc, "interface", "interface", "project", [], [f"guard:{eid}" for eid in edges] + [d for l in guard_ids_by_edge.values() for d in l],
                 label="boundary interface"))
    for org in ("A", "B"):
        ds.append(_d(uc, f"projection:{org}", "projection", "project", [el["element_id"] for el in elements_of(subs[org])], ["interface"],
                     label=f"projection {org}"))
    return ds


def build_index(merged: dict, subs: dict[str, dict], questions: list[dict], decisions: list[dict], cert_checks: list[dict],
                alignments: list[dict]) -> tuple[list[dict], list[dict]]:
    els = elements_of(subs["A"]) + elements_of(subs["B"])
    return els, derivations_of(merged, subs, questions, decisions, cert_checks, alignments)


def write_index(els: list[dict], ds: list[dict], use_case: str) -> None:
    db.col("elements").delete_many({"use_case_id": use_case})
    db.col("derivations").delete_many({"use_case_id": use_case})
    if els:
        db.col("elements").insert_many(els)
    if ds:
        db.col("derivations").insert_many(ds)


# --------------------------------------------------------------------------- republish + delta

def _numbers(x: Any) -> list[float]:
    if isinstance(x, bool):
        return []
    if isinstance(x, (int, float)):
        return [float(x)]
    if isinstance(x, dict):
        return [n for val in x.values() for n in _numbers(val)]
    if isinstance(x, list):
        return [n for val in x for n in _numbers(val)]
    return []


def _replace_amount(x: Any, old: float, new: float) -> Any:
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float)) and float(x) == old:
        return type(x)(new) if isinstance(x, int) else new
    if isinstance(x, dict):
        return {k: _replace_amount(val, old, new) for k, val in x.items()}
    if isinstance(x, list):
        return [_replace_amount(val, old, new) for val in x]
    if isinstance(x, str) and str(int(old)) in x:
        return x.replace(str(int(old)), str(int(new)))
    return x


def republish_policy(sub: dict, policy: str, max_amount: float) -> tuple[dict, dict]:
    """The org raises (or lowers) a money threshold in its own file. Every place its procedure states that
    number moves with it: the org policy rule, the gate node's policy and trigger, the shipping precondition,
    the tool's gate note. Returns (new submission, change summary)."""
    new = copy.deepcopy(sub)
    pol = new["policies"][policy]
    old_minor = max(_numbers(pol.get("rule")))
    old_major = old_minor / 100
    new_minor = max_amount * 100
    pol["rule"] = _replace_amount(pol["rule"], old_minor, new_minor)
    for n in new["nodes"]:
        for k in ("pre", "post", "branches"):
            if n.get(k):
                n[k] = _replace_amount(n[k], old_minor, new_minor)
        if n.get("policy", {}).get("max_amount"):
            n["policy"]["max_amount"]["amount"] = _replace_amount(n["policy"]["max_amount"]["amount"], old_major, max_amount)
        if (n.get("tool") or {}).get("requires_gate"):
            n["tool"]["requires_gate"] = _replace_amount(n["tool"]["requires_gate"], old_minor, new_minor)
        if n.get("text"):
            n["text"] = n["text"].replace(f"{int(old_major):,}", f"{int(max_amount):,}")
    major, minor, patch = (new.get("version") or "1.0.0").split(".")[:3]
    new["version"] = f"{major}.{int(minor) + 1}.0"
    body = {k: val for k, val in new.items() if k not in ("hash", "_id", "republished")}
    new["hash"] = sha256(body)
    change = {"org": sub["org_id"], "policy": policy, "field": "max_amount", "old": old_major, "new": max_amount,
              "currency": next((n["policy"]["max_amount"]["currency"] for n in sub["nodes"] if n.get("policy", {}).get("max_amount")), "USD"),
              "direction": "loosen" if max_amount > old_major else "tighten" if max_amount < old_major else "reshape",
              "version": {"old": sub.get("version"), "new": new["version"]}, "ts": now()}
    new["republished"] = change
    return new, change


def graph_delta(old_sub: dict, new_sub: dict) -> dict:
    old = {e["element_id"]: e for e in elements_of(old_sub)}
    new = {e["element_id"]: e for e in elements_of(new_sub)}
    changed, added, removed = [], [], []
    for eid, e in new.items():
        if eid not in old:
            added.append({"element_id": eid, "kind": e["kind"], "host": e["host"], "hash": e["hash"], "summary": e["summary"]})
        elif old[eid]["hash"] != e["hash"]:
            o = old[eid]
            direction = "reshape"
            if e["kind"] in ("policy", "rule", "tool_binding"):
                on, nn = max(_numbers_of_summary(o), default=None), max(_numbers_of_summary(e), default=None)
                if on is not None and nn is not None and on != nn:
                    direction = "loosen" if nn > on else "tighten"
            changed.append({"element_id": eid, "kind": e["kind"], "host": e["host"], "old_hash": o["hash"], "new_hash": e["hash"],
                            "old": o["summary"], "new": e["summary"], "direction": direction})
    for eid, e in old.items():
        if eid not in new:
            removed.append({"element_id": eid, "kind": e["kind"], "host": e["host"], "hash": e["hash"]})
    d = {"org": new_sub["org_id"], "h_old": old_sub["hash"], "h_new": new_sub["hash"], "changed": changed, "added": added, "removed": removed}
    d["delta_hash"] = sha256({k: d[k] for k in ("changed", "added", "removed")})
    return d


def _numbers_of_summary(el: dict) -> list[float]:
    return [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*\.?\d*", el.get("summary", "")) if x.replace(",", "").replace(".", "").isdigit()]


# --------------------------------------------------------------------------- reverse lookup + rebuild

def affected_atlas(use_case: str, changed_ids: set[str]) -> dict[str, list[str]] | None:
    """The same reverse lookup as a single $graphLookup over `derivations` (built_from is inputs + derived_inputs):
    start from the changed element ids and follow every derivation built from them, transitively."""
    if db.in_memory() or not changed_ids:
        return None
    pipeline = [
        {"$limit": 1},
        {"$graphLookup": {"from": "derivations", "startWith": sorted(changed_ids), "connectFromField": "derived_id",
                          "connectToField": "built_from", "as": "hit", "restrictSearchWithMatch": {"use_case_id": use_case}}},
        {"$project": {"hit.derived_id": 1, "hit.built_from": 1}},
    ]
    doc = next(db.col("derivations").aggregate(pipeline), None)
    if not doc:
        return None
    ids = {h["derived_id"] for h in doc["hit"]}
    return {h["derived_id"]: [x for x in h["built_from"] if x in changed_ids or x in ids] for h in doc["hit"]}


def affected(derivations: list[dict], changed_ids: set[str]) -> dict[str, list[str]]:
    """derived_id -> the changed elements / derived things it was built from (transitively). Pure-Python form;
    affected_atlas() is the same lookup as an Atlas $graphLookup."""
    by_id = {d["derived_id"]: d for d in derivations}
    hit: dict[str, list[str]] = {}
    for d in derivations:
        direct = [i for i in d["inputs"] if i in changed_ids]
        if direct:
            hit[d["derived_id"]] = direct
    frontier = list(hit)
    while frontier:
        nxt = []
        for d in derivations:
            if d["derived_id"] in hit:
                continue
            via = [i for i in d["derived_inputs"] if i in hit]
            if via:
                hit[d["derived_id"]] = via
                nxt.append(d["derived_id"])
        frontier = nxt
    return hit


def classify(delta: dict, hit: dict[str, list[str]], questions_reasked: list[str]) -> int:
    if questions_reasked or any(c["direction"] == "loosen" and c["kind"] == "schema" for c in delta["changed"]):
        return 3
    if any(k.startswith(("guardrail:", "guard:", "interface")) for k in hit):
        return 2
    if delta["changed"] or delta["added"] or delta["removed"]:
        return 1 if any(c["kind"] != "node" for c in delta["changed"]) or delta["added"] or delta["removed"] else 0
    return 0


def apply_update(org: str, policy: str, max_amount: float, *, use_case: str = DEFAULT_USE_CASE, dry_run: bool = False) -> dict:
    """The whole loop for one policy republish. Returns the report the certificate is built from."""
    current = db.col("contracts").find_one({"use_case_id": use_case, "status": {"$in": ["active", "suspect"]}}, sort=[("version", -1)])
    if not current:
        raise SystemExit("no active contract; run the pipeline first")
    merged = db.col("merged").find_one({"use_case_id": use_case, "doc_type": "graph"}, sort=[("version", -1)])
    subs = {s["org_id"]: s for s in db.col("submissions").find({"use_case_id": use_case})}
    als = list(db.col("alignments").find({"use_case_id": use_case}))
    qs = list(db.col("merge_questions").find({"use_case_id": use_case}).sort("_id", 1))
    ds_ = list(db.col("merge_log").find({"use_case_id": use_case, "type": "decision"}))
    cert = current["certificate"]

    # 1. the index as it stands, then the republish and its delta
    elements, derivations = build_index(merged, subs, qs, ds_, cert["checks"], als)
    new_sub, change = republish_policy(subs[org], policy, max_amount)
    delta = graph_delta(subs[org], new_sub)
    changed_ids = {c["element_id"] for c in delta["changed"] + delta["added"] + delta["removed"]}

    # 2. reverse lookup: $graphLookup over the index in Atlas, the same walk in Python otherwise
    write_index(elements, derivations, use_case)
    hit = affected_atlas(use_case, changed_ids) or affected(derivations, changed_ids)
    q_inputs = {q["_id"]: [i for d in derivations for i in d["inputs"] if d.get("built_by") == q["_id"]] for q in qs}
    questions_reasked = [qid for qid, ins in q_inputs.items() if set(ins) & changed_ids]
    change_class = classify(delta, hit, questions_reasked)

    # 3. rebuild: the org's own nodes take their republished facets; boundary guards on affected edges are rebuilt by rule
    new_merged = copy.deepcopy(merged)
    new_merged["_id"], new_merged["version"], new_merged["supersedes"] = f"{use_case}:v{merged['version'] + 1}", merged["version"] + 1, merged["version"]
    fresh = {n["_id"]: n for n in new_sub["nodes"]}
    facet_keys = {"rule": ("pre", "post", "branches"), "policy": ("policy",), "tool_binding": ("tool",), "node": ("text", "name", "kind", "visibility")}
    for c in delta["changed"]:                                  # only the facets that moved; guardrail-added fields survive
        n = next((x for x in new_merged["nodes"] if x["_id"] == c["host"]), None)
        f = fresh.get(c["host"])
        if not n or not f:
            continue
        if c["kind"] == "schema":
            for side in ("inputs", "outputs"):
                keep = [x for x in n.get(side, []) if x.get("origin") or x.get("source", "").startswith("echo")]
                n[side] = copy.deepcopy(f.get(side, [])) + [x for x in keep if x["path"] not in {y["path"] for y in f.get(side, [])}]
        else:
            for k in facet_keys.get(c["kind"], ()):
                if k in f:
                    n[k] = copy.deepcopy(f[k])
                elif k in n and k in ("pre", "post", "branches", "policy", "tool"):
                    n.pop(k)
    new_subs = {**subs, org: new_sub}
    rebuilt: list[dict] = []
    ctx = v.Ctx(new_merged, new_subs, als, qs, None, "revalidate")
    for r in v.pol_01(ctx):                                   # lattice: the stricter side wins, recomputed from the new policies
        if r.get("guard") and any(k == f"guardrail:d1:policy_value:{r['scope']}" or k.endswith(f":policy_value:{r['scope']}") for k in hit):
            for x in [e for e in new_merged["edges"] if e["_id"] == r["scope"]] + [e for e in new_merged["boundary_edges"] if e["_id"] == r["scope"]]:
                x["guard"] = {**(x.get("guard") or {}), **r["guard"]}
            rebuilt.append({"derived_id": next(k for k in hit if k.endswith(f":policy_value:{r['scope']}")), "rebuild_fn": "lattice", "result": r["guard"]})
    for k in hit:
        if k.startswith("guard:"):
            eid = k.split(":")[1]
            gp = v.guard_predicate(v.Ctx(new_merged, new_subs, als, qs, None, "revalidate"), next(e for e in new_merged["boundary_edges"] if e["_id"] == eid))
            rebuilt.append({"derived_id": k, "rebuild_fn": "guard", "result": to_text(gp) if gp else None})
        elif k.startswith("gate:"):
            n = next(x for x in new_merged["nodes"] if x["_id"] == k.split(":", 1)[1])
            rebuilt.append({"derived_id": k, "rebuild_fn": "guard", "result": f"{to_text(n.get('pre'))}; policy {json.dumps(n.get('policy', {}).get('max_amount'))}"})
    carried_guardrails = [d["derived_id"] for d in derivations if d["kind"] in ("guardrail", "guard", "gate") and d["derived_id"] not in hit]
    new_merged["inputs"]["h_A"], new_merged["inputs"]["h_B"] = new_subs["A"]["hash"], new_subs["B"]["hash"]
    new_merged["republishes"] = new_merged.get("republishes", []) + [change]
    new_merged = jsonable(new_merged)
    new_merged["hash"] = sha256({k: new_merged[k] for k in ("nodes", "edges", "boundary_edges", "failure_candidates", "merge_candidates", "inputs")})

    # 4. re-run only the checks that read something that moved; carry the rest from certificate n
    registry = {c for c, _, _ in v.REGISTRY}
    rerun_keys = {(k.split(":")[1], k.split(":", 2)[2]) for k in hit if k.startswith("check:") and k.split(":")[1] in registry}
    projections_affected = sorted(k.split(":")[1] for k in hit if k.startswith("projection:"))
    rerun = v.run(new_merged, list(new_subs.values()), als, qs, db=None, stage="revalidate", only=rerun_keys)
    for r in rerun:
        r["status"] = "rerun"
    carried = []
    for c in cert["checks"]:
        if (c["check"], c["scope"]) in rerun_keys or c["check"].startswith("INV") or c["check"] == "PRIV-01":
            continue
        carried.append({"_id": f"{use_case}:revalidate:v{new_merged['version']}:{c['check']}:{v._slug(c['scope'])}", "use_case_id": use_case,
                        "stage": "revalidate", "merged_version": new_merged["version"], "check": c["check"], "verdict": c["verdict"],
                        "scope": c["scope"], "detail": c["evidence"], "owner_org": "validator", "locality": c["locality"], "resolved_by": None,
                        "status": "carried"})
    results = rerun + carried

    # 5. projections + certificate n+1; sign only where the visible slice changed
    prev_proj = {o: db.col("projections").find_one({"_id": f"{use_case}:{o}:v{current['version']}"}) for o in ("A", "B")}
    out = ct.build(new_merged, list(new_subs.values()), als, qs, ds_, contract_version=current["version"] + 1, db=None,
                   supersedes=current["version"], reason=f"republish: {ORG_NAMES[org]} {policy} {change['field']} {change['old']:,.0f} -> {change['new']:,.0f} ({change['direction']})",
                   results=results,
                   cert_extra={"supersedes": current["certificate_id"], "delta": {"org": org, "delta_hash": delta["delta_hash"], "changed": delta["changed"],
                                                                                  "added": delta["added"], "removed": delta["removed"], "direction": change["direction"],
                                                                                  "class": change_class},
                               "checks_rerun": sorted(f"{c} on {s}" for c, s in rerun_keys), "checks_carried": len(carried),
                               "questions_reasked": questions_reasked, "questions_carried": [q["_id"] for q in qs if q["_id"] not in questions_reasked]})
    signatures_required = [o for o in ("A", "B") if not prev_proj[o] or prev_proj[o]["hash"] != out["projections"][o]["hash"]]
    partner = "B" if org == "A" else "A"
    friction = (f"{ORG_NAMES[partner]} will need to re-sign but not answer anything" if partner in signatures_required and not questions_reasked
                else f"{ORG_NAMES[partner]} will be asked {', '.join(questions_reasked)} and must re-sign" if questions_reasked
                else f"{ORG_NAMES[partner]}'s slice is unchanged: no signature, no questions")
    out["certificate"]["signatures_required"] = signatures_required
    out["certificate"]["friction"] = friction
    out["contract"]["certificate"]["signatures_required"] = signatures_required
    out["contract"]["certificate"]["friction"] = friction
    for o in ("A", "B"):
        sig = out["contract"]["signatures"][o]
        if o not in signatures_required:
            sig.update({"carried_from": current["certificate_id"], "signed_at": current["signatures"][o]["signed_at"]})
    out["contract"]["certificate"]["signatures"] = out["contract"]["signatures"]
    out["certificate"]["signatures"] = out["contract"]["signatures"]

    report = {"change": change, "delta": delta, "class": change_class, "direction_note": DIRECTION_TEXT[change["direction"]],
              "affected": hit, "rebuilt": rebuilt, "carried_guardrails": carried_guardrails,
              "checks_rerun": sorted(f"{c} on {s}" for c, s in rerun_keys), "checks_carried": len(carried),
              "projections_affected": projections_affected,
              "questions_reasked": questions_reasked, "questions_carried": [q["_id"] for q in qs if q["_id"] not in questions_reasked],
              "signatures_required": signatures_required, "friction": friction,
              "contract": out["contract"], "certificate": out["certificate"], "projections": out["projections"], "merged": new_merged,
              "elements": elements, "derivations": derivations, "new_submission": new_sub}
    if not dry_run:
        db.col("submissions").replace_one({"_id": new_sub["_id"]}, new_sub)
        merge.write(new_merged)
        ct.write(out, als)
        new_els, new_ds = build_index(new_merged, new_subs, qs, ds_, out["certificate"]["checks"], als)
        write_index(new_els, new_ds, use_case)
        db.col("merge_log").insert_one({"_id": f"{use_case}:republish:v{out['contract']['version']}", "use_case_id": use_case, "type": "republish",
                                        "change": change, "delta_hash": delta["delta_hash"], "class": change_class, "rebuilt": [r["derived_id"] for r in rebuilt],
                                        "checks_rerun": report["checks_rerun"], "checks_carried": len(carried), "questions_reasked": questions_reasked,
                                        "signatures_required": signatures_required, "friction": friction,
                                        "contract_version": out["contract"]["version"], "supersedes": current["version"], "ts": now()})
    return report


def summary(r: dict) -> str:
    c = r["change"]
    lines = [f"republish: {ORG_NAMES[c['org']]} {c['policy']}.{c['field']} {c['currency']} {c['old']:,.0f} -> {c['new']:,.0f}  ({c['direction']}, class {r['class']})",
             f"  delta: {len(r['delta']['changed'])} element(s) changed: " + ", ".join(f"{x['element_id']} ({x['kind']})" for x in r["delta"]["changed"]),
             f"  rebuilt: " + "; ".join(f"{x['derived_id']} [{x['rebuild_fn']}]" for x in r["rebuilt"]),
             f"  carried guardrails: {len(r['carried_guardrails'])}",
             f"  checks re-run: {', '.join(r['checks_rerun'])}; carried: {r['checks_carried']}",
             f"  questions re-asked: {r['questions_reasked'] or 'none'}; carried: {', '.join(r['questions_carried'])}",
             f"  signatures required: {', '.join(ORG_NAMES[o] for o in r['signatures_required'])}",
             f"  friction: {r['friction']}",
             f"  contract v{r['contract']['version']} {r['contract']['status']} (supersedes v{r['contract']['supersedes']}); "
             f"h3 guard now {next((to_text(e['guard']) for e in r['contract']['boundary_edges'] if e['id'] == 'h3' and e.get('guard')), 'none')}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Stage 8: republish one policy and update only what was built from it.")
    p.add_argument("--use-case", default=DEFAULT_USE_CASE)
    p.add_argument("--org", default="A", choices=["A", "B"])
    p.add_argument("--policy", default="large_order_approval")
    p.add_argument("--max-amount", type=float, default=15000)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    r = apply_update(a.org, a.policy, a.max_amount, use_case=a.use_case, dry_run=a.dry_run)
    print(summary(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
