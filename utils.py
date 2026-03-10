import os
import hashlib
import json
import requests
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from datetime import datetime

load_dotenv()

# ── Plaid config ─────────────────────────────────────────────────────────────
PLAID_CLIENT_ID = os.getenv("PLAID_CLIENT_ID") or st.secrets.get("PLAID_CLIENT_ID", "")
PLAID_SECRET    = os.getenv("PLAID_SECRET")    or st.secrets.get("PLAID_SECRET", "")
PLAID_ENV       = os.getenv("PLAID_ENV", "sandbox")
PLAID_BASE_URL  = {
    "sandbox":     "https://sandbox.plaid.com",
    "development": "https://development.plaid.com",
    "production":  "https://production.plaid.com",
}.get(PLAID_ENV, "https://sandbox.plaid.com")

CAT_OPTIONS = [
    "Groceries", "Dining Out", "Rent", "Utilities", "Shopping", "Transport",
    "Travel", "Entertainment", "Income", "Subscriptions", "Credit Card Pay", "Home Improvement",
    "Pets", "RX", "Savings", "Gambling", "Personal Loan", "Uncategorized"
]

# ── Database ──────────────────────────────────────────────────────────────────
@st.cache_resource
def get_engine():
    db_url = os.getenv("DATABASE_URL") or st.secrets.get("DATABASE_URL", "")
    if not db_url:
        st.error("❌ DATABASE_URL not found in secrets or .env")
        st.stop()
    return create_engine(
        db_url.replace("postgres://", "postgresql://"),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )

def get_db_connection():
    return get_engine()

def init_db():
    engine = get_db_connection()
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id TEXT PRIMARY KEY, date DATE, name TEXT, merchant_name TEXT,
                amount NUMERIC, category TEXT, bucket TEXT, pending BOOLEAN,
                manual_category TEXT, manual_bucket TEXT, source TEXT
            );
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS category_rules (
                rule_id SERIAL PRIMARY KEY,
                keyword TEXT NOT NULL,
                category TEXT NOT NULL,
                bucket TEXT NOT NULL
            );
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS budget_settings (
                key_name TEXT PRIMARY KEY,
                value NUMERIC
            );
        """))
        try:
            conn.execute(text("ALTER TABLE budget_settings ADD COLUMN IF NOT EXISTS str_value TEXT"))
        except Exception:
            pass
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS net_worth_accounts (
                account_id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                balance NUMERIC NOT NULL
            );
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS plaid_items (
                item_id TEXT PRIMARY KEY,
                access_token TEXT NOT NULL,
                institution_name TEXT,
                linked_at TIMESTAMP DEFAULT NOW()
            );
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS plaid_accounts (
                account_id TEXT PRIMARY KEY,
                item_id TEXT REFERENCES plaid_items(item_id) ON DELETE CASCADE,
                name TEXT, official_name TEXT, type TEXT, subtype TEXT,
                current_balance NUMERIC, available_balance NUMERIC, last_synced TIMESTAMP
            );
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS envelopes (
                envelope_id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                budgeted NUMERIC NOT NULL DEFAULT 0,
                category TEXT,
                reset_period TEXT DEFAULT 'monthly',
                sort_order INTEGER DEFAULT 0
            );
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS envelope_funding (
                funding_id SERIAL PRIMARY KEY,
                envelope_id INTEGER REFERENCES envelopes(envelope_id) ON DELETE CASCADE,
                amount NUMERIC NOT NULL,
                month TEXT NOT NULL,
                funded_at TIMESTAMP DEFAULT NOW()
            );
        """))
        conn.commit()

# ── Settings helpers ──────────────────────────────────────────────────────────
def get_budget_setting(key, default_val):
    with get_db_connection().connect() as conn:
        try:
            result = conn.execute(
                text("SELECT value FROM budget_settings WHERE key_name = :k"), {"k": key}
            ).fetchone()
            if result and result[0] is not None:
                return float(result[0])
        except Exception as e:
            st.warning(f"Could not read setting '{key}': {e}")
        return float(default_val)

def get_str_setting(key, default_val):
    with get_db_connection().connect() as conn:
        try:
            result = conn.execute(
                text("SELECT str_value FROM budget_settings WHERE key_name = :k"), {"k": key}
            ).fetchone()
            if result and result[0] is not None:
                return str(result[0])
        except Exception as e:
            st.warning(f"Could not read setting '{key}': {e}")
        return str(default_val)

def set_budget_setting(key, val, is_str=False):
    with get_db_connection().connect() as conn:
        if is_str:
            conn.execute(text("""
                INSERT INTO budget_settings (key_name, str_value) VALUES (:k, :v)
                ON CONFLICT (key_name) DO UPDATE SET str_value = :v
            """), {"k": key, "v": val})
        else:
            conn.execute(text("""
                INSERT INTO budget_settings (key_name, value) VALUES (:k, :v)
                ON CONFLICT (key_name) DO UPDATE SET value = :v
            """), {"k": key, "v": val})
        conn.commit()

