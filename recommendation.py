"""
Inventory-balancing recommendation engine.

Implements the same surplus/deficit matching logic used in the offline transfer tool so the
same surplus/deficit matching logic powers the web platform's "what you can
buy / receive" recommendations. Matching is done PER REGION (mirroring the
separate EMEA / NA result files), only for Type Trim == 'PG'.
"""

import os
import pandas as pd

REQUIRED_COLUMNS = [
    "Site Name",
    "Item",
    "Type Trim",
    "Total QOH",
    "Total MRP",
    "Local STD Cost",
    "Euro XC Rate",
]

# Optional columns we keep when present
OPTIONAL_COLUMNS = ["Region", "Site", "Description"]


def _find_header_idx(raw: pd.DataFrame, required_columns) -> int:
    """Find the header row index within an already-read (header=None) frame."""
    required_set = set(required_columns)
    for idx, row in raw.iterrows():
        values = {
            str(v).strip() for v in row.tolist()
            if pd.notna(v) and str(v).strip() != ""
        }
        if required_set.issubset(values):
            return idx
    raise ValueError(
        "Could not find a header row containing all required columns: "
        + ", ".join(required_columns)
    )


def read_and_aggregate(input_file: str) -> pd.DataFrame:
    """
    Read an ASL report Excel and aggregate by Region/Site/Item/Type Trim.
    Returns an aggregated DataFrame with computed Unit Cost EUR and Net_Balance.
    """
    ext = os.path.splitext(input_file)[1].lower()
    if ext == ".xls":
        raise ValueError("Old .xls format is not supported. Please save as .xlsx.")
    if ext not in (".xlsx", ".xlsm"):
        raise ValueError(f"Unsupported file format: {ext}. Use .xlsx or .xlsm.")

    # Read the whole sheet ONCE (header=None), then locate header and slice in memory.
    raw = pd.read_excel(input_file, engine="openpyxl", header=None)
    header_row = _find_header_idx(raw, REQUIRED_COLUMNS)

    df = raw.iloc[header_row + 1:].copy()
    df.columns = [str(c).strip() if pd.notna(c) else "" for c in raw.iloc[header_row].tolist()]
    df = df.dropna(how="all").reset_index(drop=True)

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Input file is missing required columns: {missing}")

    # Ensure optional columns exist
    for c in OPTIONAL_COLUMNS:
        if c not in df.columns:
            df[c] = ""

    # Clean text
    for c in ["Region", "Site", "Site Name", "Item", "Type Trim", "Description"]:
        df[c] = df[c].astype(str).str.strip()

    # Numeric coercion
    df["Total QOH"] = pd.to_numeric(df["Total QOH"], errors="coerce").fillna(0)
    df["Total MRP"] = pd.to_numeric(df["Total MRP"], errors="coerce").fillna(0)
    df["Local STD Cost"] = pd.to_numeric(df["Local STD Cost"], errors="coerce").fillna(0)
    df["Euro XC Rate"] = pd.to_numeric(df["Euro XC Rate"], errors="coerce").fillna(1)

    # Vectorized aggregation (no per-group python lambdas — fast on ~70k rows).
    # "first non-zero / non-empty" is done by masking to NaN, taking groupby-first
    # (which skips NaN), then filling the fallback.
    keys = ["Region", "Site", "Site Name", "Item", "Type Trim"]
    df["_cost"] = df["Local STD Cost"].where(df["Local STD Cost"] != 0)
    df["_rate"] = df["Euro XC Rate"].where(df["Euro XC Rate"] != 0)
    df["_desc"] = df["Description"].where(df["Description"].str.strip() != "")

    grouped = df.groupby(keys, as_index=False)
    agg = grouped.agg(
        **{
            "Total QOH": ("Total QOH", "sum"),
            "Total MRP": ("Total MRP", "sum"),
            "Local STD Cost": ("_cost", "first"),
            "Euro XC Rate": ("_rate", "first"),
            "Description": ("_desc", "first"),
        }
    )
    agg["Local STD Cost"] = agg["Local STD Cost"].fillna(0)
    agg["Euro XC Rate"] = agg["Euro XC Rate"].fillna(1)
    agg["Description"] = agg["Description"].fillna("")

    def unit_cost_eur(local_cost, rate):
        try:
            if pd.isna(rate) or abs(rate) < 1e-9:
                return 0.0
            return local_cost / rate
        except Exception:
            return 0.0

    agg["Unit Cost EUR"] = agg.apply(
        lambda r: unit_cost_eur(r["Local STD Cost"], r["Euro XC Rate"]), axis=1
    )
    agg["Net_Balance"] = agg["Total QOH"] - agg["Total MRP"]
    return agg


def generate_transfer_plan(agg_df: pd.DataFrame) -> pd.DataFrame:
    """
    Greedy surplus->deficit matching per Region, per Item, for Type Trim == 'PG'.
    Returns a transfer-plan DataFrame.
    """
    cols = [
        "Region", "Item", "Description", "From Site", "To Site", "Transfer Qty",
        "Local STD Cost", "Euro XC Rate", "Unit Cost EUR", "Transfer Value EUR",
    ]
    if agg_df.empty:
        return pd.DataFrame(columns=cols)

    pg = agg_df[agg_df["Type Trim"] == "PG"].copy()
    if pg.empty:
        return pd.DataFrame(columns=cols)

    logs = []
    for region, region_data in pg.groupby("Region"):
        for item, item_data in region_data.groupby("Item"):
            surplus = [
                [r["Site Name"], r["Net_Balance"]]
                for _, r in item_data.iterrows() if r["Net_Balance"] > 0
            ]
            deficit = [
                [r["Site Name"], abs(r["Net_Balance"])]
                for _, r in item_data.iterrows() if r["Net_Balance"] < 0
            ]
            if not surplus or not deficit:
                continue

            s_idx = d_idx = 0
            while s_idx < len(surplus) and d_idx < len(deficit):
                src_site, src_qty = surplus[s_idx][0], surplus[s_idx][1]
                dst_site, need_qty = deficit[d_idx][0], deficit[d_idx][1]
                amount = min(src_qty, need_qty)

                if amount > 0:
                    cost_row = item_data[item_data["Site Name"] == src_site]
                    if not cost_row.empty:
                        local_std = cost_row["Local STD Cost"].iloc[0]
                        rate = cost_row["Euro XC Rate"].iloc[0]
                        uc_eur = cost_row["Unit Cost EUR"].iloc[0]
                        desc = cost_row["Description"].iloc[0]
                    else:
                        local_std, rate, uc_eur, desc = 0, 1, 0, ""
                    logs.append({
                        "Region": region,
                        "Item": item,
                        "Description": desc,
                        "From Site": src_site,
                        "To Site": dst_site,
                        "Transfer Qty": amount,
                        "Local STD Cost": local_std,
                        "Euro XC Rate": rate,
                        "Unit Cost EUR": uc_eur,
                        "Transfer Value EUR": amount * uc_eur,
                    })

                surplus[s_idx][1] -= amount
                deficit[d_idx][1] -= amount
                if surplus[s_idx][1] <= 1e-9:
                    s_idx += 1
                if deficit[d_idx][1] <= 1e-9:
                    d_idx += 1

    return pd.DataFrame(logs, columns=cols)
