"""A tiny in-memory stand-in for the handful of pymongo Collection methods the pipeline uses.
Enabled with pmp.db.use_memory(); lets the runtime and its tests run in-process with no Atlas."""
from __future__ import annotations
import copy
from typing import Any, Iterable


def _get(doc: dict, path: str) -> Any:
    cur: Any = doc
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def _set(doc: dict, path: str, value: Any) -> None:
    cur = doc
    parts = path.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def _match(doc: dict, flt: dict) -> bool:
    for k, cond in flt.items():
        if k == "$or":
            if not any(_match(doc, c) for c in cond):
                return False
            continue
        val = _get(doc, k)
        if isinstance(cond, dict) and cond and all(str(op).startswith("$") for op in cond):
            for op, arg in cond.items():
                if op == "$in" and val not in arg:
                    return False
                if op == "$nin" and val in arg:
                    return False
                if op == "$ne" and val == arg:
                    return False
                if op == "$lt" and not (val is not None and val < arg):
                    return False
                if op == "$lte" and not (val is not None and val <= arg):
                    return False
                if op == "$gt" and not (val is not None and val > arg):
                    return False
                if op == "$gte" and not (val is not None and val >= arg):
                    return False
                if op == "$exists" and (val is not None) != bool(arg):
                    return False
        elif val != cond:
            return False
    return True


class Cursor:
    def __init__(self, docs: list[dict]):
        self._docs = docs

    def sort(self, key, direction: int = 1):
        pairs = key if isinstance(key, list) else [(key, direction)]
        docs = list(self._docs)
        for k, d in reversed(pairs):
            docs.sort(key=lambda x: (_get(x, k) is None, _get(x, k)), reverse=(d == -1))
        return Cursor(docs)

    def limit(self, n: int):
        return Cursor(self._docs[:n])

    def __iter__(self):
        return iter(copy.deepcopy(self._docs))

    def __len__(self):
        return len(self._docs)


class _Result:
    def __init__(self, matched=0, modified=0, inserted=None):
        self.matched_count, self.modified_count, self.inserted_ids = matched, modified, inserted or []


class MemoryCollection:
    def __init__(self, name: str):
        self.name, self._docs = name, []

    def find(self, flt: dict | None = None, projection=None, sort=None):
        docs = [d for d in self._docs if _match(d, flt or {})]
        cur = Cursor(docs)
        return cur.sort(sort) if sort else cur

    def find_one(self, flt: dict | None = None, projection=None, sort=None):
        for d in self.find(flt, sort=sort):
            return d
        return None

    def count_documents(self, flt: dict | None = None) -> int:
        return len(self.find(flt))

    def insert_one(self, doc: dict):
        doc = copy.deepcopy(doc)
        if "_id" in doc and any(d.get("_id") == doc["_id"] for d in self._docs):
            raise ValueError(f"duplicate _id {doc['_id']} in {self.name}")
        doc.setdefault("_id", f"{self.name}:{len(self._docs) + 1}")
        self._docs.append(doc)
        return _Result(inserted=[doc["_id"]])

    def insert_many(self, docs: Iterable[dict]):
        return _Result(inserted=[self.insert_one(d).inserted_ids[0] for d in docs])

    def replace_one(self, flt: dict, doc: dict, upsert: bool = False):
        for i, d in enumerate(self._docs):
            if _match(d, flt):
                new = copy.deepcopy(doc)
                new.setdefault("_id", d.get("_id"))
                self._docs[i] = new
                return _Result(1, 1)
        if upsert:
            new = copy.deepcopy(doc)
            for k, val in flt.items():
                new.setdefault(k, val)
            self.insert_one(new)
        return _Result(0, 0)

    def _apply(self, d: dict, update: dict) -> None:
        for k, val in (update.get("$set") or {}).items():
            _set(d, k, copy.deepcopy(val))
        for k, val in (update.get("$inc") or {}).items():
            _set(d, k, (_get(d, k) or 0) + val)
        for k, val in (update.get("$push") or {}).items():
            lst = _get(d, k) or []
            lst.append(copy.deepcopy(val))
            _set(d, k, lst)

    def update_one(self, flt: dict, update: dict, upsert: bool = False):
        for d in self._docs:
            if _match(d, flt):
                self._apply(d, update)
                return _Result(1, 1)
        if upsert:
            new = {k: v for k, v in flt.items() if not str(k).startswith("$")}
            self._apply(new, update)
            self.insert_one(new)
        return _Result(0, 0)

    def update_many(self, flt: dict, update: dict):
        n = 0
        for d in self._docs:
            if _match(d, flt):
                self._apply(d, update)
                n += 1
        return _Result(n, n)

    def delete_many(self, flt: dict):
        before = len(self._docs)
        self._docs = [d for d in self._docs if not _match(d, flt)]
        return _Result(before - len(self._docs))

    def aggregate(self, pipeline: list[dict]):
        raise NotImplementedError("MemoryCollection has no aggregation; pass db=None so checks use their pure-Python path")


class MemoryStore:
    def __init__(self):
        self._cols: dict[str, MemoryCollection] = {}

    def col(self, name: str) -> MemoryCollection:
        return self._cols.setdefault(name, MemoryCollection(name))

    def snapshot(self) -> dict[str, int]:
        return {k: len(v._docs) for k, v in self._cols.items()}
