"""One org's runtime agent. Loads its projection under the active contract, executes its own nodes in order with
stubbed tools (outputs from mock/stages/6_fixtures.json), applies adapter transforms, and hands work across the
boundary as A2A tasks. Incoming handoffs pass the four receiver checks or produce a typed rejection.

Runs in-process (tests, `pmp.runtime.run`) or as a FastAPI service: `python -m pmp.runtime.agent --org A --port 8001 --peer http://localhost:8002`.
"""
from __future__ import annotations
import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Callable

from pmp import db
from pmp.decide import now
from pmp.predicate import evaluate, fields_referenced, has, resolve, to_text
from pmp.runtime import a2a

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "mock" / "stages" / "6_fixtures.json"
DEFAULT_USE_CASE = "bnpl_checkout_v1"
Transport = Callable[[str, dict], dict]


def load_fixtures(path: Path = FIXTURES) -> dict:
    runs = json.loads(Path(path).read_text())["runs"]
    for rid, r in list(runs.items()):
        if r.get("inherits"):
            base = runs[r["inherits"]]
            runs[rid] = {**base, **r, "inputs": r.get("inputs", base.get("inputs", {})),
                         "tools": {**base.get("tools", {}), **r.get("tools", {})}}
    return runs


def _get(d: dict, path: str) -> Any:
    return resolve(path, d)


def _set(d: dict, path: str, value: Any) -> None:
    """Nested write that never clobbers a list on the way (line_item.count is computed from the list itself)."""
    cur = d
    parts = path.split(".")
    for p in parts[:-1]:
        nxt = cur.get(p)
        if isinstance(nxt, list):
            return
        if not isinstance(nxt, dict):
            nxt = cur[p] = {}
        cur = nxt
    if isinstance(cur.get(parts[-1]), list) and not isinstance(value, list):
        return
    cur[parts[-1]] = value


def _find_leaf(d: Any, key: str) -> Any:
    if isinstance(d, dict):
        if key in d:
            return d[key]
        for v in d.values():
            r = _find_leaf(v, key)
            if r is not None:
                return r
    return None


def deep_merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)


def pred_ctx(state: dict) -> dict:
    """Predicates see lists as {count, items} so `line_item.count` works."""
    ctx = {}
    for k, v in state.items():
        ctx[k] = {"count": len(v), "items": v} if isinstance(v, list) else v
    return ctx


# --------------------------------------------------------------------------- transforms

def _num(tok: str):
    try:
        return int(tok)
    except ValueError:
        try:
            return float(tok)
        except ValueError:
            return None


def _lookup(token: str, scopes: list[dict], from_paths: list[str]) -> Any:
    n = _num(token)
    if n is not None:
        return n
    for sc in scopes:
        v = _get(sc, token)
        if v is not None:
            return v
    for fp in from_paths:
        leaf = fp.split(".")[-1]
        for sc in scopes:
            base = _get(sc, fp)
            if base is None:
                base = _get(sc, leaf)
            if base is None:
                continue
            if token == leaf:
                return base
            if isinstance(base, dict):
                v = _get(base, token[len(leaf) + 1:]) if token.startswith(leaf + ".") else _get(base, token)
                if v is not None:
                    return v
    for sc in scopes:
        v = _find_leaf(sc, token.split(".")[-1])
        if v is not None:
            return v
    raise KeyError(f"transform cannot resolve {token!r}")


def eval_expr(expr: str, scopes: list[dict], from_paths: list[str]) -> Any:
    toks = expr.replace("×", "*").split()
    val = _lookup(toks[0], scopes, from_paths)
    i = 1
    while i + 1 < len(toks):
        op, rhs = toks[i], _lookup(toks[i + 1], scopes, from_paths)
        val = {"*": lambda a, b: a * b, "/": lambda a, b: a / b, "+": lambda a, b: a + b, "-": lambda a, b: a - b}[op](val, rhs)
        i += 2
    return val


