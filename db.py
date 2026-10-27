"""
Database initialization and helpers.
SQLite for MVP. Swap get_connection() for PostgreSQL/SQL Server later.
"""

import sqlite3
import hashlib
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent / "data" / "marketplace.db"

# ── Order lifecycle ───────────────────────────────────────────────────────────
# Seller (Site A, surplus) initiates an order to a deficit site (Site B).
# A single horizontal timeline of 9 stages drives the whole order.
STAGES = [
    "Order Submitted",                  # 0 — A submits qty + quote to B (auto-done on create)
    "Site B Decision",                  # 1 — B accepts or rejects
    "Price Negotiation",                # 2 — both sides agree on price
    "Logistics Review (A & B)",         # 3 — dual approval
    "Finance Review (A & B)",           # 4 — dual approval
    "Contract Signing",                 # 5 — dual approval
    "Workflow (BPM) Review",                    # 6 — single advance
    "Approve Internal Sales Contract",  # 7 — single advance
    "Approved Internal Sales Contract", # 8 — final success
]
FINAL_STAGE = len(STAGES) - 1           # index 8

# Stages requiring BOTH sites to approve before advancing.
DUAL_STAGES = {2, 3, 4, 5}
# Stage 1 is the seller-B accept/reject gate. Stages 6,7 advance by either side.

# Order states (independent of which stage is current).
STATE_IN_PROGRESS = "In Progress"
STATE_COMPLETED   = "Completed"
STATE_FAILED      = "Failed"
STATE_CANCELLED   = "Cancelled"
TERMINAL_STATES   = (STATE_COMPLETED, STATE_FAILED, STATE_CANCELLED)
# An order still "consumes" surplus unless it failed/was cancelled.
CONSUMING_STATES  = (STATE_IN_PROGRESS, STATE_COMPLETED)


def order_status_text(current_stage: int, state: str) -> str:
    """Human-readable status for lists/filters."""
    if state == STATE_COMPLETED:
        return STATE_COMPLETED
    if state == STATE_FAILED:
        return f"Failed @ {STAGES[current_stage]}"
    if state == STATE_CANCELLED:
        return STATE_CANCELLED
    return STAGES[current_stage]

DEFAULT_SITE_PASSWORD = "site123"
DEFAULT_ADMIN = ("admin", "admin123")


