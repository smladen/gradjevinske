#!/usr/bin/env python3
"""
CEOP permits map & search app (Streamlit).

Reads the scraped SQLite database, groups submissions by building (case-number
prefix, e.g. ROP-NSD-36653), and shows them on an interactive map with search
filters for investor, location, date, status and type.

Run:
    streamlit run permits_app.py

Coordinates come from the geocode_cache table — run  python geocode_permits.py
first so permits can be placed on the map.
"""

import html
import json

import pandas as pd
import streamlit as st
import folium
from folium.plugins import MarkerCluster
from streamlit_folium import st_folium

import permits_core as core

DB_PATH = "ceop_permits.db"
JSON_OUT = "dashboard_data.json"
NOVI_SAD = (45.2671, 19.8335)

st.set_page_config(page_title="CEOP dozvole — mapa i pretraga", layout="wide")

# Trim Streamlit's default empty space at the top of the page.
st.markdown(
    "<style>.block-container{padding-top:1.5rem;}</style>",
    unsafe_allow_html=True,
)


@st.cache_data(show_spinner="Učitavanje zahteva…")
def get_data(db_path: str) -> pd.DataFrame:
    return core.load_permits(db_path)


def fmt_date(ts):
    return "" if pd.isna(ts) else pd.Timestamp(ts).strftime("%Y-%m-%d")


def popup_html(row) -> str:
    cases = "<br>".join(html.escape(c) for c in str(row["case_numbers"]).split("; "))
    return f"""
    <div style="font-family:sans-serif;font-size:13px;min-width:240px">
      <b>{html.escape(str(row['case_group']))}</b>
      &nbsp;<span style="color:#555">({row['permit_count']} zahteva)</span><br>
      <b>Adresa:</b> {html.escape(str(row['address'] or ''))}<br>
      <b>Investitor:</b> {html.escape(str(row['investors'] or ''))}<br>
      <b>Period:</b> {fmt_date(row['first_date'])} – {fmt_date(row['last_date'])}<br>
      <b>Status:</b> {html.escape(str(row['statuses'] or ''))}<br>
      <b>Tipovi:</b> {html.escape(str(row['submission_types'] or ''))}<br>
      <details><summary>Brojevi predmeta</summary>{cases}</details>
    </div>
    """


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #
try:
    permits = get_data(DB_PATH)
except Exception as exc:
    st.error(f"Nije moguće pročitati {DB_PATH}: {exc}")
    st.stop()

if permits.empty:
    st.warning("Baza još nema zahteva. Prvo pokrenite skrejper.")
    st.stop()

st.title("CEOP građevinske dozvole — mapa i pretraga")

# --------------------------------------------------------------------------- #
# Sidebar filters
# --------------------------------------------------------------------------- #
sb = st.sidebar
sb.header("Filteri")

investor = sb.text_input("Investitor sadrži")
location = sb.text_input("Lokacija / adresa sadrži")
case_text = sb.text_input("Broj predmeta / grupa sadrži")

status_opts = sorted(permits["status"].dropna().unique())
type_opts = sorted(permits["submission_type"].dropna().unique())
statuses = sb.multiselect("Status", status_opts)
types = sb.multiselect("Tip zahteva", type_opts)

dmin = permits["created_dt"].min()
dmax = permits["created_dt"].max()
# Anchor relative windows on the most recent of today / latest permit.
anchor = max(pd.Timestamp.now().normalize(), dmax.normalize())

period = sb.selectbox(
    "Period",
    ["Poslednjih 7 dana", "Poslednjih 30 dana", "Poslednjih 6 godina",
     "Sve vreme", "Prilagođeno"],
    index=2,  # default: last 6 years
)
if period == "Prilagođeno":
    rng = sb.date_input(
        "Opseg datuma (kreirano)",
        value=(dmin.date(), dmax.date()),
        min_value=dmin.date(),
        max_value=dmax.date(),
    )
    if isinstance(rng, (tuple, list)) and len(rng) == 2:
        date_from, date_to = rng
    else:
        date_from, date_to = dmin.date(), dmax.date()
