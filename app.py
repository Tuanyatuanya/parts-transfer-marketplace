"""
Internal Parts Marketplace — Streamlit (seller-initiated edition)
Run:  streamlit run app.py

Flow (seller drives):
  Site A finds its own surplus (slow-moving stock) and which sites lack the part (Site B),
  submits an order (qty + unit-price quote + total) to Site B.
  → Site B accepts / rejects
  → both sides negotiate price (editable) until agreed
  → Logistics review (A & B, dual approval)
  → Finance review (A & B, dual approval)
  → Contract signing (dual approval)
  → Workflow (BPM) review
  → Approve internal sales contract
  → Approved internal sales contract (done)

Both sides can advance the status; a horizontal timeline shows progress
(green = done, yellow = current, red = failed, grey = pending).
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from datetime import date

import pandas as pd
import streamlit as st

from db import (
    init_db, get_connection, now_iso, authenticate, upsert_site_accounts,
    hash_pw, create_account_request, decide_account_request,
    add_notification, unread_notification_count,
    set_stage_approval, get_stage_approvals, add_price_offer,
    STAGES, FINAL_STAGE, DUAL_STAGES, order_status_text,
    STATE_IN_PROGRESS, STATE_COMPLETED, STATE_FAILED, STATE_CANCELLED,
    TERMINAL_STATES, CONSUMING_STATES, DEFAULT_SITE_PASSWORD,
)
from recommendation import read_and_aggregate, generate_transfer_plan

BASE_DIR    = Path(__file__).parent
EXPORTS_DIR = BASE_DIR / "exports"
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

init_db()

st.set_page_config(
    page_title="Internal Parts Marketplace",
    page_icon="🏭",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ── small helpers ─────────────────────────────────────────────────────────────
def _excel_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False)
    return buf.getvalue()


def _next_transfer_no() -> str:
    today = date.today().strftime("%Y%m%d")
    conn = get_connection()
    n = conn.execute(
        "SELECT COUNT(*) FROM transfers WHERE transfer_no LIKE ?", (f"IPR-{today}-%",)
    ).fetchone()[0] + 1
    conn.close()
    return f"IPR-{today}-{n:05d}"


def _region_from_filename(stem: str) -> str:
    s = stem.lower()
    known = {"emea": "EMEA", "na": "NA", "apac": "APAC", "latam": "LATAM",
             "china": "CHINA", "europe": "EMEA", "americas": "NA"}
    for token in re.split(r"[\s_\-.]+", s):
        if token in known:
            return known[token]
    return stem


def _committed_out_map() -> dict:
    """{(item, from_site): committed_qty} for non-failed/cancelled orders."""
    conn = get_connection()
    ph = ",".join("?" * len(CONSUMING_STATES))
    rows = conn.execute(
        f"""SELECT item, from_site, SUM(request_qty) q FROM transfers
            WHERE state IN ({ph}) GROUP BY item, from_site""", CONSUMING_STATES).fetchall()
    conn.close()
    return {(r["item"], r["from_site"]): (r["q"] or 0.0) for r in rows}


# ── timeline ──────────────────────────────────────────────────────────────────
def render_timeline(current_stage: int, state: str):
    """Horizontal coloured stage axis."""
    GREEN, YELLOW, RED, GREY, GREYDK = "#22c55e", "#eab308", "#ef4444", "#cbd5e1", "#9ca3af"
    pills = []
    for i, name in enumerate(STAGES):
        if state == STATE_COMPLETED:
            color = GREEN
        elif state == STATE_FAILED:
            color = GREEN if i < current_stage else (RED if i == current_stage else GREY)
        elif state == STATE_CANCELLED:
            color = GREEN if i < current_stage else (GREYDK if i == current_stage else GREY)
        else:  # in progress
            color = GREEN if i < current_stage else (YELLOW if i == current_stage else GREY)
        txt = "#ffffff" if color != GREY else "#374151"
        pills.append(
            f'<span style="background:{color};color:{txt};padding:6px 10px;'
            f'border-radius:14px;font-size:12px;white-space:nowrap;display:inline-block;">'
            f'{i}. {name}</span>')
    arrow = '<span style="color:#9ca3af;margin:0 2px;">→</span>'
    html = ('<div style="display:flex;flex-wrap:wrap;gap:4px;align-items:center;'
            'line-height:2.2;">' + arrow.join(pills) + '</div>')
    st.markdown(html, unsafe_allow_html=True)
    st.caption("🟩 done · 🟨 current · 🟥 failed · ⬜ pending")


# ── per-transfer chat ─────────────────────────────────────────────────────────
def _render_chat(transfer_no: str, user: dict, from_site: str, to_site: str):
    conn = get_connection()
    msgs = conn.execute(
        "SELECT * FROM messages WHERE transfer_no=? ORDER BY created_at", (transfer_no,)
    ).fetchall()
    conn.close()

    st.markdown(f"**💬 Conversation — {transfer_no}**")
    if msgs:
        for m in msgs:
            who = m["sender_site"] or m["sender"]
            st.chat_message("user").markdown(f"**{who}** · _{m['created_at']}_\n\n{m['body']}")
    else:
        st.caption("No messages yet.")

    if user["role"] == "Admin":
        st.caption("Admin view is read-only for chat.")
        return

    body = st.text_input("Type a message", key=f"chat_{transfer_no}")
    if st.button("Send", key=f"send_{transfer_no}"):
        if body.strip():
            conn = get_connection()
            conn.execute(
                "INSERT INTO messages (transfer_no,sender,sender_site,body,created_at) "
                "VALUES (?,?,?,?,?)",
                (transfer_no, user["username"], user["site_name"], body.strip(), now_iso()))
            conn.commit()
            conn.close()
            other = from_site if user["site_name"] == to_site else to_site
            add_notification(other, "message", transfer_no,
                             f"New message on {transfer_no} from {user['site_name']}")
            st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
# LOGIN + ACCOUNT REQUEST
# ═════════════════════════════════════════════════════════════════════════════
def login_screen():
    st.title("🏭 Internal Parts Marketplace")
    st.caption("Cross-Site Parts Transfer Platform")
    tab_login, tab_request = st.tabs(["🔑 Sign In", "📝 Request an Account"])

    with tab_login:
        with st.form("login"):
            username = st.text_input("Username (your Site name, or 'admin')")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign In", type="primary")
        if submitted:
            user = authenticate(username.strip(), password)
            if user:
                st.session_state["auth_user"] = user
                st.rerun()
            else:
                st.error("Invalid username or password.")
        with st.expander("Demo credentials"):
            st.markdown(
                "- **Admin:** `admin` / `admin123`\n"
                f"- **Each site:** username = its Site name, default password = `{DEFAULT_SITE_PASSWORD}`")

    with tab_request:
        st.markdown("New site? Request an account — an administrator will approve it.")
        with st.form("req_account"):
            r_site   = st.text_input("Site Name *")
            r_region = st.text_input("Region (e.g. EMEA, NA)")
            r_user   = st.text_input("Desired Username *")
            r_pw     = st.text_input("Password *", type="password")
            r_pw2    = st.text_input("Confirm Password *", type="password")
            r_note   = st.text_area("Note to admin (optional)")
            req_sub  = st.form_submit_button("Submit Request", type="primary")
        if req_sub:
            if not (r_site.strip() and r_user.strip() and r_pw):
                st.error("Site Name, Username and Password are required.")
            elif r_pw != r_pw2:
                st.error("Passwords do not match.")
            else:
                ok, msg = create_account_request(
                    r_user.strip(), r_pw, r_site.strip(), r_site.strip(),
                    r_region.strip(), r_note.strip())
                (st.success if ok else st.error)(msg)


# ═════════════════════════════════════════════════════════════════════════════
# ADMIN: ACCOUNT REQUESTS
# ═════════════════════════════════════════════════════════════════════════════
def page_admin_requests():
    st.header("🛂 Account Requests (Admin only)")
    conn = get_connection()
    pending = pd.read_sql(
        "SELECT * FROM account_requests WHERE status='Pending' ORDER BY requested_at", conn)
    history = pd.read_sql(
        "SELECT id, username, site_name, region, status, requested_at, decided_at "
        "FROM account_requests WHERE status!='Pending' ORDER BY decided_at DESC", conn)
    conn.close()

    if pending.empty:
        st.info("No pending account requests.")
    else:
        st.subheader(f"{len(pending)} pending request(s)")
        for _, r in pending.iterrows():
            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 1, 1])
                c1.markdown(f"**{r['username']}** · Site: **{r['site_name']}** · "
                            f"Region: {r['region'] or '—'}  \n_{r['requested_at']}_ — "
                            f"{r['note'] or 'no note'}")
                if c2.button("✅ Approve", key=f"appr_{r['id']}"):
                    decide_account_request(int(r["id"]), True)
                    st.rerun()
                if c3.button("❌ Reject", key=f"rej_{r['id']}"):
                    decide_account_request(int(r["id"]), False)
                    st.rerun()
    st.markdown("---")
    st.subheader("Decision history")
    st.dataframe(history, use_container_width=True)


# ═════════════════════════════════════════════════════════════════════════════
# ADMIN: IMPORT FROM FOLDER
# ═════════════════════════════════════════════════════════════════════════════
def page_admin_upload():
    st.header("📤 Import ASL Reports from a Folder (Admin only)")
    st.markdown("Point to a **folder** of ASL `.xlsx` reports — one file per region "
                "(e.g. `...emea.xlsx`, `...na.xlsx`). Regions are kept separate.")
    st.info("Importing **replaces** all data for every region found in the folder.")

    folder_str = st.text_input(r"Full path to the folder",
                               placeholder=r"C:\Users\you\Downloads\ASL_reports")
    if not folder_str:
        st.caption("Tip: open the folder in Explorer, click the address bar, copy the path.")
        return
    folder = Path(folder_str.strip().strip('"'))
    if not folder.is_dir():
        st.error(f"Folder not found: {folder}")
        return

    files = [p for p in sorted(folder.glob("*.xlsx")) if not p.name.startswith(("~$", "_upload"))]
    files += [p for p in sorted(folder.glob("*.xlsm")) if not p.name.startswith(("~$", "_upload"))]
    if not files:
        st.warning("No .xlsx/.xlsm files found in that folder.")
        return

    st.write("**Files detected:**")
    for p in files:
        st.write(f"- `{p.name}`  →  region **{_region_from_filename(p.stem)}**")

    if st.button("🔍 Read & Preview", type="primary"):
        agg_parts, errors = [], []
        with st.spinner("Reading files…"):
            for p in files:
                try:
                    a = read_and_aggregate(str(p))
                    a["Region"] = _region_from_filename(p.stem)
                    agg_parts.append(a)
                except Exception as e:
                    errors.append(f"{p.name}: {e}")
        for e in errors:
            st.error(e)
        if not agg_parts:
            return
        agg = pd.concat(agg_parts, ignore_index=True)
        st.session_state["import_agg"] = agg
        st.session_state["import_plan"] = generate_transfer_plan(agg)

    if "import_agg" not in st.session_state:
        return
    agg = st.session_state["import_agg"]
    plan = st.session_state["import_plan"]
    regions = sorted(agg["Region"].dropna().unique().tolist())
    st.success(f"Read {len(agg):,} aggregated rows | sites: {agg['Site Name'].nunique()} "
               f"| regions: {', '.join(regions)}")
    st.dataframe(agg.head(15), use_container_width=True)
    st.markdown(f"**{len(plan)} transfer recommendation(s) generated.**")
    st.dataframe(plan.head(15), use_container_width=True)

    if st.button("✅ Save to Database", type="primary"):
        ts = now_iso()
        conn = get_connection()
        cur = conn.cursor()
        for region in regions:
            cur.execute("DELETE FROM inventory WHERE region=?", (region,))
            cur.execute("DELETE FROM recommendations WHERE region=?", (region,))
        cur.executemany("""
            INSERT INTO inventory
            (region, site_code, site_name, item, description, type_trim, total_qoh, total_mrp,
             local_std_cost, euro_xc_rate, unit_cost_eur, net_balance, uploaded_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [(r["Region"], r.get("Site", ""), r["Site Name"], r["Item"], r["Description"],
               r["Type Trim"], float(r["Total QOH"]), float(r["Total MRP"]),
               float(r["Local STD Cost"]), float(r["Euro XC Rate"]),
               float(r["Unit Cost EUR"]), float(r["Net_Balance"]), ts)
              for _, r in agg.iterrows()])
        cur.executemany("""
            INSERT INTO recommendations
            (region, item, description, from_site, to_site, transfer_qty, local_std_cost,
             euro_xc_rate, unit_cost_eur, transfer_value_eur, generated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, [(r["Region"], r["Item"], r["Description"], r["From Site"], r["To Site"],
               float(r["Transfer Qty"]), float(r["Local STD Cost"]), float(r["Euro XC Rate"]),
               float(r["Unit Cost EUR"]), float(r["Transfer Value EUR"]), ts)
              for _, r in plan.iterrows()])
        conn.commit()
        conn.close()
        upsert_site_accounts(agg[["Site Name", "Region"]].drop_duplicates().values.tolist())
        st.session_state.pop("import_agg", None)
        st.session_state.pop("import_plan", None)
        st.success(f"Saved. Site accounts created/verified (default password '{DEFAULT_SITE_PASSWORD}').")
        st.balloons()


# ═════════════════════════════════════════════════════════════════════════════
# ADMIN: MANAGE ACCOUNTS / OVERVIEW
# ═════════════════════════════════════════════════════════════════════════════
def page_admin_accounts():
    st.header("👥 Manage Accounts (Admin only)")
    conn = get_connection()
    users = pd.read_sql("SELECT id, username, display_name, role, site_name, region, active "
                        "FROM users ORDER BY role, username", conn)
    conn.close()
    st.dataframe(users, use_container_width=True)
    st.markdown("---")
    st.subheader("Reset a password")
    site_users = users[users["role"] == "Site"]["username"].tolist()
    if site_users:
        target = st.selectbox("Account", site_users)
        new_pw = st.text_input("New password", value=DEFAULT_SITE_PASSWORD)
        if st.button("Reset Password"):
            conn = get_connection()
            conn.execute("UPDATE users SET password_hash=? WHERE username=?",
                         (hash_pw(new_pw), target))
            conn.commit()
            conn.close()
            st.success(f"Password reset for {target}.")
    else:
        st.info("No site accounts yet.")


def page_admin_overview():
    st.header("🌍 All Sites — Surplus & Deficit (Admin only)")
    conn = get_connection()
    inv = pd.read_sql("SELECT * FROM inventory WHERE type_trim='PG'", conn)
    conn.close()
    if inv.empty:
        st.info("No inventory data yet.")
        return
    inv["value_eur"] = inv["net_balance"].abs() * inv["unit_cost_eur"]
    by_site = inv.groupby(["region", "site_name"]).agg(
        surplus_lines=("net_balance", lambda s: int((s > 0).sum())),
        deficit_lines=("net_balance", lambda s: int((s < 0).sum())),
        surplus_qty=("net_balance", lambda s: s[s > 0].sum()),
        deficit_qty=("net_balance", lambda s: -s[s < 0].sum()),
    ).reset_index()
    st.subheader("By site")
    st.dataframe(by_site, use_container_width=True)
    st.download_button("⬇️ Export site summary", _excel_bytes(by_site),
                       file_name="all_sites_surplus_deficit.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    st.markdown("---")
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Top surplus items (EUR)")
        st.dataframe(inv[inv["net_balance"] > 0].nlargest(15, "value_eur")[
            ["region", "site_name", "item", "description", "net_balance", "value_eur"]
        ].reset_index(drop=True), use_container_width=True)
    with c2:
        st.subheader("Top deficit items (EUR)")
        st.dataframe(inv[inv["net_balance"] < 0].nlargest(15, "value_eur")[
            ["region", "site_name", "item", "description", "net_balance", "value_eur"]
        ].reset_index(drop=True), use_container_width=True)


# ═════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ═════════════════════════════════════════════════════════════════════════════
def page_dashboard(user):
    st.header("📊 Dashboard")
    is_admin = user["role"] == "Admin"
    site = user["site_name"]
    conn = get_connection()
    inv = pd.read_sql("SELECT * FROM inventory", conn)
    transfers = pd.read_sql("SELECT * FROM transfers", conn)
    conn.close()
    if inv.empty:
        st.info("No inventory data yet. " +
                ("Import ASL reports to begin." if is_admin else "Please ask the admin to import data."))
        return
    my_inv = inv if is_admin else inv[inv["site_name"] == site]
    pg = my_inv[my_inv["type_trim"] == "PG"]
    surplus = pg[pg["net_balance"] > 0]
    deficit = pg[pg["net_balance"] < 0]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("PG Item Lines", f"{len(pg):,}")
    c2.metric("My Surplus Lines (can sell)", f"{len(surplus):,}")
    c3.metric("My Deficit Lines (need)", f"{len(deficit):,}")
    c4.metric("Surplus Value (EUR)", f"€{(surplus['net_balance'] * surplus['unit_cost_eur']).sum():,.0f}")
    st.markdown("---")
    st.subheader("All Orders by Status" if is_admin else "My Orders by Status")
    t = transfers if is_admin else transfers[(transfers["from_site"] == site) | (transfers["to_site"] == site)]
    if not t.empty:
        t = t.assign(status_text=t.apply(
            lambda r: order_status_text(int(r["current_stage"]), r["state"]), axis=1))
        sc = t["status_text"].value_counts().reset_index()
        sc.columns = ["Status", "Count"]
        st.bar_chart(sc.set_index("Status"))
    else:
        st.info("No orders yet.")


# ═════════════════════════════════════════════════════════════════════════════
# SELL MY SURPLUS  (seller initiates an order to a deficit site)
# ═════════════════════════════════════════════════════════════════════════════
def page_sell_surplus(user):
    st.header("🏭 Sell My Surplus")
    site = user["site_name"]
    conn = get_connection()
    inv = pd.read_sql("SELECT * FROM inventory WHERE type_trim='PG'", conn)
    conn.close()
    if inv.empty:
        st.info("No inventory data yet.")
        return

    my_surplus = inv[(inv["site_name"] == site) & (inv["net_balance"] > 0)].copy()
    if my_surplus.empty:
        st.success("You currently have no surplus PG inventory. Nothing to sell.")
        return

    # Subtract quantities already committed in open/done orders.
    committed = _committed_out_map()
    my_surplus["committed_qty"] = my_surplus["item"].map(lambda it: committed.get((it, site), 0.0))
    my_surplus["remaining_surplus"] = my_surplus["net_balance"] - my_surplus["committed_qty"]

    st.subheader("Your surplus items")
    show = my_surplus.rename(columns={"net_balance": "surplus_qty"})[
        ["item", "description", "surplus_qty", "committed_qty", "remaining_surplus",
         "unit_cost_eur", "region"]]
    st.dataframe(show.reset_index(drop=True), use_container_width=True)

    st.markdown("---")
    st.subheader("1) Pick a surplus item")
    opts = my_surplus.apply(
        lambda r: f"{r['item']} | remaining {r['remaining_surplus']:.0f} | €{r['unit_cost_eur']:.2f}",
        axis=1).tolist()
    idx = st.selectbox("Item", range(len(opts)), format_func=lambda i: opts[i])
    sel = my_surplus.iloc[idx]
    item, region = sel["item"], sel["region"]

    # Deficit sites for this item (same region).
    deficit_sites = inv[(inv["item"] == item) & (inv["region"] == region) &
                        (inv["net_balance"] < 0) & (inv["site_name"] != site)].copy()
    deficit_sites["deficit_qty"] = -deficit_sites["net_balance"]

    st.subheader("2) Sites that NEED this part")
    if deficit_sites.empty:
        st.warning("No site in this region currently shows a deficit for this item. "
                   "You can still pick any site below if you know they need it.")
        target_pool = inv[(inv["region"] == region) & (inv["site_name"] != site)][
            ["site_name", "region"]].drop_duplicates()
        target_pool["deficit_qty"] = 0.0
    else:
        st.dataframe(deficit_sites.rename(columns={"site_name": "buyer_site"})[
            ["buyer_site", "region", "deficit_qty", "unit_cost_eur"]
        ].reset_index(drop=True), use_container_width=True)
        target_pool = deficit_sites.rename(columns={"site_name": "site_name"})[
            ["site_name", "region", "deficit_qty"]]

    st.subheader("3) Build the order")
    buyers = target_pool["site_name"].tolist()
    if not buyers:
        st.info("No candidate buyer site available.")
        return
    b_idx = st.selectbox("Send to (Site B)", range(len(buyers)), format_func=lambda i: buyers[i])
    buyer = buyers[b_idx]
    buyer_deficit = float(target_pool.iloc[b_idx]["deficit_qty"])

    default_qty = float(sel["remaining_surplus"])
    if buyer_deficit > 0:
        default_qty = min(default_qty, buyer_deficit)
    c1, c2 = st.columns(2)
    qty = c1.number_input("Quantity to sell", min_value=0.0,
                          value=max(default_qty, 0.0), step=1.0,
                          help="Default = your remaining surplus (capped at the buyer's need).")
    unit_price = c2.number_input("Unit price quote (EUR)", min_value=0.0,
                                 value=float(sel["unit_cost_eur"]), step=0.01)
    total = qty * unit_price
    st.markdown(f"**Total quote: €{total:,.2f}**  ({qty:.0f} × €{unit_price:.2f})")
    note = st.text_area("Note to buyer (optional)")

    if qty > sel["remaining_surplus"]:
        st.warning("Quantity exceeds your remaining surplus — you can still submit.")
    if st.button("📨 Submit Order to Site B", type="primary"):
        if qty <= 0:
            st.error("Quantity must be greater than 0.")
        elif unit_price <= 0:
            st.error("Unit price must be greater than 0.")
        else:
            _create_order(site, region, item, sel["description"], buyer, qty, unit_price, note)


def _create_order(seller, region, item, desc, buyer, qty, unit_price, note):
    tno = _next_transfer_no()
    ts = now_iso()
    total = qty * unit_price
    conn = get_connection()
    conn.execute("""
        INSERT INTO transfers
        (transfer_no, region, item, description, from_site, to_site, request_qty,
         unit_cost_eur, quoted_unit_price, quoted_total, total_value_eur, created_by,
         current_stage, state, status_note, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (tno, region, item, desc, seller, buyer, qty, unit_price, unit_price, total, total,
          seller, 1, STATE_IN_PROGRESS, note, ts, ts))
    conn.execute("INSERT INTO price_offers (transfer_no,offered_by_site,unit_price,total,note,created_at) "
                 "VALUES (?,?,?,?,?,?)", (tno, seller, unit_price, total, "Initial quote", ts))
    conn.commit()
    conn.close()
    add_notification(buyer, "order", tno,
                     f"New order {tno}: {seller} offers {qty:.0f} × {item} @ €{unit_price:.2f} "
                     f"(total €{total:,.2f})")
    st.success(f"✅ Order **{tno}** sent to **{buyer}**. Track it under 'My Orders'.")


# ═════════════════════════════════════════════════════════════════════════════
# MY ORDERS  (list + open detail)
# ═════════════════════════════════════════════════════════════════════════════
def page_my_orders(user):
    st.header("📋 My Orders" if user["role"] != "Admin" else "📋 All Orders")
    is_admin = user["role"] == "Admin"
    site = user["site_name"]
    conn = get_connection()
    if is_admin:
        df = pd.read_sql("SELECT * FROM transfers ORDER BY created_at DESC", conn)
    else:
        df = pd.read_sql("SELECT * FROM transfers WHERE from_site=? OR to_site=? "
                         "ORDER BY created_at DESC", conn, params=(site, site))
    conn.close()
    if df.empty:
        st.info("No orders yet.")
        return

    df["status_text"] = df.apply(lambda r: order_status_text(int(r["current_stage"]), r["state"]), axis=1)
    if not is_admin:
        df["my_role"] = df.apply(
            lambda r: "Seller (A)" if r["from_site"] == site else "Buyer (B)", axis=1)
        # Inbox: orders awaiting my (buyer) decision
        inbox = df[(df["to_site"] == site) & (df["current_stage"] == 1) & (df["state"] == STATE_IN_PROGRESS)]
        if not inbox.empty:
            st.warning(f"🔔 {len(inbox)} incoming order(s) awaiting your accept/reject decision.")

    cols = ["transfer_no", "region", "item", "from_site", "to_site", "request_qty",
            "quoted_unit_price", "quoted_total", "status_text", "updated_at"]
    if not is_admin:
        cols.insert(1, "my_role")
    st.dataframe(df[cols].reset_index(drop=True), use_container_width=True)
    st.download_button("⬇️ Export to Excel", _excel_bytes(df[cols]),
                       file_name="orders.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    st.markdown("---")
    st.subheader("Open an order")
    order_nos = df["transfer_no"].tolist()
    default_open = st.session_state.get("open_order")
    init_idx = order_nos.index(default_open) if default_open in order_nos else 0
    chosen = st.selectbox("Order number", order_nos, index=init_idx)
    if st.button("▶ Open order detail", type="primary"):
        st.session_state["open_order"] = chosen
        st.rerun()

    if st.session_state.get("open_order") in order_nos:
        st.markdown("---")
        _order_detail(st.session_state["open_order"], user)


def _order_detail(tno: str, user: dict):
    conn = get_connection()
    r = conn.execute("SELECT * FROM transfers WHERE transfer_no=?", (tno,)).fetchone()
    offers = conn.execute("SELECT * FROM price_offers WHERE transfer_no=? ORDER BY created_at",
                          (tno,)).fetchall()
    conn.close()
    if r is None:
        st.error("Order not found.")
        return
    r = dict(r)
    stage = int(r["current_stage"])
    state = r["state"]
    from_site, to_site = r["from_site"], r["to_site"]
    is_admin = user["role"] == "Admin"
    site = user["site_name"]
    my_side = "seller" if site == from_site else ("buyer" if site == to_site else None)

    top = st.columns([6, 1])
    top[0].markdown(f"### 🧾 {tno}")
    if top[1].button("✖ Close"):
        st.session_state.pop("open_order", None)
        st.rerun()

    st.markdown(
        f"**Item:** {r['item']} ({r['description'] or '—'}) · **Region:** {r['region']}  \n"
        f"**Seller (A):** {from_site} → **Buyer (B):** {to_site}  \n"
        f"**Qty:** {r['request_qty']:.0f} · **Unit price:** €{(r['quoted_unit_price'] or 0):.2f} · "
        f"**Total:** €{(r['quoted_total'] or 0):,.2f}")

    render_timeline(stage, state)

    if state in TERMINAL_STATES:
        if state == STATE_COMPLETED:
            st.success("✅ Order completed — internal sales contract approved.")
        elif state == STATE_FAILED:
            st.error(f"❌ Order failed at stage: {STAGES[stage]}. Note: {r['status_note'] or '—'}")
        else:
            st.warning(f"🚫 Order cancelled. Note: {r['status_note'] or '—'}")

    # ── Price panel ───────────────────────────────────────────────────────────
    with st.expander("💶 Price & negotiation history", expanded=(stage == 2)):
        if offers:
            odf = pd.DataFrame([dict(o) for o in offers])[
                ["created_at", "offered_by_site", "unit_price", "total", "note"]]
            st.dataframe(odf, use_container_width=True)
        if my_side and state == STATE_IN_PROGRESS and stage <= 2:
            st.markdown("**Propose a new price** (resets stage-2 approvals)")
            c1, c2 = st.columns(2)
            np_price = c1.number_input("New unit price (EUR)", min_value=0.0,
                                       value=float(r["quoted_unit_price"] or 0), step=0.01,
                                       key=f"np_{tno}")
            np_note = c2.text_input("Note", key=f"npn_{tno}")
            if st.button("Submit new offer", key=f"offer_{tno}"):
                add_price_offer(tno, site, np_price, np_price * r["request_qty"], np_note or "Counter-offer")
                conn = get_connection()
                conn.execute("DELETE FROM stage_approvals WHERE transfer_no=? AND stage_index=2", (tno,))
                conn.commit()
                conn.close()
                other = from_site if site == to_site else to_site
                add_notification(other, "message", tno, f"New price offer on {tno}: €{np_price:.2f}")
                st.rerun()

    # ── Stage controls ────────────────────────────────────────────────────────
    if not is_admin and my_side and state == STATE_IN_PROGRESS:
        st.markdown(f"**Current stage: {stage}. {STAGES[stage]}**")
        _stage_controls(r, stage, my_side, site, from_site, to_site, user)
    elif is_admin:
        st.caption("Admin view — controls are read-only.")

    st.markdown("---")
    _render_chat(tno, user, from_site, to_site)


def _advance(tno, new_stage, note=None):
    conn = get_connection()
    conn.execute("UPDATE transfers SET current_stage=?, updated_at=? WHERE transfer_no=?",
                 (new_stage, now_iso(), tno))
    if note is not None:
        conn.execute("UPDATE transfers SET status_note=? WHERE transfer_no=?", (note, tno))
    conn.commit()
    conn.close()


def _set_state(tno, state, note=None):
    conn = get_connection()
    conn.execute("UPDATE transfers SET state=?, updated_at=? WHERE transfer_no=?",
                 (state, now_iso(), tno))
    if note is not None:
        conn.execute("UPDATE transfers SET status_note=? WHERE transfer_no=?", (note, tno))
    conn.commit()
    conn.close()


def _stage_controls(r, stage, my_side, site, from_site, to_site, user):
    tno = r["transfer_no"]
    other = from_site if site == to_site else to_site

    # Stage 1 — Site B (buyer) accept/reject
    if stage == 1:
        if my_side == "buyer":
            st.info("You are **Site B** — accept or reject this order.")
            c1, c2 = st.columns(2)
            if c1.button("✅ Accept order", type="primary", key=f"acc_{tno}"):
                _advance(tno, 2)
                add_notification(from_site, "status", tno, f"{site} ACCEPTED order {tno}.")
                st.rerun()
            if c2.button("❌ Reject order", key=f"rejo_{tno}"):
                _set_state(tno, STATE_FAILED, "Rejected by Site B")
                add_notification(from_site, "status", tno, f"{site} REJECTED order {tno}.")
                st.rerun()
        else:
            st.info("Waiting for **Site B** to accept or reject.")

    # Dual-approval stages (2,3,4,5)
    elif stage in DUAL_STAGES:
        approvals = get_stage_approvals(tno, stage)
        a_ok = from_site in approvals
        b_ok = to_site in approvals
        st.write(f"- Seller (A) **{from_site}**: {'✅ approved' if a_ok else '⏳ pending'}")
        st.write(f"- Buyer (B) **{to_site}**: {'✅ approved' if b_ok else '⏳ pending'}")
        mine = a_ok if my_side == "seller" else b_ok
        label = "↩ Withdraw my approval" if mine else "✅ Approve this stage"
        if st.button(label, type="primary", key=f"appstage_{tno}_{stage}"):
            set_stage_approval(tno, stage, site, not mine)
            # Re-check: both approved → advance
            now_appr = get_stage_approvals(tno, stage)
            if {from_site, to_site}.issubset(now_appr):
                _advance(tno, stage + 1)
                add_notification(from_site, "status", tno, f"{tno} → {STAGES[stage + 1]}")
                add_notification(to_site, "status", tno, f"{tno} → {STAGES[stage + 1]}")
            st.rerun()

    # Single-advance stages (6,7)
    elif stage in (6, 7):
        st.caption("Either side may advance this stage.")
        if st.button(f"➡ Advance to: {STAGES[stage + 1]}", type="primary", key=f"adv_{tno}_{stage}"):
            _advance(tno, stage + 1)
            add_notification(other, "status", tno, f"{tno} → {STAGES[stage + 1]}")
            st.rerun()

    # Final stage
    elif stage == FINAL_STAGE:
        if st.button("🏁 Mark Completed", type="primary", key=f"done_{tno}"):
            _set_state(tno, STATE_COMPLETED)
            add_notification(other, "status", tno, f"{tno} COMPLETED.")
            st.rerun()

    # Universal: mark failed / cancel
    st.markdown("---")
    with st.expander("⚠️ Mark failed / cancel this order"):
        fnote = st.text_input("Reason", key=f"fn_{tno}")
        c1, c2 = st.columns(2)
        if c1.button("🟥 Mark Failed at current stage", key=f"fail_{tno}"):
            _set_state(tno, STATE_FAILED, fnote or f"Failed at {STAGES[stage]}")
            add_notification(other, "status", tno, f"{tno} marked FAILED at {STAGES[stage]}.")
            st.rerun()
        if c2.button("🚫 Cancel order", key=f"can_{tno}"):
            _set_state(tno, STATE_CANCELLED, fnote or "Cancelled")
            add_notification(other, "status", tno, f"{tno} CANCELLED.")
            st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
# NOTIFICATIONS
# ═════════════════════════════════════════════════════════════════════════════
def page_notifications(user):
    st.header("🔔 Notifications")
    site = user["site_name"]
    if not site:
        st.info("Admin has no site inbox.")
        return
    conn = get_connection()
    df = pd.read_sql("SELECT * FROM notifications WHERE recipient_site=? ORDER BY created_at DESC",
                     conn, params=(site,))
    conn.close()
    if df.empty:
        st.info("No notifications.")
        return
    unread = df[df["is_read"] == 0]
    st.markdown(f"**{len(unread)} unread** / {len(df)} total")
    if st.button("✅ Mark all as read"):
        conn = get_connection()
        conn.execute("UPDATE notifications SET is_read=1 WHERE recipient_site=?", (site,))
        conn.commit()
        conn.close()
        st.rerun()
    for _, n in df.iterrows():
        icon = "🟢" if n["is_read"] == 0 else "⚪"
        link = ""
        if n["ref_no"]:
            link = f" · order `{n['ref_no']}`"
        st.write(f"{icon} **[{n['kind']}]** {n['body']}{link}  ·  _{n['created_at']}_")
        if n["ref_no"] and st.button(f"Open {n['ref_no']}", key=f"open_ntf_{n['id']}"):
            st.session_state["open_order"] = n["ref_no"]
            st.session_state["nav"] = "📋 My Orders"
            st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
# TOP NAV + ROUTER
# ═════════════════════════════════════════════════════════════════════════════
def main():
    if "auth_user" not in st.session_state:
        login_screen()
        return
    user = st.session_state["auth_user"]
    is_admin = user["role"] == "Admin"
    site = user["site_name"]
    unread = unread_notification_count(site) if site else 0

    hl, hr = st.columns([4, 2])
    hl.markdown("### 🏭 Internal Parts Marketplace")
    with hr:
        chip = f"**{user['username']}** · {user['role']}"
        if site:
            chip += f" · {site} ({user['region']})"
        st.markdown(chip)
        bc1, bc2 = st.columns(2)
        if bc1.button(f"🔔 {unread}", use_container_width=True):
            st.session_state["nav"] = "🔔 Notifications"
            st.rerun()
        if bc2.button("Sign Out", use_container_width=True):
            st.session_state.clear()
            st.rerun()

    if is_admin:
        pages = ["📊 Dashboard", "📤 Import Data", "🌍 All Sites", "📋 My Orders",
                 "🛂 Account Requests", "👥 Manage Accounts"]
    else:
        pages = ["📊 Dashboard", "🏭 Sell My Surplus", "📋 My Orders", "🔔 Notifications"]

    default = st.session_state.pop("nav", pages[0])
    if default not in pages:
        default = pages[0]
    page = st.radio("Navigation", pages, horizontal=True,
                    index=pages.index(default), label_visibility="collapsed")
    st.markdown("---")

    if page == "📊 Dashboard":
        page_dashboard(user)
    elif page == "📤 Import Data":
        page_admin_upload()
    elif page == "🌍 All Sites":
        page_admin_overview()
    elif page == "🏭 Sell My Surplus":
        page_sell_surplus(user)
    elif page == "📋 My Orders":
        page_my_orders(user)
    elif page == "🔔 Notifications":
        page_notifications(user)
    elif page == "🛂 Account Requests":
        page_admin_requests()
    elif page == "👥 Manage Accounts":
        page_admin_accounts()


if __name__ == "__main__":
    main()
