"""Scraping orchestration: fetch -> parse -> dedupe -> detail pages -> paginate."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from core.config import SiteConfig
from core.fetcher import BrowserUnavailableError, FetchError, FetcherProtocol, FetchResult
from core.paginator import Paginator, build_paginator
from core.parser import (
    SelectorError,
    body_text_length,
    extract_fields,
    extract_items,
    item_key,
    parse_html,
)
from core.writer import ScrapeResult, build_result, utc_now_iso

logger = logging.getLogger(__name__)

RenderMode = Literal["static", "browser", "unknown"]


@dataclass
class RunOptions:
    """Effective run settings after merging CLI flags over the YAML config."""

    start_urls: list[str]
    config_path: str
    max_pages: int | None = None
    render_js: bool | Literal["auto"] = "auto"
    scrape_details: bool = True


@dataclass
class RunStats:
    """Counters accumulated during a run."""

    pages_scraped: int = 0
    duplicates: int = 0
    dropped: int = 0
    details_fetched: int = 0


@dataclass
class _ListingPage:
    result: FetchResult
    doc: Any
    items: list[dict[str, Any]]


@dataclass
class ScraperEngine:
    """Runs a scrape described by a :class:`SiteConfig`.

    The engine never raises for per-page problems: failures are logged and
    recorded in ``errors`` so a partial result can always be written.
    ``KeyboardInterrupt`` is caught and turned into a partial result.
    """

    config: SiteConfig
    options: RunOptions
    fetcher: FetcherProtocol
    items: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    stats: RunStats = field(default_factory=RunStats)
    interrupted: bool = False
    robots_blocked: bool = False

    def __post_init__(self) -> None:
        self.paginator: Paginator = build_paginator(self.config.pagination)
        self._seen_keys: set[str] = set()
        self._visited: set[str] = set()
        self._current_url: str = ""
        if self.options.render_js is True or self.paginator.scroll is not None:
            self._mode: RenderMode = "browser"
        elif self.options.render_js is False:
            self._mode = "static"
        else:
            self._mode = "unknown"

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def run(self) -> ScrapeResult:
        """Execute the scrape and return a validated (possibly partial) result."""
        started_at = utc_now_iso()
        logger.info(
            "Starting scrape '%s': %d start URL(s), max_pages=%s, render_js=%s, pagination=%s",
            self.config.name, len(self.options.start_urls), self.options.max_pages,
            self.options.render_js, self.config.pagination.type,
        )
        try:
            for start_url in self.options.start_urls:
                if self._page_limit_reached():
                    break
                self._crawl(start_url)
        except KeyboardInterrupt:
            self.interrupted = True
            logger.warning("Interrupted by user; saving partial results (%d items)", len(self.items))
            self._record_error(self._current_url, "Run interrupted by user (KeyboardInterrupt); results are partial")
        finally:
            try:
                self.fetcher.close()
            except Exception as exc:  # pragma: no cover - cleanup must never mask results
                logger.debug("Error while closing fetcher: %s", exc)

        logger.info(
            "Finished: %d page(s), %d item(s), %d duplicate(s) skipped, %d dropped, %d detail page(s), %d error(s)",
            self.stats.pages_scraped, len(self.items), self.stats.duplicates,
            self.stats.dropped, self.stats.details_fetched, len(self.errors),
        )
        return build_result(
            source_url=self.options.start_urls[0] if self.options.start_urls else "",
            scraped_at=started_at,
            pages_scraped=self.stats.pages_scraped,
            items=self.items,
            errors=self.errors,
            config_used=self.options.config_path,
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _record_error(self, url: str, message: str) -> None:
        self.errors.append({"url": url, "error": message, "timestamp": utc_now_iso()})

    def _page_limit_reached(self) -> bool:
        limit = self.options.max_pages
        return limit is not None and self.stats.pages_scraped >= limit

    def _check_robots(self, url: str) -> bool:
        if self.fetcher.is_allowed(url):
            return True
        self.robots_blocked = True
        message = "Disallowed by robots.txt (use --ignore-robots to override if you have permission)"
        logger.warning("%s: %s", message, url)
        self._record_error(url, message)
        return False

    def _crawl(self, start_url: str) -> None:
        url: str | None = start_url
        page_index = 0
        while url:
            if self._page_limit_reached():
                logger.info("Reached max_pages=%s; stopping", self.options.max_pages)
                return
            if url in self._visited:
                logger.warning("Already visited %s; stopping to avoid a pagination loop", url)
                return
            self._visited.add(url)
            self._current_url = url
            if not self._check_robots(url):
                return

            page = self._fetch_listing(url, page_index)
            if page is None:
                return
            self._visited.add(page.result.url)
            self.stats.pages_scraped += 1

            new_items = self._add_items(page.items)
            logger.info(
                "Page %d (%s): %d item(s) found, %d new",
                self.stats.pages_scraped, page.result.url, len(page.items), len(new_items),
            )
            if self.config.detail and self.options.scrape_details:
                self._scrape_details(new_items)

            try:
                url = self.paginator.next_url(page.result.url, page.doc, page_index, len(new_items))
            except SelectorError as exc:
                self._record_error(page.result.url, f"Pagination selector error: {exc}")
                return
            page_index += 1

    def _fetch_listing(self, url: str, page_index: int) -> _ListingPage | None:
        """Fetch and parse one listing page, auto-switching to Playwright if needed."""
        use_browser = self._mode == "browser"
        try:
            page = self._load_listing(url, render=use_browser)
            if not page.items and self._mode == "unknown":
                text_len = body_text_length(page.doc)
                logger.info(
                    "Static fetch found no items (body text %d chars); retrying with Playwright (auto-detect)",
                    text_len,
                )
                try:
                    rendered = self._load_listing(url, render=True)
                except BrowserUnavailableError as exc:
                    logger.warning("JS auto-detect fallback unavailable: %s", exc)
                    self._record_error(url, f"JS rendering fallback unavailable: {exc}")
                    self._mode = "static"
                    return page
                if rendered.items:
                    logger.info("Playwright found %d item(s); using browser rendering for this run", len(rendered.items))
                    self._mode = "browser"
                    return rendered
                logger.warning("No items found with either static or rendered fetch on %s; check item.container", url)
                return rendered
            if page.items and self._mode == "unknown":
                self._mode = "static"
                logger.debug("Static fetch works; staying in static mode")
            return page
        except FetchError as exc:
            if exc.status == 404 and page_index > 0 and self.paginator.treat_404_as_end:
                logger.info("HTTP 404 at %s; treating as end of pagination", url)
                return None
            self._record_error(url, str(exc))
            return None
        except SelectorError as exc:
            self._record_error(url, f"Selector error: {exc}")
            return None

    def _load_listing(self, url: str, *, render: bool) -> _ListingPage:
        result = self.fetcher.fetch(url, render=render, scroll=self.paginator.scroll if render else None)
        doc = parse_html(result.text, result.url)
        items, dropped = extract_items(doc, self.config.item)
        if dropped:
            self.stats.dropped += dropped
            logger.info("Dropped %d item(s) on %s for missing required fields", dropped, result.url)
        return _ListingPage(result=result, doc=doc, items=items)

    def _add_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        new: list[dict[str, Any]] = []
        for item in items:
            key = item_key(item, self.config.dedupe_on)
            if key in self._seen_keys:
                self.stats.duplicates += 1
                logger.debug("Duplicate item skipped: %s", key)
                continue
            self._seen_keys.add(key)
            self.items.append(item)
            new.append(item)
        return new

    def _scrape_details(self, items: list[dict[str, Any]]) -> None:
        detail = self.config.detail
        assert detail is not None
        for item in items:
            if detail.max_items is not None and self.stats.details_fetched >= detail.max_items:
                return
            link = item.get(detail.link_field)
            if isinstance(link, list):
                link = link[0] if link else None
            if not link or not isinstance(link, str) or not link.startswith(("http://", "https://")):
                logger.debug("Item has no usable detail link in '%s': %r", detail.link_field, link)
                continue
            self._current_url = link
            if not self._check_robots(link):
                continue
            try:
                result = self.fetcher.fetch(link, render=self._mode == "browser")
                doc = parse_html(result.text, result.url)
                extra = extract_fields(doc, detail.fields, result.url)
            except (FetchError, SelectorError) as exc:
                self._record_error(link, f"Detail page failed: {exc}")
                continue
            self.stats.details_fetched += 1
            item.update(extra)
