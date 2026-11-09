"""Output schema validation and atomic JSON writing."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from core import __version__

logger = logging.getLogger(__name__)


class OutputValidationError(Exception):
    """Raised when the scrape result does not match the output schema."""


def utc_now_iso() -> str:
    """Current UTC time as ISO-8601 with a trailing ``Z`` (second precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _check_iso(value: str) -> str:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"not an ISO-8601 timestamp: {value!r}") from exc
    return value


class Metadata(BaseModel):
    """Run metadata block."""

    model_config = ConfigDict(extra="forbid")

    source_url: str
    scraped_at: str
    pages_scraped: int = Field(ge=0)
    total_items: int = Field(ge=0)
    config_used: str
    scraper_version: str = __version__

    @field_validator("scraped_at")
    @classmethod
    def _validate_scraped_at(cls, value: str) -> str:
        return _check_iso(value)


class ErrorEntry(BaseModel):
    """One recorded failure."""

    model_config = ConfigDict(extra="forbid")

    url: str
    error: str
    timestamp: str

    @field_validator("timestamp")
    @classmethod
    def _validate_timestamp(cls, value: str) -> str:
        return _check_iso(value)


class ScrapeResult(BaseModel):
    """Top-level output document."""

    model_config = ConfigDict(extra="forbid")

    metadata: Metadata
    items: list[dict[str, Any]]
    errors: list[ErrorEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent_counts(self) -> "ScrapeResult":
        if self.metadata.total_items != len(self.items):
            raise ValueError(
                f"metadata.total_items ({self.metadata.total_items}) != number of items ({len(self.items)})"
            )
        return self


def build_result(
    *,
    source_url: str,
    scraped_at: str,
    pages_scraped: int,
    items: list[dict[str, Any]],
    errors: list[dict[str, str]],
    config_used: str,
) -> ScrapeResult:
    """Assemble and validate a :class:`ScrapeResult`.

    Raises:
        OutputValidationError: If the data does not satisfy the schema.
    """
    try:
        return ScrapeResult(
            metadata=Metadata(
                source_url=source_url,
                scraped_at=scraped_at,
                pages_scraped=pages_scraped,
                total_items=len(items),
                config_used=config_used,
                scraper_version=__version__,
            ),
            items=items,
            errors=[ErrorEntry(**e) for e in errors],
        )
    except ValidationError as exc:
        raise OutputValidationError(f"Scrape result failed schema validation:\n{exc}") from exc


def to_json(result: ScrapeResult | dict[str, Any]) -> str:
    """Validate and serialise a result (UTF-8 friendly, indent=2).

    Raises:
        OutputValidationError: If ``result`` is a dict that fails validation,
            or contains values that are not JSON-serialisable.
    """
    if not isinstance(result, ScrapeResult):
        try:
            result = ScrapeResult.model_validate(result)
        except ValidationError as exc:
            raise OutputValidationError(f"Scrape result failed schema validation:\n{exc}") from exc
    try:
        return json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise OutputValidationError(f"Scrape result is not JSON-serialisable: {exc}") from exc


def write_json(result: ScrapeResult | dict[str, Any], path: str | Path) -> Path:
    """Validate ``result`` and write it atomically to ``path``.

    The JSON is written to a temporary file in the same directory, flushed
    and fsync'd, then moved into place with :func:`os.replace`, so readers
    never see a half-written file.

    Returns:
        The output path.
    """
    path = Path(path)
    text = to_json(result)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    logger.info("Wrote %s (%.1f KB)", path, len(text.encode("utf-8")) / 1024)
    return path
