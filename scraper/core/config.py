"""Site configuration models and YAML loader.

Every target site is described by a YAML file that is validated against the
Pydantic models below. Nothing in this module (or anywhere else in the
project) is specific to a particular website.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# --------------------------------------------------------------------------- #
# Cleaning-step vocabulary (shared with core.parser)
# --------------------------------------------------------------------------- #

#: Cleaning steps that take no argument and can be written as a bare string.
SIMPLE_CLEAN_STEPS: frozenset[str] = frozenset(
    {"strip", "collapse_whitespace", "lower", "upper", "title"}
)

#: Cleaning steps that take an argument and must be written as a one-key mapping.
PARAM_CLEAN_STEPS: frozenset[str] = frozenset(
    {"strip", "regex", "replace", "prefix", "suffix", "type", "map"}
)

#: Allowed targets for the ``type`` cleaning step.
CAST_TYPES: frozenset[str] = frozenset({"str", "int", "float", "bool"})

CleanStep = Union[str, dict[str, Any]]


class ConfigError(Exception):
    """Raised when a site config file is missing, unreadable or invalid."""


def _validate_clean_step(step: Any) -> CleanStep:
    """Validate a single cleaning step and return it unchanged.

    Args:
        step: A bare string (e.g. ``"strip"``) or a one-key mapping
            (e.g. ``{"regex": "(\\d+)"}``).

    Raises:
        ValueError: If the step is unknown or malformed.
    """
    if isinstance(step, str):
        if step not in SIMPLE_CLEAN_STEPS:
            raise ValueError(
                f"unknown cleaning step '{step}'. Bare-string steps: {sorted(SIMPLE_CLEAN_STEPS)}"
            )
        return step

    if not isinstance(step, dict) or len(step) != 1:
        raise ValueError(
            f"cleaning step must be a string or a mapping with exactly one key, got {step!r}"
        )

    name, arg = next(iter(step.items()))
    if name not in PARAM_CLEAN_STEPS:
        raise ValueError(
            f"unknown cleaning step '{name}'. Mapping steps: {sorted(PARAM_CLEAN_STEPS)}"
        )

    if name == "regex":
        if isinstance(arg, dict):
            if "pattern" not in arg:
                raise ValueError("regex step mapping requires a 'pattern' key")
        elif not isinstance(arg, str):
            raise ValueError("regex step expects a pattern string or {pattern, group}")
    elif name == "replace":
        if not isinstance(arg, dict) or "from" not in arg:
            raise ValueError("replace step expects {from: ..., to: ...}")
    elif name == "type":
        if arg not in CAST_TYPES:
            raise ValueError(f"type step must be one of {sorted(CAST_TYPES)}, got {arg!r}")
    elif name == "map":
        if not isinstance(arg, dict):
            raise ValueError("map step expects a mapping of {input: output}")
    elif name in {"prefix", "suffix", "strip"}:
        if not isinstance(arg, str):
            raise ValueError(f"{name} step expects a string argument")
    return step


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


class _StrictModel(BaseModel):
    """Base model that rejects unknown keys (catches typos in YAML)."""

    model_config = ConfigDict(extra="forbid")


class FieldSpec(_StrictModel):
    """How to extract and clean one output field.

    Attributes:
        selector: CSS selector (default) or XPath (prefix ``xpath:`` or start
            with ``/`` / ``./``). Relative to the item container for listing
            fields, relative to the whole document for detail fields. ``None``
            means "the container element itself".
        attr: ``text`` (whitespace-collapsed text), ``raw_text``, ``html`` or
            any HTML attribute name (``href``, ``src``, ``data-id`` ...).
            URL-like attributes are resolved to absolute URLs.
        clean: Ordered list of cleaning steps.
        default: Value used when nothing is found or the cleaned value is empty.
        required: Drop the whole item if this field ends up empty.
        multiple: Return a list of all matches instead of the first one.
    """

    selector: str | None = None
    attr: str = "text"
    clean: list[CleanStep] = Field(default_factory=list)
    default: Any = None
    required: bool = False
    multiple: bool = False

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, data: Any) -> Any:
        """Allow ``field: "css selector"`` as shorthand for ``{selector: ...}``."""
        if isinstance(data, str):
            return {"selector": data}
        return data

    @field_validator("clean", mode="before")
    @classmethod
    def _validate_clean(cls, value: Any) -> list[CleanStep]:
        if value is None:
            return []
        if not isinstance(value, list):
            value = [value]
        return [_validate_clean_step(step) for step in value]

    @field_validator("selector")
    @classmethod
    def _non_empty_selector(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("selector must not be empty (omit it to use the container itself)")
        return value


class ItemSpec(_StrictModel):
    """Repeated item block on a listing page and the fields inside it."""

    container: str
    fields: dict[str, FieldSpec]

    @field_validator("fields")
    @classmethod
    def _at_least_one_field(cls, value: dict[str, FieldSpec]) -> dict[str, FieldSpec]:
        if not value:
            raise ValueError("item.fields must define at least one field")
        return value


class PaginationSpec(_StrictModel):
    """Pagination rule.

    * ``none`` - only the start URL(s) are scraped.
    * ``next_link`` - follow ``next_selector`` (href) until it disappears.
    * ``url_pattern`` - format ``pattern`` with ``{page}`` = start, start+step, ...
      The start URL is treated as page ``start``.
    * ``infinite_scroll`` - load the page in Playwright and scroll until no
      new content appears (or ``max_scrolls`` is reached).
    """

    type: Literal["none", "next_link", "url_pattern", "infinite_scroll"] = "none"
    next_selector: str | None = None
    pattern: str | None = None
    start: int = 1
    step: int = Field(default=1, ge=1)
    scroll_pause: float = Field(default=1.5, ge=0)
    max_scrolls: int = Field(default=20, ge=1)

    @model_validator(mode="after")
    def _check_required_keys(self) -> "PaginationSpec":
        if self.type == "next_link" and not self.next_selector:
            raise ValueError("pagination.type 'next_link' requires 'next_selector'")
        if self.type == "url_pattern":
            if not self.pattern:
                raise ValueError("pagination.type 'url_pattern' requires 'pattern'")
            if "{page}" not in self.pattern:
                raise ValueError("pagination.pattern must contain the '{page}' placeholder")
        return self


class DetailSpec(_StrictModel):
    """Optional detail-page scraping: follow ``link_field`` and extract more fields."""

    link_field: str
    fields: dict[str, FieldSpec]
    max_items: int | None = Field(default=None, ge=1)


class RequestSpec(_StrictModel):
    """HTTP behaviour: politeness delays, timeouts and retries."""

    delay: float = Field(default=1.0, ge=0)
    jitter: float = Field(default=0.5, ge=0)
    timeout: float = Field(default=20.0, gt=0)
    retries: int = Field(default=3, ge=0)
    backoff_base: float = Field(default=1.0, ge=0)
    headers: dict[str, str] = Field(default_factory=dict)
    user_agents: list[str] | None = None


class SiteConfig(_StrictModel):
    """Top-level site configuration loaded from YAML."""

    name: str = "unnamed"
    start_urls: list[str] = Field(default_factory=list)
    render_js: bool | Literal["auto"] = "auto"
    wait_for: str | None = None
    item: ItemSpec
    dedupe_on: list[str] | None = None
    pagination: PaginationSpec = Field(default_factory=PaginationSpec)
    max_pages: int | None = Field(default=10, ge=1)
    detail: DetailSpec | None = None
    request: RequestSpec = Field(default_factory=RequestSpec)

    @field_validator("start_urls", mode="before")
    @classmethod
    def _coerce_start_urls(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value

    @model_validator(mode="after")
    def _cross_checks(self) -> "SiteConfig":
        item_fields = set(self.item.fields)
        if self.dedupe_on:
            missing = [f for f in self.dedupe_on if f not in item_fields]
            if missing:
                raise ValueError(f"dedupe_on references unknown item field(s): {missing}")
        if self.detail and self.detail.link_field not in item_fields:
            raise ValueError(
                f"detail.link_field '{self.detail.link_field}' is not defined in item.fields"
            )
        if self.pagination.type == "infinite_scroll" and self.render_js is False:
            raise ValueError("infinite_scroll pagination requires render_js to be true or 'auto'")
        return self


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    lines = [f"Invalid config '{path}':"]
    for err in exc.errors():
        location = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"  - {location}: {err['msg']}")
    return "\n".join(lines)


def parse_config(data: Any, source: str | Path = "<memory>") -> SiteConfig:
    """Validate an already-parsed mapping as a :class:`SiteConfig`.

    Raises:
        ConfigError: With a human-readable list of problems.
    """
    if not isinstance(data, dict):
        raise ConfigError(f"Config '{source}' must be a YAML mapping at the top level.")
    try:
        return SiteConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(Path(str(source)), exc)) from exc


def load_config(path: str | Path) -> SiteConfig:
    """Read and validate a YAML site config.

    Args:
        path: Path to the YAML file.

    Returns:
        The validated :class:`SiteConfig`.

    Raises:
        ConfigError: If the file is missing, is not valid YAML, or fails validation.
    """
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"Config file not found: '{path}'")
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Config '{path}' is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read config '{path}': {exc}") from exc
    return parse_config(data, path)
