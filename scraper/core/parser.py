"""Selector-based extraction and value cleaning.

A single ``lxml`` tree supports both CSS selectors (via ``cssselect``) and
XPath expressions, so configs can mix them freely:

* ``"div.item h2 a"``           -> CSS (default)
* ``"xpath://div[@class='x']"`` -> XPath (explicit prefix)
* ``"//div"`` / ``"./span"``    -> XPath (auto-detected)
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from typing import Any, Iterable
from urllib.parse import urljoin

import lxml.html
from lxml import etree
from lxml.cssselect import CSSSelector
from cssselect import SelectorError as CssSelectorError

from core.config import CleanStep, FieldSpec, ItemSpec

logger = logging.getLogger(__name__)

#: Attributes whose values are URLs and get resolved to absolute form.
URL_ATTRIBUTES: frozenset[str] = frozenset(
    {"href", "src", "action", "poster", "data-src", "data-href", "data-url", "data-original", "cite"}
)

_TRUE_STRINGS = {"true", "yes", "y", "1", "on"}
_FALSE_STRINGS = {"false", "no", "n", "0", "off", ""}


class SelectorError(ValueError):
    """Raised when a CSS or XPath selector is syntactically invalid."""


# --------------------------------------------------------------------------- #
# Document handling
# --------------------------------------------------------------------------- #


def parse_html(html: str, url: str) -> lxml.html.HtmlElement:
    """Parse HTML into an lxml tree and remember the effective base URL.

    The base URL honours a ``<base href>`` tag if present. It is stored on
    the tree as ``doc.base_url`` (lxml's native attribute).

    Args:
        html: Raw HTML text (may be empty).
        url: URL the HTML was fetched from.
    """
    if not html or not html.strip():
        html = "<html><head></head><body></body></html>"
    try:
        doc = lxml.html.document_fromstring(html, base_url=url)
    except (etree.ParserError, ValueError):
        # ValueError: str with XML encoding declaration -> parse bytes instead.
        doc = lxml.html.document_fromstring(html.encode("utf-8"), base_url=url)
    base_nodes = doc.xpath("//base[@href]")
    if base_nodes:
        doc.getroottree().docinfo.URL = urljoin(url, base_nodes[0].get("href", "").strip())
    return doc


def base_url_of(node: Any, fallback: str = "") -> str:
    """Return the base URL associated with an lxml node's document."""
    try:
        return node.getroottree().docinfo.URL or fallback
    except AttributeError:
        return fallback


def body_text_length(doc: lxml.html.HtmlElement) -> int:
    """Length of visible-ish body text (scripts/styles excluded)."""
    bodies = doc.xpath("//body")
    if not bodies:
        return 0
    text = " ".join(
        t for t in bodies[0].xpath(".//text()[not(ancestor::script) and not(ancestor::style)]")
    )
    return len(" ".join(text.split()))


# --------------------------------------------------------------------------- #
# Selectors
# --------------------------------------------------------------------------- #


def is_xpath(selector: str) -> bool:
    """Return True if ``selector`` should be treated as XPath."""
    s = selector.lstrip()
    return s.startswith(("xpath:", "/", "./", "(", "../"))


@lru_cache(maxsize=512)
def _compile_css(selector: str) -> CSSSelector:
    try:
        return CSSSelector(selector)
    except CssSelectorError as exc:
        raise SelectorError(f"Invalid CSS selector {selector!r}: {exc}") from exc


@lru_cache(maxsize=512)
def _compile_xpath(expr: str) -> etree.XPath:
    try:
        return etree.XPath(expr)
    except etree.XPathSyntaxError as exc:
        raise SelectorError(f"Invalid XPath {expr!r}: {exc}") from exc


def select(node: Any, selector: str) -> list[Any]:
    """Run a CSS or XPath selector relative to ``node``.

    Returns:
        A list of elements, or strings when an XPath selects text/attributes.
    """
    s = selector.strip()
    if is_xpath(s):
        expr = s[len("xpath:"):].strip() if s.startswith("xpath:") else s
        result = _compile_xpath(expr)(node)
        if isinstance(result, list):
            return result
        # Scalar XPath results (string(), count() ...) become a single value.
        return [] if result is None or result == "" else [str(result) if not isinstance(result, bool) else result]
    return _compile_css(s)(node)


# --------------------------------------------------------------------------- #
# Value extraction
# --------------------------------------------------------------------------- #


def _collapse(text: str) -> str:
    return " ".join(text.split())


def extract_value(node: Any, attr: str, base_url: str) -> Any:
    """Extract ``attr`` from a selected node.

    Args:
        node: An lxml element, or a string returned by an XPath expression.
        attr: ``text`` | ``raw_text`` | ``html`` | ``inner_html`` | attribute name.
        base_url: Used to absolutise URL attributes.
    """
    if not isinstance(node, etree._Element):
        # XPath text()/@attr results are already strings.
        value = str(node)
        if attr in URL_ATTRIBUTES:
            return urljoin(base_url, value.strip())
        return _collapse(value) if attr == "text" else value

    if attr == "text":
        return _collapse(node.text_content())
    if attr == "raw_text":
        return node.text_content()
    if attr == "html":
        return lxml.html.tostring(node, encoding="unicode").strip()
    if attr == "inner_html":
        parts = [node.text or ""] + [lxml.html.tostring(c, encoding="unicode") for c in node]
        return "".join(parts).strip()

    value = node.get(attr)
    if value is None:
        return None
    if attr in URL_ATTRIBUTES:
        value = value.strip()
        if not value or value.startswith(("javascript:", "#")):
            return value or None
        return urljoin(base_url, value)
    return value


# --------------------------------------------------------------------------- #
# Cleaning
# --------------------------------------------------------------------------- #


def _cast(value: Any, target: str) -> Any:
    if target == "str":
        return str(value)
    if target == "bool":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in _TRUE_STRINGS:
            return True
        if s in _FALSE_STRINGS:
            return False
        raise ValueError(f"cannot interpret {value!r} as bool")
    s = str(value).strip().replace(",", "").replace("\u00a0", "").replace(" ", "")
    if target == "int":
        try:
            return int(s)
        except ValueError:
            f = float(s)
            if not f.is_integer():
                raise ValueError(f"{value!r} is not an integer") from None
            return int(f)
    if target == "float":
        return float(s)
    raise ValueError(f"unknown cast target {target!r}")


def _regex(value: str, arg: Any) -> str | None:
    if isinstance(arg, dict):
        pattern, group = arg["pattern"], arg.get("group")
        flags = re.IGNORECASE if arg.get("ignore_case") else 0
    else:
        pattern, group, flags = arg, None, 0
    match = re.search(pattern, value, flags)
    if not match:
        return None
    if group is None:
        group = 1 if match.re.groups else 0
    return match.group(group)


def apply_clean(value: Any, steps: Iterable[CleanStep], field_name: str = "?") -> Any:
    """Run ``value`` through the cleaning pipeline.

    ``None`` short-circuits the pipeline. A failed type cast logs a warning
    and yields ``None`` (so the field default applies) instead of crashing.
    """
    for step in steps:
        if value is None:
            return None
        if isinstance(step, str):
            name, arg = step, None
        else:
            name, arg = next(iter(step.items()))
        try:
            if name == "strip":
                value = str(value).strip(arg) if arg is not None else str(value).strip()
            elif name == "collapse_whitespace":
                value = _collapse(str(value))
            elif name == "lower":
                value = str(value).lower()
            elif name == "upper":
                value = str(value).upper()
            elif name == "title":
                value = str(value).title()
            elif name == "regex":
                value = _regex(str(value), arg)
            elif name == "replace":
                value = str(value).replace(str(arg["from"]), str(arg.get("to", "")))
            elif name == "prefix":
                value = f"{arg}{value}"
            elif name == "suffix":
                value = f"{value}{arg}"
            elif name == "map":
                key = value if value in arg else str(value)
                value = arg.get(key, value)
            elif name == "type":
                value = _cast(value, arg)
        except (ValueError, TypeError) as exc:
            logger.warning("Cleaning step %r failed for field '%s' (value=%r): %s", name, field_name, value, exc)
            return None
    return value


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value == "") or value == []


