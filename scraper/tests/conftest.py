"""Shared test fixtures. No test touches the live network."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from core.config import SiteConfig, parse_config
from core.fetcher import BrowserUnavailableError, FetchError, FetchResult
from core.paginator import ScrollSpec

FIXTURES = Path(__file__).parent / "fixtures"

BASE = "https://shop.test"
PAGE1 = f"{BASE}/catalog/page1.html"
PAGE2 = f"{BASE}/catalog/page2.html"
PAGE3 = f"{BASE}/catalog/page3.html"
ALPHA = f"{BASE}/products/alpha.html"
BETA = f"{BASE}/products/beta.html"
GAMMA = f"{BASE}/products/gamma.html"


def load_fixture(name: str) -> str:
    """Read an HTML fixture as UTF-8 text."""
    return (FIXTURES / name).read_text(encoding="utf-8")


SHOP_CONFIG: dict[str, Any] = {
    "name": "synthetic_shop",
    "start_urls": [PAGE1],
    "render_js": "auto",
    "item": {
        "container": "div.product",
        "fields": {
            "title": {"selector": "h2.title a", "attr": "text", "required": True},
            "url": {"selector": "h2.title a", "attr": "href"},
            "price": {
                "selector": "xpath:.//span[@class='price']",
                "clean": [{"regex": r"([\d.,]+)"}, {"type": "float"}],
            },
            "in_stock": {
                "selector": "p.stock",
                "clean": ["lower", {"map": {"in stock": True, "out of stock": False}}],
            },
            "image": {"selector": "img.thumb", "attr": "src"},
            "rating": {
                "selector": "span.rating",
                "attr": "class",
                "clean": [{"regex": r"star-(\w+)"}, {"map": {"One": 1, "Three": 3, "Five": 5}}],
            },
            "tags": {"selector": "ul.tags li", "multiple": True, "clean": ["lower"]},
        },
    },
    "dedupe_on": ["url"],
    "pagination": {"type": "next_link", "next_selector": "li.next a"},
    "max_pages": 10,
    "detail": {
        "link_field": "url",
        "fields": {
            "description": "#description p",
            "upc": "xpath://th[text()='UPC']/following-sibling::td",
            "available": {
                "selector": "xpath://th[text()='Availability']/following-sibling::td",
                "clean": [{"regex": r"\((\d+) available\)"}, {"type": "int"}],
            },
        },
    },
    "request": {"delay": 0, "jitter": 0, "retries": 2},
}


@pytest.fixture
def shop_config_dict() -> dict[str, Any]:
    """A deep copy of the synthetic shop config (safe to mutate)."""
    return copy.deepcopy(SHOP_CONFIG)


@pytest.fixture
def shop_config(shop_config_dict: dict[str, Any]) -> SiteConfig:
    """Validated synthetic shop config."""
    return parse_config(shop_config_dict)


@pytest.fixture
def shop_pages() -> dict[str, str]:
    """URL -> HTML for the synthetic shop (gamma's detail page is intentionally missing)."""
    return {
        PAGE1: load_fixture("listing_page1.html"),
        PAGE2: load_fixture("listing_page2.html"),
        ALPHA: load_fixture("detail_alpha.html"),
        BETA: load_fixture("detail_beta.html"),
    }


@pytest.fixture
def config_file(tmp_path: Path, shop_config_dict: dict[str, Any]) -> Path:
    """The synthetic shop config written to a YAML file."""
    path = tmp_path / "shop.yaml"
    path.write_text(yaml.safe_dump(shop_config_dict, allow_unicode=True), encoding="utf-8")
    return path


class FakeFetcher:
    """In-memory stand-in for :class:`core.fetcher.Fetcher`.

    Args:
        pages: URL -> HTML (or an exception to raise) for static fetches.
        rendered: URL -> HTML for ``render=True`` fetches (defaults to ``pages``).
        disallowed: URLs that robots.txt "disallows".
        browser_available: If False, rendered fetches raise BrowserUnavailableError.
    """

    def __init__(
        self,
        pages: dict[str, Any],
        rendered: dict[str, Any] | None = None,
        disallowed: set[str] | None = None,
        browser_available: bool = True,
    ) -> None:
        self.pages = pages
        self.rendered = rendered if rendered is not None else pages
        self.disallowed = disallowed or set()
        self.browser_available = browser_available
        self.calls: list[tuple[str, bool, ScrollSpec | None]] = []
        self.closed = False

    def is_allowed(self, url: str) -> bool:
        return url not in self.disallowed

    def fetch(self, url: str, *, render: bool = False, scroll: ScrollSpec | None = None) -> FetchResult:
        self.calls.append((url, render, scroll))
        if render and not self.browser_available:
            raise BrowserUnavailableError(url, "Playwright is not installed")
        value = (self.rendered if render else self.pages).get(url)
        if value is None:
            raise FetchError(url, "HTTP 404", 404)
        if isinstance(value, BaseException):
            raise value
        return FetchResult(url=url, status=200, text=value, elapsed=0.01, rendered=render)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_fetcher_cls() -> type[FakeFetcher]:
    """Expose the FakeFetcher class to tests."""
    return FakeFetcher
