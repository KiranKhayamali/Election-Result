"""
Flask application – Live Election Results

Routes
------
GET /              Render the main dashboard (HTML)
GET /api/results   Return current scraped data as JSON
POST /api/refresh  Trigger an immediate scrape and return updated data
GET /api/export/csv  Download current results as a CSV file
"""

import csv
import io
import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, Response, jsonify, render_template

from scraper import DEFAULT_SCRAPE_URL, get_cached_data, scrape_and_update

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# How often (seconds) to automatically re-scrape
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", "60"))

# Target URL (can be overridden via environment variable)
SCRAPE_URL = os.environ.get("SCRAPE_URL", DEFAULT_SCRAPE_URL)

# ---------------------------------------------------------------------------
# Background scheduler
# ---------------------------------------------------------------------------

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(
    lambda: scrape_and_update(SCRAPE_URL),
    trigger="interval",
    seconds=REFRESH_INTERVAL,
    id="scrape_job",
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    """Render the main dashboard."""
    data = get_cached_data()
    return render_template(
        "index.html",
        data=data,
        refresh_interval=REFRESH_INTERVAL,
        source_url=SCRAPE_URL,
    )


@app.route("/api/results")
def api_results():
    """Return current election results as JSON."""
    return jsonify(get_cached_data())


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Trigger an immediate scrape and return updated data as JSON."""
    scrape_and_update(SCRAPE_URL)
    return jsonify(get_cached_data())


@app.route("/api/export/csv")
def api_export_csv():
    """Download current election results as a CSV file."""
    data = get_cached_data()
    results = data.get("results", [])

    output = io.StringIO()
    if results:
        fieldnames = list(results[0].keys())
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    else:
        output.write("No data available\n")

    csv_bytes = output.getvalue().encode("utf-8")
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=election_results.csv"},
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Do an initial scrape before starting the scheduler
    scrape_and_update(SCRAPE_URL)
    scheduler.start()
    logger.info(
        "Starting Flask app – scraping every %d seconds from %s",
        REFRESH_INTERVAL,
        SCRAPE_URL,
    )
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
    )
