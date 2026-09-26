"""Stage 4: turn validator findings into decisions and questions, then compile answers into guardrails.

Deterministic. No LLM. Same findings + same answers = same merged v2.

- Low-stakes findings resolve by rule (lattice, adapter, ...) unless the owner org's merge_preferences
  say always_ask for that topic. Each rule application is a decision doc (spec/decision.schema.json) in
  merge_log and a patch on the merged graph.
- High-stakes findings become exactly one question each (spec/question.schema.json), routed to the
  owner org's role from its merge_preferences.question_routing, always with a default. IO-02 on an
  edge folds into the DUP-02 question about the same entity (q1 asks both things).
- Question text is generated from the finding, node names and boundary fields only. Nothing reads
  confidential_notes.
- Every answer compiles to guardrails (allowlist, adapter_node, predicate, compensation_edge,
  alignment_decision) applied to merged v2. Adapter nodes carry origin = question id.

CLI: python -m pmp.decide --use-case bnpl_checkout_v1 [--answer-defaults | --answers file.json] [--dry-run]
"""
from __future__ import annotations
import argparse
import copy
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from pmp import spec
from pmp.compile import jsonable, sha256
from pmp.predicate import to_text

ROOT = Path(__file__).resolve().parent.parent
ANSWERS_FIXTURE = ROOT / "mock" / "stages" / "4_answers.json"
DEFAULT_USE_CASE = "bnpl_checkout_v1"

# order in which questions are asked: along the merged flow, most-blocking first
QUESTION_ORDER = ["DUP-02", "IO-01", "STR-03", "PRE-01", "FAIL-01", "IO-02", "STR-01", "TOOL-03", "TOOL-01", "TOOL-02"]
# routing category per check (looked up in the owner org's merge_preferences.question_routing)
ROUTING = {"DUP-02": "operations", "IO-01": "operations", "IO-02": "pii", "STR-03": "money", "PRE-01": "operations",
           "FAIL-01": "operations", "TOOL-03": "money", "STR-01": "operations"}
# topics a finding touches, matched against the owner org's always_ask list
TOPICS = {"IO-01": {"money"}, "IO-02": {"pii"}, "STR-03": {"money", "paid"}, "PRE-01": {"loan identifiers"},
          "FAIL-01": {"customer"}, "DUP-02": {"operations"}, "POL-01": {"policy"}}
ORG_NAMES = {"A": "Northwind", "B": "Lakeside"}


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- helpers over the merged graph

def _nodes(merged: dict) -> dict[str, dict]:
    return {n["_id"]: n for n in merged["nodes"]}


def _edge_objs(merged: dict, eid: str) -> list[dict]:
    """A boundary edge lives in both `edges` and `boundary_edges`; patch every copy."""
    return [e for e in merged["edges"] if e["_id"] == eid] + [e for e in merged["boundary_edges"] if e["_id"] == eid]


def _boundary(merged: dict, eid: str) -> dict:
    return next(e for e in merged["boundary_edges"] if e["_id"] == eid)


def _first_sentence(text: str) -> str:
    body = text.split(": ", 1)[-1]
    return re.split(r"(?<=[.!?])\s", body)[0].rstrip(".")


def _leaf(p: str) -> str:
    return p.split(".")[-1]


