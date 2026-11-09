"""Tests for core.parser (selector extraction + cleaning)."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from core.config import FieldSpec, ItemSpec, SiteConfig
from core.parser import (
    SelectorError,
    apply_clean,
    body_text_length,
    extract_field,
    extract_items,
    is_xpath,
    item_key,
    parse_html,
    select,
)
from tests.conftest import PAGE1, PAGE2, load_fixture


@pytest.fixture
def page1_doc():
    return parse_html(load_fixture("listing_page1.html"), PAGE1)


def test_extract_items_from_listing(page1_doc, shop_config: SiteConfig) -> None:
    items, dropped = extract_items(page1_doc, shop_config.item)
    assert dropped == 1  # the product without a title
    assert len(items) == 2
    alpha, beta = items
    assert alpha == {
        "title": "Alpha Widget",
        "url": "https://shop.test/products/alpha.html",
        "price": 1299.5,
        "in_stock": True,
        "image": "https://shop.test/img/alpha.jpg",
        "rating": 3,
        "tags": ["tools", "metal"],
    }
    assert beta["title"] == "Béta Gadget — Ünïcode ✓"
    assert beta["image"] == "https://cdn.shop.test/img/beta.jpg"
    assert beta["in_stock"] is False
    assert beta["rating"] == 5
    assert beta["tags"] == []


def test_relative_parent_paths_resolved(shop_config: SiteConfig) -> None:
    doc = parse_html(load_fixture("listing_page2.html"), PAGE2)
    items, _ = extract_items(doc, shop_config.item)
    assert items[0]["image"] == "https://shop.test/img/gamma.jpg"
    assert items[0]["price"] == 7.0


def test_base_tag_respected() -> None:
    doc = parse_html(load_fixture("base_tag.html"), "https://shop.test/x/y.html")
    spec = ItemSpec(
        container="div.product",
        fields={"url": FieldSpec(selector="h2 a", attr="href"), "img": FieldSpec(selector="img", attr="src")},
    )
    items, _ = extract_items(doc, spec)
    assert items == [
        {"url": "https://mirror.shop.test/root/items/delta.html", "img": "https://mirror.shop.test/root/img/delta.png"}
    ]


@pytest.mark.parametrize(
    "selector",
    ["xpath:.//h2[@class='title']/a", ".//h2[@class='title']/a", "h2.title > a"],
)
def test_css_and_xpath_equivalent(page1_doc, selector: str) -> None:
    container = select(page1_doc, "div.product")[0]
    value = extract_field(container, "t", FieldSpec(selector=selector), PAGE1)
    assert value == "Alpha Widget"


def test_xpath_attribute_result_is_resolved(page1_doc) -> None:
    container = select(page1_doc, "div.product")[0]
    value = extract_field(container, "u", FieldSpec(selector=".//h2/a/@href", attr="href"), PAGE1)
    assert value == "https://shop.test/products/alpha.html"


def test_xpath_text_node(page1_doc) -> None:
    value = extract_field(page1_doc, "t", FieldSpec(selector="//title/text()"), PAGE1)
    assert value == "Synthetic catalog – page 1"


def test_selector_none_uses_container(page1_doc) -> None:
    container = select(page1_doc, "div.product")[0]
    assert extract_field(container, "id", FieldSpec(attr="data-id"), PAGE1) == "p1"


def test_html_and_raw_text_attrs(page1_doc) -> None:
    container = select(page1_doc, "div.product")[0]
    html = extract_field(container, "h", FieldSpec(selector="ul.tags", attr="html"), PAGE1)
    assert html.startswith('<ul class="tags">') and "<li>Tools</li>" in html
    inner = extract_field(container, "h", FieldSpec(selector="ul.tags", attr="inner_html"), PAGE1)
    assert inner.startswith("<li>Tools</li>")
    raw = extract_field(container, "r", FieldSpec(selector="p.stock", attr="raw_text"), PAGE1)
    assert raw == "  In stock "


def test_missing_field_uses_default(page1_doc) -> None:
    container = select(page1_doc, "div.product")[0]
    assert extract_field(container, "x", FieldSpec(selector="span.nope", default="n/a"), PAGE1) == "n/a"
    assert extract_field(container, "x", FieldSpec(selector="span.nope"), PAGE1) is None
    assert extract_field(container, "x", FieldSpec(selector="span.nope", multiple=True), PAGE1) == []


def test_missing_attribute_is_none(page1_doc) -> None:
    container = select(page1_doc, "div.product")[0]
    assert extract_field(container, "x", FieldSpec(selector="img.thumb", attr="data-missing"), PAGE1) is None


def test_invalid_selectors_raise(page1_doc) -> None:
    with pytest.raises(SelectorError, match="CSS"):
        select(page1_doc, "div[[")
    with pytest.raises(SelectorError, match="XPath"):
        select(page1_doc, "xpath://div[")


def test_is_xpath() -> None:
    assert is_xpath("xpath:div") and is_xpath("//a") and is_xpath("./a") and is_xpath("(//a)[1]")
    assert not is_xpath("div > a") and not is_xpath("#id")


@pytest.mark.parametrize(
    ("value", "steps", "expected"),
    [
        ("  a  b ", ["strip"], "a  b"),
        ("  a \n b ", ["collapse_whitespace"], "a b"),
        ("--x--", [{"strip": "-"}], "x"),
        ("AbC", ["lower"], "abc"),
        ("AbC", ["upper"], "ABC"),
        ("hello world", ["title"], "Hello World"),
        ("Price: £1,299.50", [{"regex": r"([\d,.]+)"}, {"type": "float"}], 1299.5),
        ("SKU-42-X", [{"regex": {"pattern": r"sku-(\d+)-(\w)", "group": 2, "ignore_case": True}}], "X"),
        ("no digits", [{"regex": r"(\d+)"}, {"type": "int"}], None),
        ("1,234", [{"type": "int"}], 1234),
        ("12.0", [{"type": "int"}], 12),
        ("Yes", [{"type": "bool"}], True),
        ("off", [{"type": "bool"}], False),
        (42, [{"type": "str"}], "42"),
        ("a-b-c", [{"replace": {"from": "-", "to": "/"}}], "a/b/c"),
        ("a-b", [{"replace": {"from": "-"}}], "ab"),
        ("path", [{"prefix": "/"}, {"suffix": ".html"}], "/path.html"),
        ("Two", [{"map": {"One": 1, "Two": 2}}], 2),
        ("Six", [{"map": {"One": 1}}], "Six"),
        (None, ["strip", {"type": "int"}], None),
    ],
)
def test_cleaning_steps(value: Any, steps: list, expected: Any) -> None:
    assert apply_clean(value, steps) == expected


def test_failed_cast_logs_and_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert apply_clean("abc", [{"type": "float"}], "price") is None
    assert "price" in caplog.text and "type" in caplog.text


def test_failed_cast_falls_back_to_default(page1_doc) -> None:
    container = select(page1_doc, "div.product")[0]
    spec = FieldSpec(selector="p.stock", clean=[{"type": "int"}], default=-1)
    assert extract_field(container, "n", spec, PAGE1) == -1


def test_item_key_dedupe() -> None:
    a = {"url": "u1", "title": "A"}
    b = {"title": "B", "url": "u1"}
    assert item_key(a, ["url"]) == item_key(b, ["url"])
    assert item_key(a, None) != item_key(b, None)
    assert item_key({"x": 1, "y": 2}, None) == item_key({"y": 2, "x": 1}, None)


def test_body_text_length_ignores_scripts() -> None:
    shell = parse_html(load_fixture("js_shell.html"), PAGE1)
    assert body_text_length(shell) < 80
    full = parse_html(load_fixture("listing_page1.html"), PAGE1)
    assert body_text_length(full) > 80


def test_empty_html_is_safe(shop_config: SiteConfig) -> None:
    doc = parse_html("", PAGE1)
    assert extract_items(doc, shop_config.item) == ([], 0)
