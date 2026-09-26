"""Drive a run end to end.

In-process (default): both agents live in this process and hand off by direct call; the mediator handles a
rejection inline. Works against Atlas (default) or fully offline with --in-memory (bootstraps the whole
pipeline from the fixtures first).

--http: agents are running services (pmp.runtime.agent) and the mediator is watching the change stream
(pmp.runtime.mediator); this process only triggers runs and waits for the new contract.

Run 0001: USD 2,780 order, financed, happy path. Run 0002: Lakeside first republishes requiring
customer.postalCode on h1; the first h1 is rejected (MISSING_FIELDS), the mediator extends Northwind's adapter
from Northwind's own data model, contract v2 goes active, the retry passes.

CLI: python -m pmp.runtime.run --run 0001 | --run 0002 [--in-memory] [--http --a-url ... --b-url ...]
"""
from __future__ import annotations
import argparse
import sys
import time

from pmp import align, compile as cp, contract as ct, db, decide, merge, validate as v
from pmp.compile import sha256
from pmp.decide import now
from pmp.runtime import mediator
from pmp.runtime.agent import Agent, InProcessTransport, load_fixtures

DEFAULT_USE_CASE = "bnpl_checkout_v1"


def bootstrap(use_case: str = DEFAULT_USE_CASE, answers: dict | None = None) -> dict:
    """compile -> align (fixture) -> merge -> validate -> decide -> contract, in whatever backend pmp.db points at."""
    subs, cfind = cp.compile_all(use_case)
    cp.write(subs, cfind, use_case)
    als = align.load_fixture(use_case)
    align.write(als, use_case)
    m1 = merge.build(subs, als, use_case, 1)
    merge.write(m1)
    f1 = v.findings(v.run(m1, subs, als, [], db=None))
    v.write(f1, use_case, 1, "validate")
    m2, ds, qs, als = decide.build_v2(m1, f1, subs, als, answers if answers is not None else {})
    decide.write(m2, ds, qs, als, f1)
    out = ct.build(m2, subs, als, qs, ds, contract_version=1, db=None)
    ct.write(out, als)
    return out["contract"]


def republish(org: str, edge: str, fields: list[str], use_case: str = DEFAULT_USE_CASE) -> dict:
    """An org republishes its procedure: the receiving step of `edge` now REQUIRES these inputs."""
    sub = db.col("submissions").find_one({"_id": f"{use_case}:{org}"})
    contract = db.col("contracts").find_one({"use_case_id": use_case, "status": {"$in": ["active", "suspect"]}}, sort=[("version", -1)])
    ce = next(e for e in contract["boundary_edges"] if e["id"] == edge)
    node = next(n for n in sub["nodes"] if n["_id"] == ce["to"])
    have = {f["path"]: f for f in node.get("inputs", [])}
    for f in fields:
        if f in have:
            have[f]["required"] = True
        else:
            node.setdefault("inputs", []).append({"path": f, "type": "string", "required": True})
    major, minor, patch = (sub.get("version") or "1.0.0").split(".")[:3]
    sub["version"] = f"{major}.{int(minor) + 1}.0"
    sub["republished"] = {"ts": now(), "edge": edge, "requires": fields, "step": node["_id"]}
    body = {k: val for k, val in sub.items() if k not in ("hash", "_id")}
    sub["hash"] = sha256(body)
    db.col("submissions").replace_one({"_id": sub["_id"]}, sub)
    return sub


