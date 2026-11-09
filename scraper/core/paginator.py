"""Pagination strategies.

Each strategy answers one question after a page has been parsed:
"what URL should be fetched next (if any)?"
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from lxml import etree

from core.config import PaginationSpec
from core.parser import base_url_of, select

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScrollSpec:
    """Instructions for the browser fetcher to perform infinite scrolling."""

    pause: float
    max_scrolls: int


class Paginator(ABC):
    """Base class for pagination strategies."""

    #: Scroll instructions for the browser; ``None`` if no scrolling is needed.
    scroll: ScrollSpec | None = None

    #: Whether an HTTP 404 on a follow-up page means "no more pages".
    treat_404_as_end: bool = False

    @abstractmethod
    def next_url(self, current_url: str, doc: Any, page_index: int, new_items: int) -> str | None:
        """Return the next page URL, or ``None`` to stop.

        Args:
            current_url: Final URL of the page that was just parsed.
            doc: Parsed lxml document of that page.
            page_index: 0-based index of that page within the current start URL.
            new_items: Number of *new* (non-duplicate) items found on it.
        """


class NoPaginator(Paginator):
    """Scrape only the start URL(s)."""

    def next_url(self, current_url: str, doc: Any, page_index: int, new_items: int) -> str | None:
        return None


class NextLinkPaginator(Paginator):
    """Follow a "next page" link identified by a selector."""

    def __init__(self, selector: str) -> None:
        self.selector = selector

    def next_url(self, current_url: str, doc: Any, page_index: int, new_items: int) -> str | None:
        base = base_url_of(doc, current_url)
        for node in select(doc, self.selector):
            if isinstance(node, etree._Element):
                href = node.get("href")
                if href is None:
                    # Selector may point at a wrapper (e.g. <li class="next">).
                    links = node.xpath(".//a[@href]")
                    href = links[0].get("href") if links else None
            else:
                href = str(node)
            if href and href.strip() and not href.strip().startswith(("#", "javascript:")):
                return urljoin(base, href.strip())
        logger.info("No next-page link found (selector %r); pagination finished", self.selector)
        return None


class UrlPatternPaginator(Paginator):
    """Generate page URLs from a ``{page}`` pattern.

    The start URL is treated as page ``start``; the next pages are
    ``start + step``, ``start + 2*step`` ... Stops when a page yields no new
    items or returns 404.
    """

    treat_404_as_end = True

    def __init__(self, pattern: str, start: int = 1, step: int = 1) -> None:
        self.pattern = pattern
        self.start = start
        self.step = step

    def url_for(self, page_number: int) -> str:
        """Build the URL for a specific page number."""
        return self.pattern.format(page=page_number)

    def next_url(self, current_url: str, doc: Any, page_index: int, new_items: int) -> str | None:
        if new_items == 0:
            logger.info("Page yielded no new items; URL-pattern pagination finished")
            return None
        return self.url_for(self.start + self.step * (page_index + 1))


class InfiniteScrollPaginator(Paginator):
    """All content is loaded on one page by scrolling in the browser."""

    def __init__(self, pause: float, max_scrolls: int) -> None:
        self.scroll = ScrollSpec(pause=pause, max_scrolls=max_scrolls)

    def next_url(self, current_url: str, doc: Any, page_index: int, new_items: int) -> str | None:
        return None


def build_paginator(spec: PaginationSpec) -> Paginator:
    """Create the paginator described by a :class:`PaginationSpec`."""
    if spec.type == "next_link":
        assert spec.next_selector is not None
        return NextLinkPaginator(spec.next_selector)
    if spec.type == "url_pattern":
        assert spec.pattern is not None
        return UrlPatternPaginator(spec.pattern, spec.start, spec.step)
    if spec.type == "infinite_scroll":
        return InfiniteScrollPaginator(spec.scroll_pause, spec.max_scrolls)
    return NoPaginator()
