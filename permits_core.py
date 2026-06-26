#!/usr/bin/env python3
"""
Core data logic for the CEOP permits map app — kept free of any Streamlit/folium
imports so it can be unit-tested and reused on its own.

Responsibilities:
  * load permits from SQLite, joined with cached geocoding coordinates
  * derive the "case group" (building) key from a case number
  * filter permits by investor / date / status / type / free text
  * aggregate permits into one row per building-group (for map markers + table)
  * build a fully documented dashboard_data.json of the calculated figures
"""

import json
import re
import sqlite3
from datetime import datetime

import pandas as pd

# Three-segment prefix: LETTERS - LETTERS - DIGITS, e.g. ROP-NSD-36653
_GROUP_RE = re.compile(r"^\s*([^\-\s]+-[^\-\s]+-\d+)")


def case_group(case_number) -> str | None:
    """ROP-NSD-36653-IUP-24/2025  ->  ROP-NSD-36653.

    Multiple submissions for the same building share this prefix. Falls back to
    the first three hyphen-separated tokens, then to the whole string.
    """
    if not case_number:
        return None
    cn = str(case_number).strip()
    m = _GROUP_RE.match(cn)
    if m:
        return m.group(1)
    parts = cn.split("-")
    return "-".join(parts[:3]) if len(parts) >= 3 else cn


def load_permits(db_path: str) -> pd.DataFrame:
    """Load all permits joined with geocoding coordinates and derived columns."""
    conn = sqlite3.connect(db_path)
    try:
        permits = pd.read_sql_query("SELECT * FROM permits", conn)
        try:
            geo = pd.read_sql_query(
                "SELECT address, lat, lon, status AS geo_status FROM geocode_cache",
                conn,
            )
        except Exception:
            geo = pd.DataFrame(columns=["address", "lat", "lon", "geo_status"])
    finally:
        conn.close()

    if not geo.empty:
        permits = permits.merge(geo, on="address", how="left")
    else:
        permits["lat"] = pd.NA
        permits["lon"] = pd.NA
        permits["geo_status"] = pd.NA

    permits["case_group"] = permits["case_number"].map(case_group)
    permits["created_dt"] = pd.to_datetime(permits["created_date"], errors="coerce")
    permits["created_year"] = permits["created_dt"].dt.year
    permits["investor_clean"] = permits.get("investors", "").fillna("").str.strip()
    permits["lat"] = pd.to_numeric(permits["lat"], errors="coerce")
    permits["lon"] = pd.to_numeric(permits["lon"], errors="coerce")
    return permits


def filter_permits(
    df: pd.DataFrame,
    investor: str = "",
    location: str = "",
    case_text: str = "",
    statuses=None,
    types=None,
    date_from=None,
    date_to=None,
) -> pd.DataFrame:
    """Apply the UI filters. Empty/None filters are ignored."""
    out = df
    if investor:
        out = out[out["investor_clean"].str.contains(investor, case=False, na=False)]
    if location:
        out = out[out["address"].fillna("").str.contains(location, case=False, na=False)]
    if case_text:
        mask = out["case_number"].fillna("").str.contains(case_text, case=False, na=False) | \
               out["case_group"].fillna("").str.contains(case_text, case=False, na=False)
        out = out[mask]
    if statuses:
        out = out[out["status"].isin(statuses)]
    if types:
        out = out[out["submission_type"].isin(types)]
    if date_from is not None:
        out = out[out["created_dt"] >= pd.Timestamp(date_from)]
    if date_to is not None:
        # inclusive of the whole end day
        out = out[out["created_dt"] < pd.Timestamp(date_to) + pd.Timedelta(days=1)]
    return out


def _first_or_none(series):
    s = series.dropna()
    return s.iloc[0] if not s.empty else None


