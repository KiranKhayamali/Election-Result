# Election-Result

A **live election results tracker** that uses web scraping to fetch and display
real-time data from public election portals.

![Live Election Results Dashboard](https://github.com/user-attachments/assets/611a4b4d-ff54-47cc-a0b5-9a00bc494746)

---

## Features

- 🔄 **Auto-refresh** – scrapes the configured source on a configurable interval (default: 60 s)
- 📊 **Results table** – displays constituency, candidate, party, votes, and status
- 🗂️ **Summary cards** – shows headline figures (total seats, declared, etc.)
- ⚡ **Manual refresh** – "Refresh Now" button triggers an immediate scrape
- 🌐 **REST API** – `/api/results` (GET) and `/api/refresh` (POST) endpoints
- ⚠️ **Graceful error handling** – shows cached data when the source is unreachable
- 📱 **Responsive design** – works on desktop and mobile

---

## Project Structure

```
Election-Result/
├── app.py              # Flask web application & scheduler
├── scraper.py          # Web scraping logic (BeautifulSoup + requests)
├── requirements.txt    # Python dependencies
├── templates/
│   └── index.html      # Jinja2 HTML template (dashboard UI)
├── static/
│   └── style.css       # Dark-theme stylesheet
└── tests/
    └── test_election.py  # pytest test suite
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Run the application

```bash
python app.py
```

Then open **http://localhost:5000** in your browser.

### 3. Configuration (optional)

| Environment variable | Default | Description |
|---|---|---|
| `SCRAPE_URL` | `https://results.eci.gov.in/` | Election results page to scrape |
| `REFRESH_INTERVAL` | `60` | Seconds between automatic scrapes |
| `PORT` | `5000` | Port for the Flask server |

```bash
SCRAPE_URL=https://results.eci.gov.in/ REFRESH_INTERVAL=30 python app.py
```

---

## API Reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Dashboard UI |
| `GET` | `/api/results` | Current scraped data (JSON) |
| `POST` | `/api/refresh` | Trigger immediate scrape, returns updated data (JSON) |

### Example JSON response (`/api/results`)

```json
{
  "results": [
    { "constituency": "Mumbai North", "candidate": "Alice", "party": "Blue", "votes": "182450", "status": "Won" }
  ],
  "summary": { "Total Seats: 543": "Total Seats: 543" },
  "source_url": "https://results.eci.gov.in/",
  "last_updated": "2024-06-04T09:15:32",
  "status": "Live",
  "error": null
}
```

---

## Running Tests

```bash
pip install pytest
pytest tests/ -v
```

---

## How It Works

1. **`scraper.py`** sends an HTTP GET request (with a browser-like `User-Agent`) to
   `SCRAPE_URL` using the `requests` library.
2. The response HTML is parsed with **BeautifulSoup** (`lxml` backend).
3. The scraper first tries to extract rows from the largest `<table>` on the page,
   normalising column headers to well-known keys (`candidate`, `party`, `votes`, …).
4. If no table is found it falls back to a regex pattern that extracts party/candidate
   names paired with numeric counts from plain text.
5. Scraped data is stored in an in-memory cache (thread-safe) that Flask routes read.
6. **APScheduler** re-runs the scrape every `REFRESH_INTERVAL` seconds in the background.
7. The frontend JavaScript polls `/api/results` on the same interval and re-renders the
   table without a full page reload.
