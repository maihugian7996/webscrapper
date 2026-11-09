"""End-to-end engine tests on saved HTML fixtures via a fake fetcher."""

from __future__ import annotations

from typing import Any

from core.config import SiteConfig, parse_config
from core.engine import RunOptions, ScraperEngine
from tests.conftest import ALPHA, BETA, GAMMA, PAGE1, PAGE2, PAGE3, FakeFetcher, load_fixture


def make_engine(config: SiteConfig, fetcher: FakeFetcher, **opts: Any) -> ScraperEngine:
    options = RunOptions(
        start_urls=opts.pop("start_urls", list(config.start_urls)),
        config_path="configs/shop.yaml",
        max_pages=opts.pop("max_pages", config.max_pages),
        render_js=opts.pop("render_js", config.render_js),
        scrape_details=opts.pop("scrape_details", True),
    )
    return ScraperEngine(config=config, options=options, fetcher=fetcher)


def test_next_link_run_with_dedupe_and_details(shop_config: SiteConfig, shop_pages: dict[str, str]) -> None:
    fetcher = FakeFetcher(shop_pages)
    engine = make_engine(shop_config, fetcher)
    result = engine.run()

    assert result.metadata.pages_scraped == 2
    assert result.metadata.total_items == 3
    assert result.metadata.source_url == PAGE1
    assert result.metadata.config_used == "configs/shop.yaml"
    assert [i["title"] for i in result.items] == ["Alpha Widget", "Béta Gadget — Ünïcode ✓", "Gamma Gizmo"]
    assert engine.stats.duplicates == 1
    assert engine.stats.dropped == 1

    alpha, beta, gamma = result.items
    assert alpha["description"] == "A sturdy alpha widget."
    assert alpha["upc"] == "ABC123" and alpha["available"] == 22
    assert beta["upc"] == "XYZ789" and beta["available"] == 0
    assert "upc" not in gamma  # its detail page 404s; item kept without detail fields

    assert len(result.errors) == 1
    assert result.errors[0].url == GAMMA and "404" in result.errors[0].error
    assert fetcher.closed is True
    assert all(render is False for _, render, _ in fetcher.calls)


def test_max_pages_limits_crawl(shop_config: SiteConfig, shop_pages: dict[str, str]) -> None:
    fetcher = FakeFetcher(shop_pages)
    result = make_engine(shop_config, fetcher, max_pages=1).run()
    assert result.metadata.pages_scraped == 1
    assert PAGE2 not in [c[0] for c in fetcher.calls]


def test_no_detail_option(shop_config: SiteConfig, shop_pages: dict[str, str]) -> None:
    fetcher = FakeFetcher(shop_pages)
    result = make_engine(shop_config, fetcher, scrape_details=False).run()
    assert [c[0] for c in fetcher.calls] == [PAGE1, PAGE2]
    assert "upc" not in result.items[0] and result.errors == []


def test_detail_max_items(shop_config_dict: dict[str, Any], shop_pages: dict[str, str]) -> None:
    shop_config_dict["detail"]["max_items"] = 1
    fetcher = FakeFetcher(shop_pages)
    result = make_engine(parse_config(shop_config_dict), fetcher).run()
    assert [c[0] for c in fetcher.calls].count(ALPHA) == 1
    assert BETA not in [c[0] for c in fetcher.calls]
    assert result.items[0]["upc"] == "ABC123"


def test_url_pattern_pagination_ends_on_404(shop_config_dict: dict[str, Any], shop_pages: dict[str, str]) -> None:
    shop_config_dict["pagination"] = {"type": "url_pattern", "pattern": "https://shop.test/catalog/page{page}.html"}
    shop_config_dict.pop("detail")
    fetcher = FakeFetcher(shop_pages)
    result = make_engine(parse_config(shop_config_dict), fetcher).run()
    assert [c[0] for c in fetcher.calls] == [PAGE1, PAGE2, PAGE3]
    assert result.metadata.pages_scraped == 2
    assert result.metadata.total_items == 3
    assert result.errors == []  # 404 past the last page is the normal end, not an error


def test_first_page_failure_is_recorded(shop_config: SiteConfig) -> None:
    result = make_engine(shop_config, FakeFetcher({})).run()
    assert result.metadata.pages_scraped == 0 and result.items == []
    assert result.errors[0].url == PAGE1 and "404" in result.errors[0].error