# ── Categorization ────────────────────────────────────────────────────────────
def run_auto_categorization():
    engine = get_db_connection()
    with engine.connect() as conn:
        rules = pd.read_sql("SELECT * FROM category_rules", conn)
        if rules.empty:
            return
        for _, rule in rules.iterrows():
            keyword = f"%{rule['keyword']}%"
            conn.execute(text("""
                UPDATE transactions
                SET category = :cat, bucket = :bucket
                WHERE name ILIKE :kw AND category = 'Uncategorized'
            """), {"cat": rule['category'], "bucket": rule['bucket'], "kw": keyword})
        conn.commit()

# ── File import helpers ───────────────────────────────────────────────────────
def process_chime_pdf(uploaded_file):
    import re, pdfplumber
    transactions = []
    filename = uploaded_file.name
    import re as _re
    year_match = _re.search(r'20\d{2}', filename)
    default_year = year_match.group(0) if year_match else str(datetime.now().year)
    try:
        with pdfplumber.open(uploaded_file) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    for line in text.split('\n'):
                        match = re.match(r'^(\d{1,2}/\d{1,2}/\d{2,4}|[A-Z][a-z]{2}\s\d{1,2})\s+(.*)', line)
                        if match:
                            date_part = match.group(1)
                            rest = match.group(2)
                            if '/' not in date_part:
                                date_part = f"{date_part}, {default_year}"
                            tokens = rest.split()
                            amount, description_parts, found = 0.0, [], False
                            for i in range(len(tokens) - 1, -1, -1):
                                clean = tokens[i].replace('$','').replace(',','').replace('(','-').replace(')','')
                                if re.match(r'^-?\d+\.\d{2}$', clean) and not found:
                                    amount = float(clean); found = True; description_parts = tokens[:i]; break
                            if found:
                                transactions.append({'date': date_part, 'name': " ".join(description_parts), 'amount': amount, 'source': 'Chime PDF'})
    except Exception as e:
        st.warning(f"PDF Error: {e}")
    if not transactions:
        return pd.DataFrame(columns=['date', 'name', 'amount', 'source'])
    return pd.DataFrame(transactions)

def clean_bank_file(uploaded_file, bank_choice):
    import re
    try:
        filename = uploaded_file.name.lower()
        if filename.endswith('.csv'):
            df = pd.read_csv(uploaded_file, encoding='utf-8-sig')
            df.columns = df.columns.str.strip().str.lower().str.replace('\ufeff', '')
            if bank_choice == "Chase":
                if 'post date' in df.columns: df = df.rename(columns={'post date': 'date'})
                elif 'transaction date' in df.columns: df = df.rename(columns={'transaction date': 'date'})
                if 'description' in df.columns: df = df.rename(columns={'description': 'name'})
            elif bank_choice == "Citi":
                df = df.rename(columns={'date': 'date', 'description': 'name'})
                if 'debit' not in df.columns: df['debit'] = 0
                if 'credit' not in df.columns: df['credit'] = 0
                if 'amount' not in df.columns: df['amount'] = df['credit'].fillna(0) - df['debit'].fillna(0)
            elif bank_choice == "Sofi":
                if 'payment date' in df.columns: df = df.rename(columns={'payment date': 'date'})
                df = df.rename(columns={'description': 'name'})
            elif bank_choice in ("Chime", "Loan/Other"):
                if 'transaction date' in df.columns: df = df.rename(columns={'transaction date': 'date'})
                if 'description' in df.columns: df = df.rename(columns={'description': 'name'})
            df['source'] = bank_choice
        elif filename.endswith('.pdf') and bank_choice == "Chime":
            df = process_chime_pdf(uploaded_file)
        else:
            st.error(f"Unsupported file type: {uploaded_file.name}"); st.stop()

        if 'amount' not in df.columns: df['amount'] = 0.0
        if df['amount'].dtype == 'object':
            df['amount'] = pd.to_numeric(df['amount'].astype(str).str.replace('$','').str.replace(',',''))
        df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
        df['transaction_id'] = df.apply(
            lambda r: hashlib.md5(f"{r.get('date')}{r.get('name')}{r.get('amount')}".encode()).hexdigest(), axis=1)
        df['bucket'] = 'SPEND'
        df['category'] = 'Uncategorized'
        req = ['transaction_id', 'date', 'name', 'amount', 'category', 'bucket', 'source']
        for c in req:
            if c not in df.columns: df[c] = None
        return df[req].dropna(subset=['date'])
    except Exception as e:
        st.error(f"Error: {e}"); st.stop()

