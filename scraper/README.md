# Site-Agnostic Web Scraper

A reusable Python scraping framework: give it a **URL** and a small **YAML config** describing what to extract, and it writes a validated, structured **JSON** file. No website is hardcoded — every site-specific detail lives in a config file you choose at runtime.

- Static pages via `requests` + `lxml` (CSS **and** XPath selectors), decoding via BeautifulSoup's `UnicodeDammit`
- JS-rendered pages via **Playwright** (`--render-js`, or auto-detected when the static fetch finds no items)
- Pagination: next-link, page-number URL patterns, infinite scroll
- Optional detail-page scraping, URL absolutisation, de-duplication, cleaning pipeline
- Responsible by default: robots.txt, delay + jitter, rotating User-Agents, retries with exponential backoff
- Pydantic-validated output, atomic writes, partial results saved on Ctrl+C

---

## 1. Install

Requires Python 3.10+ (tested on 3.13).

```powershell
cd scraper
python -m venv .venv
.\.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt      # add -dev for tests: pip install -r requirements-dev.txt
playwright install chromium          # browser binaries for --render-js / auto-detect / infinite scroll
```

Playwright is only launched when needed; static sites work without the browser download.

## 2. Usage

```powershell
python scrape.py --url <URL> --config configs/<site>.yaml --out output/<name>.json
```

| Flag | Meaning |
|---|---|
| `--config PATH` | **Required.** Site YAML config. |
| `--url URL` | Start URL (overrides `start_urls` in the config). |
| `--out PATH` | Output file (default `output/<config name>_<timestamp>.json`). |
| `--max-pages N` | Max listing pages (overrides config). |
| `--delay S` / `--jitter S` | Base delay and random extra delay between requests. |
| `--timeout S` / `--retries N` | Per-request timeout; retries on 429/5xx/timeouts. |
| `--render-js` | Always render with Playwright. |
| `--static-only` | Never use Playwright (disables JS auto-detect). |
| `--headless` / `--no-headless` | Show or hide the browser window (default headless). |
| `--ignore-robots` | Skip robots.txt checks. **Only with the site owner's permission.** |
| `--no-detail` | Skip detail pages even if configured. |
| `--log-file PATH` | Also write a DEBUG log to a file. |
| `--verbose`, `-v` | DEBUG-level console output. |

Precedence: **CLI flags > YAML config > built-in defaults.**

**Exit codes:** `0` success · `1` config/validation/output error · `2` blocked by robots.txt (no file written) · `130` interrupted (partial results written).

### Examples

```powershell
# Static site, 3 pages, slower and politer
python scrape.py --config configs/mysite.yaml --out output/mysite.json --max-pages 3 --delay 2

# Force JS rendering and watch the browser
python scrape.py --url https://example.com/app --config configs/myapp.yaml --render-js --no-headless

# Listing only, verbose logs saved to a file
python scrape.py --config configs/mysite.yaml --no-detail -v --log-file output/run.log
```

## 3. Output format

```json
{
  "metadata": {
    "source_url": "https://example.com/list",
    "scraped_at": "2026-11-08T21:30:00Z",
    "pages_scraped": 3,
    "total_items": 60,
    "config_used": "configs/mysite.yaml",
    "scraper_version": "1.0.0"
  },
  "items": [ { "title": "...", "url": "https://...", "price": 12.5 } ],
  "errors": [ { "url": "https://...", "error": "HTTP 404", "timestamp": "2026-11-08T21:30:05Z" } ]
}
```

UTF-8, `indent=2`, `ensure_ascii=False`. The document is validated with Pydantic (including `total_items == len(items)` and ISO-8601 timestamps), written to a temp file and atomically renamed into place. If you press **Ctrl+C**, everything scraped so far is saved and an `"interrupted"` entry is added to `errors`.

## 4. Writing a config for a new site

Copy the template and edit it:

```powershell
copy configs\example.yaml configs\mysite.yaml
```

### Find selectors
1. Open the listing page in your browser → right-click an item → **Inspect**.
2. Find the element that wraps **one** item (card/row/`<li>`). That is `item.container`.
3. For each field, find a selector **relative to the container**. Test in DevTools console:
   `document.querySelectorAll("div.card")` / `$x("//div[@class='card']")`.
4. Find the "next page" link (or note the `?page=N` URL pattern).
5. Check `View Source` (Ctrl+U): if your items are **not** in the raw HTML, the page is JS-rendered → set `render_js: true` (or leave `auto`).

### Config reference

