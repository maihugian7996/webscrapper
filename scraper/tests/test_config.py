"""Tests for core.config."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.config import ConfigError, FieldSpec, load_config, parse_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_example_template_is_valid() -> None:
    config = load_config(PROJECT_ROOT / "configs" / "example.yaml")
    assert config.name == "example_template"
    assert config.pagination.type == "next_link"
    assert "PLACEHOLDER" in config.item.container


def test_shop_config_parses(shop_config_dict: dict[str, Any]) -> None:
    config = parse_config(shop_config_dict)
    assert config.item.fields["title"].required is True
    # Shorthand string form becomes a FieldSpec with attr=text
    assert config.detail is not None
    desc = config.detail.fields["description"]
    assert isinstance(desc, FieldSpec) and desc.selector == "#description p" and desc.attr == "text"


def test_start_urls_string_is_coerced(shop_config_dict: dict[str, Any]) -> None:
    shop_config_dict["start_urls"] = "https://a.test/"
    assert parse_config(shop_config_dict).start_urls == ["https://a.test/"]


def test_unknown_key_rejected(shop_config_dict: dict[str, Any]) -> None:
    shop_config_dict["item"]["contianer"] = "typo"
    with pytest.raises(ConfigError, match="contianer"):
        parse_config(shop_config_dict)


@pytest.mark.parametrize(
    ("pagination", "message"),
    [
        ({"type": "next_link"}, "next_selector"),
        ({"type": "url_pattern"}, "requires 'pattern'"),
        ({"type": "url_pattern", "pattern": "https://a.test/p"}, "{page}"),
        ({"type": "bogus"}, "pagination.type"),
    ],
)
def test_pagination_validation(shop_config_dict: dict[str, Any], pagination: dict, message: str) -> None:
    shop_config_dict["pagination"] = pagination
    with pytest.raises(ConfigError, match=message):
        parse_config(shop_config_dict)


@pytest.mark.parametrize(
    "step",
    ["trim", {"regex": 5}, {"type": "decimal"}, {"replace": "x"}, {"regex": "a", "type": "int"}],
)
def test_bad_clean_steps_rejected(shop_config_dict: dict[str, Any], step: Any) -> None:
    shop_config_dict["item"]["fields"]["price"]["clean"] = [step]
    with pytest.raises(ConfigError, match="item.fields.price.clean"):
        parse_config(shop_config_dict)


def test_dedupe_on_must_reference_fields(shop_config_dict: dict[str, Any]) -> None:
    shop_config_dict["dedupe_on"] = ["nope"]
    with pytest.raises(ConfigError, match="dedupe_on"):
        parse_config(shop_config_dict)


def test_detail_link_field_must_exist(shop_config_dict: dict[str, Any]) -> None:
    shop_config_dict["detail"]["link_field"] = "missing"
    with pytest.raises(ConfigError, match="link_field"):
        parse_config(shop_config_dict)


def test_infinite_scroll_requires_js(shop_config_dict: dict[str, Any]) -> None:
    shop_config_dict["pagination"] = {"type": "infinite_scroll"}
    shop_config_dict["render_js"] = False
    with pytest.raises(ConfigError, match="infinite_scroll"):
        parse_config(shop_config_dict)


def test_empty_fields_rejected(shop_config_dict: dict[str, Any]) -> None:
    shop_config_dict["item"]["fields"] = {}
    with pytest.raises(ConfigError, match="at least one field"):
        parse_config(shop_config_dict)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_invalid_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("item: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(bad)


def test_non_mapping_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "list.yaml"
    bad.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(bad)


def test_load_from_file(config_file: Path) -> None:
    config = load_config(config_file)
    assert config.name == "synthetic_shop"
