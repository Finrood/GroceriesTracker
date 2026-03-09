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

*   **Backend:** Python 3.12+ / Django 6.0+
*   **Database:** SQLite (Development/Portability) / PostgreSQL (Production)
*   **Task Queue:** `Django-Q2` for asynchronous product enrichment and scraping.
*   **Data Science:** `RapidFuzz` for semantic product matching and `Hypothesis` for property-based testing.
*   **Visualization:** **ECharts** for advanced interactive charts (Candlesticks, Heatmaps, Radar Charts).
*   **Security:** Scraper domain whitelisting (SSRF protection) and normalized product identification (GTIN -> Internal Store Code -> Fuzzy Name).

---

## 🚀 Getting Started

### Prerequisites
*   Python 3.12+
*   Docker & Docker Compose (Optional)

### Standard Setup
```bash
# 1. Clone the repository
git clone https://github.com/yourusername/GroceriesTracker.git
cd GroceriesTracker

# 2. Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure Environment
cp .env.example .env  # Ensure SECRET_KEY and DEBUG are set

# 5. Run migrations
python manage.py migrate

# 6. Start the Task Worker (Required for product enrichment/scraping)
# Open a second terminal window and run:
python manage.py qcluster

# 7. Start the Web Server
python manage.py runserver
```

### Docker Deployment (Recommended)
The fastest way to get everything running (Web + Worker) is via Docker Compose:
```bash
docker-compose up --build
```
*   **Web:** Accessible at `http://localhost:8000`
*   **Worker:** Automatically starts and handles background product enrichment and data processing.

---

## 🛠️ Project Structure

*   `tracker/scraper.py`: The `NFCeScraper` engine for SEFAZ data extraction.
*   `tracker/services.py`: Business logic for Analytics, Smart Cart, and Receipt processing.
*   `tracker/enrichment.py`: External API integration for product metadata.
*   `tracker/models.py`: Robust schema with normalization for units (e.g., converting '5KG' to '5kg').
*   `media/`: Persistent storage for product images.
*   `db.sqlite3`: Included in the repository for immediate data portability.

---

## 🤝 Development Conventions

*   **Thin Views, Fat Services:** All business and analytical logic must reside in `services.py`.
*   **Data Integrity:** Product identification follows a strict hierarchy: GTIN -> Store Mapping -> Fuzzy Name Match.
*   **Manual Overrides:** The `is_manually_edited` flag in the `Product` model prevents the enrichment engine from overwriting user-provided names or categories.

---

## 📝 License
Distributed under the MIT License. See `LICENSE` for more information.
