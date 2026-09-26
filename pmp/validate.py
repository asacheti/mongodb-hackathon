"""Stage 3b: the check registry. Deterministic, no LLM.

Each check is a function (ctx) -> list[result]. A result is a finding doc minus _id/use_case_id/stage,
with verdict pass|fail|guarded, scope, detail, rung (1 lattice, 2 adapter, 3 reorder, 4 compensation
edge, 5 ask, 6 reject), stakes low|high, stakes_reason, owner_org and locality (local: an org can re-run
it on its projection; mediator: needs both graphs).

stakes is low ONLY if the fix is derivable from the two submissions alone and touches no money, pii or
customer messaging. Everything else is high and becomes a question in Stage 4.

Written findings = every non-pass result plus pass results that record a guard (POL-01), so the
`findings` collection holds what needs attention, while run() returns every result for the certificate.

CLI: python -m pmp.validate --use-case bnpl_checkout_v1 --version 1 [--dry-run]
"""
from __future__ import annotations
import argparse
import re
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable

from pmp import spec
from pmp.predicate import evaluate, fields_referenced, to_text

DEFAULT_USE_CASE = "bnpl_checkout_v1"
SENSITIVE = {"pii", "financial", "secret"}
NEAR_DUPLICATE_SIGMA = 0.85
FAILURE_EDGE_TYPES = {"failure", "compensation"}


# --------------------------------------------------------------------------- context

@dataclass
class Ctx:
    merged: dict
    subs: dict[str, dict]
    alignments: list[dict]
    questions: list[dict] = field(default_factory=list)
    db: Any = None
    stage: str = "validate"

    def __post_init__(self):
        self.nodes = {n["_id"]: n for n in self.merged["nodes"]}
        self.edges = self.merged["edges"]
        self.boundary = self.merged.get("boundary_edges", [])
        self.out: dict[str, list[dict]] = defaultdict(list)
        self.inc: dict[str, list[dict]] = defaultdict(list)
        for e in self.edges:
            self.out[e["from"]].append(e)
            self.inc[e["to"]].append(e)

    def node(self, nid: str) -> dict:
        return self.nodes[nid]

    def sub(self, node: dict) -> dict:
        return self.subs[node["org_id"]]

    @staticmethod
    def is_terminal(n: dict) -> bool:
        return n["kind"] == "terminal" or bool(n.get("outcome"))

    @staticmethod
    def is_failure_edge(e: dict) -> bool:
        if e.get("type") in FAILURE_EDGE_TYPES:
            return True
        return bool(e.get("condition")) and "fail" in to_text(e["condition"]).lower()

    def downstream(self, nid: str, *, skip_failure: bool = False) -> set[str]:
        seen, q = set(), deque([nid])
        while q:
            cur = q.popleft()
            for e in self.out[cur]:
                if skip_failure and self.is_failure_edge(e):
                    continue
                if e["to"] not in seen:
                    seen.add(e["to"])
                    q.append(e["to"])
        return seen

    def upstream(self, nid: str) -> set[str]:
        seen, q = set(), deque([nid])
        while q:
            cur = q.popleft()
            for e in self.inc[cur]:
                if e["from"] not in seen:
                    seen.add(e["from"])
                    q.append(e["from"])
        return seen

    def gates_before(self, nid: str) -> list[dict]:
        """human_gate nodes of the same org that feed this node, walking back through gates only."""
        org, out, q, seen = self.node(nid)["org_id"], [], deque([nid]), set()
        while q:
            cur = q.popleft()
            for e in self.inc[cur]:
                p = self.nodes.get(e["from"])
                if p and p["org_id"] == org and p["kind"] == "human_gate" and p["_id"] not in seen:
                    seen.add(p["_id"])
                    out.append(p)
                    q.append(p["_id"])
        return out

    def financed_path(self, first_boundary: dict) -> set[str]:
        """Nodes reached from the first A->B handoff by crossing it (not the sender's local alternative),
        continuing through the partner and back, ignoring failure/retry edges."""
        start = first_boundary["from"]
        seen, q = {start, first_boundary["to"]}, deque([first_boundary["to"]])
        while q:
            cur = q.popleft()
            for e in self.out[cur]:
                if self.is_failure_edge(e) or e["to"] in seen:
                    continue
                seen.add(e["to"])
                q.append(e["to"])
        return seen


