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

# Sample HTML that mimics the Nepal Election Commission portal structure.
NEPAL_TABLE_HTML = """
<html><body>
<table>
  <tr>
    <th>Constituency</th>
    <th>Candidate Name</th>
    <th>Party Name</th>
    <th>Votes Received</th>
    <th>Status</th>
  </tr>
  <tr>
    <td>Kathmandu-1</td>
    <td>Ram Prasad Sharma</td>
    <td>Nepali Congress</td>
    <td>24500</td>
    <td>Won</td>
  </tr>
  <tr>
    <td>Kathmandu-2</td>
    <td>Sita Devi Thapa</td>
    <td>CPN-UML</td>
    <td>19800</td>
    <td>Won</td>
  </tr>
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
# scraper._parse_nepal_results
# ---------------------------------------------------------------------------


def test_parse_nepal_results_english_headers():
    """Nepal parser extracts rows from a table with English column headers."""
    soup = BeautifulSoup(NEPAL_TABLE_HTML, "lxml")
    rows = scraper._parse_nepal_results(soup)
    assert len(rows) == 2
    assert rows[0]["candidate"] == "Ram Prasad Sharma"
    assert rows[0]["party"] == "Nepali Congress"
    assert rows[0]["votes"] == "24500"
    assert rows[0]["constituency"] == "Kathmandu-1"


def test_parse_nepal_results_no_recognised_columns():
    """Nepal parser returns empty list when no recognised column is present."""
    html = """<html><body>
    <table>
      <tr><th>Foo</th><th>Bar</th></tr>
      <tr><td>A</td><td>B</td></tr>
    </table></body></html>"""
    soup = BeautifulSoup(html, "lxml")
    rows = scraper._parse_nepal_results(soup)
    assert rows == []


def test_parse_nepal_results_empty_page():
    soup = BeautifulSoup("<html><body></body></html>", "lxml")
    rows = scraper._parse_nepal_results(soup)
    assert rows == []


def test_default_scrape_url_is_nepal():
    """DEFAULT_SCRAPE_URL must point to the Nepal Election Commission portal."""
    assert scraper.DEFAULT_SCRAPE_URL == "https://result.election.gov.np/"


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
# Admin fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_client():
    """Test client that is already authenticated as admin."""
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        with c.session_transaction() as sess:
            sess["admin_logged_in"] = True
        yield c


# ---------------------------------------------------------------------------
# scraper.add_result / remove_result / get_version
# ---------------------------------------------------------------------------


def test_add_result_appends_row():
    with scraper._lock:
        scraper._cache["results"] = []
        scraper._version = 0
    scraper.add_result({"candidate": "Test", "party": "Test Party", "votes": "1000"})
    data = scraper.get_cached_data()
    assert len(data["results"]) == 1
    assert data["results"][0]["candidate"] == "Test"


def test_add_result_increments_version():
    with scraper._lock:
        scraper._version = 0
    scraper.add_result({"candidate": "X"})
    assert scraper.get_version() == 1


def test_remove_result_success():
    with scraper._lock:
        scraper._cache["results"] = [
            {"candidate": "A"},
            {"candidate": "B"},
        ]
        scraper._version = 0
    removed = scraper.remove_result(0)
    assert removed is True
    data = scraper.get_cached_data()
    assert len(data["results"]) == 1
    assert data["results"][0]["candidate"] == "B"
    assert scraper.get_version() == 1


def test_remove_result_out_of_range():
    with scraper._lock:
        scraper._cache["results"] = [{"candidate": "A"}]
        scraper._version = 0
    removed = scraper.remove_result(99)
    assert removed is False
    assert scraper.get_version() == 0  # version unchanged


# ---------------------------------------------------------------------------
# Admin auth
# ---------------------------------------------------------------------------


def test_login_page_returns_200(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert b"Login" in resp.data


def test_login_with_valid_credentials(client):
    import app as app_module
    resp = client.post(
        "/login",
        data={"username": app_module.ADMIN_USERNAME, "password": app_module.ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/admin" in resp.headers["Location"]


def test_login_with_invalid_credentials(client):
    resp = client.post(
        "/login",
        data={"username": "wrong", "password": "wrong"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Invalid" in resp.data


def test_admin_panel_requires_login(client):
    resp = client.get("/admin", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_admin_panel_accessible_when_logged_in(admin_client):
    resp = admin_client.get("/admin")
    assert resp.status_code == 200
    assert b"Admin Panel" in resp.data


def test_logout_clears_session(admin_client):
    admin_client.get("/logout", follow_redirects=False)
    # Subsequent admin access should redirect to login
    resp2 = admin_client.get("/admin", follow_redirects=False)
    assert resp2.status_code == 302
    assert "/login" in resp2.headers["Location"]


# ---------------------------------------------------------------------------
# Admin CRUD API
# ---------------------------------------------------------------------------


def test_admin_add_result_requires_auth(client):
    resp = client.post(
        "/api/admin/results",
        data=json.dumps({"candidate": "Test"}),
        content_type="application/json",
    )
    assert resp.status_code == 302  # redirect to login


def test_admin_add_result_success(admin_client):
    with scraper._lock:
        scraper._cache["results"] = []
    resp = admin_client.post(
        "/api/admin/results",
        data=json.dumps({"candidate": "Alice", "party": "Blue Party", "votes": "5000"}),
        content_type="application/json",
    )
    assert resp.status_code == 201
    payload = json.loads(resp.data)
    assert any(r.get("candidate") == "Alice" for r in payload["results"])


def test_admin_add_result_empty_body(admin_client):
    resp = admin_client.post(
        "/api/admin/results",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert resp.status_code == 400


def test_admin_remove_result_success(admin_client):
    with scraper._lock:
        scraper._cache["results"] = [
            {"candidate": "Alice"},
            {"candidate": "Bob"},
        ]
    resp = admin_client.delete("/api/admin/results/0")
    assert resp.status_code == 200
    payload = json.loads(resp.data)
    assert len(payload["results"]) == 1
    assert payload["results"][0]["candidate"] == "Bob"


def test_admin_remove_result_out_of_range(admin_client):
    with scraper._lock:
        scraper._cache["results"] = [{"candidate": "Alice"}]
    resp = admin_client.delete("/api/admin/results/99")
    assert resp.status_code == 404


def test_admin_remove_result_requires_auth(client):
    resp = client.delete("/api/admin/results/0")
    assert resp.status_code == 302  # redirect to login


# ---------------------------------------------------------------------------
# News scraping – unit tests
# ---------------------------------------------------------------------------

SAMPLE_NEWS_HTML = """
<html><body>
  <article>
    <h3><a href="/election-2082-result">Election 2082: CPN wins Kathmandu seat</a></h3>
    <time>2024-11-15</time>
  </article>
  <article>
    <h3><a href="/congress-leads">Nepali Congress leads in Lalitpur</a></h3>
    <time>2024-11-15</time>
  </article>
  <article>
    <h3><a href="/voter-turnout">High voter turnout recorded across Nepal</a></h3>
    <time>2024-11-15</time>
  </article>
