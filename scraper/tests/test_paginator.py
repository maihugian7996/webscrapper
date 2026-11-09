"""Tests for core.paginator."""

from __future__ import annotations

from core.config import PaginationSpec
from core.paginator import (
    InfiniteScrollPaginator,
    NextLinkPaginator,
    NoPaginator,
    UrlPatternPaginator,
    build_paginator,
)
from core.parser import parse_html
from tests.conftest import PAGE1, PAGE2, load_fixture


def test_next_link_resolves_relative_href() -> None:
    doc = parse_html(load_fixture("listing_page1.html"), PAGE1)
    assert NextLinkPaginator("li.next a").next_url(PAGE1, doc, 0, 2) == PAGE2


def test_next_link_on_wrapper_element() -> None:
    doc = parse_html(load_fixture("listing_page1.html"), PAGE1)
    assert NextLinkPaginator("li.next").next_url(PAGE1, doc, 0, 2) == PAGE2


def test_next_link_xpath_attribute() -> None:
    doc = parse_html(load_fixture("listing_page1.html"), PAGE1)
    assert NextLinkPaginator("//li[@class='next']/a/@href").next_url(PAGE1, doc, 0, 2) == PAGE2


def test_next_link_absent_stops() -> None:
    doc = parse_html(load_fixture("listing_page2.html"), PAGE2)
    assert NextLinkPaginator("li.next a").next_url(PAGE2, doc, 1, 1) is None


def test_next_link_ignores_fragment_links() -> None:
    doc = parse_html('<a class="next" href="#">next</a>', PAGE1)
    assert NextLinkPaginator("a.next").next_url(PAGE1, doc, 0, 3) is None


def test_url_pattern_increments_and_stops_on_no_new_items() -> None:
    p = UrlPatternPaginator("https://shop.test/list?page={page}", start=1, step=1)
    assert p.next_url("https://shop.test/list", None, 0, 10) == "https://shop.test/list?page=2"
    assert p.next_url("https://shop.test/list?page=2", None, 1, 10) == "https://shop.test/list?page=3"
    assert p.next_url("https://shop.test/list?page=3", None, 2, 0) is None
    assert p.treat_404_as_end is True


def test_url_pattern_offset_step() -> None:
    p = UrlPatternPaginator("https://shop.test/list?offset={page}", start=0, step=20)
    assert p.next_url("x", None, 0, 20) == "https://shop.test/list?offset=20"
    assert p.next_url("x", None, 1, 20) == "https://shop.test/list?offset=40"


def test_infinite_scroll_has_scroll_spec_and_single_page() -> None:
    p = InfiniteScrollPaginator(pause=0.5, max_scrolls=7)
    assert p.scroll is not None and p.scroll.pause == 0.5 and p.scroll.max_scrolls == 7
    assert p.next_url("x", None, 0, 100) is None


def test_build_paginator_dispatch() -> None:
    assert isinstance(build_paginator(PaginationSpec()), NoPaginator)
    assert isinstance(build_paginator(PaginationSpec(type="next_link", next_selector="a")), NextLinkPaginator)
    assert isinstance(
        build_paginator(PaginationSpec(type="url_pattern", pattern="https://a/{page}")), UrlPatternPaginator
    )
    assert isinstance(build_paginator(PaginationSpec(type="infinite_scroll")), InfiniteScrollPaginator)
    assert NoPaginator().scroll is None
