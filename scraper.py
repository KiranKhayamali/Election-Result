"""
Election Results Web Scraper

Scrapes live election results from configurable sources using
BeautifulSoup and requests.  Falls back to cached data when the
remote source is unavailable so the web app always has something
to display.

Two-tier data source strategy
-------------------------------
Primary  – Election Commission of Nepal portal (result.election.gov.np)
Secondary – Nepali news outlets (Ekantipur, Online Khabar, Setopati)
"""

import logging
import re
import threading
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default target: Election Commission of Nepal results portal (2082 BS).
# Override by setting SCRAPE_URL in the environment before starting the app.
DEFAULT_SCRAPE_URL = "https://result.election.gov.np/"

REQUEST_TIMEOUT = 15  # seconds
NEWS_REQUEST_TIMEOUT = 10  # seconds – shorter for secondary sources
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Secondary news sources (Nepali online outlets)
# ---------------------------------------------------------------------------

NEWS_SOURCES: list[dict] = [
    {
        "name": "Ekantipur",
        "url": "https://ekantipur.com/",
    },
    {
        "name": "Online Khabar",
        "url": "https://www.onlinekhabar.com/",
    },
    {
        "name": "Setopati",
        "url": "https://www.setopati.com/",
    },
]

# ---------------------------------------------------------------------------
# Shared state (protected by a lock so Flask threads read safely)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_cache: dict[str, Any] = {
    "results": [],
    "summary": {},
    "source_url": DEFAULT_SCRAPE_URL,
    "last_updated": None,
    "status": "Initialising…",
    "error": None,
}
# Monotonically increasing counter – incremented whenever results change so
# that SSE listeners can detect updates without comparing full payloads.
_version: int = 0

# ---------------------------------------------------------------------------
# News cache (secondary sources)
# ---------------------------------------------------------------------------

_news_lock = threading.Lock()
_news_cache: dict[str, Any] = {
    "articles": [],
    "sources": [],
    "last_updated": None,
    "error": None,
}
_news_version: int = 0


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def get_cached_data() -> dict[str, Any]:
    """Return a snapshot of the most recently scraped data."""
    with _lock:
        return dict(_cache)


def get_version() -> int:
    """Return the current data version counter."""
    with _lock:
        return _version


def get_news_data() -> dict[str, Any]:
    """Return a snapshot of the most recently scraped news articles."""
    with _news_lock:
        return dict(_news_cache)


def get_news_version() -> int:
    """Return the current news data version counter."""
    with _news_lock:
        return _news_version


def add_result(row: dict) -> None:
    """Append *row* to the cached results and bump the version counter."""
    global _version
    with _lock:
        _cache["results"].append(row)
        _cache["last_updated"] = datetime.now().isoformat(timespec="seconds")
        _version += 1


def remove_result(index: int) -> bool:
    """
    Remove the result at *index* from the cache.

    Returns ``True`` on success, ``False`` when the index is out of range.
    """
    global _version
    with _lock:
        results = _cache["results"]
        if 0 <= index < len(results):
            results.pop(index)
            _cache["last_updated"] = datetime.now().isoformat(timespec="seconds")
            _version += 1
            return True
        return False


def scrape_and_update(url: str = DEFAULT_SCRAPE_URL) -> None:
    """
    Scrape *url* and update the shared cache.

    The function tries several well-known table/list patterns found on
    common election-results pages.  When a recognised structure is found
    the parsed rows are stored; otherwise a raw-text fallback is used so
    the caller always receives *something*.
    """
    global _version
    logger.info("Scraping %s …", url)
    headers = {"User-Agent": USER_AGENT}

    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Fetch failed: %s", exc)
        with _lock:
            _cache["status"] = "Fetch failed – showing last known data"
            _cache["error"] = str(exc)
            _cache["last_updated"] = datetime.now().isoformat(timespec="seconds")
        return

    soup = BeautifulSoup(response.text, "lxml")

    results = (
        _parse_nepal_results(soup)
        or _parse_results_table(soup)
        or _parse_results_list(soup)
    )
    summary = _parse_summary(soup)

    with _lock:
        _cache["results"] = results
        _cache["summary"] = summary
        _cache["source_url"] = url
        _cache["last_updated"] = datetime.now().isoformat(timespec="seconds")
        _cache["status"] = "Live" if results else "No structured data found"
        _cache["error"] = None
        _version += 1

    logger.info("Cache updated: %d rows, status=%s", len(results), _cache["status"])


# ---------------------------------------------------------------------------
# Secondary news-source scraping
# ---------------------------------------------------------------------------

# Article container selectors tried in order
_ARTICLE_SELECTORS = [
    "article",
    ".article",
    ".news-item",
    ".post",
    ".story",
    ".item",
    "[class*='article']",
    "[class*='news']",
    "[class*='story']",
]

# Phrases that indicate navigation / UI links rather than news headlines
_SKIP_PHRASES = frozenset([
    "home", "about", "contact", "login", "register", "subscribe",
    "advertisement", "cookie", "privacy", "terms", "sitemap",
])