def hash_pw(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    conn = get_connection()
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            display_name  TEXT,
            role          TEXT NOT NULL,        -- 'Admin' or 'Site'
            site_name     TEXT,
            region        TEXT,
            active        INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS inventory (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            region         TEXT,
            site_code      TEXT,
            site_name      TEXT NOT NULL,
            item           TEXT NOT NULL,
            description    TEXT,
            type_trim      TEXT,
            total_qoh      REAL,
            total_mrp      REAL,
            local_std_cost REAL,
            euro_xc_rate   REAL,
            unit_cost_eur  REAL,
            net_balance    REAL,
            uploaded_at    TEXT
        );

        CREATE TABLE IF NOT EXISTS recommendations (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            region             TEXT,
            item               TEXT,
            description        TEXT,
            from_site          TEXT,
            to_site            TEXT,
            transfer_qty       REAL,
            local_std_cost     REAL,
            euro_xc_rate       REAL,
            unit_cost_eur      REAL,
            transfer_value_eur REAL,
            generated_at       TEXT
        );

        CREATE TABLE IF NOT EXISTS transfers (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            transfer_no       TEXT NOT NULL UNIQUE,
            region            TEXT,
            item              TEXT,
            description       TEXT,
            from_site         TEXT,           -- Seller (Site A, surplus)
            to_site           TEXT,           -- Buyer (Site B, deficit)
            request_qty       REAL,
            unit_cost_eur     REAL,           -- reference cost
            quoted_unit_price REAL,           -- current agreed/proposed unit price
            quoted_total      REAL,           -- request_qty * quoted_unit_price
            total_value_eur   REAL,
            created_by        TEXT,
            current_stage     INTEGER NOT NULL DEFAULT 1,   -- index into STAGES
            state             TEXT NOT NULL DEFAULT 'In Progress',
            status_note       TEXT,
            created_at        TEXT,
            updated_at        TEXT
        );

        -- Per-side approvals for the dual-approval stages.
        CREATE TABLE IF NOT EXISTS stage_approvals (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            transfer_no  TEXT NOT NULL,
            stage_index  INTEGER NOT NULL,
            site         TEXT NOT NULL,
            approved_at  TEXT,
            UNIQUE(transfer_no, stage_index, site)
        );

        -- Price negotiation history.
        CREATE TABLE IF NOT EXISTS price_offers (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            transfer_no     TEXT NOT NULL,
            offered_by_site TEXT,
            unit_price      REAL,
            total           REAL,
            note            TEXT,
            created_at      TEXT
        );

        -- Self-service account requests, approved by the admin.
        CREATE TABLE IF NOT EXISTS account_requests (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            display_name  TEXT,
            site_name     TEXT,
            region        TEXT,
            note          TEXT,
            status        TEXT NOT NULL DEFAULT 'Pending',  -- Pending/Approved/Rejected
            requested_at  TEXT,
            decided_at    TEXT
        );

        -- Per-transfer chat between the two sites (admin can read).
        CREATE TABLE IF NOT EXISTS messages (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            transfer_no  TEXT NOT NULL,
            sender       TEXT,
            sender_site  TEXT,
            body         TEXT,
            created_at   TEXT
        );

        -- Order / message alerts targeted at a site.
        CREATE TABLE IF NOT EXISTS notifications (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient_site TEXT NOT NULL,
            kind           TEXT,        -- 'order' | 'message' | 'status'
            ref_no         TEXT,        -- transfer_no
            body           TEXT,
            is_read        INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_inv_item ON inventory(item);
        CREATE INDEX IF NOT EXISTS idx_inv_site ON inventory(site_name);
        CREATE INDEX IF NOT EXISTS idx_rec_to   ON recommendations(to_site);
        CREATE INDEX IF NOT EXISTS idx_msg_tno  ON messages(transfer_no);
        CREATE INDEX IF NOT EXISTS idx_ntf_site ON notifications(recipient_site, is_read);
    """)

    # Lightweight migration for older DBs.
    cols = [r["name"] for r in cur.execute("PRAGMA table_info(transfers)").fetchall()]
    add_cols = {
        "quoted_unit_price": "REAL",
        "quoted_total":      "REAL",
        "current_stage":     "INTEGER NOT NULL DEFAULT 1",
        "state":             "TEXT NOT NULL DEFAULT 'In Progress'",
    }
    for name, decl in add_cols.items():
        if name not in cols:
            cur.execute(f"ALTER TABLE transfers ADD COLUMN {name} {decl}")

    # Seed built-in admin
    cur.execute("SELECT COUNT(*) FROM users WHERE role='Admin'")
    if cur.fetchone()[0] == 0:
        cur.execute(
            "INSERT INTO users (username,password_hash,display_name,role,site_name,region) "
            "VALUES (?,?,?,?,?,?)",
            (DEFAULT_ADMIN[0], hash_pw(DEFAULT_ADMIN[1]), "Administrator", "Admin", None, None),
        )
    conn.commit()
    conn.close()


def authenticate(username: str, password: str):
    """Return user dict on success, else None."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM users WHERE username=? AND active=1", (username,)
    ).fetchone()
    conn.close()
    if row and row["password_hash"] == hash_pw(password):
        return dict(row)
    return None


def create_account_request(username, password, display_name, site_name, region, note):
    """Store a self-service account request for admin approval. Returns (ok, msg)."""
    conn = get_connection()
    cur = conn.cursor()
    # Reject if username already taken by an active user or a pending request.
    if cur.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        conn.close()
        return False, "That username already exists. Please sign in or pick another."
    if cur.execute("SELECT 1 FROM account_requests WHERE username=? AND status='Pending'",
                   (username,)).fetchone():
        conn.close()
        return False, "A pending request with that username already exists."
    cur.execute(
        "INSERT INTO account_requests "
        "(username,password_hash,display_name,site_name,region,note,status,requested_at) "
        "VALUES (?,?,?,?,?,?, 'Pending', ?)",
        (username, hash_pw(password), display_name, site_name, region, note, now_iso()),
    )
    conn.commit()
    conn.close()
    return True, "Request submitted. An administrator will review it."


def decide_account_request(req_id: int, approve: bool):
    """Approve (create the user) or reject an account request."""
    conn = get_connection()
    cur = conn.cursor()
    req = cur.execute("SELECT * FROM account_requests WHERE id=?", (req_id,)).fetchone()
    if req is None:
        conn.close()
        return
    if approve:
        exists = cur.execute("SELECT 1 FROM users WHERE username=?", (req["username"],)).fetchone()
        if not exists:
            cur.execute(
                "INSERT INTO users (username,password_hash,display_name,role,site_name,region) "
                "VALUES (?,?,?,?,?,?)",
                (req["username"], req["password_hash"], req["display_name"] or req["site_name"],
                 "Site", req["site_name"], req["region"]),
            )
        cur.execute("UPDATE account_requests SET status='Approved', decided_at=? WHERE id=?",
                    (now_iso(), req_id))
    else:
        cur.execute("UPDATE account_requests SET status='Rejected', decided_at=? WHERE id=?",
                    (now_iso(), req_id))
    conn.commit()
    conn.close()


def add_notification(recipient_site, kind, ref_no, body):
    if not recipient_site:
        return
    conn = get_connection()
    conn.execute(
        "INSERT INTO notifications (recipient_site,kind,ref_no,body,is_read,created_at) "
        "VALUES (?,?,?,?,0,?)",
        (recipient_site, kind, ref_no, body, now_iso()),
    )
    conn.commit()
    conn.close()


def unread_notification_count(site: str) -> int:
    if not site:
        return 0
    conn = get_connection()
    n = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE recipient_site=? AND is_read=0", (site,)
    ).fetchone()[0]
    conn.close()
    return n