```yaml
name: mysite
start_urls: ["https://example.com/list"]
render_js: auto            # auto | true | false
wait_for: null             # CSS selector Playwright waits for before reading the page

item:
  container: "div.card"
  fields:
    title: "h2"                         # shorthand = {selector: "h2", attr: text}
    url:   {selector: "a", attr: href}  # relative URLs -> absolute
    price:
      selector: "xpath:.//span[@class='price']"
      clean: [{regex: '([\d.,]+)'}, {type: float}]
      default: null
      required: false                   # true -> drop items where it's empty
    tags:  {selector: "li.tag", multiple: true}

dedupe_on: [url]           # omit to dedupe on the full item

pagination:
  type: next_link          # none | next_link | url_pattern | infinite_scroll
  next_selector: "a.next"
  # pattern: "https://example.com/list?page={page}"; start: 1; step: 1
  # scroll_pause: 1.5; max_scrolls: 20

max_pages: 10

detail:                    # optional — selectors are relative to the detail document
  link_field: url
  max_items: null
  fields:
    description: "div.description"

request:
  delay: 1.0
  jitter: 0.5
  timeout: 20
  retries: 3
  backoff_base: 1.0
  headers: {}
  # user_agents: ["..."]
```

**Selectors** — CSS by default. XPath with prefix `xpath:` or when it starts with `/`, `./`, `../`, `(`. XPath may return text/attributes directly (e.g. `.//a/@href`).

**`attr`** — `text` (trimmed, whitespace-collapsed), `raw_text`, `html`, `inner_html`, or any attribute (`href`, `src`, `title`, `data-id`, `class`...). `href`, `src`, `action`, `poster`, `data-src`, `data-href`, `data-url`, `data-original`, `cite` are resolved to absolute URLs (honouring `<base href>`).

**`clean` steps** (applied in order; `None` short-circuits; an empty result falls back to `default`):

| Step | Example | Effect |
|---|---|---|
| `strip` / `{strip: "chars"}` | `{strip: "-"}` | Trim whitespace / given chars |
| `collapse_whitespace` | | Collapse runs of whitespace |
| `lower` / `upper` / `title` | | Change case |
| `regex` | `{regex: '(\d+)'}` or `{regex: {pattern: '...', group: 2, ignore_case: true}}` | First capture group (or whole match); `None` if no match |
| `replace` | `{replace: {from: ",", to: ""}}` | Substring replace |
| `prefix` / `suffix` | `{prefix: "https://"}` | Add text |
| `map` | `{map: {One: 1, Two: 2}}` | Look up value (unmapped values pass through) |
| `type` | `{type: int}` | `str` / `int` / `float` / `bool` (`1,234` → `1234`). Failures log a warning and use `default`. |

**Pagination**
- `next_link`: follows `next_selector` (on `<a>`, a wrapper element, or an XPath `@href`).
- `url_pattern`: the start URL is page `start`; next pages are `start+step`, ... Stops at a page with no new items or a 404.
- `infinite_scroll`: Playwright scrolls until the page height stops growing or `max_scrolls` is hit.
- A visited-URL guard prevents loops; `max_pages` caps the run.

**JS auto-detect (`render_js: auto`)** — if the first static fetch finds zero items, the page is re-fetched with Playwright; if that finds items, the rest of the run uses the browser.

## 5. Responsible scraping

- **robots.txt** is checked for every URL (RFC 9309: 4xx → allowed, 5xx → disallowed). A disallowed start URL stops the run with exit code 2 and a clear message. `Crawl-delay` is honoured if larger than your delay. `--ignore-robots` exists for sites you own or have permission to scrape.
- **Delay + jitter** between every request (static and browser).
- **Rotating, realistic desktop User-Agents** (override with `request.user_agents`).
- **Retries** with exponential backoff (`backoff_base * 2^attempt` + jitter, capped) on 429, 5xx, timeouts and connection errors; `Retry-After` is respected. Non-retryable 4xx fail immediately.
- **Logging** of every request attempt (URL, status, size, time), retry and failure; failures are also recorded in the output's `errors`.

Always check a site's Terms of Service, prefer official APIs when available, and keep request rates low.

## 6. Project layout

```
scraper/
  scrape.py              CLI entry point
  core/config.py         YAML config models (Pydantic) + loader
  core/fetcher.py        requests + Playwright, robots.txt, retries, UA rotation, rate limiting
  core/parser.py         CSS/XPath extraction + cleaning pipeline
  core/paginator.py      next-link / URL-pattern / infinite-scroll strategies
  core/engine.py         orchestration, dedupe, detail pages, JS auto-detect, Ctrl+C handling
  core/writer.py         output schema validation + atomic JSON writing
  core/logging_setup.py  logging configuration
  configs/example.yaml   annotated TEMPLATE (placeholder selectors)
  tests/                 pytest suite using saved HTML fixtures (no network)
```

## 7. Tests

```powershell
pip install -r requirements-dev.txt
python -m pytest
```

The suite uses synthetic HTML fixtures, `responses` for HTTP mocking and an in-memory fake fetcher — it never touches the network or launches a browser.