def scrape_news_sources() -> None:
    """
    Fetch the latest headlines from secondary Nepali news outlets.

    Polls each source in ``NEWS_SOURCES``, parses article titles and links,
    and stores the aggregated list in ``_news_cache``.
    """
    global _news_version
    logger.info("Scraping secondary news sources…")
    all_articles: list[dict] = []
    source_statuses: list[dict] = []
    errors: list[str] = []

    for source in NEWS_SOURCES:
        try:
            articles = _scrape_single_news_source(source)
            all_articles.extend(articles)
            source_statuses.append({
                "name": source["name"],
                "url": source["url"],
                "status": "ok",
                "count": len(articles),
            })
        except Exception as exc:  # noqa: BLE001 – intentional broad catch
            logger.warning("News scrape failed for %s: %s", source["name"], exc)
            errors.append(f"{source['name']}: {exc}")
            source_statuses.append({
                "name": source["name"],
                "url": source["url"],
                "status": "error",
                "count": 0,
            })

    with _news_lock:
        _news_cache["articles"] = all_articles[:60]
        _news_cache["sources"] = source_statuses
        _news_cache["last_updated"] = datetime.now().isoformat(timespec="seconds")
        _news_cache["error"] = "; ".join(errors) if errors else None
        _news_version += 1

    logger.info(
        "News cache updated: %d articles from %d sources",
        len(all_articles),
        len(NEWS_SOURCES),
    )