def apply_transform(state: dict, t: dict, currency: str = "USD") -> None:
    frm, to = t.get("from", []), t["to"]
    prefix = frm[0].split(".")[0] if frm else None
    items = state.get(prefix) if prefix else None
    if isinstance(items, list):
        out_prefix, _, sub = to.partition(".")
        outs = state.get(out_prefix)
        if not isinstance(outs, list) or len(outs) != len(items):
            outs = [{} for _ in items]
            state[out_prefix] = outs
        for item, o in zip(items, outs):
            val = eval_expr(t["expr"], [item, state], frm)
            if sub:
                _set(o, sub, val)
                if sub.endswith(".amount"):
                    _set(o, sub[:-7] + ".currency", currency)
            else:
                o.update(val if isinstance(val, dict) else {to: val})
        return
    val = eval_expr(t["expr"], [state], frm)
    _set(state, to, val)
    if to.endswith(".amount"):
        _set(state, to[:-7] + ".currency", currency)


# --------------------------------------------------------------------------- the agent

class Agent:
    def __init__(self, org: str, transport: Transport | None = None, fixtures: dict | None = None,
                 use_case: str = DEFAULT_USE_CASE):
        self.org, self.partner, self.use_case = org, ("B" if org == "A" else "A"), use_case
        self.transport = transport
        self.fixtures = fixtures or load_fixtures()
        self.runs: dict[str, dict] = {}
        self.log: list[dict] = []
        self.contract = self.projection = self.submission = None
        self.nodes: dict[str, dict] = {}
        self.out: dict[str, list[dict]] = {}
        self.load()

    # ---- contract / projection
    def load(self) -> None:
        self.contract = db.col("contracts").find_one({"use_case_id": self.use_case, "status": "active"}, sort=[("version", -1)])
        self.submission = db.col("submissions").find_one({"_id": f"{self.use_case}:{self.org}"})
        self.projection = (db.col("projections").find_one({"_id": f"{self.use_case}:{self.org}:v{self.contract['version']}"})
                           if self.contract else None)
        self.nodes = {n["_id"]: n for n in (self.projection or {}).get("nodes", [])}
        self.out = {}
        for e in (self.projection or {}).get("edges", []):
            self.out.setdefault(e["from"], []).append(e)
        self._log(None, "load", f"contract {self.contract['contract_id']} v{self.contract['version']} ({self.contract['status']}), "
                                f"projection {len(self.nodes)} nodes" if self.contract else "no active contract")

    def fresh_contract(self) -> dict | None:
        return db.col("contracts").find_one({"_id": self.contract["_id"]}) if self.contract else None

    def _log(self, run: dict | None, event: str, detail: str, node: str | None = None) -> None:
        entry = {"ts": now(), "org": self.org, "run": run["run"] if run else None, "node": node, "event": event, "detail": detail}
        self.log.append(entry)
        if run is not None:
            run["log"].append(entry)

    # ---- runs
    def _new_run(self, run_id: str, inputs: dict | None) -> dict:
        run = {"run": run_id, "org": self.org, "state": copy.deepcopy(inputs or {}), "status": "running", "log": [],
               "done": [], "current": None, "outcome": None, "handoffs": [], "entered": None, "started_at": now()}
        self.runs[run_id] = run
        return run

    def start_run(self, run_id: str, inputs: dict | None = None) -> dict:
        inputs = inputs if inputs is not None else self.fixtures.get(run_id, {}).get("inputs", {})
        run = self._new_run(run_id, inputs)
        start = next((nid for nid, n in self.nodes.items() if not n.get("opaque") and not any(e["to"] == nid for e in (self.projection or {}).get("edges", []))), None)
        self._log(run, "start", f"run {run_id} from {start}")
        if start:
            self.walk(run, start)
        return self.run_summary(run_id)

    def run_summary(self, run_id: str) -> dict:
        run = self.runs[run_id]
        return {"run": run_id, "org": self.org, "status": run["status"], "outcome": run["outcome"], "done": list(run["done"]),
                "current": run["current"], "handoffs": list(run["handoffs"]), "rejections": [h for h in run["handoffs"] if h.get("status") == "rejected"]}

    def state(self) -> dict:
        c = self.fresh_contract() or self.contract
        return {"org": self.org, "use_case_id": self.use_case,
                "contract": {"contract_id": c["contract_id"], "version": c["version"], "status": c["status"], "certificate_id": c["certificate_id"]} if c else None,
                "projection": {"nodes": [{"id": n["_id"], "name": n["name"], "kind": n["kind"], "opaque": bool(n.get("opaque")), "origin": n.get("origin")}
                                         for n in self.nodes.values()], "edges": len((self.projection or {}).get("edges", []))},
                "runs": {rid: self.run_summary(rid) for rid in self.runs},
                "log": self.log[-60:]}

    # ---- execution
    def _tool_outputs(self, run: dict, node: dict) -> dict:
        return self.fixtures.get(run["run"], {}).get("tools", {}).get(node["_id"], {})

    def exec(self, run: dict, node: dict) -> bool:
        run["current"] = node["_id"]
        state = run["state"]
        ctx = pred_ctx(state)
        if node.get("pre") and not evaluate(node["pre"], ctx):
            if node["kind"] == "human_gate":
                for f in node.get("outputs", []):
                    _set(state, f["path"], True)
                self._log(run, "gate", f"not required: {to_text(node['pre'])} is false", node["_id"])
                run["done"].append(node["_id"])
                return True
            self._log(run, "precondition_failed", to_text(node["pre"]), node["_id"])
            return False
        if node["kind"] == "human_gate":
            outs = self._tool_outputs(run, node) or {f["path"]: True for f in node.get("outputs", [])}
            for k, v in outs.items():
                _set(state, k, v)
            self._log(run, "gate", f"human approval recorded: {outs}", node["_id"])
        elif node.get("transform"):
            for t in node["transform"]:
                apply_transform(state, t, currency=_get(state, "invoice.currency") or "USD")
            for f in node.get("outputs", []):
                v = _get(state, f["path"])
                if f.get("type") == "money" and isinstance(v, (int, float)):
                    _set(state, f["path"], {"currency": _get(state, "invoice.currency") or "USD", "amount": v})
            self._log(run, "adapter", "; ".join(f"{t['to']} = {t['expr']}" for t in node["transform"]), node["_id"])
        elif node.get("tool"):
            outs = self._tool_outputs(run, node)
            for k, v in outs.items():
                _set(state, k, v)
            self._log(run, "tool", f"{node['tool']['server']}.{node['tool']['name']} -> {list(outs) or 'no outputs'}", node["_id"])
        for f in node.get("outputs", []):
            if str(f.get("source", "")).startswith("echo of") and _get(state, f["path"]) is None:
                v = _find_leaf(state, f["path"].split(".")[-1])
                if v is not None:
                    _set(state, f["path"], v)
        if node.get("post") and not evaluate(node["post"], pred_ctx(state)):
            self._log(run, "postcondition_unmet", to_text(node["post"]), node["_id"])
        if node["kind"] not in ("human_gate",) and not node.get("transform") and not node.get("tool"):
            self._log(run, "step", node["kind"], node["_id"])
        run["done"].append(node["_id"])
        return True

    def choose_edge(self, run: dict, node: dict) -> dict | None:
        ctx = pred_ctx(run["state"])
        edges = [e for e in self.out.get(node["_id"], []) if e.get("type") not in ("compensation", "failure")]
        for e in edges:
            if e.get("condition") and evaluate(e["condition"], ctx):
                return e
        # a handoff-out node sends before it continues locally
        return (next((e for e in edges if not e.get("condition") and e.get("type") == "boundary"), None)
                or next((e for e in edges if not e.get("condition")), None))

    def walk(self, run: dict, node_id: str) -> None:
        cur: str | None = node_id
        while cur:
            node = self.nodes.get(cur)
            if not node or node.get("opaque"):
                break
            if not self.exec(run, node):
                run["status"] = "failed"
                break
            if node["kind"] == "terminal" or (node.get("outcome") and not self.out.get(cur)):
                run["status"], run["outcome"] = "completed", node.get("outcome")
                self._log(run, "completed", f"outcome {node.get('outcome')}", cur)
                break
            edge = self.choose_edge(run, node)
            if edge is None:
                run["status"] = "completed" if node.get("outcome") else "stalled"
                run["outcome"] = node.get("outcome")
                break
            if edge.get("type") == "boundary":
                resp = self.send(run, edge, node)
                if not resp.get("accepted"):
                    run["status"] = "held" if resp.get("held") else "rejected"
                    break
                if run["status"] == "completed":
                    break
                if resp.get("run_status") == "completed":
                    default = next((e for e in self.out.get(cur, []) if not e.get("condition") and e.get("type") != "boundary"), None)
                    if default:
                        cur = default["to"]
                        continue
                run["status"] = "waiting"
                break
            nxt = edge["to"]
            if self.nodes.get(nxt, {}).get("visibility") == "handoff_in" and nxt != run.get("entered") and not self.nodes[nxt].get("opaque"):
                run["status"], run["waiting_at"] = "waiting", nxt
                self._log(run, "waiting", f"for a handoff into {nxt}", cur)
                break
            cur = nxt

    # ---- boundary
    def required_inputs(self, node: dict) -> list[str]:
        sub_node = next((n for n in (self.submission or {}).get("nodes", []) if n["_id"] == node["_id"]), node)
        req = [f["path"] for f in sub_node.get("inputs", []) if f.get("required")]
        entities = (self.submission or {}).get("entities", {})
        req += [f for f in sorted(fields_referenced(node.get("pre"))) if f.split(".")[0] not in entities and f not in req]
        return req

    def send(self, run: dict, edge: dict, node: dict) -> dict:
        ce = next((e for e in self.contract.get("boundary_edges", []) if e["id"] == edge["_id"]), {})
        allowlist = ce.get("allowlist") or edge.get("allowlist") or []
        payload = a2a.build_payload(run["state"], allowlist)
        guard_results = {}
        if ce.get("guard"):
            ctx = pred_ctx(run["state"])
            g = ce["guard"]
            for clause in (g["args"] if g["op"] == "and" else [g]):
                refs = fields_referenced(clause)
                if refs and all(has(f, ctx) for f in refs):        # a clause I can decide: decide it before sending
                    ok = evaluate(clause, ctx)
                    guard_results[to_text(clause)] = ok
                    if not ok:
                        self._log(run, "guard_failed", to_text(clause), node["_id"])
                        return {"accepted": False, "held": True}
                else:                                               # references the receiver's own data: its precondition decides
                    guard_results[to_text(clause)] = "deferred to receiver"
        seq = db.col("handoffs").count_documents({"use_case_id": self.use_case, "run": run["run"], "role": "sender"}) + 1
        h = a2a.build_handoff(self.contract, edge["_id"], self.org, self.partner, run["run"], seq, payload, guard_results,
                              attestations=[f"{self.org}:{n}" for n in run["done"][-3:]])
        rec = {"_id": f"{self.use_case}:{run['run']}:{seq}:{edge['_id']}:{self.org}", "use_case_id": self.use_case, "run": run["run"],
               "seq": seq, "edge": edge["_id"], "role": "sender", "sender": self.org, "receiver": self.partner, "status": "sent",
               "handoff": h, "ts": now()}
        db.col("handoffs").insert_one(rec)
        entry = {"edge": edge["_id"], "seq": seq, "direction": "sent", "status": "sent", "rejection": None, "partner_run_status": None}
        run["handoffs"].append(entry)          # appended before the call: nested handoffs land after it in order
        self._log(run, "handoff_sent", f"{edge['_id']} seq {seq} -> {self.partner}: {sorted(a2a.flatten(payload))}", node["_id"])
        resp = self.transport(self.partner, h) if self.transport else {"accepted": False, "error": "no transport"}
        status = "accepted" if resp.get("accepted") else "rejected"
        db.col("handoffs").update_one({"_id": rec["_id"]}, {"$set": {"status": status, "response": resp}})
        entry.update({"status": status, "rejection": resp.get("rejection"), "partner_run_status": resp.get("run_status")})
        self._log(run, f"handoff_{status}", f"{edge['_id']} seq {seq}" + (f": {resp['rejection']['code']} {resp['rejection'].get('missing') or resp['rejection'].get('field') or ''}" if status == "rejected" else ""), node["_id"])
        return resp

    def receive(self, handoff: dict) -> dict:
        pmp = handoff["metadata"]["pmp"]
        run_id, edge_id = pmp["run"], pmp["edge"]
        run = self.runs.get(run_id) or self._new_run(run_id, {})
        pe = next((e for e in (self.projection or {}).get("edges", []) if e["_id"] == edge_id and e.get("type") == "boundary"), None)
        node = self.nodes.get(pe["to"]) if pe else None
        required = self.required_inputs(node) if node else []
        rej = a2a.receiver_check(handoff, self.org, self.use_case, self.fresh_contract(), self.projection or {}, node, required, run["state"])
        rec = {"_id": f"{self.use_case}:{run_id}:{pmp['seq']}:{edge_id}:{self.org}", "use_case_id": self.use_case, "run": run_id,
               "seq": pmp["seq"], "edge": edge_id, "role": "receiver", "sender": pmp["sender"], "receiver": self.org,
               "status": "rejected" if rej else "accepted", "handoff": handoff, "ts": now()}
        db.col("handoffs").insert_one(rec)
        if rej:
            rej["_id"] = f"{self.use_case}:{run_id}:{pmp['seq']}:{edge_id}:{self.org}"
            db.col("rejections").insert_one(rej)
            db.col("contracts").update_one({"_id": self.contract["_id"], "status": "active"}, {"$set": {"status": "suspect", "suspect_reason": rej["code"]}})
            run["handoffs"].append({"edge": edge_id, "seq": pmp["seq"], "direction": "received", "status": "rejected", "rejection": rej})
            self._log(run, "handoff_rejected", f"{edge_id} seq {pmp['seq']}: {rej['code']} {rej.get('missing') or rej.get('field') or ''}; contract -> suspect",
                      node["_id"] if node else None)
            return {"accepted": False, "rejection": {k: v for k, v in rej.items() if k != "_id"}}
        deep_merge(run["state"], handoff.get("payload") or {})
        run["entered"] = node["_id"]
        run["status"] = "running"
        run["handoffs"].append({"edge": edge_id, "seq": pmp["seq"], "direction": "received", "status": "accepted"})
        self._log(run, "handoff_accepted", f"{edge_id} seq {pmp['seq']} from {pmp['sender']}: checks 1-4 passed", node["_id"])
        self.walk(run, node["_id"])
        return {"accepted": True, "run_status": run["status"], "outcome": run["outcome"], "seq": pmp["seq"]}