def result(check: str, verdict: str, scope: str, detail: str, *, rung: int | None = None, stakes: str | None = None,
           stakes_reason: str | None = None, owner: str = "validator", locality: str = "local", **extra) -> dict:
    r = {"check": check, "verdict": verdict, "scope": scope, "detail": detail, "owner_org": owner, "locality": locality,
         "resolved_by": None}
    if rung is not None:
        r["rung"] = rung
    if stakes:
        r["stakes"] = stakes
    if stakes_reason:
        r["stakes_reason"] = stakes_reason
    r.update(extra)
    return r


def _leaf(path: str) -> str:
    return path.split(".")[-1]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _eq_atoms(pred: dict | None) -> list[tuple[str, Any]]:
    if not pred:
        return []
    if pred["op"] == "eq" and isinstance(pred["args"][0], str):
        return [(pred["args"][0], pred["args"][1])]
    if pred["op"] in ("and", "or"):
        return [a for p in pred["args"] for a in _eq_atoms(p)]
    return []


# --------------------------------------------------------------------------- checks

def io_01(ctx: Ctx) -> list[dict]:
    """Unit / type / name mismatch across a boundary edge: sender outputs vs receiver inputs."""
    out = []
    for e in ctx.boundary:
        S, R = e["sender_fields"], e["receiver_fields"]
        mism: list[dict] = []
        s_money = [f for f in S if f.get("unit") or f.get("type") == "money"]
        r_money = [f for f in R if f.get("unit") or f.get("type") == "money"]
        for r in r_money:
            for s in s_money:
                if (s.get("unit") or "major") != (r.get("unit") or "major"):
                    mism.append({"kind": "unit", "sender": s["path"], "sender_unit": s.get("unit") or "major",
                                 "receiver": r["path"], "receiver_unit": r.get("unit") or "major"})
        r_names = {_norm(_leaf(f["path"])) for f in R}
        for group in {(m["sender"].split(".")[0], m["receiver"].split(".")[0]) for m in mism}:
            s_ids = [f for f in S if f["path"].startswith(group[0] + ".") and f["type"] == "string"]
            r_ids = [f for f in R if f["path"].startswith(group[1] + ".") and f["type"] == "string"
                     and not any(_norm(_leaf(f["path"])) == _norm(_leaf(s["path"])) for s in S)]
            for s, r in zip(s_ids, r_ids):
                mism.append({"kind": "name", "sender": s["path"], "receiver": r["path"]})
        for r in R:
            for s in S:
                if _norm(_leaf(r["path"])) == _norm(_leaf(s["path"])) and r["type"] != s["type"] and "money" not in (r["type"], s["type"]):
                    mism.append({"kind": "type", "sender": s["path"], "receiver": r["path"],
                                 "sender_type": s["type"], "receiver_type": r["type"]})
        if not mism:
            out.append(result("IO-01", "pass", e["_id"], "sender outputs and receiver inputs agree on units, types and names"))
            continue
        units = [m for m in mism if m["kind"] == "unit"]
        names = [m for m in mism if m["kind"] == "name"]
        s_desc = ", ".join(sorted({_leaf(m["sender"]) for m in units})) + (f" ({units[0]['sender_unit']})" if units else "")
        r_desc = ", ".join(sorted({_leaf(m["receiver"]) + "{currency, amount}" for m in units})) + (f" ({units[0]['receiver_unit']} units)" if units else "")
        detail = (f"Sender emits {s_desc}" + (" + " + ", ".join(_leaf(m["sender"]) for m in names) if names else "")
                  + f"; receiver expects {r_desc}" + (" + " + ", ".join(_leaf(m["receiver"]) for m in names) if names else "") + ".")
        sender = ctx.node(e["from"])
        out.append(result("IO-01", "fail", e["_id"], detail, rung=2, stakes="low",
                          stakes_reason="unit conversion and field rename are derivable from the two submissions alone; no policy, personal-data or messaging decision",
                          owner=sender["org_id"], patch={"type": "adapter_node", "edge": e["_id"], "owner": sender["org_id"], "mismatches": mism}))
    return out


def io_02(ctx: Ctx) -> list[dict]:
    """A pii / financial / secret field crosses a boundary edge with no field allowlist."""
    out = []
    for e in ctx.boundary:
        sens = [f for f in e["sender_fields"] if f.get("sensitivity") in SENSITIVE or f.get("may_cross") in ("ask", "never")]
        if not sens:
            out.append(result("IO-02", "pass", e["_id"], "no sensitive field crosses this edge"))
        elif e.get("allowlist"):
            leaked = [f["path"] for f in sens if f["path"] in e["allowlist"] and f.get("may_cross") == "never"]
            out.append(result("IO-02", "fail" if leaked else "pass", e["_id"],
                              f"allowlist {e['allowlist']} still admits never-cross fields {leaked}" if leaked else f"allowlist declared: {e['allowlist']}",
                              **({"rung": 5, "stakes": "high", "owner": ctx.node(e["from"])["org_id"]} if leaked else {})))
        else:
            kinds = sorted({f.get("sensitivity") or f.get("may_cross") for f in sens})
            names = " and ".join(_leaf(f["path"]) for f in sens)
            sender = ctx.node(e["from"])
            out.append(result("IO-02", "fail", e["_id"], f"Sender emits customer {names}; no field allowlist declared.",
                              rung=5, stakes="high", stakes_reason=", ".join(kinds), owner=sender["org_id"],
                              patch={"type": "allowlist", "edge": e["_id"], "fields": [f["path"] for f in sens]}))
    return out


