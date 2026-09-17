# 🛒 GroceriesTracker: Market Intelligence System

GroceriesTracker is a powerful, Django-based analytical engine designed to automate the tracking of household grocery spending by scraping and analyzing Brazilian **NFCe (Electronic Consumer Invoice)** receipts. 

Beyond simple expense tracking, it provides deep financial intelligence, including **inflation tracking**, **shrinkflation detection**, **basket optimization**, and **nutritional health analysis**.

---

## ✨ Key Intelligence Features

*   **⚡ Automated Scraping:** Instant extraction of data from SEFAZ URLs (State Tax Department) with built-in SSRF protection and multi-state regex fallbacks.
*   **📊 Inflation Analysis:** Tracks price evolution per category and global averages over time to show you how much your "regular basket" is really changing.
*   **📉 Shrinkflation Detection:** Automatically compares product variants (e.g., 395g vs 350g) to identify items that decreased in volume while increasing in unit price.
*   **🎯 Pareto (80/20) Analysis:** Performs ABC classification to identify the 20% of products responsible for 80% of your total expenditure.
*   **🛒 Smart Cart (Basket Splitter):** An AI-driven shopping list optimizer that solves the "Basket Splitter" problem, recommending which stores to visit based on historical local prices for your specific items.
*   **🍎 Health & Nutrition:** Aggregates **NOVA groups** (ultra-processed vs. natural), **Eco-scores**, and nutritional density for your entire shopping history.
*   **🔍 Product Enrichment:** Automatically fetches high-resolution images and metadata using GTIN/EAN codes from **Open Food Facts** and **Mercado Livre**.

---

## 🏗️ Technical Architecture

*   **Backend:** Python 3.14+ / Django 6.0+
*   **Database:** SQLite (`db.sqlite3`, tracked in git for portability). PostgreSQL is not wired up — see `.env.example`: setting `DATABASE_URL` alone does nothing until `settings.py` is updated.
*   **Task Queue:** `Django-Q2` for asynchronous product enrichment and scraping.
*   **Data Science:** `RapidFuzz` for semantic product matching and `Hypothesis` for property-based testing.
*   **Visualization:** **ECharts** for advanced interactive charts (Candlesticks, Heatmaps, Radar Charts).
*   **Security:** Scraper domain whitelisting (SSRF protection) and normalized product identification (GTIN -> Internal Store Code -> Fuzzy Name).

---

## 🚀 Getting Started

### Prerequisites
*   Python 3.14+
*   Docker & Docker Compose (Optional)

### Standard Setup
```bash
# 1. Clone the repository (data ships in git: db.sqlite3 + media/)
git clone https://github.com/Finrood/GroceriesTracker.git
cd GroceriesTracker

# 2. Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure Environment
cp .env.example .env  # Ensure SECRET_KEY and DEBUG are set

# 5. Run migrations (no-op if the shipped db.sqlite3 is already current)
python manage.py migrate

# Data note: db.sqlite3 + media/products/ are tracked in git, so a fresh
# clone already contains all receipts, products and images. No restore step.

# 6. Start the Task Worker (Required for product enrichment/scraping)
# Open a second terminal window and run:
python manage.py qcluster

# 7. Start the Web Server
python manage.py runserver
```

### Docker Deployment (Recommended)
The fastest way to get everything running (Web + Worker) is via Docker Compose:
```bash
docker compose up --build
```
*   **Web:** Accessible at `http://localhost:8000`
*   **Worker:** Automatically starts and handles background product enrichment and data processing.
*   **Data:** `db.sqlite3` + `media/` come from the clone via the `.:/app` bind mount, so the stack starts with full history and images.

### Production Notes (Cloudflare Tunnel / reverse proxy)
The stack ships production-ready defaults for running behind a TLS-terminating proxy:
*   **Gunicorn + WhiteNoise** serve the app and hashed static files (no nginx needed).
*   **Media files** are served by Django itself (`django.views.static.serve`), because the enrichment worker writes new product images while the app is running.
*   `SECURE_PROXY_SSL_HEADER` trusts the proxy's `X-Forwarded-Proto`; cookies, HSTS, and `SECURE_SSL_REDIRECT` are all env-tunable (see `.env.example`).
*   SQLite runs in **WAL mode** with a 60s busy timeout; `db.sqlite3`, `media/` and `staticfiles/` are bind-mounted volumes (see `docker-compose.yml`: `.:/app`).
*   `.env` is never baked into the image (excluded via `.dockerignore`). `db.sqlite3` and `media/products/*` are likewise excluded from the image build to keep it slim — at runtime the containers use the git-tracked copies from the host via the bind mount, not image layers.

