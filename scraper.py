"""
Election Results Web Scraper

Scrapes live election results from configurable sources using
BeautifulSoup and requests.  Falls back to cached data when the
remote source is unavailable so the web app always has something
to display.
"""

import logging
import re
import threading
from datetime import datetime
from typing import Any

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default target: Election Commission of Nepal results portal.
# Override by setting SCRAPE_URL in the environment before starting the app.
DEFAULT_SCRAPE_URL = "https://result.election.gov.np/"

REQUEST_TIMEOUT = 15  # seconds
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

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


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def get_cached_data() -> dict[str, Any]:
    """Return a snapshot of the most recently scraped data."""
    with _lock:
        return dict(_cache)


def scrape_and_update(url: str = DEFAULT_SCRAPE_URL) -> None:
    """
    Scrape *url* and update the shared cache.

    The function tries several well-known table/list patterns found on
    common election-results pages (including Nepal's election portal).
    When a recognised structure is found the parsed rows are stored;
    otherwise a raw-text fallback is used so the caller always receives
    *something*.
    """
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

    results = _parse_nepal_results(soup) or _parse_results_table(soup) or _parse_results_list(soup)
    summary = _parse_summary(soup)

    with _lock:
        _cache["results"] = results
        _cache["summary"] = summary
        _cache["source_url"] = url
        _cache["last_updated"] = datetime.now().isoformat(timespec="seconds")
        _cache["status"] = "Live" if results else "No structured data found"
        _cache["error"] = None

    logger.info("Cache updated: %d rows, status=%s", len(results), _cache["status"])


# ---------------------------------------------------------------------------
# Private parsers
# ---------------------------------------------------------------------------


def _parse_nepal_results(soup: BeautifulSoup) -> list[dict]:
    """
    Parse election results from the Nepal Election Commission portal
    (result.election.gov.np).

    The portal renders result rows in ``<tr>`` elements within tables that
    carry class names such as ``result-table``, ``table-result``, or
    ``table-striped``.  Candidate/party names may appear in either English
    or Devanagari script; both are preserved as-is.
    """
    rows: list[dict] = []

    # Nepal portal typically uses tables with specific class patterns
    nepal_table_classes = re.compile(
        r"result|election|candidate|tally|table-striped", re.IGNORECASE
    )
    candidate_tables = [
        t for t in soup.find_all("table")
        if nepal_table_classes.search(" ".join(t.get("class", [])))
    ]

    # Fall back to all tables if none matched by class
    tables_to_try = candidate_tables or soup.find_all("table")

    best_table = None
    best_row_count = 0
    for table in tables_to_try:
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

    # Add Nepal-specific column synonyms before normalising
    col_map = _build_col_map(headers, extra_keys=_NEPAL_EXTRA_KEYS)

    data_trs = best_table.find_all("tr")[1:]
    for tr in data_trs:
        cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
        if not any(cells):
            continue
        row = _map_cells(cells, col_map, headers)
        if row:
            rows.append(row)

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
    """Fallback: scrape candidate/party names and vote counts from plain text.

    Handles both ASCII digits and Devanagari digit strings (०-९) used on
    Nepal election pages.
    """
    rows: list[dict] = []
    # Pattern: "Party Name ... 12,345 votes/seats" or Devanagari equivalents.
    # Separator between name and number can be: spaces, colon, dash, or a
    # combination (e.g. " - ", ": ", or just whitespace).
    # Devanagari letters start at U+0904 (vowels) and U+0915 (consonants);
    # avoid U+0900-U+0903 which are combining marks/signs.
    pattern = re.compile(
        r"([A-Za-z\u0904-\u0939\u0958-\u0961][A-Za-z\u0900-\u097F .\-']{2,60})"
        r"[:\-\s]+\s*([\d,\u0966-\u096F]+)\s*(?:votes?|seats?|मत|सिट)?",
        re.IGNORECASE,
    )
    text = soup.get_text(separator="\n")
    seen: set[str] = set()
    for match in pattern.finditer(text):
        name = match.group(1).strip()
        count_raw = match.group(2)
        # Translate Devanagari digits to ASCII
        count = _devanagari_to_ascii(count_raw).replace(",", "")
        key = name.lower()
        if key in seen or not count.isdigit():
            continue
        seen.add(key)
        rows.append({"candidate_party": name, "votes_seats": count})
        if len(rows) >= 50:
            break
    return rows


def _parse_summary(soup: BeautifulSoup) -> dict:
    """Extract headline summary figures (total seats, declared, etc.).

    Recognises English terms and common Nepali/Devanagari equivalents such
    as सिट (seat), घोषित (declared), and विजेता (winner).
    """
    summary: dict[str, str] = {}
    pattern = re.compile(
        r"(seat|result|declared|total|winner|leading|"
        r"सिट|घोषित|विजेता|कुल|नतिजा)",
        re.IGNORECASE,
    )
    # Look for common summary elements: <h1>-<h4>, <strong>, <b>
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "strong", "b"]):
        text = tag.get_text(strip=True)
        if pattern.search(text):
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

# Nepal-specific column name synonyms (Devanagari + transliterated English)
_NEPAL_EXTRA_KEYS: dict[str, list[str]] = {
    "candidate": ["उम्मेदवार", "नाम", "candidate name"],   # उम्मेदवार=candidate, नाम=name
    "party": ["दल", "पार्टी", "राजनीतिक दल"],              # दल/पार्टी=party
    "constituency": ["निर्वाचन क्षेत्र", "जिल्ला", "प्रदेश"],  # जिल्ला=district, प्रदेश=province
    "votes": ["मत", "कुल मत", "प्राप्त मत", "valid votes"],  # मत=vote/votes
    "status": ["स्थिति", "नतिजा", "परिणाम"],                # नतिजा/परिणाम=result
}


def _build_col_map(
    headers: list[str],
    extra_keys: dict[str, list[str]] | None = None,
) -> dict[int, str]:
    """Map column indices to normalised key names.

    *extra_keys* can supply additional synonyms per key (e.g. Devanagari
    terms for Nepal election pages).
    """
    merged: dict[str, list[str]] = {k: list(v) for k, v in _KNOWN_KEYS.items()}
    if extra_keys:
        for key, synonyms in extra_keys.items():
            merged.setdefault(key, []).extend(synonyms)

    col_map: dict[int, str] = {}
    for idx, header in enumerate(headers):
        for key, synonyms in merged.items():
            if header in synonyms:
                col_map[idx] = key
                break
        else:
            col_map[idx] = header  # keep original if not recognised
    return col_map


# ---------------------------------------------------------------------------
# Devanagari digit helper
# ---------------------------------------------------------------------------

_DEVA_DIGIT_MAP = str.maketrans("०१२३४५६७८९", "0123456789")


def _devanagari_to_ascii(text: str) -> str:
    """Translate Devanagari digit characters (०-९) to ASCII digits (0-9)."""
    return text.translate(_DEVA_DIGIT_MAP)


def _map_cells(cells: list[str], col_map: dict[int, str], headers: list[str]) -> dict:
    """Build a dict from a row's cells using the column map."""
    row: dict[str, str] = {}
    for idx, value in enumerate(cells):
        if not value:
            continue
        key = col_map.get(idx, headers[idx] if idx < len(headers) else f"col_{idx}")
        row[key] = value
    return row