def _reaches_terminal_python(ctx: Ctx, nid: str) -> bool:
    return ctx.is_terminal(ctx.node(nid)) or any(ctx.is_terminal(ctx.node(x)) for x in ctx.downstream(nid) if x in ctx.nodes)


def _reaches_terminal_atlas(ctx: Ctx, nid: str) -> bool:
    """$graphLookup over the adjacency docs in `merged`."""
    m = ctx.merged
    pipeline = [
        {"$match": {"_id": f"{m['_id']}:{nid}"}},
        {"$graphLookup": {"from": "merged", "startWith": "$next", "connectFromField": "next", "connectToField": "node_id",
                          "as": "reach", "restrictSearchWithMatch": {"use_case_id": m["use_case_id"], "version": m["version"], "doc_type": "node"}}},
        {"$project": {"terminal": 1, "reached_terminal": {"$anyElementTrue": {"$map": {"input": "$reach", "as": "r", "in": "$$r.terminal"}}}}},
    ]
    doc = next(ctx.db.col("merged").aggregate(pipeline), None)
    return bool(doc and (doc.get("terminal") or doc.get("reached_terminal")))


def str_01(ctx: Ctx) -> list[dict]:
    """Every node reaches a terminal ($graphLookup when a db is given, BFS otherwise)."""
    reach = _reaches_terminal_atlas if ctx.db is not None else _reaches_terminal_python
    dead = [nid for nid in ctx.nodes if not reach(ctx, nid)]
    if dead:
        return [result("STR-01", "fail", nid, f"{nid} cannot reach any terminal", rung=5, stakes="high",
                       stakes_reason="a step that never completes", owner=ctx.node(nid)["org_id"]) for nid in dead]
    return [result("STR-01", "pass", "merged", f"all {len(ctx.nodes)} nodes reach a terminal"
                   + (" ($graphLookup)" if ctx.db is not None else ""))]


def _sets(n: dict, fld: str, value: Any) -> bool:
    """Does this node establish field == value? Yes if its postcondition says so. If its postcondition is silent
    on the field, yes when it outputs the field through something that acts (side_effect / pure tool, a human
    gate) rather than merely observes (a verify tool such as a webhook listener)."""
    post = _eq_atoms(n.get("post"))
    if any(f == fld for f, _ in post):
        return any(f == fld and v == value for f, v in post)
    if not any(f["path"] == fld for f in n.get("outputs", [])):
        return False
    tool = n.get("tool")
    if not tool:                      # a tool-less step (pure transform, decision) only forwards fields
        return n["kind"] == "human_gate"
    return tool.get("effect", "side_effect") != "verify"


