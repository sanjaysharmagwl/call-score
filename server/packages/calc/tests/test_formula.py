import pytest

from callscore_calc import SpecError, col, lit, parse_formula, to_formula
from callscore_calc.spec.expr import Binary, ColRef, Compare, InList, IsNull, Lit, Logic


@pytest.mark.parametrize(
    "text",
    [
        "price * qty * (1 + bump)",
        "region == 'EMEA' and qty > 0",
        "-1.5 * x ** 2 ** 3",
        "a if b > 1 else None",
        "sector in ('Tech', 'Fin')",
        "sector not in ('Tech',)",
        "x is not None",
        "col('Market Value') / 1000.0",
        "round(price, 2)",
        "decimal(p, 4) + int(q)",
        "date('2026-01-31') <= d",
        "not (a or b) and c",
        "contains(lower(name), 'bank')",
        "coalesce(a, b, 0)",
    ],
)
def test_round_trip(text: str) -> None:
    tree = parse_formula(text)
    assert parse_formula(to_formula(tree)) == tree


def test_numbers_are_exact_decimals() -> None:
    tree = parse_formula("price * 1.05")
    assert isinstance(tree, Binary)
    assert tree.right == Lit(type="num", value="1.05")
    assert parse_formula("1_000.50") == Lit(type="num", value="1000.5")
    assert parse_formula("-2") == Lit(type="int", value=-2)


def test_chained_comparison_expands() -> None:
    tree = parse_formula("0 < x <= 1")
    assert isinstance(tree, Logic)
    assert [type(a) for a in tree.args] == [Compare, Compare]


def test_keywords_can_be_column_names() -> None:
    assert parse_formula("yield") == ColRef(name="yield")
    expected = Binary(op="mul", left=ColRef(name="yield"), right=Lit(type="int", value=2))
    assert parse_formula("col('yield') * 2") == expected
    assert parse_formula("yield * 2") == expected
    tree = parse_formula("return + kw0_return")  # aliases never collide with real names
    assert tree == Binary(op="add", left=ColRef(name="return"), right=ColRef(name="kw0_return"))
    assert to_formula(tree) == "col('return') + kw0_return"


def test_membership_and_null_checks() -> None:
    assert isinstance(parse_formula("x in (1, 2)"), InList)
    assert parse_formula("x is None") == IsNull(arg=ColRef(name="x"))


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("x.y", "Attribute"),
        ("x[0]", "Subscript"),
        ("lambda: 1", "Lambda"),
        ("__import__('os')", "unknown function"),
        ("Open('f')", "unknown function"),
        ("x // 2", "FloorDiv"),
        ("x % 2", "Mod"),
        ("x == None", "is None"),
        ("x in y", "literal list"),
        ("round(x, n=2)", "keyword"),
        ("min(1)", "min() takes"),
        ("__secret", "reserved"),
        ("1 +", "syntax"),
        ("", "empty"),
        ("a in (1,) < 2", "chained"),
    ],
)
def test_rejects_unsafe_or_invalid(text: str, fragment: str) -> None:
    with pytest.raises(SpecError) as info:
        parse_formula(text)
    assert fragment in info.value.message


def test_limits() -> None:
    with pytest.raises(SpecError, match="longer than"):
        parse_formula("a + " * 2000 + "a")
    assert parse_formula("(" * 50 + "a" + ")" * 50) == ColRef(name="a")  # parentheses are free
    with pytest.raises(SpecError) as info:
        parse_formula("-(" * 100 + "a" + ")" * 100)
    assert info.value.code == "formula_too_complex"


def test_builders() -> None:
    assert lit(0.1) == Lit(type="num", value="0.1")
    assert lit(None) == Lit(type="null")
    assert col("a") == ColRef(name="a")
    assert to_formula(Lit(type="num", value="1000")) == "1000.0"
