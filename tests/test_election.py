"""Tests for the election results scraper and Flask application."""

import json
from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup

import scraper
from app import app as flask_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# scraper._build_col_map
# ---------------------------------------------------------------------------


def test_build_col_map_known_headers():
    headers = ["candidate name", "party", "votes", "status"]
    result = scraper._build_col_map(headers)
    assert result[0] == "candidate"
    assert result[1] == "party"
    assert result[2] == "votes"
    assert result[3] == "status"


def test_build_col_map_unknown_headers():
    headers = ["foo", "bar"]
    result = scraper._build_col_map(headers)
    assert result[0] == "foo"
    assert result[1] == "bar"


# ---------------------------------------------------------------------------
# scraper._parse_results_table
# ---------------------------------------------------------------------------

SAMPLE_TABLE_HTML = """
<html><body>
<table>
  <tr><th>Candidate Name</th><th>Party</th><th>Votes</th></tr>
  <tr><td>Alice</td><td>Blue Party</td><td>12345</td></tr>
  <tr><td>Bob</td><td>Red Party</td><td>9876</td></tr>
</table>
</body></html>
"""


def test_parse_results_table_returns_rows():
    soup = BeautifulSoup(SAMPLE_TABLE_HTML, "lxml")
    rows = scraper._parse_results_table(soup)
    assert len(rows) == 2
    assert rows[0]["candidate"] == "Alice"
    assert rows[0]["party"] == "Blue Party"
    assert rows[0]["votes"] == "12345"


def test_parse_results_table_empty():
    soup = BeautifulSoup("<html><body></body></html>", "lxml")
    rows = scraper._parse_results_table(soup)
    assert rows == []


# ---------------------------------------------------------------------------
# scraper._parse_results_list
# ---------------------------------------------------------------------------


def test_parse_results_list_extracts_matches():
    html = """<html><body>
    <p>Blue Party - 45,000 votes</p>
    <p>Red Party - 38,200 votes</p>
    </body></html>"""
    soup = BeautifulSoup(html, "lxml")
    rows = scraper._parse_results_list(soup)
    assert len(rows) >= 2
    names = [r["candidate_party"] for r in rows]
    assert any("Blue Party" in n for n in names)
    assert any("Red Party" in n for n in names)


def test_parse_results_list_no_matches():
    soup = BeautifulSoup("<html><body><p>no data here</p></body></html>", "lxml")
    rows = scraper._parse_results_list(soup)
    assert isinstance(rows, list)


# ---------------------------------------------------------------------------
# scraper._parse_summary
# ---------------------------------------------------------------------------


def test_parse_summary_extracts_headings():
    html = """<html><body>
    <h2>Total Seats: 543</h2>
    <h3>Results Declared: 200</h3>
    </body></html>"""
    soup = BeautifulSoup(html, "lxml")
    summary = scraper._parse_summary(soup)
    assert len(summary) >= 1


# ---------------------------------------------------------------------------
# scraper.scrape_and_update – network mocked
# ---------------------------------------------------------------------------


def _make_mock_response(html: str, status_code: int = 200):
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.text = html
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


@patch("scraper.requests.get")
def test_scrape_and_update_success(mock_get):
    mock_get.return_value = _make_mock_response(SAMPLE_TABLE_HTML)
    scraper.scrape_and_update("http://fake-election.example.com/")
    data = scraper.get_cached_data()
    assert data["error"] is None
    assert data["last_updated"] is not None
    assert len(data["results"]) == 2


@patch("scraper.requests.get")
def test_scrape_and_update_network_error(mock_get):
    import requests as req

    mock_get.side_effect = req.RequestException("connection refused")
    # Seed cache with previous data so we can verify it is preserved
    with scraper._lock:
        scraper._cache["results"] = [{"candidate": "Previous"}]
    scraper.scrape_and_update("http://fake-election.example.com/")
    data = scraper.get_cached_data()
    assert data["error"] == "connection refused"
    assert data["results"] == [{"candidate": "Previous"}]  # preserved


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------