</body></html>
"""

FALLBACK_NEWS_HTML = """
<html><body>
  <a href="/news/1">Election 2082 results: CPN-UML wins majority in Province 1 according to officials</a>
  <a href="/news/2">Nepali Congress candidate claims victory in Kathmandu-3 constituency</a>
  <a href="/">Home</a>
</body></html>
"""


def test_parse_news_articles_structured():
    """_parse_news_articles extracts articles from <article> containers."""
    soup = BeautifulSoup(SAMPLE_NEWS_HTML, "lxml")
    articles = scraper._parse_news_articles(soup, "TestSource", "https://example.com")
    assert len(articles) >= 2
    sources = {a["source"] for a in articles}
    assert sources == {"TestSource"}
    for art in articles:
        assert "title" in art
        assert "link" in art
        assert "source" in art
        assert len(art["title"]) >= 15


def test_parse_news_articles_fallback():
    """_parse_news_articles falls back to scanning anchor texts."""
    soup = BeautifulSoup(FALLBACK_NEWS_HTML, "lxml")
    articles = scraper._parse_news_articles(soup, "Fallback", "https://example.com")
    assert len(articles) >= 1
    titles = [a["title"] for a in articles]
    assert any("Election" in t or "Congress" in t for t in titles)


def test_parse_news_articles_relative_links_made_absolute():
    """Relative href values are resolved against the base URL."""
    html = """<html><body>
    <article><h3><a href="/article/live-election">Live election updates from across Nepal</a></h3></article>
    <article><h3><a href="/article/live-election2">CPN-UML celebrates early lead in rural constituencies</a></h3></article>
    <article><h3><a href="/article/live-election3">Voter turnout reaches 70 percent in hilly districts today</a></h3></article>
    </body></html>"""
    soup = BeautifulSoup(html, "lxml")
    articles = scraper._parse_news_articles(soup, "Src", "https://news.example.com/")
    for art in articles:
        assert art["link"].startswith("https://news.example.com/"), art["link"]


def test_parse_news_articles_skip_short_titles():
    """Titles shorter than 15 characters are ignored."""
    html = """<html><body>
    <article><h3><a href="/x">Short</a></h3></article>
    <article><h3><a href="/y">Nepal Election 2082: Full Results Now Available for All Constituencies</a></h3></article>
    <article><h3><a href="/z">CPN-UML claims substantial victory across eastern Nepal regions</a></h3></article>
    <article><h3><a href="/w">Vote count finalized for 165 of 275 constituencies in Nepal</a></h3></article>
    </body></html>"""
    soup = BeautifulSoup(html, "lxml")
    articles = scraper._parse_news_articles(soup, "Src", "https://example.com/")
    titles = [a["title"] for a in articles]
    assert not any(t == "Short" for t in titles)


def test_get_news_data_returns_dict():
    """get_news_data() returns a dict with expected keys."""
    data = scraper.get_news_data()
    assert isinstance(data, dict)
    assert "articles" in data
    assert "sources" in data
    assert "last_updated" in data


def test_get_news_version_returns_int():
    assert isinstance(scraper.get_news_version(), int)


@patch("scraper.requests.get")
def test_scrape_news_sources_success(mock_get):
    """scrape_news_sources updates news cache on successful fetch."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.text = SAMPLE_NEWS_HTML
    mock_get.return_value = mock_resp

    before_version = scraper.get_news_version()
    scraper.scrape_news_sources()
    after_version = scraper.get_news_version()

    assert after_version > before_version
    data = scraper.get_news_data()
    assert data["last_updated"] is not None
    assert isinstance(data["articles"], list)
    assert isinstance(data["sources"], list)
    # At least some sources should have status reported
    assert len(data["sources"]) == len(scraper.NEWS_SOURCES)