def str_03(ctx: Ctx) -> list[dict]:
    """Hidden cycles. (a) a structural cycle not made of failure/retry edges; (b) a wait-for cycle across the
    boundary: a sender's precondition needs a value nobody on the financed path establishes before it,
    while the partner's completing step sits after it."""
    out = []
    # (a) structural, ignoring failure/retry edges
    color: dict[str, int] = {}
    def dfs(u: str, path: list[str]) -> list[str] | None:
        color[u] = 1
        for e in ctx.out[u]:
            if ctx.is_failure_edge(e):
                continue
            v = e["to"]
            if color.get(v) == 1:
                return path[path.index(v):] + [v]
            if color.get(v, 0) == 0:
                cyc = dfs(v, path + [v])
                if cyc:
                    return cyc
        color[u] = 2
        return None
    for nid in ctx.nodes:
        if color.get(nid, 0) == 0:
            cyc = dfs(nid, [nid])
            if cyc:
                out.append(result("STR-03", "fail", "merged", "Structural cycle: " + " -> ".join(cyc), rung=3, stakes="high",
                                  stakes_reason="ordering", owner=ctx.node(cyc[0])["org_id"]))
                break
    # (b) wait-for cycle across the boundary
    a_to_b = [e for e in ctx.boundary if e["direction"] == "A->B"]
    if a_to_b:
        path = ctx.financed_path(a_to_b[0])
        for e in a_to_b:
            S = ctx.node(e["from"])
            for fld, value in _eq_atoms(S.get("pre")):
                before = (ctx.upstream(S["_id"]) & path) | {a_to_b[0]["from"]}
                if any(_sets(ctx.node(x), fld, value) for x in before if x in ctx.nodes):
                    continue
                after = [ctx.node(x) for x in ctx.downstream(S["_id"]) if x in ctx.nodes and ctx.node(x)["org_id"] != S["org_id"]
                         and (ctx.node(x).get("outcome") or (ctx.node(x).get("tool") or {}).get("money"))]
                if not after:
                    continue
                P = after[0]
                partner = "B" if S["org_id"] == "A" else "A"
                back = next((b["_id"] for b in ctx.boundary if b["from"] == S["_id"]), "the boundary")
                out.append(result("STR-03", "fail", "merged",
                                  f"Cycle: {S['org_id']} {S['name']} runs only when {fld} == {value!r}; {partner}'s notion of payment "
                                  f"({P.get('outcome') or P['name']}) only happens after {P['_id']}, which needs {S['name']} (via {back}). "
                                  f"Reorder impossible without deciding what {value!r} means.",
                                  rung=5, stakes="high", stakes_reason="money: what counts as paid", owner=S["org_id"],
                                  patch={"type": "adapter_node", "needs": {"field": fld, "value": value}, "before": S["_id"]}))
                break
    if not out:
        out.append(result("STR-03", "pass", "merged", "no cycle outside failure/retry edges; every boundary precondition is established upstream"))
    return out


def pre_01(ctx: Ctx) -> list[dict]:
    """Receiver precondition references a field that is not in the boundary schema."""
    out = []
    for e in ctx.boundary:
        R, S = ctx.node(e["to"]), ctx.node(e["from"])
        need = {f for f in fields_referenced(R.get("pre")) if not _local_field(f, ctx.sub(R))}
        if not need:
            out.append(result("PRE-01", "pass", e["_id"], f"{R['name']} has no precondition on the handoff"))
            continue
        allowed = set(e.get("allowlist") or [])
        have = ({f["path"] for f in e["sender_fields"]} | {_leaf(f["path"]) for f in e["sender_fields"]}
                | allowed | {_leaf(a) for a in allowed})
        missing = sorted(f for f in need if f not in have and _leaf(f) not in have)
        if not missing:
            out.append(result("PRE-01", "pass", e["_id"], f"boundary schema covers {sorted(need)}"))
            continue
        carried = [n["_id"] for n in ctx.sub(S)["nodes"]
                   if any(_leaf(f["path"]) in {_leaf(m) for m in missing} for f in n.get("outputs", []) + n.get("inputs", []))]
        where = f"{S['org_id']} carries it at {', '.join(carried)}" if carried else f"{S['org_id']} never carries it"
        out.append(result("PRE-01", "fail", e["_id"],
                          f"Receiver precondition path {', '.join(missing)} is not in the edge's boundary schema; {where}.",
                          rung=5, stakes="high", stakes_reason="a loan identifier must be allowed to cross", owner=R["org_id"],
                          patch={"type": "allowlist", "fields": missing, "carried_by_sender": carried}))
    return out


def _local_field(fld: str, sub: dict) -> bool:
    """A field from the receiver's own data model (its first segment names one of the org's entities, e.g.
    A's invoice.total). A receiver may compare handoff data against its own records; those fields are not
    expected to cross the boundary. Anything else named in a precondition must arrive on the edge."""
    return fld.split(".")[0] in sub.get("entities", {})


def _policy_values(n: dict) -> dict:
    p = n.get("policy") or {}
    out = {}
    if "requires_human_approval" in p:
        out["requires_human_approval"] = bool(p["requires_human_approval"])
    if p.get("max_amount"):
        out["max_amount"] = p["max_amount"]
    return out


