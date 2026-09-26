"""Predicate AST evaluation. The only place predicates are evaluated. No LLM here, ever.

AST: {"op": "and|or|not|eq|ne|lt|lte|gt|gte|in|exists", "args": [...]}
Field paths are dotted strings resolved against a context dict, e.g. "invoice.total_minor".
Literals are numbers, booleans, strings that are not paths, or lists.
"""
from __future__ import annotations
from typing import Any

OPS = {"and", "or", "not", "eq", "ne", "lt", "lte", "gt", "gte", "in", "exists"}

def resolve(path: str, ctx: dict) -> Any:
    cur: Any = ctx
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur

def has(path: str, ctx: dict) -> bool:
    cur: Any = ctx
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True

def _val(arg: Any, ctx: dict) -> Any:
    if isinstance(arg, dict) and "op" in arg:
        return evaluate(arg, ctx)
    if isinstance(arg, str):
        if has(arg, ctx):
            return resolve(arg, ctx)
        if "." in arg:
            return None  # a dotted string is a field path; absent path reads as null, never as a literal
    return arg

def evaluate(pred: dict | None, ctx: dict) -> bool:
    if pred is None:
        return True
    op, args = pred["op"], pred.get("args", [])
    if op not in OPS:
        raise ValueError(f"unknown op {op}")
    if op == "and":
        return all(evaluate(a, ctx) for a in args)
    if op == "or":
        return any(evaluate(a, ctx) for a in args)
    if op == "not":
        return not evaluate(args[0], ctx)
    if op == "exists":
        return has(args[0], ctx)
    a = _val(args[0], ctx)
    b = _val(args[1], ctx)
    if op == "eq":
        return a == b
    if op == "ne":
        return a != b
    if op == "in":
        return a in (b or [])
    if a is None or b is None:
        return False
    try:
        return {"lt": a < b, "lte": a <= b, "gt": a > b, "gte": a >= b}[op]
    except TypeError:  # e.g. comparing a string to a number: not ordered, so not satisfied
        return False

def fields_referenced(pred: dict | None) -> set[str]:
    """All field paths a predicate reads. Used by PRE-01..03 to see what a receiver needs."""
    out: set[str] = set()
    if not pred:
        return out
    op, args = pred["op"], pred.get("args", [])
    if op in {"and", "or", "not"}:
        for a in args:
            out |= fields_referenced(a)
    elif op == "exists":
        out.add(args[0])
    else:
        for a in args[:1] if op == "in" else args:
            if isinstance(a, dict) and "op" in a:
                out |= fields_referenced(a)
            elif isinstance(a, str) and "." in a:
                out.add(a)
    return out

def to_text(pred: dict | None) -> str:
    """Human-readable form for questions and certificates."""
    if not pred:
        return "true"
    op, args = pred["op"], pred.get("args", [])
    if op in {"and", "or"}:
        return "(" + f" {op.upper()} ".join(to_text(a) for a in args) + ")"
    if op == "not":
        return f"NOT {to_text(args[0])}"
    if op == "exists":
        return f"exists({args[0]})"
    sym = {"eq": "==", "ne": "!=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">=", "in": "in"}[op]
    return f"{args[0]} {sym} {args[1]!r}"
