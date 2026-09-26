"""Stage 1: compile the Stage 0 inputs into typed graphs.

Northwind: mock/inputs/northwind/SKILL.md (frontmatter + YAML sections + one YAML block per "## A<n>" step).
Lakeside:  mock/inputs/lakeside/bnpl-arazzo.yaml (verbatim OAI example) + merge-profile.yaml sidecar.

Output per org: a submission {org_id, use_case_id, nodes, transitions, handoffs, policies, ..., hash}
plus compile findings (LINT-01 dangling $steps reference, LINT-02 policy in prose).
Every node validates against spec/node.schema.json, every transition against spec/transition.schema.json.
Node `text` is name + prose only; confidential_notes never enter it.

CLI: python -m pmp.compile --use-case bnpl_checkout_v1 [--dry-run] [--fixture]
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from pmp import spec
from pmp.predicate import OPS, to_text

ROOT = Path(__file__).resolve().parent.parent
INPUTS = ROOT / "mock" / "inputs"
DEFAULT_USE_CASE = "bnpl_checkout_v1"
FIELD_TYPES = {"string", "integer", "number", "boolean", "date", "datetime", "money", "address", "object", "array", "enum"}
EFFECT_RANK = {"pure": 0, "verify": 1, "side_effect": 2}


# --------------------------------------------------------------------------- helpers

def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(obj).encode()).hexdigest()


def jsonable(obj: Any) -> Any:
    """Round-trip through JSON so dates become strings and everything is BSON-safe."""
    return json.loads(json.dumps(obj, default=str))


def to_ast(short: Any) -> dict | None:
    """SKILL.md / profile shorthand {eq: [a, b]} / {exists: x} / {and: [...]} -> predicate AST."""
    if short is None:
        return None
    if isinstance(short, dict) and "op" in short:
        return short
    if isinstance(short, dict) and len(short) == 1:
        op, args = next(iter(short.items()))
        if op in {"and", "or"}:
            return {"op": op, "args": [to_ast(a) for a in args]}
        if op == "not":
            return {"op": "not", "args": [to_ast(args)]}
        if op == "exists":
            return {"op": "exists", "args": [args]}
        if op in OPS:
            return {"op": op, "args": list(args)}
    raise ValueError(f"cannot compile predicate shorthand {short!r}")


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _transition(org: str, use_case: str, frm: str, to: str, cond: dict | None, guidance: str | None = None,
                pitfalls: str | None = None) -> dict:
    t = {"_id": f"{frm}->{to}", "org_id": org, "use_case_id": use_case, "from": frm, "to": to, "type": "normal",
         "guidance": guidance or (to_text(cond) if cond else "otherwise")}
    if cond:
        t["condition"] = cond
    if pitfalls:
        t["pitfalls"] = pitfalls
    return t


def _add_transition(transitions: list[dict], t: dict) -> None:
    """Same from->to twice collapses into one edge whose condition is the OR of both."""
    for existing in transitions:
        if existing["_id"] == t["_id"]:
            c1, c2 = existing.get("condition"), t.get("condition")
            if c1 and c2:
                existing["condition"] = {"op": "or", "args": [c1, c2]}
            elif not c1 or not c2:
                existing.pop("condition", None)
            existing["guidance"] = f"{existing['guidance']} | {t['guidance']}"
            return
    transitions.append(t)


def _finding(use_case: str, org: str, check: str, n: int, scope: str, detail: str) -> dict:
    return {"_id": f"{use_case}:compile:{org}:{check}:{n}", "use_case_id": use_case, "stage": "compile",
            "check": check, "verdict": "fail", "scope": scope, "detail": detail, "owner_org": org,
            "locality": "local", "resolved_by": "compile:auto-corrected, flagged for owner"}


def _primary_tool(tools: list[dict]) -> dict:
    return max(tools, key=lambda t: (EFFECT_RANK.get(t["effect"], 2), bool(t.get("money"))))


def _catalog_entry(catalog: dict, ref: str) -> dict:
    """Exact ref first; else the one catalog entry whose name is a prefix of ours or vice versa
    (the Lakeside profile says updateBnplLoanTransaction, the Arazzo operationId adds ...Status)."""
    if ref in catalog:
        return catalog[ref]
    hits = [k for k in catalog if k.startswith(ref) or ref.startswith(k)]
    return catalog[hits[0]] if len(hits) == 1 else {}


def _tool_doc(server: str, name: str, cat: dict) -> dict:
    tool = {"server": server, "name": name, "effect": cat.get("effect", "side_effect")}
    for k in ("reversible", "idempotent", "money", "requires_gate"):
        if k in cat:
            tool[k] = cat[k]
    return tool


def _submission(org: str, org_name: str, use_case: str, version: str, nodes: list, transitions: list,
                extra: dict) -> dict:
    for n in nodes:
        spec.validate(n, "node")
    for t in transitions:
        spec.validate(t, "transition")
    sub = jsonable({"org_id": org, "org_name": org_name, "use_case_id": use_case, "version": str(version),
                    "nodes": nodes, "transitions": transitions, **extra})
    sub["hash"] = sha256(sub)
    sub["_id"] = f"{use_case}:{org}"
    return sub


# --------------------------------------------------------------------------- Northwind (SKILL.md)

# The hand-written inputs use a few YAML shorthands PyYAML rejects; rewrite them before parsing.
_YAML_FIXES = [
    (re.compile(r"^(\s*[\w:.\-]+):\{", re.M), r"\1: {"),                # `key:{ ... }` -> `key: { ... }`
]
_SKILL_FIXES = _YAML_FIXES + [
    (re.compile(r"^(\s*)- when: (\{.*\})\s+then: (\S+)\s*$", re.M), r"\1- when: \2\n\1  then: \3"),
    (re.compile(r"then: escalate_to: (\w+)"), r"then: { escalate_to: \1 }"),
]


def load_yaml(path: Path) -> Any:
    raw = path.read_text()
    for rx, rep in _YAML_FIXES:
        raw = rx.sub(rep, raw)
    return yaml.safe_load(raw)
_MONEY = re.compile(r"(USD|\$)\s?(\d[\d,]*)")
_APPROVAL = re.compile(r"approv|manager|sign-?off", re.I)


def parse_skill(path: Path) -> dict:
    raw = path.read_text()
    for rx, rep in _SKILL_FIXES:
        raw = rx.sub(rep, raw)
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.S)
    if not m:
        raise ValueError(f"{path}: no frontmatter")
    doc: dict = {"frontmatter": yaml.safe_load(m.group(1)), "steps": [], "terminals": []}
    for chunk in re.split(r"^(?=# \d+\. )", m.group(2), flags=re.M):
        if not chunk.strip():
            continue
        header, _, text = chunk.partition("\n")
        title = re.sub(r"^# \d+\. ", "", header).strip()
        if title.lower() != "steps":
            doc.update(yaml.safe_load(text) or {})
            continue
        for sub in re.split(r"^(?=## )", text, flags=re.M):
            if not sub.strip():
                continue
            h, _, body = sub.partition("\n")
            lines = body.split("\n")
            start = next((i for i, l in enumerate(lines) if l.startswith("- id:")), None)
            if start is None:
                continue
            prose = " ".join(l.strip() for l in lines[:start] if l.strip())
            items = yaml.safe_load("\n".join(lines[start:])) or []
            if h.startswith("## Terminals"):
                doc["terminals"] = items
            else:
                for it in items:
                    it["prose"], it["heading"] = prose, h[3:].strip()
                    doc["steps"].append(it)
    return doc


def _a_field(path: str, f: dict) -> dict:
    out = {"path": path, "type": f.get("type", "string")}
    if out["type"] not in FIELD_TYPES:
        out["type"] = "string"
    for k in ("unit", "currency", "values", "pattern", "sensitivity", "may_cross"):
        if k in f:
            out[k] = f[k]
    return out


def _a_catalog(doc: dict) -> dict[str, dict]:
    cat: dict[str, dict] = {}
    for ent, e in doc.get("entities", {}).items():
        for fname, f in e.get("fields", {}).items():
            cat[f"{ent}.{fname}"] = _a_field(f"{ent}.{fname}", f)
    for name, values in doc.get("code_lists", {}).items():
        cat.setdefault(name, {"path": name, "type": "enum", "values": list(values)})
    return cat


def _a_fields(paths: list[str], cat: dict) -> list[dict]:
    out = []
    for p in paths:
        if p.endswith(".*"):
            out += [dict(v) for k, v in cat.items() if k.startswith(p[:-1])]
        elif p in cat:
            out.append(dict(cat[p]))
        else:
            out.append({"path": p, "type": "integer" if p.endswith(".count") else "string"})
    return out


def _a_tool(ref: str, catalog: dict) -> dict:
    server, _, name = ref.partition(":")
    return _tool_doc(server, name, catalog.get(ref, {}))


def _pitfalls(prose: str) -> str | None:
    hits = [s.strip() for s in re.split(r"(?<=[.!])\s+", prose) if re.match(r"(Never|Stripe caps|After this)", s.strip())]
    return " ".join(hits) or None


def _lint02(nodes: list[dict], transitions: list[dict], org: str, use_case: str) -> list[dict]:
    """A money threshold + approval language in prose, with no policy field -> policy + human_gate."""
    findings = []
    for node in list(nodes):
        m = _MONEY.search(node["text"])
        if not m or not _APPROVAL.search(node["text"]) or node.get("policy"):
            continue
        amount = int(m.group(2).replace(",", ""))
        policy = {"requires_human_approval": True, "max_amount": {"currency": "USD", "amount": amount},
                  "source": "LINT-02: compiled from prose"}
        if node["kind"] == "human_gate":
            node["policy"] = policy
            gate_note = "attached to the existing HUMAN_GATE node"
        else:
            gate = {"_id": f"{org}.approve_{node['name']}", "org_id": org, "use_case_id": use_case,
                    "name": f"approve_{node['name']}", "kind": "human_gate", "visibility": "internal",
                    "inputs": list(node.get("inputs", [])), "outputs": [{"path": "approval.granted", "type": "boolean"}],
                    "policy": policy, "origin": "LINT-02",
                    "text": f"approve_{node['name']}: human approval gate compiled from the policy sentence in {node['name']}"}
            for t in transitions:
                if t["to"] == node["_id"]:
                    t["to"] = gate["_id"]
                    t["_id"] = f"{t['from']}->{t['to']}"
            transitions.append(_transition(org, use_case, gate["_id"], node["_id"],
                                           {"op": "eq", "args": ["approval.granted", True]}))
            nodes.append(gate)
            gate_note = f"new HUMAN_GATE node {gate['_id']} inserted"
        prose = node["text"].split(": ", 1)[-1]
        sentence = next((s for s in re.split(r"(?<=[.!])\s+", prose) if _MONEY.search(s)), prose)
        findings.append(_finding(use_case, org, "LINT-02", len(findings) + 1, f"SKILL.md step {node['name']}",
                                 f'"{sentence.strip()}" is a policy in prose with no policy field -> '
                                 f'policy {{requires_human_approval: true, max_amount: {{USD, {amount}}}}} '
                                 f'plus a HUMAN_GATE node ({gate_note}).'))
    return findings


def compile_northwind(path: Path = INPUTS / "northwind" / "SKILL.md", use_case: str = DEFAULT_USE_CASE) -> tuple[dict, list[dict]]:
    doc = parse_skill(path)
    org, fm = "A", doc["frontmatter"]
    cat = _a_catalog(doc)
    handoffs = doc.get("handoffs", [])
    step_handoffs: dict[str, list[str]] = {}
    for h in handoffs:
        step_handoffs.setdefault(h["step"], []).append(h["id"])
    idmap = {s["id"]: f"{org}.{s['name']}" for s in doc["steps"]}
    idmap.update({t["id"]: f"{org}.{t['id']}" for t in doc["terminals"]})

    nodes: list[dict] = []
    transitions: list[dict] = []
    for s in doc["steps"]:
        nid = idmap[s["id"]]
        tools = [_a_tool(r, doc.get("tools", {})) for r in s.get("tools", [])]
        node = _clean({
            "_id": nid, "org_id": org, "use_case_id": use_case, "name": s["name"], "kind": s["type"],
            "visibility": s.get("visibility", "internal"), "role": s.get("role"),
            "inputs": _a_fields(s.get("inputs", []), cat), "outputs": _a_fields(s.get("outputs", []), cat),
            "pre": to_ast(s.get("preconditions")), "post": to_ast(s.get("postconditions")),
            "obligation": s.get("obligation"), "dedupe": s.get("dedupe"), "timeout": s.get("timeout"),
            "on_failure": s.get("on_failure"), "confidential_notes": s.get("confidential_notes"),
            "text": f"{s['name']}: {s['prose']}",
            "source": {"file": "mock/inputs/northwind/SKILL.md", "step": s["id"], "heading": s["heading"]},
        })
        if tools:
            node["tool"], node["tools"] = _primary_tool(tools), tools
        if s["id"] in step_handoffs:
            node["handoffs"] = step_handoffs[s["id"]]
        branches = []
        for b in s.get("branches", []):
            target = b.get("then") or b.get("otherwise")
            br: dict = {"then": idmap.get(target, target)}
            if "when" in b:
                br["when"] = to_ast(b["when"])
            else:
                br["otherwise"] = True
            branches.append(br)
            if target in idmap:  # handoff targets (H1..H3) become boundary edges at merge time, not here
                _add_transition(transitions, _transition(org, use_case, nid, idmap[target], br.get("when"),
                                                         pitfalls=_pitfalls(s["prose"])))
        node["branches"] = branches
        nodes.append(node)
    for t in doc["terminals"]:
        nodes.append({"_id": idmap[t["id"]], "org_id": org, "use_case_id": use_case, "name": t["id"],
                      "kind": "terminal", "visibility": "internal", "outcome": t["outcome"],
                      "text": f"{t['id']}: terminal outcome {t['outcome']}"})

    findings = _lint02(nodes, transitions, org, use_case)
    policies = {k: {**v, "rule": to_ast(v["rule"])} if isinstance(v, dict) and "rule" in v else v
                for k, v in doc.get("policies", {}).items()}
    extra = {
        "procedure": fm.get("name"), "description": fm.get("description"),
        "handoffs": handoffs, "policies": policies,
        "tools": [{"ref": k, **v} for k, v in doc.get("tools", {}).items()],
        "entities": doc.get("entities", {}), "roles": doc.get("roles", {}),
        "merge_preferences": doc.get("merge_preferences", {}), "outcomes": doc.get("outcomes", []),
        "exceptions": doc.get("exceptions", []), "evidence": doc.get("evidence", {}), "terms": doc.get("terms", {}),
    }
    return _submission(org, fm.get("org_id", "northwind"), use_case, fm.get("procedure_version", "0"),
                       nodes, transitions, extra), findings


# --------------------------------------------------------------------------- Lakeside (Arazzo + profile)

_STEP_REF = re.compile(r"\$steps\.(\w+)\.outputs\.(\w+)")
_INPUT_REF = re.compile(r"\$inputs\.(\w+)(?:#/(\w+))?")
_BODY_REF = re.compile(r"\$response\.body#/(\w+)")
_JSONPATH_COUNT = re.compile(r"\$\[\?count\(@\.(\w+)\)\s*(==|!=|>=|<=|>|<)\s*(\d+)\]")
_CMP = re.compile(r"^(\S+)\s*(==|!=|>=|<=|>|<)\s*(.+?)\s*$")
_OPMAP = {"==": "eq", "!=": "ne", ">": "gt", ">=": "gte", "<": "lt", "<=": "lte"}


def _lit(s: str) -> Any:
    s = s.strip()
    if s in ("true", "false"):
        return s == "true"
    if s == "null":
        return None
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    return s.strip("'\"")


def _lhs(s: str) -> str:
    if s == "$statusCode":
        return "response.statusCode"
    m = _BODY_REF.fullmatch(s)
    if m:
        return f"response.body.{m.group(1)}"
    m = _STEP_REF.fullmatch(s)
    if m:
        return m.group(2)
    m = _INPUT_REF.fullmatch(s)
    if m:
        return f"{m.group(1)}.{m.group(2)}" if m.group(2) else m.group(1)
    return s.lstrip("$")


def arazzo_condition(cond: str) -> dict:
    """Arazzo runtime-expression condition / jsonpath count() -> predicate AST."""
    cond = cond.strip()
    if "||" in cond:
        return {"op": "or", "args": [arazzo_condition(c) for c in cond.split("||")]}
    if "&&" in cond:
        return {"op": "and", "args": [arazzo_condition(c) for c in cond.split("&&")]}
    m = _JSONPATH_COUNT.fullmatch(cond)
    if m:
        return {"op": _OPMAP[m.group(2)], "args": [f"response.body.{m.group(1)}.count", int(m.group(3))]}
    m = _CMP.fullmatch(cond)
    if m:
        return {"op": _OPMAP[m.group(2)], "args": [_lhs(m.group(1)), _lit(m.group(3))]}
    raise ValueError(f"cannot compile Arazzo condition {cond!r}")


def _criteria_ast(criteria: list[dict], success: set[str]) -> dict | None:
    parts = [arazzo_condition(c["condition"]) for c in criteria if c.get("condition") not in success]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else {"op": "and", "args": parts}


def _b_catalog(prof: dict) -> dict[str, dict]:
    cat: dict[str, dict] = {}
    for ent, e in prof.get("entities", {}).items():
        for fname, f in e.get("fields", {}).items():
            d = {"path": fname, "type": f.get("type", "string")}
            if d["type"] not in FIELD_TYPES:
                d["type"] = "string"
            if d["type"] == "money":
                d["unit"] = "major" if f.get("unit", "major") == "major" else "cents"
            for k in ("pattern", "sensitivity", "may_cross"):
                if k in f:
                    d[k] = f[k]
            d["entity"] = ent
            cat[fname] = d
    return cat


def _b_field(name: str, cat: dict, path: str | None = None) -> dict:
    if name in cat:
        d = dict(cat[name])
    elif name.endswith(("Url", "Uri", "Token", "Id")):
        d = {"path": name, "type": "string"}
    elif name.endswith(("Required", "Eligible")) or name.startswith("is"):
        d = {"path": name, "type": "boolean"}
    elif name.lower().endswith("products"):
        d = {"path": name, "type": "array"}
    elif name.endswith("Amount"):
        d = {"path": name, "type": "money", "unit": "major"}
    elif name.endswith(("Plan", "AndConditions", "Status")) or name == "customer":
        d = {"path": name, "type": "object" if name != "customer" else "string"}
    else:
        d = {"path": name, "type": "string"}
    if path:
        d["path"] = path
    return d


def _b_input_fields(name: str, sub: str | None, wf_inputs: dict, cat: dict) -> list[dict]:
    """$inputs.X[#/y] -> typed fields, expanded from the workflow input schema."""
    schema = wf_inputs.get(name, {})
    if sub:
        return [_b_field(sub, cat, path=f"{name}.{sub}")]
    if schema.get("type") == "array":
        props = schema.get("items", {}).get("properties", {})
        out = []
        for k, v in props.items():
            if k in schema.get("items", {}).get("required", []):
                if v.get("type") == "object" and {"currency", "amount"} <= set(v.get("properties", {})):
                    out.append({"path": f"{name}.{k}", "type": "money", "unit": "major"})
                else:
                    out.append(_b_field(k, cat, path=f"{name}.{k}"))
        return out
    variants = schema.get("oneOf") or [schema]
    props = variants[0].get("properties", {})
    return [_b_field(k, cat, path=f"{name}.{k}") for k in props if k != "additionalProperties"]


def _refs_in_step(i: int, st: dict) -> list[tuple[str, str]]:
    """(scope, expression) pairs for every runtime expression in the step's parameters, body and criteria."""
    refs: list[tuple[str, str]] = []
    for p in st.get("parameters", []) or []:
        refs.append((f"steps[{i}].parameters.{p['name']}", str(p.get("value", ""))))
    body = (st.get("requestBody") or {}).get("payload")
    if isinstance(body, str):
        for key, expr in re.findall(r'"(\w+)":\s*"\{?(\$[^"}]+)\}?"', body):
            refs.append((f"steps[{i}].requestBody.{key}", expr))
    elif isinstance(body, dict):
        for key, expr in body.items():
            if isinstance(expr, str) and "$" in expr:
                refs.append((f"steps[{i}].requestBody.{key}", expr))
    for j, o in enumerate(st.get("onSuccess", []) or []):
        for c in o.get("criteria", []):
            refs.append((f"steps[{i}].onSuccess[{j}].criteria", str(c.get("condition", ""))))
    return refs


def compile_lakeside(arazzo_path: Path = INPUTS / "lakeside" / "bnpl-arazzo.yaml",
                     profile_path: Path = INPUTS / "lakeside" / "merge-profile.yaml",
                     use_case: str = DEFAULT_USE_CASE) -> tuple[dict, list[dict]]:
    az = load_yaml(arazzo_path)
    prof = load_yaml(profile_path)
    org = "B"
    wf = az["workflows"][0]
    steps = wf["steps"]
    server = az["sourceDescriptions"][0]["name"]
    ann, tool_cat = prof.get("step_annotations", {}), prof.get("tools", {})
    cat = _b_catalog(prof)
    wf_inputs = wf.get("inputs", {}).get("properties", {})
    src_file = "mock/inputs/lakeside/bnpl-arazzo.yaml"

    nodes: list[dict] = []
    by_id: dict[str, dict] = {}
    declared: dict[str, dict[str, dict]] = {}
    findings: list[dict] = []

    # pass 1: one node per Arazzo step, outputs declared in order, inputs resolved with LINT-01
    for i, st in enumerate(steps):
        sid = st["stepId"]
        a = ann.get(sid, {})
        outputs = [_b_field(name, cat) for name in (st.get("outputs") or {})]
        declared[sid] = {f["path"]: f for f in outputs}
        desc = re.sub(r"\s+", " ", st.get("description", "")).strip()
        node = _clean({
            "_id": f"{org}.{sid}", "org_id": org, "use_case_id": use_case, "name": sid, "kind": "action",
            "visibility": a.get("visibility", "internal"), "role": a.get("role"),
            "tool": _tool_doc(server, st["operationId"], _catalog_entry(tool_cat, f"{server}.{st['operationId']}")),
            "inputs": [], "outputs": outputs, "pre": to_ast(a.get("preconditions")),
            "obligation": a.get("obligation"), "dedupe": a.get("dedupe"),
            "confidential_notes": a.get("confidential_notes"),
            "text": f"{sid}: {desc}",
            "source": {"file": src_file, "step": i, "stepId": sid, "operationId": st["operationId"]},
        })
        if i == len(steps) - 1:
            node["outcome"] = prof.get("outcomes", ["completed"])[0]
        seen: set[str] = set()
        for scope, expr in _refs_in_step(i, st):
            for X, Y in _STEP_REF.findall(expr):
                field = None
                if X == sid:
                    src = next((s2["stepId"] for s2 in steps[:i] if Y in declared.get(s2["stepId"], {})), None)
                    findings.append(_finding(use_case, org, "LINT-01", len(findings) + 1, scope,
                        f"References $steps.{X}.outputs.{Y} (itself); the value is produced by {src}. "
                        f"Compiled with corrected source; flagged for the owner."))
                    if src:
                        field = {**declared[src][Y], "source": f"$steps.{src}.outputs.{Y}",
                                 "corrected_from": f"$steps.{X}.outputs.{Y}"}
                elif Y not in declared.get(X, {}):
                    declares = ", ".join(declared.get(X, {})) or "nothing"
                    findings.append(_finding(use_case, org, "LINT-01", len(findings) + 1, scope,
                        f"{X} declares {declares}, not {Y}. Compiled as a derived output of {X}; flagged for the owner."))
                    derived = {**_b_field(Y, cat), "derived": True, "source": "LINT-01: derived, not declared by the step"}
                    declared[X][Y] = derived
                    if X in by_id:
                        by_id[X]["outputs"].append(derived)
                    field = {**_b_field(Y, cat), "source": f"$steps.{X}.outputs.{Y}"}
                else:
                    field = {**declared[X][Y], "source": f"$steps.{X}.outputs.{Y}"}
                if field and field["path"] not in seen and not scope.endswith(".criteria"):
                    field.pop("entity", None)
                    node["inputs"].append(field)
                    seen.add(field["path"])
            if scope.endswith(".criteria"):
                continue
            for name, sub in _INPUT_REF.findall(expr):
                for f in _b_input_fields(name, sub or None, wf_inputs, cat):
                    f.pop("entity", None)
                    if f["path"] not in seen:
                        node["inputs"].append(f)
                        seen.add(f["path"])
        for f in node["outputs"]:
            f.pop("entity", None)
        nodes.append(node)
        by_id[sid] = node

    # terminal(s) from the profile
    for tid, t in prof.get("terminals", {}).items():
        nodes.append({"_id": f"{org}.{tid}", "org_id": org, "use_case_id": use_case, "name": tid, "kind": "terminal",
                      "visibility": t.get("visibility", "internal"), "outcome": t.get("outcome", tid),
                      "text": f"{tid}: terminal outcome {t.get('outcome', tid)}. {t.get('note', '')}".strip()})
    end_id = next((n["_id"] for n in nodes if n["kind"] == "terminal"), f"{org}.END")

    # pass 2: edges. >=2 gotos from one step -> an explicit decision node; all `end` exits -> the one terminal
    transitions: list[dict] = []
    for i, st in enumerate(steps):
        sid, nid = st["stepId"], f"{org}.{st['stepId']}"
        success = {c["condition"] for c in st.get("successCriteria", [])}
        on = st.get("onSuccess") or []
        if not on:
            if i + 1 < len(steps):
                _add_transition(transitions, _transition(org, use_case, nid, f"{org}.{steps[i + 1]['stepId']}", None,
                                                         guidance="next step (sequential)"))
            continue
        gotos = [o for o in on if o["type"] == "goto"]
        ends = [o for o in on if o["type"] == "end"]
        src = nid
        if len(gotos) >= 2:
            first = " ".join(c.get("condition", "") for c in gotos[0]["criteria"])
            m = _STEP_REF.search(first) or _BODY_REF.search(first)
            field = (m.group(2) if m and m.re is _STEP_REF else m.group(1)) if m else f"after_{sid}"
            dec = {"_id": f"{org}.decide_{field}", "org_id": org, "use_case_id": use_case, "name": f"decide_{field}",
                   "kind": "decision", "visibility": "internal", "role": ann.get(sid, {}).get("role", "lending_agent"),
                   "inputs": [_b_field(field, cat)], "outputs": [],
                   "text": f"decide_{field}: branch on {field} after {sid}: " + " / ".join(g["name"] for g in gotos),
                   "source": {"file": src_file, "step": i, "stepId": sid, "derived_from": "onSuccess goto criteria"}}
            dec["inputs"][0].pop("entity", None)
            nodes.append(dec)
            _add_transition(transitions, _transition(org, use_case, nid, dec["_id"], None, guidance="on success"))
            src = dec["_id"]
        for g in gotos:
            _add_transition(transitions, _transition(org, use_case, src, f"{org}.{g['stepId']}",
                                                     _criteria_ast(g.get("criteria", []), success), guidance=g["name"]))
        if ends:
            conds = [c for c in (_criteria_ast(e.get("criteria", []), success) for e in ends) if c]
            cond = None if len(conds) < len(ends) else (conds[0] if len(conds) == 1 else {"op": "or", "args": conds})
            _add_transition(transitions, _transition(org, use_case, nid, end_id, cond,
                                                     guidance=" | ".join(e["name"] for e in ends)))

    policies = {k: {**v, "rule": to_ast(v["rule"])} if isinstance(v, dict) and "rule" in v else v
                for k, v in prof.get("policies", {}).items()}
    extra = {
        "procedure": prof.get("name"), "description": prof.get("description"),
        "procedure_ref": prof.get("procedure_ref"), "workflow_id": wf.get("workflowId"),
        "handoffs": prof.get("handoffs", []), "policies": policies,
        "tools": [{"ref": k, **v} for k, v in tool_cat.items()],
        "entities": prof.get("entities", {}), "roles": prof.get("roles", {}),
        "merge_preferences": prof.get("merge_preferences", {}), "outcomes": prof.get("outcomes", []),
        "exceptions": prof.get("exceptions", []), "evidence": prof.get("evidence", {}), "terms": prof.get("terms", {}),
    }
    return _submission(org, prof.get("org_id", "lakeside"), use_case, prof.get("procedure_version", "0"),
                       nodes, transitions, extra), findings


# --------------------------------------------------------------------------- driver

def compile_all(use_case: str = DEFAULT_USE_CASE) -> tuple[list[dict], list[dict]]:
    sub_a, f_a = compile_northwind(use_case=use_case)
    sub_b, f_b = compile_lakeside(use_case=use_case)
    findings = f_a + f_b
    for f in findings:
        spec.validate(f, "finding")
    return [sub_a, sub_b], findings


def write(submissions: list[dict], findings: list[dict], use_case: str) -> None:
    from pmp import db
    for s in submissions:
        db.col("submissions").replace_one({"_id": s["_id"]}, s, upsert=True)
    db.col("findings").delete_many({"use_case_id": use_case, "stage": "compile"})
    if findings:
        db.col("findings").insert_many(findings)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Stage 1: compile Stage 0 inputs into typed graphs.")
    p.add_argument("--use-case", default=DEFAULT_USE_CASE)
    p.add_argument("--dry-run", action="store_true", help="compile and print, do not write to Atlas")
    p.add_argument("--fixture", action="store_true", help="also write mock/stages/1_compile.json")
    a = p.parse_args(argv)
    subs, findings = compile_all(a.use_case)
    for s in subs:
        gates = sum(n["kind"] == "human_gate" for n in s["nodes"])
        print(f"{s['org_name']:<10} org {s['org_id']}  nodes {len(s['nodes']):2d}  transitions {len(s['transitions']):2d}"
              f"  human_gates {gates}  hash {s['hash'][:19]}…")
    for f in findings:
        print(f"  [{f['owner_org']}] {f['check']} {f['scope']}: {f['detail']}")
    if a.fixture:
        out = ROOT / "mock" / "stages" / "1_compile.json"
        out.write_text(json.dumps({"submissions": subs, "findings": findings}, indent=2, sort_keys=True) + "\n")
        print(f"fixture written: {out.relative_to(ROOT)}")
    if not a.dry_run:
        write(subs, findings, a.use_case)
        print(f"wrote {len(subs)} submissions and {len(findings)} compile findings to Atlas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