def set_stage_approval(transfer_no: str, stage_index: int, site: str, approved: bool):
    conn = get_connection()
    if approved:
        conn.execute(
            "INSERT OR IGNORE INTO stage_approvals (transfer_no,stage_index,site,approved_at) "
            "VALUES (?,?,?,?)", (transfer_no, stage_index, site, now_iso()))
    else:
        conn.execute(
            "DELETE FROM stage_approvals WHERE transfer_no=? AND stage_index=? AND site=?",
            (transfer_no, stage_index, site))
    conn.commit()
    conn.close()


def get_stage_approvals(transfer_no: str, stage_index: int) -> set:
    """Return the set of sites that have approved this stage."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT site FROM stage_approvals WHERE transfer_no=? AND stage_index=?",
        (transfer_no, stage_index)).fetchall()
    conn.close()
    return {r["site"] for r in rows}


def add_price_offer(transfer_no, site, unit_price, total, note):
    conn = get_connection()
    conn.execute(
        "INSERT INTO price_offers (transfer_no,offered_by_site,unit_price,total,note,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (transfer_no, site, unit_price, total, note, now_iso()))
    # Latest offer becomes the order's current quote.
    conn.execute(
        "UPDATE transfers SET quoted_unit_price=?, quoted_total=?, total_value_eur=?, updated_at=? "
        "WHERE transfer_no=?", (unit_price, total, total, now_iso(), transfer_no))
    conn.commit()
    conn.close()


def upsert_site_accounts(sites: list[tuple]):
    """
    sites: list of (site_name, region). Create a Site account for any new site
    with the default password. Existing accounts are left untouched.
    """
    conn = get_connection()
    cur = conn.cursor()
    for site_name, region in sites:
        if not site_name:
            continue
        username = site_name.strip()
        existing = cur.execute(
            "SELECT id FROM users WHERE username=?", (username,)
        ).fetchone()
        if existing is None:
            cur.execute(
                "INSERT INTO users (username,password_hash,display_name,role,site_name,region) "
                "VALUES (?,?,?,?,?,?)",
                (username, hash_pw(DEFAULT_SITE_PASSWORD), site_name, "Site", site_name, region),
            )
    conn.commit()
    conn.close()
