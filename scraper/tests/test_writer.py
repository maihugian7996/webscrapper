"""Tests for core.writer (schema validation + atomic writes)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core import __version__
from core.writer import OutputValidationError, ScrapeResult, build_result, to_json, utc_now_iso, write_json


def sample_result(**overrides: object) -> ScrapeResult:
    kwargs = dict(
        source_url="https://shop.test/catalog/page1.html",
        scraped_at=utc_now_iso(),
        pages_scraped=2,
        items=[{"title": "Béta ✓", "price": 1.5}, {"title": "Alpha", "price": None}],
        errors=[{"url": "https://shop.test/x", "error": "HTTP 404", "timestamp": utc_now_iso()}],
        config_used="configs/shop.yaml",
    )
    kwargs.update(overrides)
    return build_result(**kwargs)  # type: ignore[arg-type]


def test_utc_now_iso_format() -> None:
    ts = utc_now_iso()
    assert ts.endswith("Z") and "T" in ts and len(ts) == 20


def test_build_result_schema() -> None:
    data = sample_result().model_dump(mode="json")
    assert set(data) == {"metadata", "items", "errors"}
    assert set(data["metadata"]) == {
        "source_url", "scraped_at", "pages_scraped", "total_items", "config_used", "scraper_version",
    }
    assert data["metadata"]["total_items"] == 2
    assert data["metadata"]["scraper_version"] == __version__ == "1.0.0"
    assert set(data["errors"][0]) == {"url", "error", "timestamp"}


def test_total_items_mismatch_rejected() -> None:
    data = sample_result().model_dump(mode="json")
    data["metadata"]["total_items"] = 99
    with pytest.raises(OutputValidationError, match="total_items"):
        to_json(data)


def test_bad_timestamp_rejected() -> None:
    with pytest.raises(OutputValidationError, match="ISO-8601"):
        sample_result(scraped_at="yesterday")


def test_bad_error_entry_rejected() -> None:
    with pytest.raises(OutputValidationError):
        sample_result(errors=[{"url": "x"}])


def test_negative_pages_rejected() -> None:
    with pytest.raises(OutputValidationError):
        sample_result(pages_scraped=-1)


def test_write_json_utf8_pretty_atomic(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "out.json"
    write_json(sample_result(), out)
    raw = out.read_bytes().decode("utf-8")
    assert "Béta ✓" in raw  # ensure_ascii=False
    assert '\n  "metadata": {' in raw  # indent=2
    assert json.loads(raw)["metadata"]["total_items"] == 2
    assert [p.name for p in out.parent.iterdir()] == ["out.json"]  # no temp files left


def test_write_json_failure_keeps_old_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "out.json"
    out.write_text("ORIGINAL", encoding="utf-8")

    def boom(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        write_json(sample_result(), out)
    assert out.read_text(encoding="utf-8") == "ORIGINAL"
    assert [p.name for p in tmp_path.iterdir()] == ["out.json"]


def test_invalid_dict_not_written(tmp_path: Path) -> None:
    out = tmp_path / "out.json"
    with pytest.raises(OutputValidationError):
        write_json({"metadata": {}, "items": []}, out)
    assert not out.exists()
