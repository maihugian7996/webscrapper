"""Tests for core.fetcher (HTTP fully mocked with `responses`)."""

from __future__ import annotations

import random

import pytest
import requests
import responses

from core.config import RequestSpec
from core.fetcher import (
    DEFAULT_USER_AGENTS,
    MAX_BACKOFF_SECONDS,
    FetchError,
    Fetcher,
    RateLimiter,
    UserAgentRotator,
    backoff_delay,
    decode_body,
    parse_retry_after,
)

URL = "https://site.test/page"
ROBOTS = "https://site.test/robots.txt"


class SleepRecorder:
    """Records requested sleeps instead of sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_fetcher(sleep: SleepRecorder, respect_robots: bool = False, **request: object) -> Fetcher:
    spec = RequestSpec(**{"delay": 0, "jitter": 0, "retries": 3, "backoff_base": 1.0, **request})
    return Fetcher(spec, respect_robots=respect_robots, session=requests.Session(), sleep=sleep)


@responses.activate
def test_successful_get_returns_text() -> None:
    responses.get(URL, body="<html><body>ok</body></html>", content_type="text/html; charset=utf-8")
    result = make_fetcher(SleepRecorder()).fetch(URL)
    assert result.status == 200 and "ok" in result.text and result.rendered is False


@responses.activate
def test_retries_on_503_with_exponential_backoff() -> None:
    responses.get(URL, status=503)
    responses.get(URL, status=503)
    responses.get(URL, body="fine")
    sleep = SleepRecorder()
    result = make_fetcher(sleep).fetch(URL)
    assert result.text == "fine"
    assert len(responses.calls) == 3
    assert len(sleep.calls) == 2
    assert 1.0 <= sleep.calls[0] <= 2.0  # base * 2**0 + jitter[0, base]
    assert 2.0 <= sleep.calls[1] <= 3.0  # base * 2**1 + jitter[0, base]


@responses.activate
def test_429_honours_retry_after() -> None:
    responses.get(URL, status=429, headers={"Retry-After": "7"})
    responses.get(URL, body="ok")
    sleep = SleepRecorder()
    make_fetcher(sleep).fetch(URL)
    assert sleep.calls == [pytest.approx(7.0)]


@responses.activate
def test_timeout_is_retried() -> None:
    responses.get(URL, body=requests.exceptions.ReadTimeout("slow"))
    responses.get(URL, body="ok")
    assert make_fetcher(SleepRecorder()).fetch(URL).text == "ok"
    assert len(responses.calls) == 2


@responses.activate
def test_connection_error_is_retried() -> None:
    responses.get(URL, body=requests.exceptions.ConnectionError("refused"))
    responses.get(URL, body="ok")
    assert make_fetcher(SleepRecorder()).fetch(URL).text == "ok"


@responses.activate
def test_404_is_not_retried() -> None:
    responses.get(URL, status=404)
    with pytest.raises(FetchError) as info:
        make_fetcher(SleepRecorder()).fetch(URL)
    assert info.value.status == 404 and len(responses.calls) == 1


@responses.activate
def test_gives_up_after_retries() -> None:
    responses.get(URL, status=500)
    with pytest.raises(FetchError, match=r"HTTP 500 \(after 3 attempts\)") as info:
        make_fetcher(SleepRecorder(), retries=2).fetch(URL)
    assert info.value.status == 500 and len(responses.calls) == 3


@responses.activate
def test_user_agent_rotates_and_headers_sent() -> None:
    responses.get(URL, body="ok")
    fetcher = make_fetcher(SleepRecorder(), headers={"X-Test": "1"})
    for _ in range(4):
        fetcher.fetch(URL)
    agents = [c.request.headers["User-Agent"] for c in responses.calls]
    assert all(ua in DEFAULT_USER_AGENTS for ua in agents)
    assert all(a != b for a, b in zip(agents, agents[1:]))
    assert all(c.request.headers["X-Test"] == "1" for c in responses.calls)
    assert "text/html" in responses.calls[0].request.headers["Accept"]


@responses.activate
def test_utf8_without_charset_header_decoded_correctly() -> None:
    body = '<html><head><meta charset="utf-8"></head><body>£51.77 café</body></html>'.encode("utf-8")
    responses.get(URL, body=body, content_type="text/html")
    assert "£51.77 café" in make_fetcher(SleepRecorder()).fetch(URL).text


def test_decode_body_prefers_header_charset() -> None:
    assert decode_body("é".encode("latin-1"), "text/html; charset=ISO-8859-1") == "é"
    assert decode_body("é".encode("utf-8"), "text/html; charset=bogus-charset") == "é"


@responses.activate
def test_robots_disallow_and_allow() -> None:
    responses.get(ROBOTS, body="User-agent: *\nDisallow: /private\n")
    fetcher = make_fetcher(SleepRecorder(), respect_robots=True)
    assert fetcher.is_allowed("https://site.test/public") is True
    assert fetcher.is_allowed("https://site.test/private/x") is False
    # robots.txt fetched once per host
    assert sum(1 for c in responses.calls if c.request.url == ROBOTS) == 1


@responses.activate
def test_robots_404_allows_everything() -> None:
    responses.get(ROBOTS, status=404)
    assert make_fetcher(SleepRecorder(), respect_robots=True).is_allowed(URL) is True


@responses.activate
def test_robots_5xx_disallows() -> None:
    responses.get(ROBOTS, status=503)
    assert make_fetcher(SleepRecorder(), respect_robots=True).is_allowed(URL) is False


@responses.activate
def test_robots_network_error_allows() -> None:
    responses.get(ROBOTS, body=requests.exceptions.ConnectionError("down"))
    assert make_fetcher(SleepRecorder(), respect_robots=True).is_allowed(URL) is True


@responses.activate
def test_robots_crawl_delay_raises_delay() -> None:
    responses.get(ROBOTS, body="User-agent: *\nCrawl-delay: 4\nDisallow:\n")
    fetcher = make_fetcher(SleepRecorder(), respect_robots=True, delay=1.0)
    assert fetcher.is_allowed(URL)
    assert fetcher.rate_limiter.delay == 4.0


def test_robots_disabled_never_requests() -> None:
    with responses.RequestsMock() as mock:  # would raise on any unexpected request
        assert make_fetcher(SleepRecorder(), respect_robots=False).is_allowed(URL) is True
        assert len(mock.calls) == 0


def test_rate_limiter_enforces_delay_with_jitter() -> None:
    now = [100.0]
    sleep = SleepRecorder()
    limiter = RateLimiter(2.0, 1.0, sleep=sleep, clock=lambda: now[0], rng=random.Random(0))
    assert limiter.wait() == 0.0  # first request: no wait
    now[0] += 0.5
    limiter.wait()
    assert len(sleep.calls) == 1 and 1.5 <= sleep.calls[0] <= 2.5
    now[0] += 10
    limiter.wait()
    assert len(sleep.calls) == 1  # enough time already passed


def test_user_agent_rotator_custom_pool() -> None:
    rot = UserAgentRotator(["A", "B"], rng=random.Random(1))
    seq = [rot.next() for _ in range(6)]
    assert set(seq) == {"A", "B"} and all(a != b for a, b in zip(seq, seq[1:]))
    assert UserAgentRotator(["only"]).next() == "only"


def test_parse_retry_after() -> None:
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0  # in the past


def test_backoff_is_capped() -> None:
    assert backoff_delay(20, 1.0, random.Random(0)) == MAX_BACKOFF_SECONDS


def test_browser_not_started_for_static_fetch() -> None:
    fetcher = make_fetcher(SleepRecorder())
    assert fetcher.browser._context is None
    fetcher.close()