class InProcessRuntime:
    def __init__(self, use_case: str = DEFAULT_USE_CASE, fixtures: dict | None = None):
        self.use_case = use_case
        self.fixtures = fixtures or load_fixtures()
        self.transport = InProcessTransport()
        self.agents = {org: Agent(org, self.transport, self.fixtures, use_case) for org in ("A", "B")}
        self.transport.agents = self.agents
        self.events: list[dict] = []

    def _ev(self, what: str) -> None:
        self.events.append({"ts": now(), "event": what})

    def reload(self) -> None:
        for a in self.agents.values():
            a.load()

    def run(self, run_id: str) -> dict:
        fx = self.fixtures.get(run_id, {})
        if fx.get("republish"):
            r = fx["republish"]
            republish(r["org"], "h1", r["require_on_h1"], self.use_case)
            self._ev(f"{r['org']} republished: {r['require_on_h1']} now required on h1")
            self.reload()
        before = db.col("rejections").count_documents({"use_case_id": self.use_case, "run": run_id})
        summary = self.agents["A"].start_run(run_id)
        self._ev(f"run {run_id} attempt 1: A {summary['status']} ({summary['outcome']})")
        rejections = list(db.col("rejections").find({"use_case_id": self.use_case, "run": run_id}))
        patched = None
        if summary["status"] == "rejected" and len(rejections) > before:
            rej = rejections[-1]
            self._ev(f"rejection {rej['code']} on {rej['edge']} seq {rej['seq']}: {rej.get('missing') or rej.get('field')}; contract suspect")
            patched = mediator.handle_rejection(rej, self.use_case)
            self._ev(f"mediator: contract v{patched['version']} active, supersedes v{patched['supersedes']} ({patched['reason']})")
            self.reload()
            summary = self.agents["A"].start_run(run_id)
            self._ev(f"run {run_id} attempt 2 under contract v{patched['version']}: A {summary['status']} ({summary['outcome']})")
        b = self.agents["B"].runs.get(run_id, {})
        contract = db.col("contracts").find_one({"use_case_id": self.use_case, "status": "active"}, sort=[("version", -1)])
        return {"run": run_id, "A": summary, "B": {"status": b.get("status"), "outcome": b.get("outcome"), "done": b.get("done", [])},
                "rejections": [{k: r.get(k) for k in ("code", "edge", "seq", "missing", "field")} for r in
                               db.col("rejections").find({"use_case_id": self.use_case, "run": run_id})],
                "contract_version": contract["version"] if contract else None, "patched": patched is not None, "events": self.events}


def run_http(run_id: str, a_url: str, b_url: str, use_case: str, fixtures: dict) -> dict:
    import httpx
    events = []
    fx = fixtures.get(run_id, {})
    if fx.get("republish"):
        r = fx["republish"]
        republish(r["org"], "h1", r["require_on_h1"], use_case)
        events.append(f"{r['org']} republished: {r['require_on_h1']} now required on h1")
        for u in (a_url, b_url):
            httpx.post(f"{u}/reload", timeout=30)
    before = db.col("rejections").count_documents({"use_case_id": use_case, "run": run_id})
    summary = httpx.post(f"{a_url}/run/{run_id}", timeout=300).json()
    events.append(f"run {run_id} attempt 1: A {summary['status']}")
    if summary["status"] == "rejected":
        cur = db.col("contracts").find_one({"use_case_id": use_case}, sort=[("version", -1)])["version"]
        deadline = time.time() + 120
        while time.time() < deadline:
            new = db.col("contracts").find_one({"use_case_id": use_case, "status": "active", "version": {"$gt": cur}})
            if new:
                events.append(f"mediator: contract v{new['version']} active ({new['reason']})")
                break
            time.sleep(1)
        else:
            events.append("mediator did not publish a new contract in time")
        for u in (a_url, b_url):
            httpx.post(f"{u}/reload", timeout=30)
        summary = httpx.post(f"{a_url}/run/{run_id}", timeout=300).json()
        events.append(f"run {run_id} attempt 2: A {summary['status']}")
    contract = db.col("contracts").find_one({"use_case_id": use_case, "status": "active"}, sort=[("version", -1)])
    return {"run": run_id, "A": summary, "rejections": list(db.col("rejections").find({"use_case_id": use_case, "run": run_id})),
            "contract_version": contract["version"] if contract else None, "events": events}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Drive PMP run 0001 / 0002.")
    p.add_argument("--run", required=True)
    p.add_argument("--use-case", default=DEFAULT_USE_CASE)
    p.add_argument("--in-memory", action="store_true", help="no Atlas: bootstrap the pipeline in memory first")
    p.add_argument("--http", action="store_true", help="use running agent services + mediator")
    p.add_argument("--a-url", default="http://127.0.0.1:8001")
    p.add_argument("--b-url", default="http://127.0.0.1:8002")
    a = p.parse_args(argv)
    t0 = time.time()
    if a.in_memory:
        db.use_memory()
        bootstrap(a.use_case)
        print(f"bootstrapped pipeline in memory ({time.time() - t0:.1f}s)")
    fixtures = load_fixtures()
    if a.http:
        out = run_http(a.run, a.a_url, a.b_url, a.use_case, fixtures)
        for e in out["events"]:
            print(f"  {e}")
    else:
        rt = InProcessRuntime(a.use_case, fixtures)
        out = rt.run(a.run)
        for e in out["events"]:
            print(f"  {e['ts']}  {e['event']}")
        print(f"  B: {out['B']['status']} ({out['B']['outcome']}); nodes {len(out['B']['done'])}")
    print(f"run {a.run}: A {out['A']['status']} ({out['A'].get('outcome')}); handoffs {[(h['edge'], h['status']) for h in out['A'].get('handoffs', [])]}; "
          f"rejections {len(out['rejections'])}; contract v{out['contract_version']}; {time.time() - t0:.1f}s")
    return 0 if out["A"]["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