def pol_01(ctx: Ctx) -> list[dict]:
    """Policy lattice across a boundary: requires_human_approval OR, max_amount min. Auto-resolves (rung 1)."""
    out = []
    for e in ctx.boundary:
        S, R = ctx.node(e["from"]), ctx.node(e["to"])
        side = {}
        for org, n in ((S["org_id"], S), (R["org_id"], R)):
            vals = {}
            for g in [n] + ctx.gates_before(n["_id"]):
                for k, v in _policy_values(g).items():
                    vals.setdefault(k, v)
            side[org] = vals
        if not any(side.values()):
            continue
        guard, parts = {}, []
        rha = [v.get("requires_human_approval") for v in side.values() if "requires_human_approval" in v]
        if rha:
            guard["requires_human_approval"] = any(rha)
            parts.append("requires_human_approval: " + ", ".join(f"{o} {v.get('requires_human_approval', 'unset')}" for o, v in side.items())
                         + f" -> OR -> {guard['requires_human_approval']}")
        amts = [v["max_amount"] for v in side.values() if v.get("max_amount")]
        if amts:
            guard["max_amount"] = min(amts, key=lambda m: m["amount"])
            parts.append("max_amount: " + ", ".join(f"{o} {v['max_amount']['currency']} {v['max_amount']['amount']:,}" if v.get("max_amount") else f"{o} unset"
                                                      for o, v in side.items())
                         + f" -> {guard['max_amount']['currency']} {guard['max_amount']['amount']:,}")
        out.append(result("POL-01", "pass", e["_id"], ". ".join(parts) + ". Recorded as a boundary guard, no question needed.",
                          rung=1, stakes="low", stakes_reason="lattice rule: the stricter side wins", owner="agent",
                          guard=guard, patch={"type": "guard", "edge": e["_id"], "guard": guard, "rule": "lattice"}))
    return out


def fail_01(ctx: Ctx) -> list[dict]:
    """A partner-visible failure terminal with no failure/compensation edge back to the other org."""
    out = []
    for t in ctx.merged["nodes"]:
        if t["kind"] != "terminal" or t["visibility"] != "handoff_out":
            continue
        other = "A" if t["org_id"] == "B" else "B"
        has_edge = any(e.get("type") in FAILURE_EDGE_TYPES and ctx.nodes.get(e["to"], {}).get("org_id") == other for e in ctx.out[t["_id"]])
        entry = next((e["_id"] for e in ctx.boundary if e["to"] in ctx.upstream(t["_id"]) and ctx.node(e["from"])["org_id"] == other), "merged")
        if has_edge:
            out.append(result("FAIL-01", "pass", entry, f"{other} has a failure path for {t['name']}"))
            continue
        cand = next((c for c in ctx.merged.get("failure_candidates", []) if c["from"] == t["_id"]), None)
        out.append(result("FAIL-01", "fail", entry, f"No failure path in {other} for the {'lender' if t['org_id'] == 'B' else 'merchant'} terminal {t['name']}.",
                          rung=5, rung_from=4, stakes="high", stakes_reason="customer-facing: what the customer is told and charged on a decline",
                          owner=other, patch={"type": "compensation_edge", "from": t["_id"], "to": cand["to"] if cand else None,
                                              "on": f"{'lender' if t['org_id'] == 'B' else 'merchant'}.declined"}))
    return out


def dup_02(ctx: Ctx) -> list[dict]:
    """Near-duplicate pair (sigma >= 0.85) with no decision yet."""
    out = []
    for al in ctx.alignments:
        # a pair the aligner says feeds the other (or is a fallback for it) is by definition not a duplicate
        if al.get("sigma", 0) < NEAR_DUPLICATE_SIGMA or al.get("proposal") in ("provides_input", "on_failure"):
            continue
        scope = f"{al.get('a_name', al['a'])} ~ {al.get('b_name', al['b'])}"
        if al.get("confirmed_by"):
            out.append(result("DUP-02", "pass", scope, f"decided by {al['confirmed_by']}", locality="mediator"))
            continue
        both_se = "side_effect/side_effect" == al.get("tau")
        out.append(result("DUP-02", "fail", scope,
                          f"Near-duplicate pair (σ {al['sigma']:.2f}) still undecided" + ("; both side-effecting." if both_se else "."),
                          rung=5, stakes="high", stakes_reason="a merge or keep-both decision on side-effecting steps", owner="A",
                          locality="mediator", patch={"type": "alignment_decision", "alignment": al["_id"]}))
    return out


def _catalog_refs(sub: dict) -> list[str]:
    return [t["ref"] for t in sub.get("tools", [])]


def _in_catalog(tool: dict, refs: list[str]) -> bool:
    cands = {f"{tool['server']}:{tool['name']}", f"{tool['server']}.{tool['name']}"}
    return any(r in cands or any(r.startswith(c) or c.startswith(r) for c in cands) for r in refs)