def save_to_neon(df):
    engine = get_db_connection()
    count = 0
    with engine.connect() as conn:
        for _, row in df.iterrows():
            try:
                conn.execute(text("""
                    INSERT INTO transactions
                        (transaction_id, date, name, merchant_name, amount, category, bucket, pending, source)
                    VALUES (:tid, :date, :name, :name, :amount, :cat, :bucket, :pending, :source)
                    ON CONFLICT (transaction_id) DO NOTHING
                """), {
                    "tid": row['transaction_id'], "date": row['date'], "name": row['name'],
                    "amount": row['amount'], "cat": row['category'], "bucket": row['bucket'],
                    "pending": False, "source": row['source']
                })
                count += 1
            except Exception as e:
                st.warning(f"Skipped row: {e}")
        conn.commit()
    return count

# ── Plaid helpers ─────────────────────────────────────────────────────────────
def plaid_post(endpoint, payload):
    payload["client_id"] = PLAID_CLIENT_ID
    payload["secret"]    = PLAID_SECRET
    resp = requests.post(
        f"{PLAID_BASE_URL}{endpoint}",
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()

def plaid_create_link_token():
    data = plaid_post("/link/token/create", {
        "user": {"client_user_id": "budget-master-user"},
        "client_name": "My Budget Master",
        "products": ["transactions"],
        "country_codes": ["US"],
        "language": "en",
    })
    return data.get("link_token")

def plaid_exchange_public_token(public_token):
    data = plaid_post("/item/public_token/exchange", {"public_token": public_token})
    return data.get("access_token"), data.get("item_id")

def plaid_get_institution_name(institution_id):
    try:
        data = plaid_post("/institutions/get_by_id", {
            "institution_id": institution_id,
            "country_codes": ["US"],
        })
        return data["institution"]["name"]
    except Exception:
        return "Unknown Bank"

def plaid_sync_item(access_token, item_id, institution_name):
    engine = get_db_connection()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT str_value FROM budget_settings WHERE key_name = :k"),
            {"k": f"plaid_cursor_{item_id}"}
        ).fetchone()
        cursor = row[0] if row and row[0] else ""

    added_total = 0
    has_more = True
    while has_more:
        payload = {"access_token": access_token}
        if cursor:
            payload["cursor"] = cursor
        data        = plaid_post("/transactions/sync", payload)
        added       = data.get("added", [])
        modified    = data.get("modified", [])
        removed     = data.get("removed", [])
        has_more    = data.get("has_more", False)
        next_cursor = data.get("next_cursor", "")

        rows = []
        for txn in added + modified:
            if txn.get("pending"):
                continue
            amount = txn.get("amount", 0)
            rows.append({
                "transaction_id": txn["transaction_id"],
                "date":           txn.get("date"),
                "name":           txn.get("merchant_name") or txn.get("name", ""),
                "amount":         amount,
                "category":       "Uncategorized",
                "bucket":         "INCOME" if amount < 0 else "SPEND",
                "source":         f"Plaid – {institution_name}",
            })
        if rows:
            added_total += save_to_neon(pd.DataFrame(rows))
        if removed:
            with engine.connect() as conn:
                for r in removed:
                    conn.execute(text("DELETE FROM transactions WHERE transaction_id = :tid"), {"tid": r["transaction_id"]})
                conn.commit()
        cursor = next_cursor

    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO budget_settings (key_name, str_value) VALUES (:k, :v)
            ON CONFLICT (key_name) DO UPDATE SET str_value = :v
        """), {"k": f"plaid_cursor_{item_id}", "v": cursor})
        conn.commit()

    try:
        acc_data = plaid_post("/accounts/get", {"access_token": access_token})
        with engine.connect() as conn:
            for acc in acc_data.get("accounts", []):
                conn.execute(text("""
                    INSERT INTO plaid_accounts
                        (account_id, item_id, name, official_name, type, subtype,
                         current_balance, available_balance, last_synced)
                    VALUES (:aid, :iid, :name, :oname, :type, :sub, :cur, :avail, NOW())
                    ON CONFLICT (account_id) DO UPDATE SET
                        current_balance = EXCLUDED.current_balance,
                        available_balance = EXCLUDED.available_balance,
                        last_synced = NOW()
                """), {
                    "aid": acc["account_id"], "iid": item_id, "name": acc["name"],
                    "oname": acc.get("official_name"), "type": acc["type"],
                    "sub": acc.get("subtype"),
                    "cur": acc["balances"].get("current"),
                    "avail": acc["balances"].get("available"),
                })
            conn.commit()
    except Exception as e:
        st.warning(f"Could not refresh account balances: {e}")

    run_auto_categorization()
    return added_total
