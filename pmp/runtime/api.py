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
    contracts = list(db.col("contracts").find({"use_case_id": use_case}).sort("version", 1))
    latest = contracts[-1] if contracts else None
    stages = {
        "compile": db.col("submissions").count_documents({"use_case_id": use_case}),
        "align": db.col("alignments").count_documents({"use_case_id": use_case}),
        "merge": db.col("merged").count_documents({"use_case_id": use_case, "doc_type": "graph"}),
        "validate": db.col("findings").count_documents({"use_case_id": use_case, "stage": "validate"}),
        "decide": db.col("merge_questions").count_documents({"use_case_id": use_case}),
        "contract": len(contracts),
        "handoffs": db.col("handoffs").count_documents({"use_case_id": use_case, "role": "sender"}),
        "rejections": db.col("rejections").count_documents({"use_case_id": use_case}),
    }
    return {
        "use_case_id": use_case,
        "contract": {k: latest.get(k) for k in ("contract_id", "version", "status", "certificate_id", "reason", "supersedes", "issued_at")} if latest else None,
        "contracts": [{k: c.get(k) for k in ("version", "status", "reason", "issued_at")} for c in contracts],
        "certificate_checks": len((latest or {}).get("certificate", {}).get("checks", [])) if latest else 0,
        "stages": stages,
        "questions": [{k: q.get(k) for k in ("_id", "trigger", "to_org", "to_role", "text", "answer", "accepted_default")}
                      for q in db.col("merge_questions").find({"use_case_id": use_case}).sort("_id", 1)],
        "findings": [{k: f.get(k) for k in ("check", "verdict", "scope", "stakes", "owner_org", "stage", "resolved_by")}
                     for f in db.col("findings").find({"use_case_id": use_case, "stage": {"$in": ["validate", "revalidate"]}})],
        "rejections": [{**r, "_id": str(r.get("_id"))} for r in db.col("rejections").find({"use_case_id": use_case})],
        "handoffs": [{k: h.get(k) for k in ("run", "seq", "edge", "sender", "receiver", "status", "ts")}
                     for h in db.col("handoffs").find({"use_case_id": use_case, "role": "sender"}).sort([("run", 1), ("seq", 1)])],
        "merge_log": [{k: e.get(k) for k in ("_id", "type", "rule", "status", "contract_version", "ts")}
                      for e in db.col("merge_log").find({"use_case_id": use_case})],
        "agents": {org: _agent_state(u) for org, u in urls.items()},
    }


def create_app(use_case: str, urls: dict[str, str]):
    from fastapi import FastAPI
    from fastapi.responses import FileResponse
    app = FastAPI(title="PMP mediator API")

    @app.get("/")
    def index():
        return FileResponse(UI)

    @app.get("/state")
    def state():
        return aggregate(use_case, urls)

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