def tool_01(ctx: Ctx) -> list[dict]:
    bad = [n["_id"] for n in ctx.merged["nodes"] if n.get("tool") and not _in_catalog(n["tool"], _catalog_refs(ctx.sub(n)))]
    if bad:
        return [result("TOOL-01", "fail", nid, f"{nid} binds a tool that is not in its org's catalog", rung=6, stakes="high",
                       owner=ctx.node(nid)["org_id"]) for nid in bad]
    return [result("TOOL-01", "pass", "merged", "every bound tool exists in its own org's catalog")]


def tool_02(ctx: Ctx) -> list[dict]:
    servers = {org: {r.split(":")[0].split(".")[0] for r in _catalog_refs(s)} for org, s in ctx.subs.items()}
    bad = [n["_id"] for n in ctx.merged["nodes"] if n.get("tool")
           and any(n["tool"]["server"] in srv and n["org_id"] != org for org, srv in servers.items())
           and n["tool"]["server"] not in servers[n["org_id"]]]
    if bad:
        return [result("TOOL-02", "fail", nid, f"{nid} references the other org's tool server", rung=6, stakes="high",
                       owner=ctx.node(nid)["org_id"], locality="mediator") for nid in bad]
    return [result("TOOL-02", "pass", "merged", "no cross-org tool reference")]


def tool_03(ctx: Ctx) -> list[dict]:
    """Side-effecting tools that declare requires_gate sit behind a human gate or an org policy naming that role."""
    gated, ungated = [], []
    for n in ctx.merged["nodes"]:
        tool = n.get("tool") or {}
        if not tool.get("requires_gate"):
            continue
        role = tool["requires_gate"].split()[0]
        policies = ctx.sub(n).get("policies", {})
        by_gate = bool(ctx.gates_before(n["_id"]))
        by_policy = any(isinstance(p, dict) and p.get("requires") == role for p in policies.values())
        ref = f"{tool['server']}.{tool['name']}"
        (gated if by_gate or by_policy else ungated).append((n["_id"], ref, "human_gate" if by_gate else "org policy"))
    out = [result("TOOL-03", "fail", nid, f"{ref} requires a gate and none is on its path", rung=4, stakes="high",
                  stakes_reason="money / irreversible", owner=ctx.node(nid)["org_id"]) for nid, ref, _ in ungated]
    if gated:
        out.append(result("TOOL-03", "pass", "merged", "gated tool: " + "; ".join(f"{ref} ({how})" for _, ref, how in gated)))
    return out or [result("TOOL-03", "pass", "merged", "no tool requires a gate")]


def str_02(ctx: Ctx) -> list[dict]:
    """Every node is reachable from a start node (a node with no incoming edge)."""
    starts = [nid for nid in ctx.nodes if not ctx.inc[nid]]
    seen: set[str] = set(starts)
    for st in starts:
        seen |= ctx.downstream(st)
    orphans = sorted(set(ctx.nodes) - seen)
    if orphans:
        return [result("STR-02", "fail", nid, f"{nid} is unreachable from any start node", rung=5, stakes="high",
                       owner=ctx.node(nid)["org_id"]) for nid in orphans]
    return [result("STR-02", "pass", "merged", f"all {len(ctx.nodes)} nodes reachable from {', '.join(starts)}")]


def _gate_outputs(ctx: Ctx, nid: str) -> set[str]:
    return {f["path"] for g in ctx.gates_before(nid) for f in g.get("outputs", [])}


def _clauses(pred: dict | None) -> list[dict]:
    if not pred:
        return []
    return list(pred["args"]) if pred["op"] == "and" else [pred]


def guard_predicate(ctx: Ctx, e: dict) -> dict | None:
    """The runtime guard on a boundary edge: an explicit predicate (from an answer), or, for a policy guard
    (lattice), the sender's own precondition clause that depends on a human gate's output."""
    g = e.get("guard") or {}
    if g.get("predicate"):
        return g["predicate"]
    if g.get("requires_human_approval") or g.get("max_amount"):
        S = ctx.node(e["from"])
        gate_out = _gate_outputs(ctx, S["_id"])
        for c in _clauses(S.get("pre")):
            if fields_referenced(c) & gate_out:
                return c
    return None


def pre_03(ctx: Ctx) -> list[dict]:
    """Certificate-time: a boundary edge whose receiver needs more than the sender's postcondition promises is
    `guarded` by a runtime predicate; without a guard it simply passes."""
    out = []
    for e in ctx.boundary:
        gp = guard_predicate(ctx, e)
        if not gp:
            out.append(result("PRE-03", "pass", e["_id"], "no runtime guard needed on this edge"))
            continue
        S = ctx.node(e["from"])
        human = bool(fields_referenced(gp) & _gate_outputs(ctx, S["_id"]))
        post = to_text(S.get("post")) if S.get("post") else "only statusCode == 200"
        detail = (f"guard {to_text(gp)}; the flag is set by a human gate at runtime" if human
                  else f"sender postcondition is {post}; guard {to_text(gp)}")
        out.append(result("PRE-03", "guarded", e["_id"], detail, rung=1, stakes="low", stakes_reason="runtime guard, checked by the sender before every handoff",
                          owner=S["org_id"], guard_predicate=gp))
    return out


