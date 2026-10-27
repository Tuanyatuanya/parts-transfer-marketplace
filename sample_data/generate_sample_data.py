"""
Generate synthetic inventory snapshots (one .xlsx per region) for the demo.

All site names, part numbers, quantities and costs are randomly generated —
no real company data is used.

    python sample_data/generate_sample_data.py
"""
from pathlib import Path

import numpy as np
import pandas as pd

OUT_DIR = Path(__file__).parent
RNG = np.random.default_rng(7)

REGIONS = {
    "emea": {
        "sites": ["Northbridge Plant", "Riverside Plant", "Lakeview Plant", "Hillcrest Plant", "Westport Plant"],
        "currency_rate": [1.0, 1.0, 25.3, 4.35, 1.0],   # local currency per EUR
    },
    "na": {
        "sites": ["Maple Valley Plant", "Cedar Point Plant", "Pine Ridge Plant", "Oak Harbor Plant"],
        "currency_rate": [1.08, 1.08, 1.08, 18.6],
    },
}
PART_FAMILIES = ["Clip", "Bracket", "Foam Pad", "Fastener", "Trim Panel", "Seal", "Harness", "Bezel"]
N_ITEMS = 120


def build_region(region: str, cfg: dict) -> pd.DataFrame:
    items = [f"PN-{region.upper()}-{i:04d}" for i in range(N_ITEMS)]
    desc = {it: f"{RNG.choice(PART_FAMILIES)} {RNG.integers(10, 99)}" for it in items}
    base_cost = {it: round(float(RNG.lognormal(1.5, 0.9)), 2) for it in items}
    rows = []
    for s_idx, site in enumerate(cfg["sites"]):
        rate = cfg["currency_rate"][s_idx]
        for it in RNG.choice(items, size=int(N_ITEMS * 0.7), replace=False):
            mrp = int(RNG.integers(0, 2000))
            qoh = max(0, int(mrp + RNG.normal(0, 600)))
            type_trim = RNG.choice(["PG", "PG", "PG", "RM"])
            # split each item over 1–3 storage locations so the app has to aggregate
            n_loc = int(RNG.integers(1, 4))
            q_parts = RNG.multinomial(qoh, [1 / n_loc] * n_loc)
            m_parts = RNG.multinomial(mrp, [1 / n_loc] * n_loc)
            for q, m in zip(q_parts, m_parts):
                rows.append({
                    "Region": region.upper(),
                    "Site": f"S{100 + s_idx}",
                    "Site Name": site,
                    "Item": it,
                    "Description": desc[it],
                    "Type Trim": type_trim,
                    "Total QOH": int(q),
                    "Total MRP": int(m),
                    "Local STD Cost": round(base_cost[it] * rate, 2),
                    "Euro XC Rate": rate,
                })
    return pd.DataFrame(rows)


def main():
    for region, cfg in REGIONS.items():
        df = build_region(region, cfg)
        path = OUT_DIR / f"inventory_snapshot_{region}.xlsx"
        with pd.ExcelWriter(path, engine="openpyxl") as w:
            # two title rows above the header, like a typical ERP export
            pd.DataFrame([["Inventory Snapshot (synthetic demo data)"], [f"Region: {region.upper()}"]]).to_excel(
                w, index=False, header=False, startrow=0)
            df.to_excel(w, index=False, startrow=3)
        print(f"wrote {path.name}: {len(df):,} rows, {df['Site Name'].nunique()} sites")


if __name__ == "__main__":
    main()
