"""Demo API: serves ui/index.html at / and aggregates /state from both agents, the latest contract and the
pipeline collections. `python -m pmp.runtime.api --port 8000 --a-url http://localhost:8001 --b-url http://localhost:8002`"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

from pmp import db

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui" / "index.html"
DEFAULT_USE_CASE = "bnpl_checkout_v1"


def _agent_state(url: str) -> dict:
    try:
        import httpx
        return httpx.get(f"{url.rstrip('/')}/state", timeout=5).json()
    except Exception as e:  # agent down: still render the rest
        return {"error": str(e)}


def aggregate(use_case: str, urls: dict[str, str]) -> dict:
    """A dozen independent reads (Atlas round trips + two agent calls) run in parallel so the UI's one-second poll stays fast."""
    from concurrent.futures import ThreadPoolExecutor
    q = {"use_case_id": use_case}
    jobs = {
        "contracts": lambda: list(db.col("contracts").find(q).sort("version", 1)),
        "n_submissions": lambda: db.col("submissions").count_documents(q),
        "n_alignments": lambda: db.col("alignments").count_documents(q),
        "n_merged": lambda: db.col("merged").count_documents({**q, "doc_type": "graph"}),
        "n_validate": lambda: db.col("findings").count_documents({**q, "stage": "validate"}),
        "n_questions": lambda: db.col("merge_questions").count_documents(q),
        "n_handoffs": lambda: db.col("handoffs").count_documents({**q, "role": "sender"}),
        "n_rejections": lambda: db.col("rejections").count_documents(q),
        "questions": lambda: list(db.col("merge_questions").find(q).sort("_id", 1)),
        "findings": lambda: list(db.col("findings").find({**q, "stage": {"$in": ["validate", "revalidate"]}})),
        "rejections": lambda: list(db.col("rejections").find(q)),
        "handoffs": lambda: list(db.col("handoffs").find({**q, "role": "sender"}).sort([("run", 1), ("seq", 1)])),
        "merge_log": lambda: list(db.col("merge_log").find(q)),
        "seed": lambda: db.col("merge_log").find_one({"_id": f"{use_case}:seed"}),
        "done": lambda: db.col("handoffs").find_one({**q, "run": "0002", "edge": "h3", "role": "sender", "status": "accepted"}, sort=[("ts", -1)]),
        "projA": lambda: db.col("projections").find_one({"use_case_id": use_case, "org_id": "A"}, sort=[("contract_version", -1)]),
        "projB": lambda: db.col("projections").find_one({"use_case_id": use_case, "org_id": "B"}, sort=[("contract_version", -1)]),
        **{f"agent{org}": (lambda u=u: _agent_state(u)) for org, u in urls.items()},
    }
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {k: ex.submit(fn) for k, fn in jobs.items()}
        r = {k: f.result() for k, f in futures.items()}
    contracts = r["contracts"]
    latest = contracts[-1] if contracts else None
    stages = {"compile": r["n_submissions"], "align": r["n_alignments"], "merge": r["n_merged"], "validate": r["n_validate"],
              "decide": r["n_questions"], "contract": len(contracts), "handoffs": r["n_handoffs"], "rejections": r["n_rejections"]}
    projections = {}
    for org in ("A", "B"):
        pr = r[f"proj{org}"]
        if pr and latest and pr.get("contract_version") != latest["version"]:
            pr = db.col("projections").find_one({"_id": f"{use_case}:{org}:v{latest['version']}"}) or pr
        if pr:
            opaque = next((n for n in pr["nodes"] if n.get("opaque")), None)
            projections[org] = {
                "contract_version": pr["contract_version"], "counts": pr.get("counts"),
                "nodes": [{"id": n["_id"], "name": n["name"], "kind": n["kind"], "origin": n.get("origin"), "opaque": bool(n.get("opaque")),
                           "visibility": n.get("visibility")} for n in pr["nodes"]],
                "edges": [{"id": e["_id"], "from": e["from"], "to": e["to"], "type": e.get("type", "normal"), "allowlist": e.get("allowlist"),
                           "guard": e.get("guard")} for e in pr["edges"]],
                "opaque": {"id": opaque["_id"], "name": opaque["name"], "text": opaque["text"], "interface": opaque["interface"]} if opaque else None,
                "invariants": [{"check": r["check"], "verdict": r["verdict"], "detail": r["detail"]} for r in pr.get("invariants", [])],
                "privacy": (pr.get("privacy") or {}).get("verdict"),
            }
    seed, done = r["seed"], r["done"]
    runs = {}
    for h in r["handoffs"]:
        runs.setdefault(h["run"], []).append({"seq": h["seq"], "edge": h["edge"], "status": h["status"], "ts": h["ts"]})
    return {
        "use_case_id": use_case,
        "stopwatch": {"seed_ts": seed["ts"] if seed else None, "done_ts": done["ts"] if done else None},
        "projections": projections,
        "runs": runs,
        "contract": {k: latest.get(k) for k in ("contract_id", "version", "status", "certificate_id", "reason", "supersedes", "issued_at")} if latest else None,
        "contracts": [{k: c.get(k) for k in ("version", "status", "reason", "issued_at")} for c in contracts],
        "certificate_checks": len((latest or {}).get("certificate", {}).get("checks", [])) if latest else 0,
        "stages": stages,
        "questions": [{k: x.get(k) for k in ("_id", "trigger", "to_org", "to_role", "text", "answer", "accepted_default")} for x in r["questions"]],
        "findings": [{k: x.get(k) for k in ("check", "verdict", "scope", "stakes", "owner_org", "stage", "resolved_by")} for x in r["findings"]],
        "rejections": [{**x, "_id": str(x.get("_id"))} for x in r["rejections"]],
        "handoffs": [{k: h.get(k) for k in ("run", "seq", "edge", "sender", "receiver", "status", "ts")} for h in r["handoffs"]],
        "merge_log": [{k: e.get(k) for k in ("_id", "type", "rule", "status", "contract_version", "supersedes", "ts", "check", "scope")}
                      | ({"plan": {"code": e["plan"].get("code"), "edge": e["plan"].get("edge"),
                                   "ops": [{"op": o.get("op"), "adapter": o.get("adapter"), "found_in": o.get("found_in"),
                                            "transform": o.get("transform")} for o in e["plan"].get("ops", [])]}} if e.get("plan") else {})
                      for e in r["merge_log"]],
        "agents": {org: r[f"agent{org}"] for org in urls},
    }


