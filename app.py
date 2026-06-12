"""
Listing Monitor - local UI.
Run:  streamlit run app.py
"""

import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import audit_core as core

load_dotenv()

st.set_page_config(page_title="Listing Monitor", page_icon="\U0001f6d2", layout="wide")

RED = "background-color:#FF0000;color:#FFFFFF"


def secret(name: str, default: str = "") -> str:
    """Read from Streamlit Cloud secrets first, then environment/.env."""
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:  # noqa: BLE001 - no secrets file locally
        pass
    return os.environ.get(name, default)


# ------------------------------------------------- password gate (public URL)
APP_PASSWORD = secret("APP_PASSWORD")
if APP_PASSWORD:
    if not st.session_state.get("authed"):
        pw = st.text_input("Password", type="password")
        if pw and pw == APP_PASSWORD:
            st.session_state["authed"] = True
            st.rerun()
        elif pw:
            st.error("Wrong password.")
        st.stop()

# ------------------------------------------------------------------ sidebar
st.sidebar.title("\U0001f6d2 Listing Monitor")

api_key = secret("RAPIDAPI_KEY")
if not os.environ.get("LM_DB_PATH") and secret("HOSTED", ""):
    st.sidebar.warning("Hosted test mode: history resets when the app redeploys or sleeps.")
if api_key:
    st.sidebar.success("API key loaded")
else:
    st.sidebar.error("RAPIDAPI_KEY missing - set it in Streamlit secrets (cloud) or .env (local)")

pacing = st.sidebar.slider("Pacing between API calls (seconds)", 0.2, 2.0, 0.5, 0.1)

st.sidebar.divider()
st.sidebar.caption(
    "Red cells = live Amazon data does not match your Masterfile "
    "(Title / Sale Price / # of Reviews only). Empty Masterfile cells are not judged."
)

tab_audit, tab_master, tab_history = st.tabs(["Audit", "Masterfile", "History"])

# ------------------------------------------------------------------- audit
with tab_audit:
    col_run, col_info = st.columns([1, 3])
    with col_run:
        run_clicked = st.button("\u25b6 Run Full Audit", type="primary", use_container_width=True,
                                disabled=not api_key)
    with col_info:
        n_master = len(core.load_masterfile())
        st.caption(f"{n_master} rows in Masterfile. Each run = {n_master} API calls.")

    if run_clicked:
        bar = st.progress(0.0, text="Starting...")

        def _cb(done, total, label):
            bar.progress(min(done / max(total, 1), 1.0), text=f"{done}/{total}  {label}")

        try:
            run_id = core.run_audit(api_key, pacing_seconds=pacing, progress_cb=_cb)
            st.success(f"Run #{run_id} complete.")
        except Exception as e:  # noqa: BLE001
            st.error(str(e))

    runs = core.list_runs()
    if not runs:
        st.info("No runs yet. Import your Masterfile, then run an audit.")
    else:
        run_labels = {
            r["id"]: f"Run #{r['id']}  -  {r['started_at'][:19].replace('T', ' ')} UTC  -  "
                     f"{r['ok']}/{r['total']} ok  -  {r['mismatched_rows']} mismatched"
            for r in runs
        }
        sel = st.selectbox("Run", options=list(run_labels), format_func=lambda i: run_labels[i])
        results = core.get_results(sel)

        rows, styles = [], []
        for res in results:
            if res["error"]:
                rows.append({
                    "Input": res["input"], "Marketplace": res["marketplace"] or "",
                    "Title": f"ERROR: {res['error']}", "Sale Price": "", "# of Reviews": "",
                    "List Price": "", "Buybox Winner": "", "Strikethrough?": "",
                    "Discount %": "", "Main Image": "",
                })
                styles.append({"Title": "background-color:#FFA500;color:#000000"})
                continue
            live = res["live"]
            rows.append({
                "Input": res["input"], "Marketplace": res["marketplace"] or "",
                "Title": live.get("Title", ""), "Sale Price": live.get("Sale Price", ""),
                "# of Reviews": live.get("# of Reviews", ""), "List Price": live.get("List Price", ""),
                "Buybox Winner": live.get("Buybox Winner", ""),
                "Strikethrough?": live.get("Strikethrough?", ""), "Discount %": live.get("Discount %", ""),
                "Main Image": live.get("Main Image", ""),
            })
            styles.append({
                "Title": RED if res["title_match"] == 0 else "",
                "Sale Price": RED if res["price_match"] == 0 else "",
                "# of Reviews": RED if res["reviews_match"] == 0 else "",
            })

        df = pd.DataFrame(rows)
        style_df = pd.DataFrame(styles).reindex(columns=df.columns, fill_value="").fillna("")
        st.dataframe(
            df.style.apply(lambda _: style_df, axis=None),
            use_container_width=True, height=560,
            column_config={"Main Image": st.column_config.ImageColumn("Main Image", width="small")},
        )

        mismatch_only = st.checkbox("Show mismatched rows only")
        if mismatch_only:
            mask = [
                (r["error"] is not None) or 0 in (r["title_match"], r["price_match"], r["reviews_match"])
                for r in results
            ]
            st.dataframe(
                df[pd.Series(mask).values], use_container_width=True,
                column_config={"Main Image": st.column_config.ImageColumn(width="small")},
            )

