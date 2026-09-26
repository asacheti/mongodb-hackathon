"""Single entry point to Atlas. Every module imports from here."""
from __future__ import annotations
import os
from functools import lru_cache
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.collection import Collection

load_dotenv()

COLLECTIONS = [
    "submissions", "steps_vec", "alignments", "merged", "findings",
    "merge_questions", "merge_log", "contracts", "projections", "handoffs", "rejections",
]

_memory = None  # set by use_memory(): an in-process stand-in for tests and --in-process runs


def use_memory(store=None):
    """Route every pmp.db.col() call to an in-memory store (no Atlas). Returns the store."""
    global _memory
    from pmp.memstore import MemoryStore
    _memory = store or MemoryStore()
    return _memory


def use_atlas() -> None:
    global _memory
    _memory = None


def in_memory() -> bool:
    return _memory is not None


@lru_cache(maxsize=1)
def client() -> MongoClient:
    return MongoClient(os.environ["MONGODB_URI"])

def db():
    return client()[os.environ.get("PMP_DB", "pmp")]

def col(name: str) -> Collection:
    if name not in COLLECTIONS:
        raise KeyError(f"unknown collection {name}; add it to pmp.db.COLLECTIONS")
    if _memory is not None:
        return _memory.col(name)
    return db()[name]

def reset(use_case_id: str) -> None:
    """Delete every document for one use case (used by scripts/seed.sh and tests)."""
    for name in COLLECTIONS:
        col(name).delete_many({"use_case_id": use_case_id})
