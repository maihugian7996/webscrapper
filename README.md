# Site-Agnostic Web Scraper

A robust, site-agnostic Python web scraping framework. Provide a target URL and a declarative YAML configuration defining CSS/XPath selectors, and the engine extracts, cleans, validates, and exports structured JSON data.

Built for both static HTML and JavaScript-heavy Single Page Applications (SPAs) with automatic JS detection, polite rate-limiting, comprehensive pagination support, and deep-page crawling.

---

## Features

- **Static & Dynamic Scraping**: High-performance static parsing via `requests` + `lxml`, with seamless fallback or forced rendering via **Playwright** (Chromium).
- **Declarative YAML Configurations**: No scraping logic is hardcoded. Define selectors, data cleaning pipelines, and pagination rules cleanly in YAML.
- **Multiple Pagination Strategies**: Supports next-page links, numeric URL patterns (`page={n}`), and dynamic infinite scrolling.
- **Deep & Detail Scraping**: Automatically visit extracted item links to enrich data from detail pages.
- **Responsible & Polite**: Built-in `robots.txt` compliance, randomized delays and jitter, rotating desktop User-Agents, and exponential backoff on HTTP 429/5xx.
- **Schema-Validated Output**: Pydantic v2 validation ensures data integrity, ISO-8601 timestamps, and atomic file writes. Graceful `Ctrl+C` interrupt handling preserves partial scrapes.
- **Utilities Included**: Course catalog fuzzy matching (`RapidFuzz`) and multi-format CSV conversion tools.

---

## Project Structure

```
webscrapper/
├── scraper/
│   ├── configs/                  # Site-specific YAML scraping configurations
│   │   ├── books_toscrape.yaml   # BooksToScrape pagination demo
│   │   ├── mit_ocw_6_0001.yaml   # MIT OCW course extraction config
│   │   ├── nptel.yaml            # NPTEL course scraping config
│   │   └── example.yaml          # Documented template config
│   ├── core/                     # Reusable scraper engine modules
│   │   ├── config.py             # Pydantic YAML schema & loader
│   │   ├── engine.py             # Scraper pipeline orchestrator
│   │   ├── fetcher.py            # Static (requests) & dynamic (Playwright) fetcher
│   │   ├── logging_setup.py      # Colored & file logging setup
│   │   ├── paginator.py          # Next-link, URL pattern & infinite scroll handlers
│   │   ├── parser.py             # CSS/XPath parser and data cleaner
│   │   └── writer.py             # Atomic JSON export and validation
│   ├── tests/                    # 125+ automated pytest suite (100% offline)
│   ├── export_to_csv.py          # Converter from scraped JSON to CSV
│   ├── match_courses_to_nptel.py # Fuzzy course matcher using RapidFuzz
│   ├── scrape_mit_ocw_full.py    # Recursive MIT OCW course & lecture scraper
│   ├── scrape.py                 # Primary CLI entrypoint
│   ├── requirements.txt          # Production dependencies
│   ├── requirements-dev.txt      # Testing and development dependencies
│   └── pytest.ini                # Pytest configuration
├── .env.example                  # Environment variable template
├── .gitignore                    # Git ignore rules for venv, caches, and outputs
└── README.md                     # Root project documentation
```

---

## Getting Started

### 1. Prerequisites

- Python 3.10 or higher (tested on Python 3.13)
- Git

### 2. Installation

Clone the repository and set up a virtual environment:

```bash
# Clone the repository
git clone https://github.com/maihugian7996/webscrapper.git
cd webscrapper/scraper

# Create and activate a virtual environment
python -m venv .venv
# On Windows (PowerShell):
.\.venv\Scripts\Activate.ps1
# On macOS/Linux:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Install Playwright browser binaries (for dynamic JS rendering)
playwright install chromium
```

---

## How to Run

Run the scraper using the CLI entry point `scrape.py`:

```bash
python scrape.py --config configs/<config-name>.yaml [options]
```

### Common CLI Options

| Flag | Description |
|---|---|
| `--config PATH` | **(Required)** Path to YAML configuration file. |
| `--url URL` | Start URL (overrides `start_urls` defined in the config). |
| `--out PATH` | Custom output JSON file path. |
| `--max-pages N` | Cap on the maximum listing pages to traverse. |
| `--render-js` | Force rendering with Playwright headless browser. |
| `--static-only` | Enforce static requests only (disables JS auto-detection). |
| `--delay SEC` | Base delay between HTTP requests (default: 1.0s). |
| `--jitter SEC` | Maximum random additional delay (default: 0.5s). |
| `--timeout SEC` | Per-request timeout in seconds (default: 20s). |
| `--ignore-robots`| Bypass `robots.txt` verification (*use with permission only*). |
| `--no-detail` | Skip extracting detail pages even if configured. |
| `-v`, `--verbose`| Enable verbose debug logs in console. |

### Examples

**1. Scrape a static paginated catalog:**
```bash
python scrape.py --config configs/books_toscrape.yaml --max-pages 3 --out output/books.json
```

**2. Scrape with dynamic JavaScript rendering:**
```bash
python scrape.py --url "https://example.com/catalog" --config configs/example.yaml --render-js
```

**3. Convert scraped JSON to CSV:**
```bash
python export_to_csv.py
```

**4. Run course matching with fuzzy search:**
```bash
python match_courses_to_nptel.py
```

---

## Output Format

Output is written to a structured JSON file adhering to the Pydantic schema:

```json
{
  "metadata": {
    "source_url": "https://example.com/courses",
    "scraped_at": "2026-11-09T16:00:00Z",
    "pages_scraped": 3,
    "total_items": 42,
    "config_used": "configs/example.yaml",
    "scraper_version": "1.0.0"
  },
  "items": [
    {
      "title": "Introduction to Computer Science",
      "url": "https://example.com/courses/cs101",
      "instructor": "Dr. Smith",
      "rating": 4.8
    }
  ],
  "errors": []
}
```

### Using Scraped Data in Python

```python
import json
import pandas as pd

# Load scraped JSON
with open("output/results.json", "r", encoding="utf-8") as f:
    data = json.load(f)

# Convert items to a Pandas DataFrame
df = pd.DataFrame(data["items"])
print(df.head())
```

---

## Troubleshooting

- **`Playwright Host Executable Missing`**:
  Run `playwright install chromium` inside your active virtual environment.
- **HTTP 403 Forbidden / Anti-Bot Detection**:
  Some sites block automated user-agents. Ensure `--render-js` is enabled to execute genuine browser headers, or customize `request.headers` in your YAML config.
- **Missing Elements on Dynamic Pages**:
  If selectors return empty results, the page may still be loading via AJAX. Specify `wait_for: "selector"` in the YAML config or use `--render-js`.
- **Target Site Disallows Scraping (`robots.txt`)**:
  The scraper exits with code `2` if the URL is disallowed by `robots.txt`. If you have permission from the site owner, pass `--ignore-robots`.

---

## Running Tests

The test suite runs 100% offline using synthetic HTML fixtures and mocked HTTP requests:

```bash
cd scraper
python -m pytest
```

---

## License

This project is licensed under the MIT License.