def _scrape_single_news_source(source: dict) -> list[dict]:
    """Fetch and parse articles from one news source entry.

    Raises ``requests.RequestException`` on network failure so that
    ``scrape_news_sources`` can record the error status for that source.
    """
    hdrs = {"User-Agent": USER_AGENT, "Accept-Language": "ne,en;q=0.9"}
    resp = requests.get(source["url"], headers=hdrs, timeout=NEWS_REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    return _parse_news_articles(soup, source["name"], source["url"])


def _parse_news_articles(
    soup: BeautifulSoup, source_name: str, base_url: str
) -> list[dict]:
    """
    Extract article titles and links from a parsed HTML page.

    Tries structured article containers first; falls back to scanning all
    anchor tags for substantial headline text.
    """
    articles: list[dict] = []
    seen: set[str] = set()
    parsed_base = urlparse(base_url)

    def _make_absolute(link: str) -> str:
        if link.startswith("http"):
            return link
        if link.startswith("/"):
            return f"{parsed_base.scheme}://{parsed_base.netloc}{link}"
        return base_url

    def _add(title: str, link: str, timestamp: str = "") -> None:
        norm = title.lower().strip()
        if norm in seen or len(title) < 15:
            return
        if any(skip in norm for skip in _SKIP_PHRASES):
            return
        seen.add(norm)
        articles.append({
            "title": title[:140],
            "link": _make_absolute(link),
            "source": source_name,
            "timestamp": timestamp,
        })

    # Try structured selectors
    for selector in _ARTICLE_SELECTORS:
        items = soup.select(selector)
        if len(items) < 3:
            continue
        for item in items[:25]:
            heading = item.find(["h1", "h2", "h3", "h4"])
            title = heading.get_text(strip=True) if heading else ""
            if not title:
                a_tag = item.find("a", href=True)
                title = a_tag.get_text(strip=True) if a_tag else ""
            if not title:
                continue
            a_tag = item.find("a", href=True)
            link = a_tag["href"] if a_tag else base_url
            time_tag = item.find("time")
            ts = time_tag.get_text(strip=True) if time_tag else ""
            _add(title, link, ts)
        if articles:
            break

    # Fallback: scan significant anchor texts
    if not articles:
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            if len(text) > 35:
                _add(text, a["href"])
            if len(articles) >= 20:
                break

    return articles[:20]





# Column synonyms used on the Nepal Election Commission portal.
# Keys are canonical names; values are lists of header texts (lowercased)
# that map to that key.
_NEPAL_COL_SYNONYMS: dict[str, list[str]] = {
    "constituency": [
        "constituency",
        "निर्वाचन क्षेत्र",
        "district",
        "जिल्ला",
        "constituency no",
        "निर्वाचन क्षेत्र नं.",
    ],
    "candidate": [
        "candidate",
        "candidate name",
        "उम्मेदवारको नाम",
        "उम्मेदवार",
        "name",
    ],
    "party": [
        "party",
        "party name",
        "दलको नाम",
        "दल",
        "political party",
    ],
    "votes": [
        "votes",
        "total votes",
        "vote count",
        "मत",
        "कुल मत",
        "votes received",
        "प्राप्त मत",
    ],
    "status": [
        "status",
        "result",
        "निर्वाचित",
        "leading",
        "winning",
        "नतिजा",
    ],
}


def _parse_nepal_results(soup: BeautifulSoup) -> list[dict]:
    """
    Parse election-result tables from the Nepal Election Commission portal
    (result.election.gov.np).

    The portal renders one or more ``<table>`` elements whose headers contain
    Nepali or English column names recognised in ``_NEPAL_COL_SYNONYMS``.
    Returns an empty list when none of the tables look like a results table.
    """
    rows: list[dict] = []

    for table in soup.find_all("table"):
        # Collect header cells from <th> or the first <tr>
        header_cells = table.find_all("th")
        if header_cells:
            raw_headers = [h.get_text(strip=True) for h in header_cells]
        else:
            first_tr = table.find("tr")
            if not first_tr:
                continue
            raw_headers = [td.get_text(strip=True) for td in first_tr.find_all("td")]

        headers_lower = [h.lower() for h in raw_headers]

        # Build a column-index → canonical-key map using Nepal synonyms
        col_map: dict[int, str] = {}
        for idx, header in enumerate(headers_lower):
            for canonical, synonyms in _NEPAL_COL_SYNONYMS.items():
                if header in synonyms:
                    col_map[idx] = canonical
                    break
            else:
                col_map[idx] = raw_headers[idx]

        # Only treat the table as a results table when at least one
        # recognised column (candidate, party, or votes) is present.
        recognised = {v for v in col_map.values() if v in ("candidate", "party", "votes")}
        if not recognised:
            continue

        data_trs = table.find_all("tr")[1:]
        for tr in data_trs:
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if not any(cells):
                continue
            row: dict[str, str] = {}
            for idx, value in enumerate(cells):
                if not value:
                    continue
                key = col_map.get(idx, f"col_{idx}")
                row[key] = value
            if row:
                rows.append(row)

        if rows:
            logger.info("Nepal-specific parser found %d rows.", len(rows))
            return rows

    return rows


def _parse_results_table(soup: BeautifulSoup) -> list[dict]:
    """Try to extract candidate/party rows from an HTML <table>."""
    rows: list[dict] = []

    # Look for the most data-rich table on the page
    tables = soup.find_all("table")
    best_table = None
    best_row_count = 0
    for table in tables:
        trs = table.find_all("tr")
        if len(trs) > best_row_count:
            best_row_count = len(trs)
            best_table = table

    if best_table is None or best_row_count < 2:
        return rows

    # Extract headers from <th> or first <tr>
    header_cells = best_table.find_all("th")
    if header_cells:
        headers = [h.get_text(strip=True).lower() for h in header_cells]
    else:
        first_tr = best_table.find("tr")
        headers = [
            td.get_text(strip=True).lower()
            for td in (first_tr.find_all("td") if first_tr else [])
        ]

    # Normalise header names to well-known keys
    col_map = _build_col_map(headers)

    # Parse data rows
    data_trs = best_table.find_all("tr")[1:] if header_cells else best_table.find_all("tr")[1:]
    for tr in data_trs:
        cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
        if not any(cells):
            continue
        row = _map_cells(cells, col_map, headers)
        if row:
            rows.append(row)

    return rows


def _parse_results_list(soup: BeautifulSoup) -> list[dict]:
    """Fallback: scrape candidate/party names and vote counts from plain text."""
    rows: list[dict] = []
    # Pattern: "Party Name ... 12,345 votes" or similar
    pattern = re.compile(
        r"([A-Za-z][A-Za-z .\-']{2,50})\s+[:\-]?\s*([\d,]+)\s*(?:votes?|seats?)?",
        re.IGNORECASE,
    )
    text = soup.get_text(separator="\n")
    seen: set[str] = set()
    for match in pattern.finditer(text):
        name = match.group(1).strip()
        count = match.group(2).replace(",", "")
        key = name.lower()
        if key in seen or not count.isdigit():
            continue
        seen.add(key)
        rows.append({"candidate_party": name, "votes_seats": count})
        if len(rows) >= 50:
            break
    return rows


def _parse_summary(soup: BeautifulSoup) -> dict:
    """Extract headline summary figures (total seats, declared, etc.)."""
    summary: dict[str, str] = {}
    # Look for common summary elements: <h1>-<h4>, <strong>, <b>
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "strong", "b"]):
        text = tag.get_text(strip=True)
        if re.search(r"(seat|result|declared|total|winner|leading)", text, re.I):
            summary[text[:60]] = text
            if len(summary) >= 5:
                break
    return summary


# ---------------------------------------------------------------------------
# Column-name normalisation helpers
# ---------------------------------------------------------------------------

_KNOWN_KEYS = {
    "candidate": ["candidate", "name", "candidate name", "winner"],
    "party": ["party", "political party", "party name"],
    "constituency": ["constituency", "state", "district", "seat"],
    "votes": ["votes", "vote count", "total votes", "vote", "seats", "seat count"],
    "status": ["status", "result", "winning", "leading"],
}


def _build_col_map(headers: list[str]) -> dict[int, str]:
    """Map column indices to normalised key names."""
    col_map: dict[int, str] = {}
    for idx, header in enumerate(headers):
        for key, synonyms in _KNOWN_KEYS.items():
            if header in synonyms:
                col_map[idx] = key
                break
        else:
            col_map[idx] = header  # keep original if not recognised
    return col_map


def _map_cells(cells: list[str], col_map: dict[int, str], headers: list[str]) -> dict:
    """Build a dict from a row's cells using the column map."""
    row: dict[str, str] = {}
    for idx, value in enumerate(cells):
        if not value:
            continue
        key = col_map.get(idx, headers[idx] if idx < len(headers) else f"col_{idx}")
        row[key] = value
    return row