def _humanize(camel: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", " ", camel).lower()


def org_name(sub_or_org, subs: dict[str, dict]) -> str:
    org = sub_or_org if isinstance(sub_or_org, str) else sub_or_org["org_id"]
    return subs[org].get("org_name", ORG_NAMES.get(org, org)).capitalize()


def route(org: str, category: str, subs: dict[str, dict]) -> str:
    routing = subs[org].get("merge_preferences", {}).get("question_routing", {})
    return routing.get(category) or routing.get("operations") or "owner"


def always_ask(org: str, check: str, subs: dict[str, dict]) -> bool:
    tags = " ".join(subs[org].get("merge_preferences", {}).get("always_ask", [])).lower()
    return any(t in tags for t in TOPICS.get(check, set()))


# --------------------------------------------------------------------------- graph patches

def add_node(merged: dict, node: dict) -> None:
    spec.validate(node, "node")
    merged["nodes"].append(node)


def add_edge(merged: dict, edge: dict) -> None:
    spec.validate(edge, "transition")
    merged["edges"].append(edge)


def set_allowlist(merged: dict, eid: str, fields: list[str]) -> list[str]:
    cur = set(_boundary(merged, eid).get("allowlist") or [])
    new = sorted(cur | set(fields))
    for e in _edge_objs(merged, eid):
        e["allowlist"] = new
    return new


def set_guard(merged: dict, eid: str, guard: dict) -> None:
    for e in _edge_objs(merged, eid):
        e["guard"] = {**(e.get("guard") or {}), **guard}


def extend_outputs(merged: dict, nid: str, fields: list[dict]) -> None:
    n = _nodes(merged)[nid]
    have = {f["path"] for f in n.get("outputs", [])}
    n.setdefault("outputs", []).extend(f for f in fields if f["path"] not in have)


def insert_adapter_before_boundary(merged: dict, eid: str, node: dict) -> None:
    """sender -> adapter (keeps the branch condition) -> boundary edge now leaves the adapter."""
    b = _boundary(merged, eid)
    sender = b["from"]
    add_node(merged, node)
    e = {"_id": f"{sender}->{node['_id']}", "org_id": node["org_id"], "use_case_id": merged["use_case_id"],
         "from": sender, "to": node["_id"], "type": "normal", "origin": node.get("origin"),
         "guidance": f"adapter {node['name']} before {eid}"}
    if b.get("condition"):
        e["condition"] = b["condition"]
    add_edge(merged, e)
    for x in _edge_objs(merged, eid):
        x["from"] = node["_id"]
        x["adapter"] = node["_id"]
        x["sender_fields"] = copy.deepcopy(node.get("outputs", []))
        x.pop("condition", None)


def insert_adapter_after_boundary(merged: dict, eid: str, node: dict) -> None:
    """boundary edge now enters the adapter -> adapter -> original receiver."""
    b = _boundary(merged, eid)
    receiver = b["to"]
    add_node(merged, node)
    add_edge(merged, {"_id": f"{node['_id']}->{receiver}", "org_id": node["org_id"], "use_case_id": merged["use_case_id"],
                      "from": node["_id"], "to": receiver, "type": "normal", "origin": node.get("origin"),
                      "guidance": f"adapter {node['name']} after {eid}"})
    for x in _edge_objs(merged, eid):
        x["to"] = node["_id"]
        x["adapter"] = node["_id"]
        x["receiver_fields"] = copy.deepcopy(node.get("inputs", []))


def add_compensation_edge(merged: dict, frm: str, to: str, on: str, origin: str) -> dict:
    e = {"_id": f"{frm}->{to}", "org_id": "M", "use_case_id": merged["use_case_id"], "from": frm, "to": to,
         "type": "compensation", "origin": origin, "condition": {"op": "eq", "args": ["handoff.outcome", on]},
         "guidance": f"on {on}: fall back to {to}"}
    add_edge(merged, e)
    return e


# --------------------------------------------------------------------------- planning: findings -> decisions + questions

def _decision(n: int, use_case: str, finding: dict, rule: str, patch: list[dict], signals: dict) -> dict:
    d = {"_id": f"d{n}", "use_case_id": use_case, "type": "decision", "finding": finding["_id"], "rule": rule,
         "signals": signals, "patch": patch, "ts": now(), "check": finding["check"], "scope": finding["scope"]}
    spec.validate(d, "decision")
    return d


def _question(n: int, use_case: str, finding: dict, to_org: str, subs: dict, text: str, default: str,
              options: list[str], default_data: dict, triggers: list[str]) -> dict:
    q = {"_id": f"q{n}", "use_case_id": use_case, "trigger": finding["check"], "triggers": triggers,
         "finding": finding["_id"], "findings": [finding["_id"]], "scope": finding["scope"], "to_org": to_org,
         "to_role": route(to_org, ROUTING.get(finding["check"], "operations"), subs), "text": text,
         "options": options, "default": default, "default_data": default_data, "answer": None, "answered_by": None,
         "guardrails": []}   # ts and guardrail are set when answered: the schema wants them typed, not null
    spec.validate(q, "question")
    return q


def _sensitive_fields(edge: dict) -> list[dict]:
    return [f for f in edge["sender_fields"] if f.get("sensitivity") in ("pii", "financial", "secret") or f.get("may_cross") in ("ask", "never")]


def _never_fields(sub: dict, entity: str) -> list[str]:
    return [f"{entity}.{k}" for k, v in sub.get("entities", {}).get(entity, {}).get("fields", {}).items() if v.get("may_cross") == "never"]


def plan(findings: list[dict], merged: dict, subs: dict[str, dict], alignments: list[dict]) -> tuple[list[dict], list[dict]]:
    use_case = merged["use_case_id"]
    nodes = _nodes(merged)
    by_check: dict[str, list[dict]] = {}
    for f in findings:
        by_check.setdefault(f["check"], []).append(f)

    decisions: list[dict] = []
    for f in findings:
        if f["verdict"] == "pass" and f.get("guard"):            # POL-01 lattice: record the guard
            decisions.append(_decision(len(decisions) + 1, use_case, f, "lattice",
                                       [{"op": "set_policy", "edge": f["scope"], "guard": f["guard"]}],
                                       {"lattice": f["detail"]}))

    questions: list[dict] = []
    folded_io02: set[str] = set()
    ordered = sorted((f for f in findings if f["verdict"] == "fail"),
                     key=lambda f: (QUESTION_ORDER.index(f["check"]) if f["check"] in QUESTION_ORDER else 99, f["scope"]))
    for f in ordered:
        check, owner = f["check"], f["owner_org"]
        if f["stakes"] == "low" and not always_ask(owner, check, subs):
            # rule-resolvable and the owner does not insist on being asked
            decisions.append(_decision(len(decisions) + 1, use_case, f, "adapter" if check == "IO-01" else "field_rename",
                                       [{"op": "insert_node", "edge": f["scope"], "spec": f.get("patch")}], {"stakes": "low"}))
            continue
        if f["_id"] in folded_io02:
            continue
        n = len(questions) + 1
        partner = "B" if owner == "A" else "A"
        me, them = org_name(owner, subs), org_name(partner, subs)

        if check == "DUP-02":
            al = next(a for a in alignments if a["_id"] == f["patch"]["alignment"])
            a_n, b_n = nodes[al["a"]], nodes[al["b"]]
            text = (f'Your "{a_n["name"]}" ({_first_sentence(a_n["text"])}) and {them}\'s "{b_n["name"]}" '
                    f'({_first_sentence(b_n["text"])}) share a name but not a meaning. Keep both as separate steps?')
            default, data, triggers = "keep_both", {"decision": "keep_both", "alignment": al["_id"]}, [check]
            # fold IO-02 on an edge whose sensitive fields belong to the same entity as this pair
            entity = _leaf(a_n["name"]).replace("create_", "").replace("create", "").lower()
            for io in by_check.get("IO-02", []):
                edge = _boundary(merged, io["scope"])
                sens = _sensitive_fields(edge)
                if sens and all(s["path"].split(".")[0].lower() == entity for s in sens):
                    allowed = [s["path"] for s in sens if s.get("may_cross") != "never"]
                    never = _never_fields(subs[owner], sens[0]["path"].split(".")[0])
                    names = " and ".join(_leaf(p) for p in allowed)
                    text += f" And may the {entity}'s {names} be sent to {them} as enrollment details?"
                    default += f"; send {', '.join(allowed)}" + (f"; never {', '.join(_leaf(x) for x in never)}" if never else "")
                    data.update({"edge": edge["_id"], "fields": allowed, "never": never})
                    triggers.append("IO-02")
                    folded_io02.add(io["_id"])
            q = _question(n, use_case, f, owner, subs, text, default, ["keep_both", "merge", "relate"], data, triggers)
            q["findings"] += sorted(folded_io02)
            questions.append(q)

        elif check == "IO-01":
            mism = f["patch"]["mismatches"]
            units = [m for m in mism if m["kind"] == "unit"]
            names = [m for m in mism if m["kind"] == "name"]
            edge = _boundary(merged, f["scope"])
            sender, receiver = nodes[edge["from"]], nodes[edge["to"]]
            s_org, r_org = sender["org_id"], receiver["org_id"]
            s_money = sorted({_leaf(m["sender"]) for m in units}); r_money = sorted({_leaf(m["receiver"]) for m in units})
            s_id = [ _leaf(m["sender"]) for m in names]; r_id = [_leaf(m["receiver"]) for m in names]
            text = (f"{org_name(s_org, subs)} line items carry {', '.join(s_money)} in {units[0]['sender_unit']} units"
                    + (f" and a {', '.join(s_id)}" if s_id else "") + f"; {org_name(r_org, subs)} expects "
                    + (f"{', '.join(r_id)} and " if r_id else "") + f"{', '.join(r_money)} in {units[0]['receiver_unit']} units. "
                    f"Who converts" + (f", and what is {', '.join(r_id)}?" if r_id else "?"))
            qty = next((x["path"] for x in edge["sender_fields"] if _leaf(x["path"]) == "quantity"), None)
            payload = (edge.get("handoff_out") or {}).get("payload") or {}
            payload_key = _payload_key_for(payload, {m["sender"] for m in units} | {m["sender"] for m in names})
            exprs, extra = [], []
            per_item = [m for m in units if _leaf(m["sender"]).startswith("unit_")] or units[:1]
            for m in per_item:
                exprs.append({"to": m["receiver"] + ".amount", "from": [m["sender"]] + ([qty] if qty else []),
                              "expr": f"{_leaf(m['sender'])}" + (" × quantity" if qty else "") + (" / 100" if m["sender_unit"] == "cents" else "")})
            for m in names:
                exprs.append({"to": m["receiver"], "from": [m["sender"]], "expr": _leaf(m["sender"])})
            for m in units:
                if m not in per_item and m["sender"] not in {x for e in exprs for x in e["from"]}:
                    key = _payload_key_for(payload, {m["sender"]}) or _leaf(m["sender"]).replace("_minor", "")
                    extra.append({"to": key, "from": [m["sender"]], "expr": f"{_leaf(m['sender'])} / 100", "type": "money", "unit": "major"})
            default = f"{org_name(s_org, subs)} converts; " + "; ".join(f"{_leaf(e['to'].replace('.amount', ''))}{'.amount' if e['to'].endswith('.amount') else ''} = {e['expr']}" for e in exprs)
            data = {"who": "sender", "edge": edge["_id"], "owner": s_org, "transform": exprs + extra, "object": payload_key,
                    "consumes": sorted({p for e in exprs + extra for p in e["from"]})}
            questions.append(_question(n, use_case, f, owner, subs, text, default, ["sender_converts", "receiver_converts"], data, [check]))

        elif check == "STR-03":
            need = f["patch"]["needs"]
            S = nodes[f["patch"]["before"]]
            tool = (nodes[S["_id"]].get("tool") or {}).get("server", "your payment system")
            back = next((e for e in merged["boundary_edges"] if e["direction"].startswith(partner)), None)
            pred = _default_paid_predicate(subs[owner], subs[partner], back)
            term = subs[owner].get("terms", {}).get("paid_out_of_band")
            text = (f"Your procedure fulfils only after {tool} reports {need['field']} == {need['value']!r}. {them} activates the loan "
                    f"only after you report fulfilment. Does an authorized payment plan from {them} count as payment for fulfilment "
                    f"purposes? If yes, {me} would mark the invoice paid out of band"
                    + (f" ({term.rstrip('.')})" if term else "") + ".")
            default = f"yes, provided {to_text(pred)}"
            data = {"answer": "yes", "predicate": pred, "edge": back["_id"] if back else None, "field": need["field"], "value": need["value"]}
            questions.append(_question(n, use_case, f, owner, subs, text, default, ["yes", "no"], data, [check]))

        elif check == "PRE-01":
            edge = _boundary(merged, f["scope"])
            receiver, sender = nodes[edge["to"]], nodes[edge["from"]]
            missing = f["patch"]["fields"]
            back = next((e for e in merged["boundary_edges"] if e["direction"] == f"{owner}->{partner}"), None)
            obj = back["sender_fields"][0]["path"] if back and back["sender_fields"] else "response"
            text = (f"{receiver['name']} needs {', '.join(missing)}, which {them} never sees. May {me} include it in the "
                    f"{obj} it returns, so {them} can echo it back?")
            data = {"answer": "yes", "fields": missing, "return_edge": back["_id"] if back else None, "object": obj,
                    "echo_edge": edge["_id"], "echo_node": sender["_id"]}
            questions.append(_question(n, use_case, f, owner, subs, text, "yes", ["yes", "no"], data, [check]))

        elif check == "FAIL-01":
            terminal = nodes[f["patch"]["from"]]
            reasons = sorted({g.strip() for e in merged["edges"] if e["to"] == terminal["_id"] for g in e.get("guidance", "").split("|")})
            why = ", ".join(_humanize(r).replace("not eligible", "not eligible") for r in reasons if r)
            fallback = f["patch"]["to"]
            fb_name = nodes[fallback]["name"] if fallback else "cancel"
            text = (f"If {them} declines the loan ({why}), what should {me} do with the finalized invoice?")
            default = f"fall back to card payment ({fb_name})" if fallback else "cancel the invoice"
            data = {"from": terminal["_id"], "to": fallback, "on": f["patch"]["on"], "edge": f["scope"]}
            questions.append(_question(n, use_case, f, owner, subs, text, default, ["fall back to card payment", "cancel the invoice"], data, [check]))

        elif check == "IO-02":
            edge = _boundary(merged, f["scope"])
            sens = _sensitive_fields(edge)
            allowed = [s["path"] for s in sens if s.get("may_cross") != "never"]
            text = f"May {', '.join(allowed)} be sent to {them} on {edge['_id']}?"
            questions.append(_question(n, use_case, f, owner, subs, text, f"send {', '.join(allowed)}", ["allow", "deny"],
                                       {"edge": edge["_id"], "fields": allowed}, [check]))
        else:
            text = f"{check} on {f['scope']}: {f['detail']} How should this be resolved?"
            questions.append(_question(n, use_case, f, owner, subs, text, "reject", ["reject"], {}, [check]))
    return decisions, questions


def _payload_key_for(payload: dict, sender_paths: set[str]) -> str | None:
    """The declared payload object (e.g. 'basket') whose mappings read from these sender fields."""
    def froms(v):
        if isinstance(v, dict):
            return ([v["from"]] if isinstance(v.get("from"), str) else []) + [x for w in v.values() for x in froms(w)]
        if isinstance(v, list):
            return [x for w in v for x in froms(w)]
        return []
    for key, v in payload.items():
        if set(froms(v)) & sender_paths:
            return key
    return None


def _default_paid_predicate(sub_owner: dict, sub_partner: dict, back_edge: dict | None) -> dict:
    """The owner's own 'financing counts as paid' policy, with its field names mapped through the partner's
    declared return payload (plan_status <- finalizedPaymentPlan.status, plan_total <- totalLoanAmount)."""
    rule = None
    for name, pol in sub_owner.get("policies", {}).items():
        if isinstance(pol, dict) and "paid" in name and pol.get("rule"):
            rule = copy.deepcopy(pol["rule"])
    mapping = {}
    if back_edge and back_edge.get("handoff_out") and isinstance(back_edge["handoff_out"].get("payload"), dict):
        for k, v in back_edge["handoff_out"]["payload"].items():
            if isinstance(v, dict) and v.get("from"):
                mapping[k] = v["from"]
    def sub(p):
        if isinstance(p, dict):
            return {"op": p["op"], "args": [sub(a) for a in p["args"]]}
        return mapping.get(p, p) if isinstance(p, str) else p
    if rule:
        return sub(rule)
    return {"op": "and", "args": [{"op": "eq", "args": ["finalizedPaymentPlan.status", "authorized"]},
                                  {"op": "eq", "args": ["totalLoanAmount", "invoice.total"]}]}


# --------------------------------------------------------------------------- answering

def answer_question(q: dict, given: dict | str | None) -> dict:
    """Fill answer/answered_by/ts. `given` is None or "default" for the default, else a dict with an
    'answer' and any structured fields (fields, predicate, who, ...)."""
    if given is None or given == "default":
        q["answer"] = q["default"]
        q["answer_data"] = dict(q["default_data"])
        q["answered_by"] = {"org": q["to_org"], "user": "default"}
        q["accepted_default"] = True
    else:
        q["answer"] = str(given.get("answer", q["default"]))
        q["answer_data"] = {**q["default_data"], **{k: v for k, v in given.items() if k not in ("answered_by", "ts", "note")}}
        q["answered_by"] = given.get("answered_by") or {"org": q["to_org"], "user": q["to_role"]}
        q["accepted_default"] = False
    q["ts"] = (given.get("ts") if isinstance(given, dict) else None) or now()
    return q


def ask_interactively(q: dict) -> dict | str:
    print(f"\n{q['_id']}  trigger {q['trigger']}  to {ORG_NAMES.get(q['to_org'], q['to_org'])} / {q['to_role']}")
    print(q["text"])
    print(f"  options: {', '.join(q['options'])}\n  default: {q['default']}")
    raw = input("  answer [enter = default]: ").strip()
    if not raw:
        return "default"
    if q["trigger"] == "DUP-02":
        first = raw.split(";")[0].strip().replace(" ", "_")
        return {"answer": first if first in q["options"] else "keep_both"}
    if q["trigger"] == "IO-01":
        return {"answer": "receiver_converts", "who": "receiver"} if "receiver" in raw.lower() else "default"
    if q["trigger"] in ("STR-03", "PRE-01"):
        return {"answer": "no"} if raw.lower().startswith("n") else "default"
    if q["trigger"] == "FAIL-01":
        return {"answer": "cancel the invoice"} if "cancel" in raw.lower() else "default"
    return {"answer": raw}


# --------------------------------------------------------------------------- guardrails: answers -> merged v2

def _adapter_node(merged: dict, org: str, name: str, origin: str, *, inputs, outputs, text, visibility="internal",
                  pre=None, post=None, tool=None, role=None, transform=None) -> dict:
    n = {"_id": f"{org}.{name}", "org_id": org, "use_case_id": merged["use_case_id"], "name": name, "kind": "action",
         "visibility": visibility, "origin": origin, "inputs": inputs, "outputs": outputs, "text": f"{name}: {text}",
         "dedupe": "never", "obligation": {"required": True, "reason": f"guardrail from {origin}"}}
    if role:
        n["role"] = role
    if pre:
        n["pre"] = pre
    if post:
        n["post"] = post
    if tool:
        n["tool"] = tool
    if transform:
        n["transform"] = transform
    return n


def apply_answer(merged: dict, q: dict, alignments: list[dict], subs: dict[str, dict]) -> list[dict]:
    d, qid, org = q["answer_data"], q["_id"], q["to_org"]
    partner = "B" if org == "A" else "A"
    guardrails: list[dict] = []

    if q["trigger"] == "DUP-02":
        decision = d.get("answer", d.get("decision", "keep_both"))
        if decision not in ("keep_both", "merge", "relate", "distinct"):
            decision = "keep_both"
        al = next(a for a in alignments if a["_id"] == d["alignment"])
        al["confirmed_by"] = qid
        al["decision"] = decision
        guardrails.append({"type": "alignment_decision", "decision": decision, "value": al["_id"]})
        if d.get("edge") and d.get("fields"):
            fields = [f for f in d["fields"] if f not in set(d.get("never", []))]
            guardrails.append({"type": "allowlist", "edge": d["edge"], "fields": set_allowlist(merged, d["edge"], fields)})

    elif q["trigger"] == "IO-01":
        edge = _boundary(merged, d["edge"])
        nodes = _nodes(merged)
        sender, receiver = nodes[edge["from"]], nodes[edge["to"]]
        who = d.get("who", "sender")
        if str(d.get("answer", "")).startswith("receiver"):
            who = "receiver"
        consumed = set(d["consumes"])
        r_fields = {f["path"]: f for f in edge["receiver_fields"]}
        outputs = []
        for e in d["transform"]:
            path = e["to"].replace(".amount", "")
            f = dict(r_fields.get(path) or {"path": path, "type": e.get("type", "string")})
            if e.get("unit"):
                f["unit"] = e["unit"]
            outputs.append(f)
        outputs = list({o["path"]: o for o in outputs}.values())
        passthrough = [copy.deepcopy(f) for f in edge["sender_fields"] if f["path"] not in consumed]
        inputs = [copy.deepcopy(f) for f in edge["sender_fields"] if f["path"] in consumed]
        expr_text = "; ".join(f"{e['to']} = {e['expr']}" for e in d["transform"])
        if who == "sender":
            to_role = (edge.get("handoff_out") or {}).get("to_role") or ("lender" if partner == "B" else "merchant")
            node = _adapter_node(merged, sender["org_id"], f"adapt_{d.get('object') or edge['_id']}_for_{to_role}", qid,
                                 inputs=inputs, outputs=outputs + passthrough, visibility="handoff_out", role=sender.get("role"),
                                 text=f"convert {sender['name']} outputs to {receiver['name']}'s schema: {expr_text}. Adapter compiled from {qid}, owned by {sender['org_id']} because {sender['org_id']} answered.",
                                 transform=d["transform"])
            insert_adapter_before_boundary(merged, edge["_id"], node)
        else:
            node = _adapter_node(merged, receiver["org_id"], f"adapt_{edge['_id']}_inbound", qid,
                                 inputs=copy.deepcopy(edge["sender_fields"]), outputs=copy.deepcopy(edge["receiver_fields"]),
                                 visibility="handoff_in", role=receiver.get("role"),
                                 text=f"convert {sender['name']} outputs to {receiver['name']}'s schema on arrival: {expr_text}. Adapter compiled from {qid}.",
                                 transform=d["transform"])
            insert_adapter_after_boundary(merged, edge["_id"], node)
        guardrails.append({"type": "adapter_node", "edge": edge["_id"], "node": {"_id": node["_id"], "origin": qid, "owner": node["org_id"]}})

    elif q["trigger"] == "STR-03":
        if str(d.get("answer", "yes")).lower().startswith("n"):
            guardrails.append({"type": "predicate", "value": "rejected: cycle stays a first-class conflict", "predicate": d["predicate"]})
        else:
            pred = d["predicate"]
            edge = _boundary(merged, d["edge"])
            nodes = _nodes(merged)
            receiver = nodes[edge["to"]]
            pay_tool = next((t for t in subs[org].get("tools", []) if t["ref"].endswith("invoices.pay")), None)
            tool = None
            if pay_tool:
                server, _, name = pay_tool["ref"].partition(":")
                tool = {"server": server, "name": name, "effect": pay_tool.get("effect", "side_effect"), "money": bool(pay_tool.get("money")),
                        "reversible": pay_tool.get("reversible", False), "idempotent": bool(pay_tool.get("idempotent", False))}
            from pmp.predicate import fields_referenced
            inputs = [{"path": p, "type": "string"} for p in sorted(fields_referenced(pred))]
            node = _adapter_node(merged, org, "mark_invoice_paid_out_of_band", qid,
                                 inputs=inputs + [{"path": "invoice.invoice_id", "type": "string"}],
                                 outputs=[{"path": d["field"], "type": "enum", "values": ["paid"]}],
                                 pre=pred, post={"op": "eq", "args": [d["field"], d["value"]]}, tool=tool, role=receiver.get("role"),
                                 visibility="handoff_in",
                                 text=f"an authorized financing plan counts as payment: when {to_text(pred)}, mark the invoice paid out of band so {d['field']} == {d['value']!r}. Adapter compiled from {qid}.")
            insert_adapter_after_boundary(merged, edge["_id"], node)
            guardrails.append({"type": "adapter_node", "edge": edge["_id"], "node": {"_id": node["_id"], "origin": qid, "owner": org}, "predicate": pred})
            guardrails.append({"type": "predicate", "edge": edge["_id"], "predicate": pred})
            set_guard(merged, edge["_id"], {"predicate": pred})

    elif q["trigger"] == "PRE-01":
        if str(d.get("answer", "yes")).lower().startswith("n"):
            guardrails.append({"type": "allowlist", "edge": d["echo_edge"], "fields": [], "value": "rejected"})
        else:
            fields = d["fields"]
            ret = _boundary(merged, d["return_edge"]) if d.get("return_edge") else None
            if ret:
                declared = [v["from"] for v in ((ret.get("handoff_out") or {}).get("payload") or {}).values() if isinstance(v, dict) and v.get("from")]
                qualified = [f"{d['object']}.{_leaf(f)}" for f in fields]
                new = set_allowlist(merged, ret["_id"], [x for x in declared if _leaf(x) not in {_leaf(f) for f in fields}] + qualified)
                for x in _edge_objs(merged, ret["_id"]):
                    have = {f["path"] for f in x["sender_fields"]}
                    x["sender_fields"] += [{"path": qf, "type": "string", "origin": q["_id"]} for qf in qualified if qf not in have]
                guardrails.append({"type": "allowlist", "edge": ret["_id"], "fields": new})
            extend_outputs(merged, d["echo_node"], [{"path": _leaf(f), "type": "string", "source": f"echo of {d['return_edge']}", "origin": qid} for f in fields]
                           + [{"path": "fulfilled_at", "type": "datetime", "origin": qid}])
            echo = _boundary(merged, d["echo_edge"])
            for x in _edge_objs(merged, d["echo_edge"]):
                x["sender_fields"] = copy.deepcopy(_nodes(merged)[echo["from"]]["outputs"])
            new = set_allowlist(merged, d["echo_edge"], [_leaf(f) for f in fields] + ["fulfilled_at"])
            guardrails.append({"type": "allowlist", "edge": d["echo_edge"], "fields": new})

    elif q["trigger"] == "FAIL-01":
        if "cancel" in str(d.get("answer", "")).lower():
            target = next(n["_id"] for n in merged["nodes"] if n["org_id"] == org and n["kind"] == "terminal" and "cancel" in n["_id"].lower())
        else:
            target = d["to"]
        e = add_compensation_edge(merged, d["from"], target, d["on"], qid)
        guardrails.append({"type": "compensation_edge", "from": d["from"], "to": target, "on": d["on"], "value": e["_id"]})

    elif q["trigger"] == "IO-02":
        if str(d.get("answer", "")).lower().startswith("deny"):
            guardrails.append({"type": "allowlist", "edge": d["edge"], "fields": set_allowlist(merged, d["edge"], [])})
        else:
            guardrails.append({"type": "allowlist", "edge": d["edge"], "fields": set_allowlist(merged, d["edge"], d["fields"])})

    for g in guardrails:
        spec.validate({"_id": "q0", "use_case_id": "x", "trigger": "x", "to_org": "A", "to_role": "x", "text": "x", "default": "x",
                       "guardrail": g}, "question")
    q["guardrails"] = guardrails
    if guardrails:
        q["guardrail"] = guardrails[0]
    return guardrails


def apply_decision(merged: dict, d: dict) -> None:
    for p in d["patch"]:
        if p["op"] == "set_policy":
            set_guard(merged, p["edge"], p["guard"])
        # insert_node decisions for auto-resolved IO-01 would build the adapter the same way as q2; not exercised here


def build_v2(merged_v1: dict, findings: list[dict], subs_list: list[dict], alignments: list[dict],
             answers: dict | None, *, version: int = 2) -> tuple[dict, list[dict], list[dict], list[dict]]:
    """Returns (merged_v2, decisions, questions, alignments) — alignments updated in place with confirmed_by."""
    subs = {s["org_id"]: s for s in subs_list}
    merged = copy.deepcopy(merged_v1)
    merged["_id"] = f"{merged['use_case_id']}:v{version}"
    merged["version"], merged["supersedes"] = version, merged_v1["version"]
    decisions, questions = plan(findings, merged, subs, alignments)
    for d in decisions:
        apply_decision(merged, d)
    answers = answers or {}
    for q in questions:
        given = answers.get(q["_id"], answers.get(q["trigger"]))
        answer_question(q, given)
        apply_answer(merged, q, alignments, subs)
    for f in findings:
        f["resolved_by"] = next((q["_id"] for q in questions if f["_id"] in q["findings"]), None) \
            or next((d["_id"] for d in decisions if d["finding"] == f["_id"]), None)
    merged["decisions_applied"] = [d["_id"] for d in decisions]
    merged["questions_applied"] = [q["_id"] for q in questions]
    merged["guardrails"] = [{"question": q["_id"], **g} for q in questions for g in q["guardrails"]] + \
                           [{"decision": d["_id"], "type": "policy_value", "edge": p["edge"], "value": p.get("guard")} for d in decisions for p in d["patch"] if p["op"] == "set_policy"]
    merged["inputs"]["h_answers"] = sha256([{"q": q["_id"], "answer": q["answer"], "data": q["answer_data"]} for q in questions])
    merged = jsonable(merged)
    merged["hash"] = sha256({k: merged[k] for k in ("nodes", "edges", "boundary_edges", "failure_candidates", "merge_candidates", "inputs")})
    for n in merged["nodes"]:
        spec.validate(n, "node")
    for e in merged["edges"]:
        spec.validate(e, "transition")
    for q in questions:
        spec.validate(q, "question")
    return merged, decisions, [jsonable(q) for q in questions], alignments


def load_answers(path: Path) -> dict:
    doc = json.loads(Path(path).read_text())
    return doc.get("answers", doc)


def write(merged: dict, decisions: list[dict], questions: list[dict], alignments: list[dict], findings: list[dict]) -> None:
    from pmp import db, merge
    use_case = merged["use_case_id"]
    merge.write(merged)
    db.col("merge_questions").delete_many({"use_case_id": use_case})
    if questions:
        db.col("merge_questions").insert_many(questions)
    db.col("merge_log").delete_many({"use_case_id": use_case, "type": {"$in": ["decision", "merged_version"]}, "version": {"$in": [None, merged["version"]]}})
    db.col("merge_log").delete_many({"use_case_id": use_case, "type": "decision"})
    rows = list(decisions) + [{"_id": f"{use_case}:merged_version:{merged['version']}", "use_case_id": use_case, "type": "merged_version",
                               "version": merged["version"], "supersedes": merged.get("supersedes"), "decisions": [d["_id"] for d in decisions],
                               "questions": [q["_id"] for q in questions], "hash": merged["hash"], "ts": now()}]
    db.col("merge_log").insert_many(rows)
    for al in alignments:
        db.col("alignments").update_one({"_id": al["_id"]}, {"$set": {"confirmed_by": al.get("confirmed_by"), "decision": al.get("decision")}})
    for f in findings:
        db.col("findings").update_one({"_id": f["_id"]}, {"$set": {"resolved_by": f.get("resolved_by")}})


def summary(questions: list[dict], decisions: list[dict], merged: dict) -> str:
    lines = [f"{len(decisions)} decision(s) by rule, {len(questions)} question(s):"]
    for d in decisions:
        lines.append(f"  {d['_id']}  {d['check']} on {d['scope']}  rule {d['rule']}")
    for q in questions:
        who = "default accepted" if q.get("accepted_default") else f"answered by {q['answered_by']['user']}"
        lines.append(f"  {q['_id']}  trigger {'+'.join(q['triggers'])}  to {ORG_NAMES.get(q['to_org'])}/{q['to_role']}  {who}")
        lines.append(f"      Q: {q['text']}")
        lines.append(f"      A: {q['answer']}")
        lines.append(f"      guardrails: " + ", ".join(f"{g['type']}" + (f"({g['edge']})" if g.get('edge') else "") for g in q["guardrails"]))
    adapters = [n["_id"] for n in merged["nodes"] if n.get("origin")]
    lines.append(f"merged {merged['_id']}: {len(merged['nodes'])} nodes, {len(merged['edges'])} edges; adapters {adapters}; "
                 f"{sum(1 for e in merged['edges'] if e.get('type') == 'compensation')} compensation edge(s); hash {merged['hash'][:19]}…")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Stage 4: decide low-stakes findings by rule, ask the rest, apply answers to merged v2.")
    p.add_argument("--use-case", default=DEFAULT_USE_CASE)
    p.add_argument("--version", type=int, default=1, help="merged version to decide on (writes version+1)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--answer-defaults", action="store_true", help="accept every default (no prompt)")
    g.add_argument("--answers", type=Path, help="JSON file of answers keyed by question id or trigger check")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    from pmp import db, merge
    merged_v1 = merge.load(a.use_case, a.version)
    subs = list(db.col("submissions").find({"use_case_id": a.use_case}))
    als = list(db.col("alignments").find({"use_case_id": a.use_case}))
    findings = list(db.col("findings").find({"use_case_id": a.use_case, "stage": "validate", "merged_version": a.version}))
    if not findings:
        raise SystemExit("no validate findings for this version; run pmp.validate first")
    answers: dict | None
    if a.answers:
        answers = load_answers(a.answers)
    elif a.answer_defaults:
        answers = {}
    else:
        subs_map = {s["org_id"]: s for s in subs}
        _, qs = plan(findings, copy.deepcopy(merged_v1), subs_map, copy.deepcopy(als))
        answers = {q["_id"]: ask_interactively(q) for q in qs}
    merged_v2, decisions, questions, als = build_v2(merged_v1, findings, subs, als, answers, version=a.version + 1)
    print(summary(questions, decisions, merged_v2))
    if not a.dry_run:
        write(merged_v2, decisions, questions, als, findings)
        print(f"wrote {merged_v2['_id']}, {len(questions)} merge_questions, {len(decisions)} merge_log decisions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