### Data integrity invariants
*   A receipt is uniquely identified per user by its 44-digit NFCe **access key** (DB-enforced); re-submitting the same NFCe is a no-op, not a duplicate.
*   `PriceHistory` rows are linked to their `Receipt` and cascade on delete, so refreshing or removing a receipt leaves no ghost data. `python manage.py backfill_history` rebuilds the whole table from `ReceiptItem`s if ever needed.

---

## 🛠️ Project Structure

*   `tracker/scraper.py`: The `NFCeScraper` engine for SEFAZ data extraction.
*   `tracker/services.py`: Business logic for Analytics, Smart Cart, and Receipt processing.
*   `tracker/enrichment.py`: External API integration for product metadata.
*   `tracker/models.py`: Robust schema with normalization for units (e.g., converting '5KG' to '5kg').
*   `media/`: Persistent storage for product images (tracked in git for portability).
*   `db.sqlite3`: Your live database (tracked in git for portability — clone anywhere and data is directly available).

### Runtime data vs. git
The live database and product images **are tracked** (`db.sqlite3` + `media/products/`), so every clone starts with full data. Only transient files stay ignored: SQLite sidecars (`db.sqlite3-*`), `staticfiles/`, `django_cache/`, logs, `.env` (see `.gitignore`).
Historic snapshot of the pre-tracking era is preserved on the **`sample-data`** git tag, but it is stale — `master` is the source of truth:
```bash
git log --oneline -1                     # master HEAD, e.g. 68e97e1
git ls-tree master -- db.sqlite3 media   # what a fresh clone receives
```
To sync data between machines: commit and push the DB from the machine that changed it, then pull on the other machine (see updating flow below). Never edit the same DB on two machines without syncing in between — SQLite binaries always conflict.

### Updating an existing deployment (git-pull flow)
If the deployment directory is a git clone of this repo (e.g.
`/opt/appdata/groceries` on the Raspberry Pi), updating is one command when
the working tree is clean:
```bash
cd /opt/appdata/groceries
./deploy.sh        # = git pull --ff-only && docker compose build && up -d + health wait
```
Or manually:
```bash
git pull --ff-only
docker compose build && docker compose up -d
docker compose run --rm --no-deps web python manage.py test   # optional sanity check
```
Because `db.sqlite3` and `media/` are tracked, a `pull` **can** conflict or
refuse to run when the local machine has its own data changes:
*   Commit + push data changes before pulling elsewhere:
    `git add db.sqlite3 media && git commit -m "Update data" && git push`.
*   If `git pull --ff-only` says “fetch first / diverged” or “your local
    changes would be overwritten”: commit the local DB first, then
    `git pull --rebase` (binary files cannot auto-merge — keep one side with
    `git checkout --ours/--theirs -- db.sqlite3`, then commit the result).
*   Never run `deploy.sh` with uncommitted receipt/scrape changes — stop the
    stack or wait for the worker to finish writing (`db.sqlite3-wal` should be
    gone), commit, push, then deploy.
Still git-ignored and safe across pulls/builds: `.env`, `staticfiles/`,
`django_cache/`, `*.log`, SQLite sidecars (`db.sqlite3-shm/-wal`).

### Data sources & attribution (product enrichment)

New products are enriched in the background (`groceries-worker`, Django-Q2)
by `tracker/enrichment.py`, highest-confidence source wins per field:

*   **Open Food Facts** (`world.openfoodfacts.org`, free JSON API v2, no key)
    — names, brands, NOVA, Nutri-Score, Eco-Score, nutrition and front images
    for packaged food by GTIN. Data © Open Food Facts contributors, available
    under the **Open Database License (ODbL)**.
*   **Open Beauty Facts** (same platform/API) — same fields for personal-care
    and cosmetic GTINs.
*   **Mercado Livre** — official catalog API when `MELI_ACCESS_TOKEN` is set
    (GTIN-verified entries; see `.env.example`), otherwise the public search
    API, otherwise page scraping as a last resort. Marketplace titles are
    similarity-checked before acceptance, so wrong products are rejected.
*   Local keyword heuristics fill NOVA groups only when no API has data.

Set `ENRICHMENT_CONTACT` (see `.env.example`) so the APIs can identify our
traffic per their usage terms.

---

## 🤝 Development Conventions

*   **Thin Views, Fat Services:** All business and analytical logic must reside in `services.py`.
*   **Data Integrity:** Product identification follows a strict hierarchy: GTIN -> Store Mapping -> Fuzzy Name Match.
*   **Manual Overrides:** The `is_manually_edited` flag in the `Product` model prevents the enrichment engine from overwriting user-provided names or categories.

---

## 📝 License
Distributed under the MIT License. See `LICENSE` for more information.