@patch("scraper.requests.get")
def test_scrape_news_sources_network_error(mock_get):
    """scrape_news_sources records error but still updates cache."""
    import requests as req
    mock_get.side_effect = req.RequestException("timeout")

    with scraper._news_lock:
        scraper._news_cache["articles"] = []

    scraper.scrape_news_sources()
    data = scraper.get_news_data()

    assert data["last_updated"] is not None
    # All sources should show error status
    for src in data["sources"]:
        assert src["status"] == "error"
        assert src["count"] == 0


# ---------------------------------------------------------------------------
# /api/news endpoint
# ---------------------------------------------------------------------------


def test_api_news_returns_json(client):
    """GET /api/news returns 200 with expected JSON keys."""
    resp = client.get("/api/news")
    assert resp.status_code == 200
    payload = json.loads(resp.data)
    assert "articles" in payload
    assert "sources" in payload
    assert "last_updated" in payload


def test_api_news_articles_are_list(client):
    resp = client.get("/api/news")
    payload = json.loads(resp.data)
    assert isinstance(payload["articles"], list)


def test_api_news_with_seeded_data(client):
    """Seeded articles appear in /api/news response."""
    with scraper._news_lock:
        scraper._news_cache["articles"] = [
            {"title": "CPN-UML wins Kathmandu", "link": "https://example.com/1",
             "source": "Ekantipur", "timestamp": "2024-11-15"},
        ]
    resp = client.get("/api/news")
    payload = json.loads(resp.data)
    assert len(payload["articles"]) >= 1
    assert payload["articles"][0]["source"] == "Ekantipur"


# ---------------------------------------------------------------------------
# NEWS_SOURCES configuration
# ---------------------------------------------------------------------------


def test_news_sources_have_required_keys():
    """Every entry in NEWS_SOURCES has 'name' and 'url' keys."""
    for src in scraper.NEWS_SOURCES:
        assert "name" in src, f"Missing 'name' in {src}"
        assert "url" in src, f"Missing 'url' in {src}"
        assert src["url"].startswith("http"), f"URL must be absolute: {src['url']}"


def test_news_sources_not_empty():
    assert len(scraper.NEWS_SOURCES) >= 2