# --------------------------------------------------------------------------- transports + service

class InProcessTransport:
    def __init__(self, agents: dict[str, Agent] | None = None):
        self.agents = agents if agents is not None else {}

    def __call__(self, org: str, handoff: dict) -> dict:
        return self.agents[org].receive(copy.deepcopy(handoff))


class HttpTransport:
    def __init__(self, urls: dict[str, str]):
        self.urls = urls

    def __call__(self, org: str, handoff: dict) -> dict:
        import httpx
        return httpx.post(f"{self.urls[org].rstrip('/')}/a2a", json=handoff, timeout=180).json()


def create_app(agent: Agent):
    from fastapi import FastAPI
    app = FastAPI(title=f"PMP agent {agent.org}")

    @app.post("/a2a")
    def a2a_endpoint(handoff: dict):
        return agent.receive(handoff)

    @app.post("/run/{run_id}")
    def run_endpoint(run_id: str):
        return agent.start_run(run_id)

    @app.post("/reload")
    def reload():
        agent.load()
        return agent.state()["contract"]

    @app.get("/state")
    def state():
        return agent.state()

    @app.get("/health")
    def health():
        return {"ok": True, "org": agent.org, "contract_version": agent.contract["version"] if agent.contract else None}

    return app


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="PMP runtime agent for one org.")
    p.add_argument("--org", required=True, choices=["A", "B"])
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--peer", required=True, help="base URL of the other org's agent")
    p.add_argument("--use-case", default=DEFAULT_USE_CASE)
    a = p.parse_args(argv)
    import uvicorn
    agent = Agent(a.org, HttpTransport({("B" if a.org == "A" else "A"): a.peer}), use_case=a.use_case)
    uvicorn.run(create_app(agent), host="127.0.0.1", port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