def test_index_returns_200(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Election" in resp.data


def test_api_results_returns_json(client):
    resp = client.get("/api/results")
    assert resp.status_code == 200
    payload = json.loads(resp.data)
    assert "results" in payload
    assert "status" in payload
    assert "last_updated" in payload


@patch("app.scrape_and_update")
def test_api_refresh_calls_scraper(mock_scrape, client):
    resp = client.post(
        "/api/refresh",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    mock_scrape.assert_called_once()
    payload = json.loads(resp.data)
    assert "results" in payload


# ---------------------------------------------------------------------------
# CSV export endpoint
# ---------------------------------------------------------------------------


def test_api_export_csv_returns_csv_content_type(client):
    resp = client.get("/api/export/csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.content_type


def test_api_export_csv_has_attachment_header(client):
    resp = client.get("/api/export/csv")
    cd = resp.headers.get("Content-Disposition", "")
    assert "attachment" in cd
    assert "election_results.csv" in cd


def test_api_export_csv_with_data(client):
    """Seed cache with known rows and verify CSV output."""
    with scraper._lock:
        scraper._cache["results"] = [
            {"candidate": "Alice", "party": "Blue", "votes": "12345"},
            {"candidate": "Bob",   "party": "Red",  "votes": "9876"},
        ]
    resp = client.get("/api/export/csv")
    assert resp.status_code == 200
    text = resp.data.decode("utf-8")
    assert "candidate" in text
    assert "Alice" in text
    assert "Bob" in text


def test_api_export_csv_empty_cache(client):
    """When no results are cached the response still contains the fallback message."""
    with scraper._lock:
        scraper._cache["results"] = []
    resp = client.get("/api/export/csv")
    assert resp.status_code == 200
    assert b"No data available" in resp.data


# ---------------------------------------------------------------------------
# Nepal-specific tests
# ---------------------------------------------------------------------------


def test_default_scrape_url_is_nepal():
    """Default scrape target must point to Nepal's election commission portal."""
    assert scraper.DEFAULT_SCRAPE_URL == "https://result.election.gov.np/"


def test_devanagari_to_ascii_digits():
    assert scraper._devanagari_to_ascii("०१२३४५६७८९") == "0123456789"
    assert scraper._devanagari_to_ascii("१२३") == "123"
    assert scraper._devanagari_to_ascii("456") == "456"  # ASCII unchanged


def test_build_col_map_nepal_devanagari_headers():
    headers = ["उम्मेदवार", "दल", "मत", "नतिजा"]
    result = scraper._build_col_map(headers, extra_keys=scraper._NEPAL_EXTRA_KEYS)
    assert result[0] == "candidate"
    assert result[1] == "party"
    assert result[2] == "votes"
    assert result[3] == "status"


NEPAL_TABLE_HTML = """
<html><body>
<table class="result-table">
  <tr><th>उम्मेदवार</th><th>दल</th><th>मत</th></tr>
  <tr><td>Ram Bahadur</td><td>नेपाली काँग्रेस</td><td>15000</td></tr>
  <tr><td>Sita Kumari</td><td>CPN-UML</td><td>12500</td></tr>
</table>
</body></html>
"""


def test_parse_nepal_results_returns_rows():
    soup = BeautifulSoup(NEPAL_TABLE_HTML, "lxml")
    rows = scraper._parse_nepal_results(soup)
    assert len(rows) == 2
    assert rows[0]["candidate"] == "Ram Bahadur"
    assert rows[0]["party"] == "नेपाली काँग्रेस"
    assert rows[0]["votes"] == "15000"


def test_parse_nepal_results_empty():
    soup = BeautifulSoup("<html><body></body></html>", "lxml")
    rows = scraper._parse_nepal_results(soup)
    assert rows == []


def test_parse_results_list_devanagari_digits():
    html = """<html><body>
    <p>नेपाली काँग्रेस: १५०००  votes</p>
    <p>CPN-UML: १२५०० votes</p>
    </body></html>"""
    soup = BeautifulSoup(html, "lxml")
    rows = scraper._parse_results_list(soup)
    # Devanagari numbers should be translated to ASCII digits
    vote_values = {r["candidate_party"]: r["votes_seats"] for r in rows}
    nepal_congress_key = next((k for k in vote_values if "नेपाली काँग्रेस" in k), None)
    cpn_uml_key = next((k for k in vote_values if "CPN-UML" in k), None)
    assert nepal_congress_key is not None
    assert cpn_uml_key is not None
    assert vote_values[nepal_congress_key] == "15000"
    assert vote_values[cpn_uml_key] == "12500"


def test_parse_summary_nepali_keywords():
    html = """<html><body>
    <h2>कुल सिट: 275</h2>
    <h3>घोषित नतिजा: 200</h3>
    </body></html>"""
    soup = BeautifulSoup(html, "lxml")
    summary = scraper._parse_summary(soup)
    assert len(summary) >= 1
    keys = list(summary.keys())
    assert any("सिट" in k or "नतिजा" in k for k in keys)


@patch("scraper.requests.get")
def test_scrape_and_update_uses_nepal_url_by_default(mock_get):
    """scrape_and_update should use the Nepal URL when none is provided."""
    mock_get.return_value = MagicMock(
        status_code=200,
        text=NEPAL_TABLE_HTML,
        raise_for_status=MagicMock(),
    )
    scraper.scrape_and_update()
    mock_get.assert_called_once()
    called_url = mock_get.call_args[0][0]
    assert called_url == "https://result.election.gov.np/"


def test_index_contains_nepal(client):
    """The homepage should mention Nepal."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Nepal" in resp.data