def _confirmation(ctx: Ctx, al: dict) -> str | None:
    if al.get("confirmed_by"):
        return al["confirmed_by"]
    edges = {e["_id"]: e for e in ctx.boundary}
    for q in ctx.questions:
        for g in q.get("guardrails", []):
            if g.get("edge") and edges.get(g["edge"], {}).get("alignment") == al["_id"]:
                return q["_id"]
            if g.get("type") == "compensation_edge" and any(fc["alignment"] == al["_id"] and fc["from"] == g.get("from")
                                                            for fc in ctx.merged.get("failure_candidates", [])):
                return q["_id"]
            if g.get("type") == "alignment_decision" and g.get("value") == al["_id"]:
                return q["_id"]
    return None


def aln_01(ctx: Ctx) -> list[dict]:
    """Certificate-time, mediator-only: every alignment the merge used has a confirmation source."""
    used = set(ctx.merged.get("alignments_used", []))
    missing = [al for al in ctx.alignments if al["_id"] in used and not _confirmation(ctx, al)]
    if missing:
        return [result("ALN-01", "fail", f"{al.get('a_name', al['a'])} ~ {al.get('b_name', al['b'])}",
                       "alignment used by the merge has no confirmation source", rung=5, stakes="high", owner="A",
                       locality="mediator") for al in missing]
    return [result("ALN-01", "pass", "merged", "every alignment used has a confirmation source; "
                   + ", ".join(f"{al.get('a_name', al['a'])}~{al.get('b_name', al['b'])} by {_confirmation(ctx, al)}"
                               for al in ctx.alignments if al["_id"] in used), locality="mediator")]


def _sample(f: dict, k: int) -> Any:
    t = f.get("type", "string")
    leaf = _leaf(f["path"])
    if t == "money":
        return {"currency": f.get("currency", "USD"), "amount": round(2780.0 * k, 2)}
    if t == "integer":
        return 100 * k
    if t == "number":
        return 27.8 * k
    if t == "boolean":
        return True
    if t == "enum" and f.get("values"):
        return f["values"][0]
    if t in ("datetime", "date"):
        return "2026-09-26T09:12:03Z"
    if t == "object":
        return {}
    if t == "array":
        return []
    return f"{leaf}_{k}"


def _put(ctx_: dict, path: str, value: Any) -> None:
    cur = ctx_
    parts = path.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
        if not isinstance(cur, dict):
            return
    cur[parts[-1]] = value


def effective_allowlist(e: dict) -> list[str]:
    """What may cross: every non-sensitive sender field plus the allowlisted sensitive ones, minus never_send."""
    never = [n.rstrip("*").rstrip(".") for n in ((e.get("handoff_out") or {}).get("never_send") or [])]
    ok = [f["path"] for f in e["sender_fields"] if not (f.get("sensitivity") in SENSITIVE or f.get("may_cross") in ("ask", "never"))]
    ok += [a for a in (e.get("allowlist") or []) if a not in ok]
    return sorted(p for p in ok if not any(p == n or p.startswith(n + ".") for n in never if n))


def conf_01(ctx: Ctx) -> list[dict]:
    """Certificate-time: generate 4 payloads per boundary edge from its interface and dry-run the receiver's
    precondition and the edge guard on them."""
    out = []
    for e in ctx.boundary:
        S, R = ctx.node(e["from"]), ctx.node(e["to"])
        fields = {f["path"]: f for f in e["sender_fields"]}
        allowed = effective_allowlist(e)
        gp = guard_predicate(ctx, e)
        preds = [p for p in (R.get("pre"), gp) if p]
        rejected = []
        for k in range(1, 5):
            payload: dict = {}
            for path in allowed:
                _put(payload, path, _sample(fields.get(path) or next((f for f in fields.values() if _leaf(f["path"]) == _leaf(path)), {"path": path}), k))
            world = dict(payload)
            for p in preds:
                for fld in fields_referenced(p):
                    if fld not in world and not any(fld.startswith(a + ".") or a.startswith(fld + ".") for a in allowed):
                        _put(world, fld, _sample(fields.get(fld, {"path": fld, "type": "integer" if fld.endswith("_minor") else "string"}), k))
                for fld, val in _eq_atoms(p):
                    if isinstance(val, str) and "." in val:      # field == other field: make them equal
                        _put(world, val, 2780.0 * k)
                        _put(world, fld, 2780.0 * k)
                    else:
                        _put(world, fld, val)
            if not all(evaluate(p, world) for p in preds):
                rejected.append(k)
        if rejected:
            out.append(result("CONF-01", "fail", e["_id"], f"generated payloads {rejected} rejected by {R['name']} in dry run",
                              rung=5, stakes="high", owner=S["org_id"]))
        else:
            out.append(result("CONF-01", "pass", e["_id"], f"4 generated payloads accepted in dry run ({len(allowed)} fields: {', '.join(allowed)})"))
    return out


