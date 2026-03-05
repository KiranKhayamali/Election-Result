"""
Flask application – Live Election Results

Routes
------
GET  /                    Render the main dashboard (HTML)
GET  /api/results         Return current scraped data as JSON
POST /api/refresh         Trigger an immediate scrape and return updated data
GET  /api/export/csv      Download current results as a CSV file
GET  /api/stream          Server-Sent Events stream for live push updates

Admin routes (session-protected)
---------------------------------
GET  /login               Admin login page
POST /login               Authenticate admin
GET  /logout              Log out admin
GET  /admin               Admin panel – view / add / remove results
POST /api/admin/results   Add a new result row (JSON body)
DELETE /api/admin/results/<int:idx>  Remove a result row by index
"""

import csv
import io
import logging
import os
import time
from functools import wraps

from apscheduler.schedulers.background import BackgroundScheduler
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)

from scraper import (
    DEFAULT_SCRAPE_URL,
    add_result,
    get_cached_data,
    get_version,
    remove_result,
    scrape_and_update,
)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Secret key used to sign session cookies.  Override in production via env var.
_DEFAULT_SECRET = "change-me-in-production"  # noqa: S105 – intentional placeholder
_secret_key = os.environ.get("SECRET_KEY", _DEFAULT_SECRET)
if _secret_key == _DEFAULT_SECRET:
    logger.warning(
        "SECRET_KEY is not set – using insecure default. "
        "Set the SECRET_KEY environment variable before deploying to production."
    )
app.secret_key = _secret_key

# How often (seconds) to automatically re-scrape
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", "60"))

# Target URL (can be overridden via environment variable)
SCRAPE_URL = os.environ.get("SCRAPE_URL", DEFAULT_SCRAPE_URL)

# Admin credentials (override via environment variables in production)
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
if not os.environ.get("ADMIN_USERNAME") or not os.environ.get("ADMIN_PASSWORD"):
    logger.warning(
        "ADMIN_USERNAME and/or ADMIN_PASSWORD are not set – using insecure defaults. "
        "Set these environment variables before deploying to production."
    )

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
# Auth helper
# ---------------------------------------------------------------------------


def admin_required(f):
    """Decorator that redirects unauthenticated requests to the login page."""

    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated


# ---------------------------------------------------------------------------
# Routes – public
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
        is_admin=session.get("admin_logged_in", False),
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


@app.route("/api/stream")
def api_stream():
    """
    Server-Sent Events endpoint.

    Clients receive a ``data: <version>\\n\\n`` message whenever the results
    cache changes (scrape, admin add, or admin remove).  The client can react
    by fetching ``/api/results`` immediately instead of waiting for the next
    polling cycle.
    """

    def generate():
        last_version = get_version()
        # Send an initial heartbeat so the browser connection is established.
        yield "data: {}\n\n".format(last_version)
        while True:
            time.sleep(2)
            current = get_version()
            if current != last_version:
                last_version = current
                yield "data: {}\n\n".format(current)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Routes – admin auth
# ---------------------------------------------------------------------------


@app.route("/login", methods=["GET", "POST"])
def login():
    """Admin login page."""
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            session.permanent = False
            return redirect(url_for("admin_panel"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    """Log out the admin and redirect to the home page."""
    session.pop("admin_logged_in", None)
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Routes – admin panel & CRUD
# ---------------------------------------------------------------------------


@app.route("/admin")
@admin_required
def admin_panel():
    """Admin dashboard – view, add, and remove results."""
    data = get_cached_data()
    return render_template("admin.html", data=data)


@app.route("/api/admin/results", methods=["POST"])
@admin_required
def admin_add_result():
    """
    Add a new result row.

    Accepts ``application/json`` with at least one non-empty field.
    Returns the updated results list.
    """
    payload = request.get_json(silent=True) or {}
    # Strip whitespace and drop empty values
    row = {k.strip(): v.strip() for k, v in payload.items() if str(v).strip()}
    if not row:
        return jsonify({"error": "Request body must contain at least one non-empty field."}), 400
    add_result(row)
    return jsonify(get_cached_data()), 201


@app.route("/api/admin/results/<int:idx>", methods=["DELETE"])
@admin_required
def admin_remove_result(idx):
    """Remove the result at *idx* (0-based).  Returns 404 if out of range."""
    if remove_result(idx):
        return jsonify(get_cached_data())
    return jsonify({"error": "Index out of range."}), 404


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