def test_js_auto_detect_switches_to_browser(shop_config: SiteConfig, shop_pages: dict[str, str]) -> None:
    static = {PAGE1: load_fixture("js_shell.html"), PAGE2: load_fixture("js_shell.html")}
    fetcher = FakeFetcher(static, rendered=shop_pages)
    engine = make_engine(shop_config, fetcher, scrape_details=False)
    result = engine.run()
    assert result.metadata.total_items == 3
    assert fetcher.calls[0][:2] == (PAGE1, False)
    assert fetcher.calls[1][:2] == (PAGE1, True)
    assert fetcher.calls[2][:2] == (PAGE2, True)  # stays in browser mode afterwards


def test_js_auto_detect_without_browser_records_error(shop_config: SiteConfig) -> None:
    fetcher = FakeFetcher({PAGE1: load_fixture("js_shell.html")}, browser_available=False)
    result = make_engine(shop_config, fetcher).run()
    assert result.items == []
    assert any("JS rendering fallback unavailable" in e.error for e in result.errors)


def test_static_mode_never_renders(shop_config: SiteConfig) -> None:
    fetcher = FakeFetcher({PAGE1: load_fixture("js_shell.html")})
    make_engine(shop_config, fetcher, render_js=False).run()
    assert all(render is False for _, render, _ in fetcher.calls)


def test_render_js_true_always_renders(shop_config: SiteConfig, shop_pages: dict[str, str]) -> None:
    fetcher = FakeFetcher({}, rendered=shop_pages)
    result = make_engine(shop_config, fetcher, render_js=True).run()
    assert result.metadata.total_items == 3
    assert all(render is True for _, render, _ in fetcher.calls)


def test_infinite_scroll_uses_browser_with_scroll(shop_config_dict: dict[str, Any], shop_pages: dict[str, str]) -> None:
    shop_config_dict["pagination"] = {"type": "infinite_scroll", "scroll_pause": 0.1, "max_scrolls": 3}
    shop_config_dict.pop("detail")
    fetcher = FakeFetcher({}, rendered=shop_pages)
    result = make_engine(parse_config(shop_config_dict), fetcher).run()
    assert len(fetcher.calls) == 1
    url, render, scroll = fetcher.calls[0]
    assert url == PAGE1 and render is True and scroll is not None and scroll.max_scrolls == 3
    assert result.metadata.total_items == 2


def test_robots_blocked_start_url(shop_config: SiteConfig, shop_pages: dict[str, str]) -> None:
    fetcher = FakeFetcher(shop_pages, disallowed={PAGE1})
    engine = make_engine(shop_config, fetcher)
    result = engine.run()
    assert engine.robots_blocked is True
    assert result.metadata.pages_scraped == 0 and fetcher.calls == []
    assert "robots.txt" in result.errors[0].error


def test_robots_blocked_detail_page_skipped(shop_config: SiteConfig, shop_pages: dict[str, str]) -> None:
    fetcher = FakeFetcher(shop_pages, disallowed={ALPHA})
    result = make_engine(shop_config, fetcher).run()
    assert ALPHA not in [c[0] for c in fetcher.calls]
    assert "upc" not in result.items[0] and result.items[1]["upc"] == "XYZ789"


def test_keyboard_interrupt_saves_partial(shop_config: SiteConfig, shop_pages: dict[str, str]) -> None:
    pages = {**shop_pages, PAGE2: KeyboardInterrupt()}
    fetcher = FakeFetcher(pages)
    engine = make_engine(shop_config, fetcher)
    result = engine.run()
    assert engine.interrupted is True
    assert result.metadata.pages_scraped == 1
    assert [i["title"] for i in result.items] == ["Alpha Widget", "Béta Gadget — Ünïcode ✓"]
    assert result.errors[-1].url == PAGE2 and "interrupted" in result.errors[-1].error
    assert fetcher.closed is True


def test_pagination_loop_guard(shop_config: SiteConfig) -> None:
    loop = load_fixture("listing_page1.html").replace('href="page2.html"', 'href="page1.html"')
    fetcher = FakeFetcher({PAGE1: loop})
    result = make_engine(shop_config, fetcher, scrape_details=False).run()
    assert [c[0] for c in fetcher.calls] == [PAGE1]
    assert result.metadata.pages_scraped == 1


def test_multiple_start_urls_share_dedupe(shop_config: SiteConfig, shop_pages: dict[str, str]) -> None:
    fetcher = FakeFetcher(shop_pages)
    result = make_engine(shop_config, fetcher, start_urls=[PAGE2, PAGE1], scrape_details=False).run()
    # PAGE2 first (gamma, alpha), then PAGE1 (alpha dup, beta) -> PAGE2 already visited
    assert [i["title"] for i in result.items] == ["Gamma Gizmo", "Alpha Widget", "Béta Gadget — Ünïcode ✓"]
    assert result.metadata.pages_scraped == 2
