"""Seed Atlas for one use case: reset its documents, ensure every collection exists,
and ensure the vector index `steps_vec_idx` on steps_vec.embedding. Invoked by scripts/seed.sh."""
from __future__ import annotations
import argparse
import sys
import time

from pymongo.operations import SearchIndexModel

from pmp import db

VECTOR_INDEX = "steps_vec_idx"
VECTOR_INDEX_DEFINITION = {
    "fields": [
        {"type": "vector", "path": "embedding", "numDimensions": 1536, "similarity": "cosine"},
        {"type": "filter", "path": "org_id"},
        {"type": "filter", "path": "use_case_id"},
    ]
}


def ensure_collections() -> list[str]:
    """Create any missing collection from pmp.db.COLLECTIONS. Returns the names created."""
    existing = set(db.db().list_collection_names())
    created = []
    for name in db.COLLECTIONS:
        if name not in existing:
            db.db().create_collection(name)
            created.append(name)
    return created


def ensure_vector_index(wait: bool = True, timeout_s: int = 300) -> str:
    """Create steps_vec_idx if absent. Returns 'exists', 'created' or 'created (not yet queryable)'."""
    col = db.col("steps_vec")
    if any(ix["name"] == VECTOR_INDEX for ix in col.list_search_indexes()):
        return "exists"
    col.create_search_index(
        SearchIndexModel(definition=VECTOR_INDEX_DEFINITION, name=VECTOR_INDEX, type="vectorSearch")
    )
    if not wait:
        return "created (not yet queryable)"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for ix in col.list_search_indexes():
            if ix["name"] == VECTOR_INDEX and ix.get("queryable"):
                return "created"
        time.sleep(3)
    return "created (not yet queryable)"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Reset a use case and prepare Atlas collections + vector index.")
    p.add_argument("--use-case", default="bnpl_checkout_v1")
    p.add_argument("--no-wait", action="store_true", help="do not block until the vector index is queryable")
    a = p.parse_args(argv)

    print(f"seed: database {db.db().name}")
    created = ensure_collections()
    print(f"seed: collections ready ({len(db.COLLECTIONS)} total, {len(created)} created)")
    db.reset(a.use_case)
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    db.col("merge_log").insert_one({"_id": f"{a.use_case}:seed", "use_case_id": a.use_case, "type": "seed", "ts": ts})
    print(f"seed: reset use case {a.use_case} at {ts}")
    print(f"seed: vector index {VECTOR_INDEX} {ensure_vector_index(wait=not a.no_wait)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
