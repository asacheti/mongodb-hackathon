"""A2A-shaped handoffs and the receiver's four checks.

A handoff is {metadata: {pmp: {contract_id, contract_version, certificate_id, edge, sender, receiver, run, seq,
attestations, guard_results}}, payload}. The payload holds only the fields the contract lets cross that edge.

receiver_check runs, in order: (1) contract signed and active, (2) edge in my projection, (3) payload matches the
boundary schema and allowlist, (4) my preconditions hold. Any failure is a typed rejection (spec/rejection.schema.json).
"""
from __future__ import annotations
import copy
from typing import Any

from pmp import spec
from pmp.decide import now
from pmp.predicate import evaluate, fields_referenced, resolve, to_text

MONEY = "money"


def _get(d: dict, path: str) -> Any:
    return resolve(path, d)


def _put(d: dict, path: str, value: Any) -> None:
    cur = d
    parts = path.split(".")
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = cur[p] = {}
        cur = nxt
    cur[parts[-1]] = value


def build_payload(state: dict, allowlist: list[str]) -> dict:
    """Project the run state onto the allowlist. A top-level list (line items, products) is carried as a list of
    objects filtered to the allowed sub-fields."""
    out: dict = {}
    by_first: dict[str, list[str]] = {}
    for a in allowlist:
        by_first.setdefault(a.split(".")[0], []).append(a)
    for first, paths in by_first.items():
        val = state.get(first)
        if val is None:
            continue
        if isinstance(val, list):
            subs = [p[len(first) + 1:] for p in paths if p != first]
            if not subs:
                out[first] = copy.deepcopy(val)
                continue
            items = []
            for item in val:
                if not isinstance(item, dict):
                    continue
                o: dict = {}
                for sp in subs:
                    v = _get(item, sp)
                    if v is not None:
                        _put(o, sp, copy.deepcopy(v))
                items.append(o)
            out[first] = items
        else:
            for p in paths:
                v = _get(state, p)
                if v is not None:
                    _put(out, p, copy.deepcopy(v))
    return out


def flatten(payload: Any, prefix: str = "") -> dict[str, Any]:
    """Dotted paths -> values. Arrays flatten without indexes: products.productCode -> [v1, v2]."""
    out: dict[str, Any] = {}
    if isinstance(payload, dict):
        for k, v in payload.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(payload, list):
        for item in payload:
            for k, v in flatten(item, prefix).items():
                out.setdefault(k, []).append(v) if isinstance(out.get(k), list) or k in out else out.__setitem__(k, [v])
    else:
        out[prefix] = payload
    return out


def allowed(path: str, allowlist: list[str]) -> bool:
    return any(path == a or path.startswith(a + ".") or a.startswith(path + ".") for a in allowlist)


def type_ok(value: Any, ftype: str) -> bool:
    if isinstance(value, list) and ftype not in ("array",):
        return all(type_ok(v, ftype) for v in value)
    if ftype == MONEY:
        return isinstance(value, dict) and "currency" in value and isinstance(value.get("amount"), (int, float))
    if ftype in ("string", "enum", "datetime", "date"):
        return isinstance(value, str)
    if ftype == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if ftype == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if ftype == "boolean":
        return isinstance(value, bool)
    if ftype == "object":
        return isinstance(value, dict)
    if ftype == "array":
        return isinstance(value, list)
    return True


def build_handoff(contract: dict, edge: str, sender: str, receiver: str, run: str, seq: int, payload: dict,
                  guard_results: dict | None = None, attestations: list[str] | None = None) -> dict:
    h = {"metadata": {"pmp": {"contract_id": contract["contract_id"], "contract_version": contract["version"],
                              "certificate_id": contract["certificate_id"], "edge": edge, "sender": sender, "receiver": receiver,
                              "run": run, "seq": seq, "attestations": attestations or [], "guard_results": guard_results or {}}},
         "payload": payload}
    spec.validate(h, "handoff")
    return h


def rejection(handoff: dict, use_case: str, receiver: str, code: str, **extra) -> dict:
    pmp = handoff["metadata"]["pmp"]
    r = {"use_case_id": use_case, "code": code, "edge": pmp["edge"], "run": pmp["run"], "seq": pmp["seq"], "receiver": receiver,
         "contract_version": pmp["contract_version"], "ts": now(), **extra}
    spec.validate(r, "rejection")
    return r


def receiver_check(handoff: dict, org: str, use_case: str, contract: dict | None, projection: dict, node: dict | None,
                   required: list[str], own_state: dict) -> dict | None:
    pmp = handoff["metadata"]["pmp"]
    payload = handoff.get("payload") or {}
    # 1. contract signed and active, and the one I hold
    if (not contract or contract.get("status") != "active" or pmp["contract_id"] != contract["contract_id"]
            or pmp["contract_version"] != contract["version"] or pmp["certificate_id"] != contract["certificate_id"]
            or not (contract.get("signatures") or {}).get(org, {}).get("sig")):
        exp = f"{contract['contract_id']} v{contract['version']} {contract['status']}" if contract else "an active contract"
        return rejection(handoff, use_case, org, "CONTRACT_NOT_ACTIVE", expected=exp,
                         got=f"{pmp['contract_id']} v{pmp['contract_version']} ({pmp['certificate_id']})")
    # 2. edge in my projection, entering one of my nodes
    pe = next((e for e in projection.get("edges", []) if e["_id"] == pmp["edge"] and e.get("type") == "boundary"), None)
    if not pe or node is None or pe["to"] != node["_id"] or pmp["receiver"] != org:
        return rejection(handoff, use_case, org, "EDGE_NOT_IN_PROJECTION", field=pmp["edge"],
                         expected="a boundary edge entering one of my nodes", got=pmp["edge"])
    ce = next((e for e in contract.get("boundary_edges", []) if e["id"] == pmp["edge"]), None)
    allowlist = (ce or {}).get("allowlist") or pe.get("allowlist") or []
    fields = {f["path"]: f for f in ((ce or {}).get("schema") or {}).get("fields", pe.get("fields", []))}
    flat = flatten(payload)
    # 3. boundary schema + allowlist
    bad = [p for p in flat if not allowed(p, allowlist)]
    if bad:
        return rejection(handoff, use_case, org, "ALLOWLIST_VIOLATION", field=bad[0], expected=f"one of {allowlist}", got=flat[bad[0]])
    missing = [r for r in required if r not in flat and not any(f.startswith(r + ".") for f in flat)]
    if missing:
        return rejection(handoff, use_case, org, "MISSING_FIELDS", missing=missing, expected=f"required by {node['name']}")
    for p, f in fields.items():
        if p in flat and not type_ok(flat[p], f.get("type", "string")):
            return rejection(handoff, use_case, org, "TYPE_MISMATCH", field=p, expected=f.get("type"), got=flat[p])
    # 4. my preconditions on payload + my own state
    if node.get("pre"):
        ctx = copy.deepcopy(own_state)
        for k, v in payload.items():
            ctx[k] = copy.deepcopy(v)
        if not evaluate(node["pre"], ctx):
            return rejection(handoff, use_case, org, "PRECONDITION_FAILED", field=to_text(node["pre"]), expected="true",
                             got={f: resolve(f, ctx) for f in sorted(fields_referenced(node["pre"]))})
    return None