ALL = {"validate", "revalidate"}
CERT = {"revalidate"}
REGISTRY: list[tuple[str, Callable[[Ctx], list[dict]], set[str]]] = [
    ("IO-01", io_01, ALL), ("IO-02", io_02, ALL), ("STR-01", str_01, ALL), ("STR-02", str_02, ALL), ("STR-03", str_03, ALL),
    ("PRE-01", pre_01, ALL), ("PRE-03", pre_03, CERT), ("POL-01", pol_01, ALL), ("FAIL-01", fail_01, ALL),
    ("DUP-02", dup_02, ALL), ("TOOL-01", tool_01, ALL), ("TOOL-02", tool_02, ALL), ("TOOL-03", tool_03, ALL),
    ("ALN-01", aln_01, CERT), ("CONF-01", conf_01, CERT),
]


# --------------------------------------------------------------------------- driver

def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.~-]+", "_", s)


def run(merged: dict, submissions: list[dict], alignments: list[dict], questions: list[dict] | None = None,
        db: Any = None, stage: str = "validate") -> list[dict]:
    ctx = Ctx(merged, {s["org_id"]: s for s in submissions}, alignments, questions or [], db, stage)
    use_case, version = merged["use_case_id"], merged["version"]
    results = []
    for check, fn, stages in REGISTRY:
        if stage not in stages:
            continue
        for r in fn(ctx):
            assert r["check"] == check
            doc = {"_id": f"{use_case}:{stage}:v{version}:{check}:{_slug(r['scope'])}", "use_case_id": use_case,
                   "stage": stage, "merged_version": version, **r}
            spec.validate(doc, "finding")
            results.append(doc)
    return results


def findings(results: list[dict]) -> list[dict]:
    """What goes to the `findings` collection: every non-pass result, plus passes that record a guard."""
    return [r for r in results if r["verdict"] != "pass" or r.get("guard")]


def write(rows: list[dict], use_case: str, version: int, stage: str = "validate") -> None:
    from pmp import db
    db.col("findings").delete_many({"use_case_id": use_case, "stage": stage, "merged_version": version})
    if rows:
        db.col("findings").insert_many(rows)


def table(results: list[dict]) -> str:
    head = f"{'check':<8} {'verdict':<8} {'scope':<30} {'rung':>4} {'stakes':<6} {'owner':<6} detail"
    lines = [head, "-" * 120]
    for r in results:
        lines.append(f"{r['check']:<8} {r['verdict']:<8} {r['scope'][:30]:<30} {str(r.get('rung', '')):>4} {r.get('stakes', ''):<6} "
                     f"{r['owner_org']:<6} {r['detail'][:200]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Stage 3b: run the check registry on a merged graph.")
    p.add_argument("--use-case", default=DEFAULT_USE_CASE)
    p.add_argument("--version", type=int, default=1)
    p.add_argument("--stage", default="validate", choices=["validate", "revalidate"])
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    from pmp import db, merge
    merged = merge.load(a.use_case, a.version)
    subs = list(db.col("submissions").find({"use_case_id": a.use_case}))
    als = list(db.col("alignments").find({"use_case_id": a.use_case}))
    qs = list(db.col("merge_questions").find({"use_case_id": a.use_case}))
    results = run(merged, subs, als, qs, db=db, stage=a.stage)
    print(table(results))
    rows = findings(results)
    n_fail = sum(r["verdict"] == "fail" for r in results)
    print(f"{len(results)} results: {n_fail} fail, {sum(r['verdict'] == 'guarded' for r in results)} guarded, "
          f"{len(results) - n_fail} pass/guarded; {len(rows)} findings" + (" (dry run)" if a.dry_run else " written"))
    if not a.dry_run:
        write(rows, a.use_case, a.version, a.stage)
    return 0


if __name__ == "__main__":
    sys.exit(main())
