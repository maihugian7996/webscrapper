"""HTTP fetching: static (requests) and JS-rendered (Playwright).

Responsible-scraping features live here:

* robots.txt checks (:class:`RobotsChecker`), including ``Crawl-delay``;
* a polite delay with random jitter between requests (:class:`RateLimiter`);
* rotating realistic desktop User-Agents (:class:`UserAgentRotator`);
* retries with exponential backoff on 429 / 5xx / timeouts / connection
  errors, honouring ``Retry-After``;
* a log line for every request attempt and failure.
"""

from __future__ import annotations

import email.utils
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4.dammit import UnicodeDammit

from core.config import RequestSpec
from core.paginator import ScrollSpec

logger = logging.getLogger(__name__)

#: Status codes that trigger a retry.
RETRY_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504, 520, 521, 522, 523, 524})

#: Upper bound on any single backoff / Retry-After sleep (seconds).
MAX_BACKOFF_SECONDS = 120.0

#: Realistic, recent desktop browser User-Agent strings.
DEFAULT_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_1) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.7; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
)

DEFAULT_HEADERS: dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}


# --------------------------------------------------------------------------- #
# Data types and errors
# --------------------------------------------------------------------------- #


@dataclass
class FetchResult:
    """Outcome of a successful fetch."""

    url: str
    """Final URL after redirects."""
    status: int
    text: str
    elapsed: float
    rendered: bool = False
    """True if the page was rendered by Playwright."""


class FetchError(Exception):
    """A request failed permanently (after retries, or with a non-retryable status)."""

    def __init__(self, url: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status = status


class BrowserUnavailableError(FetchError):
    """Playwright is not installed or its browser binaries are missing."""


class FetcherProtocol(Protocol):
    """Interface the engine depends on (lets tests inject a fake)."""

    def is_allowed(self, url: str) -> bool: ...

    def fetch(self, url: str, *, render: bool = False, scroll: ScrollSpec | None = None) -> FetchResult: ...

    def close(self) -> None: ...


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class UserAgentRotator:
    """Hand out User-Agent strings, never repeating the previous one twice in a row."""

    def __init__(self, user_agents: list[str] | tuple[str, ...] | None = None, rng: random.Random | None = None) -> None:
        self.user_agents = list(user_agents or DEFAULT_USER_AGENTS)
        if not self.user_agents:
            raise ValueError("At least one User-Agent string is required")
        self._rng = rng or random.Random()
        self._last: str | None = None

    def next(self) -> str:
        """Return the next User-Agent."""
        choices = [ua for ua in self.user_agents if ua != self._last] or self.user_agents
        self._last = self._rng.choice(choices)
        return self._last


class RateLimiter:
    """Enforce ``delay`` + random ``[0, jitter]`` seconds between requests."""

    def __init__(
        self,
        delay: float,
        jitter: float = 0.0,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self.delay = max(0.0, delay)
        self.jitter = max(0.0, jitter)
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()
        self._last: float | None = None

    def wait(self) -> float:
        """Sleep as needed before the next request. Returns seconds slept."""
        slept = 0.0
        if self._last is not None:
            target = self.delay + (self._rng.uniform(0, self.jitter) if self.jitter else 0.0)
            remaining = target - (self._clock() - self._last)
            if remaining > 0:
                logger.debug("Politeness delay: sleeping %.2fs", remaining)
                self._sleep(remaining)
                slept = remaining
        self._last = self._clock()
        return slept


def decode_body(content: bytes, content_type: str | None) -> str:
    """Decode an HTTP body, preferring the header charset, then HTML meta / detection."""
    match = re.search(r"charset=([\w\-:.]+)", content_type or "", re.IGNORECASE)
    if match:
        try:
            return content.decode(match.group(1).strip("\"'"))
        except (LookupError, UnicodeDecodeError):
            logger.debug("Header charset %r failed; falling back to detection", match.group(1))
    dammit = UnicodeDammit(content, is_html=True)
    if dammit.unicode_markup is not None:
        return dammit.unicode_markup
    return content.decode("utf-8", errors="replace")


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header (seconds or HTTP date) into seconds."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return max(0.0, dt.timestamp() - time.time())


def backoff_delay(attempt: int, base: float, rng: random.Random | None = None) -> float:
    """Exponential backoff: ``base * 2**attempt`` plus up to ``base`` of jitter."""
    rng = rng or random.Random()
    return min(MAX_BACKOFF_SECONDS, base * (2 ** attempt) + rng.uniform(0, base))


# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #


class RobotsChecker:
    """Cache and evaluate robots.txt per host (RFC 9309 semantics).

    * 2xx  -> parse the rules.
    * 4xx  -> no restrictions.
    * 5xx  -> treat the site as fully disallowed (it may be overloaded).
    * Network error -> warn and allow (the page fetch itself will surface errors).
    """

    def __init__(self, session: requests.Session, timeout: float = 10.0, user_agent: str = "*") -> None:
        self.session = session
        self.timeout = timeout
        self.user_agent = user_agent
        self._cache: dict[str, RobotFileParser | bool] = {}

    @staticmethod
    def robots_url(url: str) -> str:
        """URL of the robots.txt governing ``url``."""
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))

    def _load(self, url: str) -> RobotFileParser | bool:
        key = urlsplit(url).netloc.lower()
        if key in self._cache:
            return self._cache[key]
        robots_url = self.robots_url(url)
        result: RobotFileParser | bool
        try:
            resp = self.session.get(robots_url, timeout=self.timeout, headers={"User-Agent": DEFAULT_USER_AGENTS[0]})
            logger.info("GET %s -> %s (robots.txt)", robots_url, resp.status_code)
            if 200 <= resp.status_code < 300:
                parser = RobotFileParser(robots_url)
                parser.parse(decode_body(resp.content, resp.headers.get("Content-Type")).splitlines())
                result = parser
            elif 400 <= resp.status_code < 500:
                result = True
            else:
                logger.warning("robots.txt returned %s; treating site as disallowed", resp.status_code)
                result = False
        except requests.RequestException as exc:
            logger.warning("Could not fetch %s (%s); assuming allowed", robots_url, exc)
            result = True
        self._cache[key] = result
        return result

    def allowed(self, url: str) -> bool:
        """True if robots.txt permits fetching ``url``."""
        rules = self._load(url)
        if isinstance(rules, bool):
            return rules
        return rules.can_fetch(self.user_agent, url)

    def crawl_delay(self, url: str) -> float | None:
        """``Crawl-delay`` for ``url``'s host, if declared."""
        rules = self._load(url)
        if isinstance(rules, bool):
            return None
        delay = rules.crawl_delay(self.user_agent)
        return float(delay) if delay is not None else None