elif period == "Sve vreme":
    date_from, date_to = dmin.date(), dmax.date()
else:
    _deltas = {
        "Poslednjih 7 dana": pd.Timedelta(days=7),
        "Poslednjih 30 dana": pd.Timedelta(days=30),
        "Poslednjih 6 godina": pd.DateOffset(years=6),
    }
    date_to = anchor.date()
    date_from = (anchor - _deltas[period]).date()
    sb.caption(f"Period: {date_from} – {date_to}")

only_mapped = sb.checkbox("Mapa: prikaži samo geokodirane objekte", value=True)

filters_applied = {
    "investor": investor, "location": location, "case_text": case_text,
    "statuses": statuses, "types": types,
    "date_from": str(date_from), "date_to": str(date_to),
}

# --------------------------------------------------------------------------- #
# Apply filters + aggregate
# --------------------------------------------------------------------------- #
filtered = core.filter_permits(
    permits, investor=investor, location=location, case_text=case_text,
    statuses=statuses, types=types, date_from=date_from, date_to=date_to,
)
groups = core.aggregate_groups(filtered)
mapped = groups.dropna(subset=["lat", "lon"])

# Documented JSON of the current view (written to disk + offered for download).
dash = core.build_dashboard_data(filtered, groups, filters_applied)
try:
    core.write_dashboard_json(dash, JSON_OUT)
except Exception:
    pass

# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
c1, c2, c3, c4 = st.columns(4)
c1.metric("Zahtevi", len(filtered))
c2.metric("Objekti (grupe)", len(groups))
c3.metric("Objekti na mapi", len(mapped))
c4.metric("Investitori", dash["summary"]["distinct_investors"])

if permits["lat"].notna().sum() == 0:
    st.info(
        "Još nema koordinata — pokrenite  `python geocode_permits.py`  da bi se "
        "zahtevi prikazali na mapi. Pretraga radi i bez toga."
    )

# --------------------------------------------------------------------------- #
# Map
# --------------------------------------------------------------------------- #
fmap = folium.Map(location=list(NOVI_SAD), zoom_start=12, tiles="OpenStreetMap")
cluster = MarkerCluster().add_to(fmap)
for _, row in mapped.iterrows():
    # CircleMarker is a vector (SVG) marker — no external icon image, so it
    # avoids the broken Leaflet CDN icon/shadow PNGs that 404.
    folium.CircleMarker(
        location=[row["lat"], row["lon"]],
        radius=7,
        color="#1f6feb",
        weight=1,
        fill=True,
        fill_color="#1f6feb",
        fill_opacity=0.85,
        popup=folium.Popup(popup_html(row), max_width=320),
        tooltip=f"{row['case_group']} ({row['permit_count']})",
    ).add_to(cluster)

if not mapped.empty:
    fmap.fit_bounds([[mapped["lat"].min(), mapped["lon"].min()],
                     [mapped["lat"].max(), mapped["lon"].max()]])

st_folium(fmap, use_container_width=True, height=760, returned_objects=[])

if only_mapped:
    st.caption(f"Prikazano {len(mapped)} od {len(groups)} objekata sa koordinatama.")

# --------------------------------------------------------------------------- #
# Downloads
# --------------------------------------------------------------------------- #
group_view = groups.copy()
group_view["first_date"] = group_view["first_date"].map(fmt_date)
group_view["last_date"] = group_view["last_date"].map(fmt_date)

d1, d2, d3 = st.columns(3)
d1.download_button(
    "⬇ dashboard_data.json (dokumentovano)",
    data=json.dumps(dash, ensure_ascii=False, indent=2).encode("utf-8"),
    file_name="dashboard_data.json", mime="application/json",
)
d2.download_button(
    "⬇ grupe.csv",
    data=group_view.to_csv(index=False).encode("utf-8-sig"),
    file_name="permit_groups.csv", mime="text/csv",
)
d3.download_button(
    "⬇ zahtevi.csv (filtrirano)",
    data=filtered.to_csv(index=False).encode("utf-8-sig"),
    file_name="permits_filtered.csv", mime="text/csv",
)
