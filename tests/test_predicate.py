"""Every op in pmp/predicate.py has at least one true and one false case, plus
fields_referenced on nested and/or, resolve/has, to_text and error handling."""
import pytest

from pmp.predicate import OPS, evaluate, fields_referenced, has, resolve, to_text

CTX = {
    "invoice": {"total": 2780, "status": "paid", "currency": "USD", "items": ["a", "b"]},
    "approval": {"granted": True},
    "plan": {"status": "authorized", "loanTransactionId": None},
}


def P(op, *args):
    return {"op": op, "args": list(args)}


# ---- one true and one false case per op -------------------------------------

CASES = [
    # eq
    (P("eq", "invoice.status", "paid"), True),
    (P("eq", "invoice.status", "open"), False),
    # ne
    (P("ne", "invoice.currency", "EUR"), True),
    (P("ne", "invoice.currency", "USD"), False),
    # lt
    (P("lt", "invoice.total", 10000), True),
    (P("lt", "invoice.total", 1000), False),
    # lte
    (P("lte", "invoice.total", 2780), True),
    (P("lte", "invoice.total", 2779), False),
    # gt
    (P("gt", "invoice.total", 1000), True),
    (P("gt", "invoice.total", 2780), False),
    # gte
    (P("gte", "invoice.total", 2780), True),
    (P("gte", "invoice.total", 2781), False),
    # in
    (P("in", "invoice.status", ["paid", "void"]), True),
    (P("in", "invoice.status", ["open", "draft"]), False),
    # exists
    (P("exists", "approval.granted"), True),
    (P("exists", "approval.reviewer"), False),
    # not
    (P("not", P("eq", "invoice.status", "open")), True),
    (P("not", P("eq", "invoice.status", "paid")), False),
    # and
    (P("and", P("eq", "invoice.status", "paid"), P("lt", "invoice.total", 10000)), True),
    (P("and", P("eq", "invoice.status", "paid"), P("gt", "invoice.total", 10000)), False),
    # or
    (P("or", P("gt", "invoice.total", 10000), P("eq", "approval.granted", True)), True),
    (P("or", P("gt", "invoice.total", 10000), P("eq", "approval.granted", False)), False),
]


@pytest.mark.parametrize("pred,expected", CASES, ids=[f"{c[0]['op']}-{c[1]}" for c in CASES])
def test_each_op_true_and_false(pred, expected):
    assert evaluate(pred, CTX) is expected


def test_every_op_is_covered_by_cases():
    covered = {c[0]["op"] for c in CASES}
    assert covered == OPS


# ---- edge cases ----------------------------------------------------------------

def test_none_predicate_is_true():
    assert evaluate(None, CTX) is True


def test_unknown_op_raises():
    with pytest.raises(ValueError):
        evaluate({"op": "xor", "args": []}, CTX)


def test_comparison_against_missing_or_null_field_is_false():
    assert evaluate(P("lt", "invoice.missing", 5), CTX) is False
    assert evaluate(P("gt", "plan.loanTransactionId", 0), CTX) is False


def test_eq_missing_field_compares_to_none():
    assert evaluate(P("eq", "invoice.missing", None), CTX) is True


def test_both_sides_can_be_paths():
    ctx = {"plan": {"totalLoanAmount": 2780}, "invoice": {"total": 2780}}
    assert evaluate(P("eq", "plan.totalLoanAmount", "invoice.total"), ctx) is True
    ctx["invoice"]["total"] = 2781
    assert evaluate(P("eq", "plan.totalLoanAmount", "invoice.total"), ctx) is False


def test_string_literal_that_looks_like_a_path_but_is_absent_stays_literal():
    assert evaluate(P("eq", "invoice.status", "not.a.path"), CTX) is False


def test_in_with_null_list_is_false():
    assert evaluate(P("in", "invoice.status", None), CTX) is False


# ---- resolve / has -------------------------------------------------------------

def test_resolve_and_has():
    assert resolve("invoice.total", CTX) == 2780
    assert resolve("invoice.nope", CTX) is None
    assert resolve("invoice.total.deeper", CTX) is None
    assert has("plan.loanTransactionId", CTX) is True   # present even though null
    assert has("plan.nope", CTX) is False


# ---- fields_referenced ---------------------------------------------------------

def test_fields_referenced_nested_and_or():
    pred = P(
        "and",
        P("eq", "plan.status", "authorized"),
        P("or",
          P("lt", "invoice.total", 10000),
          P("eq", "approval.granted", True),
          P("not", P("exists", "lender.declined"))),
        P("in", "invoice.currency", ["USD", "EUR"]),
    )
    assert fields_referenced(pred) == {
        "plan.status", "invoice.total", "approval.granted", "lender.declined", "invoice.currency",
    }


def test_fields_referenced_ignores_literals_and_empty():
    assert fields_referenced(None) == set()
    assert fields_referenced(P("eq", "invoice.status", "paid")) == {"invoice.status"}
    # a bare word without a dot is treated as a literal, not a field path
    assert fields_referenced(P("eq", "status", "paid")) == set()
    # both sides may be paths
    assert fields_referenced(P("eq", "plan.totalLoanAmount", "invoice.total")) == {
        "plan.totalLoanAmount", "invoice.total",
    }


# ---- to_text -------------------------------------------------------------------

def test_to_text():
    assert to_text(None) == "true"
    assert to_text(P("eq", "plan.status", "authorized")) == "plan.status == 'authorized'"
    assert to_text(P("and", P("lt", "invoice.total", 10000), P("exists", "approval.granted"))) == \
        "(invoice.total < 10000 AND exists(approval.granted))"
    assert to_text(P("not", P("in", "invoice.status", ["paid"]))) == "NOT invoice.status in ['paid']"