def aggregate_groups(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse permits into one row per building-group (case-number prefix)."""
    if df.empty:
        return pd.DataFrame(
            columns=[
                "case_group", "permit_count", "investors", "address", "lat", "lon",
                "first_date", "last_date", "statuses", "submission_types", "case_numbers",
            ]
        )

    records = []
    for key, sub in df.groupby("case_group"):
        coords = sub.dropna(subset=["lat", "lon"])
        addr_mode = sub["address"].dropna().mode()
        records.append(
            {
                "case_group": key,
                "permit_count": len(sub),
                "investors": "; ".join(sorted({i for i in sub["investor_clean"] if i})),
                "address": addr_mode.iloc[0] if not addr_mode.empty else _first_or_none(sub["address"]),
                "lat": coords["lat"].iloc[0] if not coords.empty else None,
                "lon": coords["lon"].iloc[0] if not coords.empty else None,
                "first_date": sub["created_dt"].min(),
                "last_date": sub["created_dt"].max(),
                "statuses": "; ".join(sorted({s for s in sub["status"].dropna()})),
                "submission_types": "; ".join(sorted({t for t in sub["submission_type"].dropna()})),
                "case_numbers": "; ".join(sorted(sub["case_number"].dropna())),
            }
        )
    out = pd.DataFrame(records).sort_values("permit_count", ascending=False)
    return out.reset_index(drop=True)


def _counts(series) -> dict:
    return {str(k): int(v) for k, v in series.value_counts(dropna=True).items()}


def build_dashboard_data(permits: pd.DataFrame, groups: pd.DataFrame,
                         filters_applied: dict | None = None) -> dict:
    """Build the documented JSON of all calculated figures.

    Every number lives under a clear key and is explained in `_documentation`.
    """
    geocoded_groups = int(groups["lat"].notna().sum()) if not groups.empty else 0
    years = [int(y) for y in permits["created_year"].dropna().unique()]
    dates = permits["created_dt"].dropna()

    data = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "filters_applied": filters_applied or {},
        "summary": {
            "total_permits": int(len(permits)),
            "total_building_groups": int(len(groups)),
            "geocoded_groups": geocoded_groups,
            "ungeocoded_groups": int(len(groups) - geocoded_groups),
            "distinct_investors": int(permits["investor_clean"].replace("", pd.NA).nunique()),
            "date_min": dates.min().isoformat() if not dates.empty else None,
            "date_max": dates.max().isoformat() if not dates.empty else None,
        },
        "permits_by_status": _counts(permits["status"]),
        "permits_by_submission_type": _counts(permits["submission_type"]),
        "permits_by_year": {str(int(k)): int(v)
                            for k, v in permits["created_year"].dropna().astype(int)
                            .value_counts().sort_index().items()},
        "top_investors_by_permit_count": dict(
            sorted(_counts(permits["investor_clean"].replace("", pd.NA).dropna()).items(),
                   key=lambda kv: kv[1], reverse=True)[:20]
        ),
        "groups": [
            {
                "case_group": r["case_group"],
                "permit_count": int(r["permit_count"]),
                "investors": r["investors"],
                "address": r["address"],
                "lat": None if pd.isna(r["lat"]) else float(r["lat"]),
                "lon": None if pd.isna(r["lon"]) else float(r["lon"]),
                "first_date": None if pd.isna(r["first_date"]) else r["first_date"].isoformat(),
                "last_date": None if pd.isna(r["last_date"]) else r["last_date"].isoformat(),
                "statuses": r["statuses"],
                "submission_types": r["submission_types"],
                "case_numbers": r["case_numbers"],
            }
            for _, r in groups.iterrows()
        ],
        "_documentation": {
            "generated_at": "ISO timestamp when this file was produced.",
            "filters_applied": "The filter values active when this snapshot was exported (empty = no filter).",
            "summary.total_permits": "Count of individual permit submissions after filters.",
            "summary.total_building_groups": "Count of distinct buildings, where a building = the case-number prefix (e.g. ROP-NSD-36653). Multiple submissions for one building collapse into one group.",
            "summary.geocoded_groups": "Building-groups that have map coordinates (address was successfully geocoded).",
            "summary.ungeocoded_groups": "Building-groups with no coordinates yet (address not geocoded or not found); not shown on the map.",
            "summary.distinct_investors": "Number of unique non-empty investor strings among the filtered permits.",
            "summary.date_min": "Earliest permit CreatedDate among the filtered permits.",
            "summary.date_max": "Latest permit CreatedDate among the filtered permits.",
            "permits_by_status": "Permit counts keyed by registry status (StatusName).",
            "permits_by_submission_type": "Permit counts keyed by submission type (SubmissionTypeName), e.g. building permit, use permit.",
            "permits_by_year": "Permit counts keyed by the year of CreatedDate.",
            "top_investors_by_permit_count": "Up to 20 investors with the most permits (key = investor, value = permit count).",
            "groups": "One entry per building-group. permit_count = submissions for that building; lat/lon = map position (null if not geocoded); first_date/last_date = span of its submissions; case_numbers = all submission numbers in the group.",
        },
    }
    return data


def write_dashboard_json(data: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