class Refresher:
    """Keeps a fresh snapshot in the background so /state answers instantly whatever Atlas latency is."""

    def __init__(self, use_case: str, urls: dict[str, str], period: float = 0.7):
        import threading
        self.use_case, self.urls, self.period = use_case, urls, period
        self.snapshot: dict | None = None
        self.error: str | None = None
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self):
        import time
        while True:
            t0 = time.time()
            try:
                self.snapshot = {**aggregate(self.use_case, self.urls), "refreshed_at": time.time(), "refresh_seconds": round(time.time() - t0, 2)}
                self.error = None
            except Exception as e:  # keep serving the last good snapshot
                self.error = str(e)
            time.sleep(max(0.0, self.period - (time.time() - t0)))


def create_app(use_case: str, urls: dict[str, str]):
    from fastapi import FastAPI
    from fastapi.responses import FileResponse
    app = FastAPI(title="PMP mediator API")
    refresher = Refresher(use_case, urls)

    @app.get("/")
    def index():
        return FileResponse(UI)

    @app.get("/demo.json")
    def demo_json():
        return FileResponse(UI.parent / "demo.json", media_type="application/json")

    @app.get("/state")
    def state():
        if refresher.snapshot is None:
            return aggregate(use_case, urls)
        return {**refresher.snapshot, "refresh_error": refresher.error}

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="PMP demo API + UI.")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--a-url", default="http://127.0.0.1:8001")
    p.add_argument("--b-url", default="http://127.0.0.1:8002")
    p.add_argument("--use-case", default=DEFAULT_USE_CASE)
    a = p.parse_args(argv)
    import uvicorn
    uvicorn.run(create_app(a.use_case, {"A": a.a_url, "B": a.b_url}), host="127.0.0.1", port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