# -------------------------------------------------------------- masterfile
with tab_master:
    st.subheader("Masterfile (source of truth)")
    st.caption(
        "Import once from Google Sheets: open the Masterfile tab, File > Download > "
        "Comma Separated Values (.csv), then upload it here. After that, edit directly below."
    )

    up = st.file_uploader("Import Masterfile CSV", type=["csv"])
    if up is not None:
        imp = pd.read_csv(up, dtype=str).fillna("")
        missing = [c for c in core.MASTER_COLUMNS if c not in imp.columns]
        if missing:
            st.error(f"CSV is missing columns: {missing}. Export the Masterfile tab unchanged.")
        else:
            core.save_masterfile_rows(imp[core.MASTER_COLUMNS].to_dict("records"))
            st.success(f"Imported {len(imp)} rows. This replaced the previous Masterfile.")

    current = core.load_masterfile()
    df_master = (pd.DataFrame(current, columns=core.MASTER_COLUMNS)
                 if current else pd.DataFrame(columns=core.MASTER_COLUMNS))
    edited = st.data_editor(df_master, num_rows="dynamic", use_container_width=True, height=480)
    if st.button("\U0001f4be Save Masterfile changes"):
        core.save_masterfile_rows(edited.fillna("").to_dict("records"))
        st.success("Saved. Next audit run will compare against these values.")

# ----------------------------------------------------------------- history
with tab_history:
    st.subheader("Per-ASIN history")
    conn = core.get_db()
    asins = [r[0] for r in conn.execute(
        "SELECT DISTINCT asin FROM results WHERE asin IS NOT NULL ORDER BY asin")]
    conn.close()
    if not asins:
        st.info("No history yet - run at least one audit.")
    else:
        asin = st.selectbox("ASIN", asins)
        hist = core.asin_history(asin)
        hdf = pd.DataFrame([
            {
                "When (UTC)": h["created_at"][:19].replace("T", " "),
                "Sale Price": h["sale_price_num"],
                "# of Reviews": h["reviews_num"],
                "Title": h["live"].get("Title", ""),
                "Buybox": h["live"].get("Buybox Winner", ""),
                "Mismatch": "YES" if 0 in (h["title_match"], h["price_match"], h["reviews_match"]) else "",
                "Error": h["error"] or "",
            }
            for h in hist
        ])
        price_series = hdf.dropna(subset=["Sale Price"])
        if len(price_series) > 1:
            st.line_chart(price_series.set_index("When (UTC)")["Sale Price"])
        st.dataframe(hdf, use_container_width=True)
