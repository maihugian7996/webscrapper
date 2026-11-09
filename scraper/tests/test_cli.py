"""Tests for the scrape.py CLI (fetcher replaced by an in-memory fake)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

import scrape
from core.config import load_config
from tests.conftest import PAGE1, PAGE2, FakeFetcher


@pytest.fixture(autouse=True)
def _no_logging_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the CLI from replacing pytest's log handlers."""
    monkeypatch.setattr(scrape, "setup_logging", lambda *a, **k: None)


def install_fake(monkeypatch: pytest.MonkeyPatch, fake: FakeFetcher) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def factory(request: Any, **kwargs: Any) -> FakeFetcher:
        captured["request"] = request
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(scrape, "Fetcher", factory)
    return captured


def test_cli_writes_valid_json(
    tmp_path: Path, config_file: Path, shop_pages: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = install_fake(monkeypatch, FakeFetcher(shop_pages))
    out = tmp_path / "out" / "shop.json"
    code = scrape.main(["--url", PAGE1, "--config", str(config_file), "--out", str(out), "--delay", "0.25", "--no-headless"])
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["metadata"]["total_items"] == 3 == len(data["items"])
    assert data["metadata"]["pages_scraped"] == 2
    assert data["metadata"]["config_used"] == config_file.as_posix()
    assert data["metadata"]["scraper_version"] == "1.0.0"
    assert data["items"][1]["title"] == "Béta Gadget — Ünïcode ✓"
    assert captured["request"].delay == 0.25
    assert captured["headless"] is False
    assert captured["respect_robots"] is True


def test_cli_max_pages_and_ignore_robots(
    tmp_path: Path, config_file: Path, shop_pages: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeFetcher(shop_pages)
    captured = install_fake(monkeypatch, fake)
    out = tmp_path / "o.json"
    code = scrape.main(["--config", str(config_file), "--out", str(out), "--max-pages", "1", "--ignore-robots", "--no-detail"])
    assert code == 0
    assert captured["respect_robots"] is False
    assert [c[0] for c in fake.calls] == [PAGE1]
    assert json.loads(out.read_text(encoding="utf-8"))["metadata"]["pages_scraped"] == 1


def test_cli_invalid_config_returns_1(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"item": {"container": "div"}}), encoding="utf-8")
    assert scrape.main(["--config", str(bad), "--out", str(tmp_path / "x.json")]) == 1


def test_cli_missing_url_returns_1(tmp_path: Path, shop_config_dict: dict[str, Any]) -> None:
    shop_config_dict["start_urls"] = []
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.safe_dump(shop_config_dict, allow_unicode=True), encoding="utf-8")
    assert scrape.main(["--config", str(cfg)]) == 1


def test_cli_bad_url_returns_1(config_file: Path) -> None:
    assert scrape.main(["--config", str(config_file), "--url", "ftp://nope"]) == 1


def test_cli_robots_blocked_returns_2_and_writes_nothing(
    tmp_path: Path, config_file: Path, shop_pages: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake(monkeypatch, FakeFetcher(shop_pages, disallowed={PAGE1}))
    out = tmp_path / "o.json"
    assert scrape.main(["--config", str(config_file), "--out", str(out)]) == 2
    assert not out.exists()


def test_cli_interrupt_returns_130_with_partial_file(
    tmp_path: Path, config_file: Path, shop_pages: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake(monkeypatch, FakeFetcher({**shop_pages, PAGE2: KeyboardInterrupt()}))
    out = tmp_path / "partial.json"
    assert scrape.main(["--config", str(config_file), "--out", str(out)]) == 130
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["metadata"]["total_items"] == 2
    assert "interrupted" in data["errors"][-1]["error"]


def test_apply_overrides(config_file: Path) -> None:
    config = load_config(config_file)
    parser = scrape.build_arg_parser()

    args = parser.parse_args(["--config", "x", "--render-js", "--delay", "3", "--retries", "5", "--max-pages", "2"])
    updated = scrape.apply_overrides(config, args)
    assert updated.render_js is True and updated.request.delay == 3 and updated.request.retries == 5
    assert updated.max_pages == 2
    assert config.request.delay == 0  # original untouched

    args = parser.parse_args(["--config", "x", "--static-only"])
    assert scrape.apply_overrides(config, args).render_js is False

    args = parser.parse_args(["--config", "x"])
    assert scrape.apply_overrides(config, args).render_js == "auto"


def test_render_js_and_static_only_are_exclusive() -> None:
    with pytest.raises(SystemExit):
        scrape.build_arg_parser().parse_args(["--config", "x", "--render-js", "--static-only"])


def test_default_out_path(config_file: Path) -> None:
    path = scrape.default_out_path(load_config(config_file))
    assert path.parent == Path("output") and path.name.startswith("synthetic_shop_") and path.suffix == ".json"
