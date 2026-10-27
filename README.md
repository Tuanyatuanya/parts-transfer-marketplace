# Parts Transfer Marketplace

An internal web platform that lets manufacturing sites **trade surplus inventory with sites that are short of the same part**, instead of writing it off at one plant while another places a new purchase order.

Built during my 2026 Global Operations Excellence internship at an automotive-interiors supplier. **This public version is sanitized:** company, site and system names are removed and the app ships with a synthetic data generator. No real inventory data is included.

**Stack:** Python · Streamlit · pandas · SQLite · openpyxl

---

## The problem
Each plant exports an inventory snapshot from the ERP (on-hand quantity vs. MRP demand per part). Across dozens of sites in a region, the same part can sit idle at one plant while another is short. Finding those matches by hand meant cross-referencing ~70K-row Excel exports and long email chains for approvals.

## What the app does
| Feature | Details |
|---|---|
| **Data import (admin)** | Reads a folder of regional `.xlsx` exports (one per region). It auto-detects the header row under any title rows, aggregates storage locations to site × part level, and converts local standard cost to EUR. |
| **Recommendation engine** | For each region and part, it computes `net balance = on-hand − MRP demand`. A **greedy surplus → deficit matching** step then proposes transfers with quantity and value in EUR ([`recommendation.py`](recommendation.py)). |
| **Site accounts** | One login per site plus an admin. New sites can request an account, and the admin approves it. |
| **Sell my surplus** | A site sees its own surplus, which sites need each part, and how much is already committed in open orders. It then submits an order with a quantity and price quote. |
| **9-stage order workflow** | Order submitted → buyer accept/reject → price negotiation → logistics review → finance review → contract signing → BPM review → contract approval → done. Some stages need **dual approval** from both sites. A timeline shows progress. |
| **Collaboration** | Each order has a chat thread, and each site has a notification inbox. |
| **Admin oversight** | All orders across sites, plus a surplus/deficit overview by region and site. |

## Recommendation logic
```text
for each region:
    for each part (Type = PG):
        surplus = [(site, net_balance) for sites with net_balance > 0]
        deficit = [(site, |net_balance|) for sites with net_balance < 0]
        walk both lists, transferring min(surplus, need) until one side is exhausted
```
Matching stays inside a region because cross-region shipping is rarely economical. Committed quantities from open orders are subtracted, so the same surplus isn't offered twice.

## Run locally
```bash
pip install -r requirements.txt
python sample_data/generate_sample_data.py     # creates synthetic EMEA / NA snapshots
streamlit run app.py
```
1. Log in as `admin` / `admin123` (demo credentials).
2. Go to **Import Data**, paste the full path of the `sample_data/` folder, then click **Read & Preview** → **Save to Database**.
3. Log out and log in as a site, e.g. `Riverside Plant` / `site123`. Site accounts are created automatically on import.

On the synthetic data, the engine produces about 200 transfer recommendations across 9 fictional sites.

## Project structure
```
├── app.py                 # Streamlit UI: login, pages, order workflow
├── db.py                  # SQLite schema, accounts, stage/state machine
├── recommendation.py      # Excel reader, aggregation, transfer-matching engine
├── sample_data/
│   └── generate_sample_data.py
└── .streamlit/config.toml
```

## Notes
- This is an MVP. Passwords use unsalted SHA-256, so a real deployment should use SSO or a salted hash.
- To move to PostgreSQL or SQL Server, swap `get_connection()` in `db.py`.
