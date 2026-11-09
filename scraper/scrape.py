#!/usr/bin/env python
"""Command-line entry point for the site-agnostic scraper.

Example:
    python scrape.py --url https://example.com/list --config configs/mysite.yaml --out output/mysite.json
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal, Sequence
from urllib.parse import urlsplit

from core import __version__
from core.config import ConfigError, SiteConfig, load_config
from core.engine import RunOptions, ScraperEngine
from core.fetcher import Fetcher
from core.logging_setup import setup_logging
from core.writer import OutputValidationError, write_json

logger = logging.getLogger("scrape")

EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_ROBOTS_BLOCKED = 2
EXIT_INTERRUPTED = 130


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    p = argparse.ArgumentParser(
        prog="scrape.py",
        description="Site-agnostic web scraper: URL + YAML config -> structured JSON.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--config", required=True, help="Path to the site YAML config (e.g. configs/mysite.yaml)")
    p.add_argument("--url", help="Start URL (overrides start_urls in the config)")
    p.add_argument("--out", help="Output JSON path (default: output/<config name>_<timestamp>.json)")
    p.add_argument("--max-pages", type=int, help="Maximum listing pages to scrape (overrides config)")
    p.add_argument("--delay", type=float, help="Base delay in seconds between requests (overrides config)")
    p.add_argument("--jitter", type=float, help="Max random extra delay in seconds (overrides config)")
    p.add_argument("--timeout", type=float, help="Per-request timeout in seconds (overrides config)")
    p.add_argument("--retries", type=int, help="Retries on 429/5xx/timeouts (overrides config)")
    js = p.add_mutually_exclusive_group()
    js.add_argument("--render-js", action="store_true", help="Always render pages with Playwright")
    js.add_argument("--static-only", action="store_true", help="Never use Playwright (disable JS auto-detect)")
    p.add_argument(
        "--headless", action=argparse.BooleanOptionalAction, default=True,
        help="Run the Playwright browser headless",
    )
    p.add_argument("--ignore-robots", action="store_true",
                   help="Do NOT honour robots.txt. Only use with the site owner's permission.")
    p.add_argument("--no-detail", action="store_true", help="Skip detail-page scraping even if configured")
    p.add_argument("--log-file", help="Also write a DEBUG-level log to this file")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose (DEBUG) console logging")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _validate_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ConfigError(f"Invalid URL {url!r}: must be an absolute http(s) URL")


def apply_overrides(config: SiteConfig, args: argparse.Namespace) -> SiteConfig:
    """Return a copy of ``config`` with CLI overrides applied (CLI > YAML > defaults)."""
    request_updates = {
        key: value
        for key, value in (
            ("delay", args.delay), ("jitter", args.jitter),
            ("timeout", args.timeout), ("retries", args.retries),
        )
        if value is not None
    }
    for key, value in request_updates.items():
        if value < 0 or (key == "timeout" and value == 0):
            raise ConfigError(f"--{key} must be {'positive' if key == 'timeout' else 'non-negative'}")
    updates: dict[str, object] = {"request": config.request.model_copy(update=request_updates)}
    if args.max_pages is not None:
        if args.max_pages < 1:
            raise ConfigError("--max-pages must be at least 1")
        updates["max_pages"] = args.max_pages
    render: bool | Literal["auto"] = config.render_js
    if args.render_js:
        render = True
    elif args.static_only:
        if config.pagination.type == "infinite_scroll":
            raise ConfigError("--static-only cannot be used with infinite_scroll pagination")
        render = False
    updates["render_js"] = render
    return config.model_copy(update=updates)


def default_out_path(config: SiteConfig) -> Path:
    """``output/<name>_<YYYYmmdd_HHMMSS>.json``."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in config.name) or "scrape"
    return Path("output") / f"{safe}_{stamp}.json"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI. Returns the process exit code."""
    args = build_arg_parser().parse_args(argv)
    setup_logging(args.verbose, args.log_file)

    try:
        config = apply_overrides(load_config(args.config), args)
        start_urls = [args.url] if args.url else list(config.start_urls)
        if not start_urls:
            raise ConfigError("No start URL: pass --url or set start_urls in the config")
        for url in start_urls:
            _validate_url(url)
    except ConfigError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG_ERROR

    out_path = Path(args.out) if args.out else default_out_path(config)
    if args.ignore_robots:
        logger.warning("robots.txt checking is DISABLED (--ignore-robots). Make sure you have permission.")

    fetcher = Fetcher(
        config.request,
        headless=args.headless,
        wait_for=config.wait_for,
        respect_robots=not args.ignore_robots,
    )
    engine = ScraperEngine(
        config=config,
        options=RunOptions(
            start_urls=start_urls,
            config_path=Path(args.config).as_posix(),
            max_pages=config.max_pages,
            render_js=config.render_js,
            scrape_details=not args.no_detail,
        ),
        fetcher=fetcher,
    )

    try:
        result = engine.run()
    except OutputValidationError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG_ERROR

    if engine.robots_blocked and result.metadata.pages_scraped == 0:
        logger.error(
            "Stopped: robots.txt disallows scraping %s. No output written. "
            "Re-run with --ignore-robots only if you have permission.", start_urls[0],
        )
        return EXIT_ROBOTS_BLOCKED

    try:
        write_json(result, out_path)
    except OutputValidationError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG_ERROR
    except OSError as exc:
        logger.error("Could not write output file '%s': %s", out_path, exc)
        return EXIT_CONFIG_ERROR

    logger.info(
        "Done: %d item(s) from %d page(s), %d error(s) -> %s",
        result.metadata.total_items, result.metadata.pages_scraped, len(result.errors), out_path,
    )
    return EXIT_INTERRUPTED if engine.interrupted else EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logging.getLogger("scrape").warning("Interrupted before scraping started")
        sys.exit(EXIT_INTERRUPTED)
