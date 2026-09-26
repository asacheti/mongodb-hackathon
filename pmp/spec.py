"""JSON Schema validation over spec/. One validator per schema; $refs between spec/*.schema.json files
(e.g. node -> predicate) resolve through a registry built from every file in spec/."""
from __future__ import annotations
import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

SPEC_DIR = Path(__file__).resolve().parent.parent / "spec"


class SchemaError(ValueError):
    pass


@lru_cache(maxsize=1)
def _schemas() -> dict[str, dict]:
    return {p.name: json.loads(p.read_text()) for p in sorted(SPEC_DIR.glob("*.schema.json"))}


@lru_cache(maxsize=1)
def _registry() -> Registry:
    pairs = []
    for fname, schema in _schemas().items():
        res = Resource.from_contents(schema)
        pairs.append((fname, res))
        if schema.get("$id") and schema["$id"] != fname:
            pairs.append((schema["$id"], res))
    return Registry().with_resources(pairs)


@lru_cache(maxsize=None)
def validator(name: str) -> Draft202012Validator:
    """`name` is the schema stem: node, transition, finding, alignment, ..."""
    return Draft202012Validator(_schemas()[f"{name}.schema.json"], registry=_registry())


def errors(doc: dict, name: str) -> list[str]:
    out = []
    for e in sorted(validator(name).iter_errors(doc), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in e.path) or "<root>"
        out.append(f"{loc}: {e.message}")
    return out


def validate(doc: dict, name: str) -> None:
    errs = errors(doc, name)
    if errs:
        raise SchemaError(f"{name} {doc.get('_id', '<no _id>')} invalid:\n  " + "\n  ".join(errs))
