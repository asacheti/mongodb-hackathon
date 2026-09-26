# PMP: Procedure Merge Profile
MongoDB Harness Engineering hackathon, NYC, Sep 26 2026. Everything here is built today.

## What this is
Two organizations' private procedures become one signed, executable joint procedure through a
mediator on MongoDB Atlas. Neither org sees the other's internals. Use case `bnpl_checkout_v1`:
- ORG_A `northwind` (merchant): Stripe invoicing procedure, `mock/inputs/northwind/SKILL.md`
- ORG_B `lakeside` (lender): Arazzo BNPL workflow `mock/inputs/lakeside/bnpl-arazzo.yaml`
  plus `merge-profile.yaml` sidecar (roles, sensitivity, policies, handoffs, merge preferences)

The full expected output of every stage is in `mock/STAGES.md`. Build to match it.

## Pipeline (one module per stage)
compile → align → merge → validate → decide → contract → runtime (agents + mediator)

## Rules
- Python 3.11, pymongo, fastapi, openai. No LangChain, no Streamlit, no notebooks.
- The LLM is used ONLY in `pmp/align.py`. merge, validate, decide, contract are deterministic:
  same inputs + same decision records = same output, byte for byte.
- All predicates (preconditions, postconditions, branch conditions, policy rules) are a JSON AST:
  `{"op": "and|or|not|eq|ne|lt|lte|gt|gte|in|exists", "args": [...]}`. Field paths are strings.
  Never evaluate predicates with an LLM. `pmp/predicate.py` owns evaluation.
- Every Atlas access goes through `pmp/db.py`. Collections: submissions, steps_vec, alignments,
  merged, findings, merge_questions, merge_log, contracts, projections, handoffs, rejections.
- Every document carries `use_case_id` and, where it belongs to one org, `org_id`.
- Hashes: sha256 over canonical JSON (`json.dumps(obj, sort_keys=True, separators=(",", ":"))`),
  stored as `"sha256:<hex>"`.
- Confidential fields (`confidential_notes`, `may_cross: never`, `sensitivity: secret`) never
  appear in anything written to `projections`, `merge_questions` text, or a handoff payload.
- Each module has `tests/test_<module>.py`. Run `pytest -q` before every commit.
- Commit every 20 minutes with a message that says what works now. Never commit `.env`.
- Keep module contracts stable: inputs → outputs → collection as listed in `mock/STAGES.md`.
  If a module is behind schedule, load its expected output from `mock/stages/*.json` and move on.

## Layout
```
spec/          JSON Schemas (node, transition, alignment, finding, question, decision, contract, certificate, handoff)
mock/inputs/   Stage 0 inputs (do not edit)
mock/stages/   expected outputs per stage, used by tests and as fallback
mock/STAGES.md the spec
pmp/           db.py predicate.py compile.py align.py merge.py validate.py decide.py contract.py
pmp/runtime/   agent.py a2a.py mediator.py
ui/            index.html (served by pmp/runtime/api.py at /)
scripts/       demo.sh seed.sh
tests/
```

## Environment
`.env`: `MONGODB_URI`, `OPENAI_API_KEY`, `PMP_DB=pmp`. Embeddings: `text-embedding-3-small` (1536 dims).
Vector index `steps_vec_idx` on `steps_vec.embedding`, filters `org_id`, `use_case_id`.
