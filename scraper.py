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

# Default target: Election Commission of Nepal results portal (2082 BS).
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
    "party_tally": [],
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
    common election-results pages.  When a recognised structure is found
    the parsed rows are stored; otherwise a raw-text fallback is used so
    the caller always receives *something*.
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

    results = (
        _parse_nepal_results(soup)
        or _parse_results_table(soup)
        or _parse_results_list(soup)
    )
    summary = _parse_summary(soup)
    party_tally = _aggregate_party_tally(results)

    with _lock:
        _cache["results"] = results
        _cache["party_tally"] = party_tally
        _cache["summary"] = summary
        _cache["source_url"] = url
        _cache["last_updated"] = datetime.now().isoformat(timespec="seconds")
        _cache["status"] = "Live" if results else "No structured data found"
        _cache["error"] = None

    logger.info("Cache updated: %d rows, status=%s", len(results), _cache["status"])


# ---------------------------------------------------------------------------
# Private parsers
# ---------------------------------------------------------------------------


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
# Party tally aggregation
# ---------------------------------------------------------------------------

# Status keywords that indicate a candidate has won a seat.
_WON_KEYWORDS = frozenset([
    "won", "win", "winner", "elected", "निर्वाचित", "विजयी",
])
# Status keywords that indicate a candidate is currently leading.
_LEADING_KEYWORDS = frozenset([
    "leading", "lead", "अग्रणी", "leading by",
])


def _aggregate_party_tally(results: list[dict]) -> list[dict]:
    """
    Aggregate seat-level results by party.

    Returns a list of dicts sorted by total seats (won + leading) descending.
    Each dict has: party, won, leading, seats (won+leading), total_votes.
    """
    tally: dict[str, dict] = {}
    for row in results:
        party = (
            row.get("party")
            or row.get("candidate_party")
            or "Unknown"
        ).strip()
        if not party:
            party = "Unknown"

        votes_raw = row.get("votes") or row.get("votes_seats") or "0"
        try:
            votes = int(str(votes_raw).replace(",", "").strip())
        except ValueError:
            votes = 0

        status_raw = (row.get("status") or "").lower().strip()

        if party not in tally:
            tally[party] = {
                "party": party,
                "won": 0,
                "leading": 0,
                "total_votes": 0,
            }

        tally[party]["total_votes"] += votes

        if any(kw in status_raw for kw in _WON_KEYWORDS):
            tally[party]["won"] += 1
        elif any(kw in status_raw for kw in _LEADING_KEYWORDS):
            tally[party]["leading"] += 1

    result_list = []
    for entry in tally.values():
        entry["seats"] = entry["won"] + entry["leading"]
        result_list.append(entry)

    result_list.sort(key=lambda x: (x["seats"], x["total_votes"]), reverse=True)
    return result_list


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