# --------------------------------------------------------------------------- #
# Field / item extraction
# --------------------------------------------------------------------------- #


def extract_field(scope: Any, name: str, spec: FieldSpec, base_url: str) -> Any:
    """Extract and clean a single field relative to ``scope``."""
    nodes = [scope] if spec.selector is None else select(scope, spec.selector)

    if spec.multiple:
        values = []
        for node in nodes:
            v = apply_clean(extract_value(node, spec.attr, base_url), spec.clean, name)
            if not _is_empty(v):
                values.append(v)
        if values:
            return values
        return spec.default if spec.default is not None else []

    value = None
    for node in nodes:
        value = extract_value(node, spec.attr, base_url)
        if not _is_empty(value):
            break
    value = apply_clean(value, spec.clean, name)
    return spec.default if _is_empty(value) else value


def extract_fields(scope: Any, fields: dict[str, FieldSpec], base_url: str) -> dict[str, Any]:
    """Extract every field in ``fields`` relative to ``scope``."""
    return {name: extract_field(scope, name, spec, base_url) for name, spec in fields.items()}


def missing_required(item: dict[str, Any], fields: dict[str, FieldSpec]) -> list[str]:
    """Names of required fields that are empty in ``item``."""
    return [n for n, spec in fields.items() if spec.required and _is_empty(item.get(n))]


def extract_items(
    doc: lxml.html.HtmlElement, item_spec: ItemSpec, base_url: str | None = None
) -> tuple[list[dict[str, Any]], int]:
    """Extract all items on a listing page.

    Returns:
        ``(items, dropped)`` where ``dropped`` counts items rejected for
        missing required fields.
    """
    base = base_url or base_url_of(doc)
    containers = select(doc, item_spec.container)
    items: list[dict[str, Any]] = []
    dropped = 0
    for container in containers:
        if not isinstance(container, etree._Element):
            logger.debug("Container selector returned a non-element value; skipping")
            continue
        item = extract_fields(container, item_spec.fields, base)
        missing = missing_required(item, item_spec.fields)
        if missing:
            dropped += 1
            logger.debug("Dropping item missing required field(s) %s: %s", missing, item)
            continue
        items.append(item)
    return items, dropped


def count_containers(doc: lxml.html.HtmlElement, item_spec: ItemSpec) -> int:
    """Number of nodes matched by the item container selector."""
    return len(select(doc, item_spec.container))


def item_key(item: dict[str, Any], fields: list[str] | None) -> str:
    """Stable key used for de-duplication."""
    data: Any = {f: item.get(f) for f in fields} if fields else item
    return json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