# --------------------------------------------------------------------------- #
# Static fetcher
# --------------------------------------------------------------------------- #


class StaticFetcher:
    """Fetch pages with ``requests`` (no JavaScript)."""

    def __init__(
        self,
        request: RequestSpec,
        rate_limiter: RateLimiter,
        ua_rotator: UserAgentRotator,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.request = request
        self.rate_limiter = rate_limiter
        self.ua_rotator = ua_rotator
        self.session = session or requests.Session()
        self._sleep = sleep
        self._rng = rng or random.Random()

    def fetch(self, url: str) -> FetchResult:
        """GET ``url`` with retries.

        Raises:
            FetchError: On a non-retryable status or after exhausting retries.
        """
        attempts = self.request.retries + 1
        last_error = "unknown error"
        last_status: int | None = None
        for attempt in range(attempts):
            self.rate_limiter.wait()
            headers = {**DEFAULT_HEADERS, **self.request.headers, "User-Agent": self.ua_rotator.next()}
            started = time.monotonic()
            retry_after: float | None = None
            try:
                resp = self.session.get(url, headers=headers, timeout=self.request.timeout)
            except requests.Timeout:
                last_error, last_status = f"timeout after {self.request.timeout}s", None
                logger.warning("GET %s -> TIMEOUT (attempt %d/%d)", url, attempt + 1, attempts)
            except requests.ConnectionError as exc:
                last_error, last_status = f"connection error: {exc}", None
                logger.warning("GET %s -> CONNECTION ERROR (attempt %d/%d): %s", url, attempt + 1, attempts, exc)
            except requests.RequestException as exc:
                logger.error("GET %s -> REQUEST ERROR: %s", url, exc)
                raise FetchError(url, f"request error: {exc}") from exc
            else:
                elapsed = time.monotonic() - started
                status = resp.status_code
                size_kb = len(resp.content) / 1024
                if status in RETRY_STATUSES:
                    last_error, last_status = f"HTTP {status}", status
                    retry_after = parse_retry_after(resp.headers.get("Retry-After"))
                    logger.warning(
                        "GET %s -> %d (%.1f KB, %.2fs) retryable (attempt %d/%d)",
                        url, status, size_kb, elapsed, attempt + 1, attempts,
                    )
                elif status >= 400:
                    logger.error("GET %s -> %d (%.1f KB, %.2fs) not retryable", url, status, size_kb, elapsed)
                    raise FetchError(url, f"HTTP {status}", status)
                else:
                    logger.info("GET %s -> %d (%.1f KB, %.2fs)", url, status, size_kb, elapsed)
                    text = decode_body(resp.content, resp.headers.get("Content-Type"))
                    return FetchResult(url=resp.url or url, status=status, text=text, elapsed=elapsed)

            if attempt < attempts - 1:
                wait = backoff_delay(attempt, self.request.backoff_base, self._rng)
                if retry_after is not None:
                    wait = min(MAX_BACKOFF_SECONDS, max(wait, retry_after))
                logger.info("Retrying %s in %.2fs", url, wait)
                self._sleep(wait)

        logger.error("GET %s failed after %d attempt(s): %s", url, attempts, last_error)
        raise FetchError(url, f"{last_error} (after {attempts} attempts)", last_status)

    def close(self) -> None:
        """Close the HTTP session."""
        self.session.close()


# --------------------------------------------------------------------------- #
# Playwright fetcher
# --------------------------------------------------------------------------- #


class BrowserFetcher:
    """Render pages with Playwright (Chromium). Started lazily on first use."""

    def __init__(
        self,
        request: RequestSpec,
        rate_limiter: RateLimiter,
        ua_rotator: UserAgentRotator,
        *,
        headless: bool = True,
        wait_for: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.request = request
        self.rate_limiter = rate_limiter
        self.ua_rotator = ua_rotator
        self.headless = headless
        self.wait_for = wait_for
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None

    def _start(self) -> None:
        if self._context is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserUnavailableError(
                "", "Playwright is not installed. Run: pip install playwright && playwright install chromium"
            ) from exc
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=self.headless)
            self._context = self._browser.new_context(
                user_agent=self.ua_rotator.next(),
                extra_http_headers={**DEFAULT_HEADERS, **self.request.headers},
                viewport={"width": 1366, "height": 900},
            )
        except Exception as exc:  # Playwright raises its own Error type
            self.close()
            raise BrowserUnavailableError(
                "", f"Could not launch Chromium via Playwright ({exc}). Did you run 'playwright install chromium'?"
            ) from exc
        logger.info("Playwright Chromium started (headless=%s)", self.headless)

    def _scroll(self, page: Any, scroll: ScrollSpec) -> None:
        last_height = page.evaluate("document.body.scrollHeight")
        for i in range(scroll.max_scrolls):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(int(scroll.pause * 1000))
            height = page.evaluate("document.body.scrollHeight")
            logger.debug("Scroll %d/%d: height %s -> %s", i + 1, scroll.max_scrolls, last_height, height)
            if height == last_height:
                logger.info("Infinite scroll: no new content after %d scroll(s)", i + 1)
                return
            last_height = height
        logger.info("Infinite scroll: reached max_scrolls=%d", scroll.max_scrolls)

    def fetch(self, url: str, scroll: ScrollSpec | None = None) -> FetchResult:
        """Navigate to ``url``, optionally scroll, and return the rendered HTML."""
        self._start()
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        attempts = self.request.retries + 1
        timeout_ms = int(self.request.timeout * 1000)
        last_error, last_status = "unknown error", None
        for attempt in range(attempts):
            self.rate_limiter.wait()
            page = self._context.new_page()
            started = time.monotonic()
            try:
                response = page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                status = response.status if response is not None else 200
                if status in RETRY_STATUSES:
                    last_error, last_status = f"HTTP {status}", status
                    logger.warning("RENDER %s -> %d retryable (attempt %d/%d)", url, status, attempt + 1, attempts)
                elif status >= 400:
                    logger.error("RENDER %s -> %d not retryable", url, status)
                    raise FetchError(url, f"HTTP {status}", status)
                else:
                    try:
                        page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 10_000))
                    except PlaywrightTimeout:
                        logger.debug("networkidle not reached for %s; continuing", url)
                    if self.wait_for:
                        try:
                            page.wait_for_selector(self.wait_for, timeout=timeout_ms)
                        except PlaywrightTimeout:
                            logger.warning("wait_for selector %r not found on %s", self.wait_for, url)
                    if scroll:
                        self._scroll(page, scroll)
                    html = page.content()
                    elapsed = time.monotonic() - started
                    logger.info(
                        "RENDER %s -> %d (%.1f KB, %.2fs)", url, status, len(html.encode("utf-8")) / 1024, elapsed
                    )
                    return FetchResult(url=page.url or url, status=status, text=html, elapsed=elapsed, rendered=True)
            except PlaywrightTimeout:
                last_error, last_status = f"timeout after {self.request.timeout}s", None
                logger.warning("RENDER %s -> TIMEOUT (attempt %d/%d)", url, attempt + 1, attempts)
            except PlaywrightError as exc:
                last_error, last_status = f"browser error: {exc}", None
                logger.warning("RENDER %s -> ERROR (attempt %d/%d): %s", url, attempt + 1, attempts, exc)
            finally:
                try:
                    page.close()
                except Exception:  # pragma: no cover - best effort cleanup
                    pass
            if attempt < attempts - 1:
                wait = backoff_delay(attempt, self.request.backoff_base, self._rng)
                logger.info("Retrying %s in %.2fs", url, wait)
                self._sleep(wait)
        logger.error("RENDER %s failed after %d attempt(s): %s", url, attempts, last_error)
        raise FetchError(url, f"{last_error} (after {attempts} attempts)", last_status)

    def close(self) -> None:
        """Shut down the browser and Playwright."""
        for obj, method in ((self._context, "close"), (self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:  # pragma: no cover - best effort cleanup
                    pass
        self._pw = self._browser = self._context = None


# --------------------------------------------------------------------------- #
# Facade
# --------------------------------------------------------------------------- #


class Fetcher:
    """Single entry point combining robots.txt, static and browser fetching."""

    def __init__(
        self,
        request: RequestSpec,
        *,
        headless: bool = True,
        wait_for: str | None = None,
        respect_robots: bool = True,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        browser: BrowserFetcher | None = None,
    ) -> None:
        self.request = request
        self.ua_rotator = UserAgentRotator(request.user_agents)
        self.rate_limiter = RateLimiter(request.delay, request.jitter, sleep=sleep)
        self.static = StaticFetcher(request, self.rate_limiter, self.ua_rotator, session=session, sleep=sleep)
        self.browser = browser or BrowserFetcher(
            request, self.rate_limiter, self.ua_rotator, headless=headless, wait_for=wait_for, sleep=sleep
        )
        self.robots = RobotsChecker(self.static.session, timeout=request.timeout) if respect_robots else None
        self._crawl_delay_applied: set[str] = set()

    def is_allowed(self, url: str) -> bool:
        """Check robots.txt (always True when robots checking is disabled)."""
        if self.robots is None:
            return True
        allowed = self.robots.allowed(url)
        host = urlsplit(url).netloc.lower()
        if allowed and host not in self._crawl_delay_applied:
            self._crawl_delay_applied.add(host)
            delay = self.robots.crawl_delay(url)
            if delay is not None and delay > self.rate_limiter.delay:
                logger.info("robots.txt Crawl-delay=%ss for %s; increasing delay", delay, host)
                self.rate_limiter.delay = delay
        return allowed

    def fetch(self, url: str, *, render: bool = False, scroll: ScrollSpec | None = None) -> FetchResult:
        """Fetch ``url`` statically, or via Playwright if ``render`` or ``scroll`` is set."""
        if render or scroll is not None:
            return self.browser.fetch(url, scroll=scroll)
        return self.static.fetch(url)

    def close(self) -> None:
        """Release network and browser resources."""
        self.browser.close()
        self.static.close()
