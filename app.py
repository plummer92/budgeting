import streamlit as st
import pandas as pd
import os
import hashlib
import re
import requests
import json
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from datetime import datetime, timedelta
import plotly.express as px
import plotly.graph_objects as go
import pdfplumber

load_dotenv()

# --- CONFIG ---
st.set_page_config(page_title="My Budget Master", layout="wide", page_icon="💰")

# --- PLAID CONFIG ---
PLAID_CLIENT_ID = os.getenv("PLAID_CLIENT_ID") or st.secrets.get("PLAID_CLIENT_ID", "")
PLAID_SECRET    = os.getenv("PLAID_SECRET")    or st.secrets.get("PLAID_SECRET", "")
PLAID_ENV       = os.getenv("PLAID_ENV", "sandbox")  # "sandbox" | "development" | "production"
PLAID_BASE_URL  = {
    "sandbox":     "https://sandbox.plaid.com",
    "development": "https://development.plaid.com",
    "production":  "https://production.plaid.com",
}.get(PLAID_ENV, "https://sandbox.plaid.com")

def plaid_post(endpoint, payload):
    """Make an authenticated POST request to Plaid."""
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

# --- DATABASE CONNECTION ---
@st.cache_resource
def get_engine():
    db_url = os.getenv("DATABASE_URL")
    if not db_url and "DATABASE_URL" in st.secrets:
        db_url = st.secrets["DATABASE_URL"]
    if not db_url:
        st.error("❌ Database URL not found!")
        st.stop()
    return create_engine(
        db_url.replace("postgres://", "postgresql://"),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )

def get_db_connection():
    return get_engine()

# --- DB INIT (AUTO-FIXING) ---
def init_db():
    engine = get_db_connection()
    with engine.connect() as conn:
        # 1. Transactions Table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id TEXT PRIMARY KEY, date DATE, name TEXT, merchant_name TEXT, 
                amount NUMERIC, category TEXT, bucket TEXT, pending BOOLEAN, 
                manual_category TEXT, manual_bucket TEXT, source TEXT
            );
        """))
        # 2. Rules Table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS category_rules (
                rule_id SERIAL PRIMARY KEY,
                keyword TEXT NOT NULL,
                category TEXT NOT NULL,
                bucket TEXT NOT NULL
            );
        """))
        # 3. Settings Table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS budget_settings (
                key_name TEXT PRIMARY KEY,
                value NUMERIC
            );
        """))
        # 4. DATABASE PATCH: Add str_value if missing
        try:
            conn.execute(text("ALTER TABLE budget_settings ADD COLUMN IF NOT EXISTS str_value TEXT"))
        except Exception:
            pass # Ignore if it already exists or fails harmlessly

        # 5. Net Worth Table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS net_worth_accounts (
                account_id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL, -- 'Asset' or 'Liability'
                balance NUMERIC NOT NULL
            );
        """))
        # 6. Plaid Items (one row per connected bank)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS plaid_items (
                item_id TEXT PRIMARY KEY,
                access_token TEXT NOT NULL,
                institution_name TEXT,
                linked_at TIMESTAMP DEFAULT NOW()
            );
        """))
        # 7. Plaid Accounts (checking, savings, credit cards, etc.)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS plaid_accounts (
                account_id TEXT PRIMARY KEY,
                item_id TEXT REFERENCES plaid_items(item_id) ON DELETE CASCADE,
                name TEXT,
                official_name TEXT,
                type TEXT,
                subtype TEXT,
                current_balance NUMERIC,
                available_balance NUMERIC,
                last_synced TIMESTAMP
            );
        """))
        conn.commit()

# --- HELPERS ---
def get_budget_setting(key, default_val):
    with get_db_connection().connect() as conn:
        try:
            result = conn.execute(text("SELECT value FROM budget_settings WHERE key_name = :k"), {"k": key}).fetchone()
            if result and result[0] is not None:
                return float(result[0])
        except Exception as e:
            st.warning(f"Could not read setting '{key}': {e}")
        return float(default_val)

def get_str_setting(key, default_val):
    with get_db_connection().connect() as conn:
        try:
            result = conn.execute(text("SELECT str_value FROM budget_settings WHERE key_name = :k"), {"k": key}).fetchone()
            if result and result[0] is not None:
                return str(result[0])
        except Exception as e:
            st.warning(f"Could not read setting '{key}': {e}")
        return str(default_val)

def set_budget_setting(key, val, is_str=False):
    with get_db_connection().connect() as conn:
        if is_str:
            conn.execute(text("INSERT INTO budget_settings (key_name, str_value) VALUES (:k, :v) ON CONFLICT (key_name) DO UPDATE SET str_value = :v"), {"k": key, "v": val})
        else:
            conn.execute(text("INSERT INTO budget_settings (key_name, value) VALUES (:k, :v) ON CONFLICT (key_name) DO UPDATE SET value = :v"), {"k": key, "v": val})
        conn.commit()

def run_auto_categorization():
    engine = get_db_connection()
    with engine.connect() as conn:
        rules = pd.read_sql("SELECT * FROM category_rules", conn)
        if rules.empty: return
        for _, rule in rules.iterrows():
            keyword = f"%{rule['keyword']}%"
            conn.execute(text("""
                UPDATE transactions 
                SET category = :cat, bucket = :bucket
                WHERE name ILIKE :kw 
                AND category = 'Uncategorized'
            """), {"cat": rule['category'], "bucket": rule['bucket'], "kw": keyword})
            conn.commit()

# --- FILE PROCESSORS ---
def process_chime_pdf(uploaded_file):
    transactions = []
    filename = uploaded_file.name
    year_match = re.search(r'20\d{2}', filename)
    default_year = year_match.group(0) if year_match else str(datetime.now().year)

    try:
        with pdfplumber.open(uploaded_file) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    lines = text.split('\n')
                    for line in lines:
                        match = re.match(r'^(\d{1,2}/\d{1,2}/\d{2,4}|[A-Z][a-z]{2}\s\d{1,2})\s+(.*)', line)
                        if match:
                            date_part = match.group(1)
                            rest_of_line = match.group(2)
                            if '/' not in date_part: date_part = f"{date_part}, {default_year}"
                            
                            tokens = rest_of_line.split()
                            amount = 0.0
                            description_parts = []
                            found_amount = False
                            
                            for i in range(len(tokens) - 1, -1, -1):
                                clean_token = tokens[i].replace('$', '').replace(',', '').replace('(', '-').replace(')', '')
                                if re.match(r'^-?\d+\.\d{2}$', clean_token) and not found_amount:
                                    amount = float(clean_token)
                                    found_amount = True
                                    description_parts = tokens[:i]
                                    break
                            
                            if found_amount:
                                transactions.append({'date': date_part, 'name': " ".join(description_parts), 'amount': amount, 'source': 'Chime PDF'})
    except Exception as e: st.warning(f"PDF Error: {e}")
    if not transactions: return pd.DataFrame(columns=['date', 'name', 'amount', 'source'])
    return pd.DataFrame(transactions)

def clean_bank_file(uploaded_file, bank_choice):
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
            elif bank_choice == "Chime" or bank_choice == "Loan/Other":
                if 'transaction date' in df.columns: df = df.rename(columns={'transaction date': 'date'})
                if 'description' in df.columns: df = df.rename(columns={'description': 'name'})
            
            df['source'] = bank_choice
            
        elif filename.endswith('.pdf') and bank_choice == "Chime":
            df = process_chime_pdf(uploaded_file)
        else:
            st.error(f"Unsupported file type: {uploaded_file.name}")
            st.stop()

        if 'amount' not in df.columns: df['amount'] = 0.0
        if df['amount'].dtype == 'object':
            df['amount'] = df['amount'].astype(str).str.replace('$', '').str.replace(',', '')
            df['amount'] = pd.to_numeric(df['amount'])
            
        df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
        df['transaction_id'] = df.apply(lambda row: hashlib.md5(f"{row.get('date')}{row.get('name')}{row.get('amount')}".encode()).hexdigest(), axis=1)
        df['bucket'] = 'SPEND'
        df['category'] = 'Uncategorized'
        
        req = ['transaction_id', 'date', 'name', 'amount', 'category', 'bucket', 'source']
        for c in req: 
            if c not in df.columns: df[c] = None
        return df[req].dropna(subset=['date'])
    except Exception as e: st.error(f"Error: {e}"); st.stop()

def save_to_neon(df):
    engine = get_db_connection()
    count = 0
    with engine.connect() as conn:
        for _, row in df.iterrows():
            try:
                conn.execute(text("""
                    INSERT INTO transactions (transaction_id, date, name, merchant_name, amount, category, bucket, pending, source)
                    VALUES (:tid, :date, :name, :name, :amount, :cat, :bucket, :pending, :source)
                    ON CONFLICT (transaction_id) DO NOTHING
                """), {
                    "tid": row['transaction_id'], "date": row['date'], "name": row['name'],
                    "amount": row['amount'], "cat": row['category'], "bucket": row['bucket'],
                    "pending": False, "source": row['source']
                })
                count += 1
            except Exception as e:
                st.warning(f"Skipped duplicate or invalid row: {e}")
        conn.commit()
    return count

# --- PLAID HELPERS ---

def plaid_create_link_token():
    """Create a link token to initialize Plaid Link."""
    redirect_uri = (
        os.getenv("PLAID_REDIRECT_URI")
        or st.secrets.get("PLAID_REDIRECT_URI", "")
        or "https://budgeting-pvssa2ft3xeahtshrebtxp.streamlit.app/~/+/app/static/plaid_link.html"
    )
    payload = {
        "user": {"client_user_id": "budget-master-user"},
        "client_name": "My Budget Master",
        "products": ["transactions"],
        "country_codes": ["US"],
        "language": "en",
        "redirect_uri": redirect_uri,
    }
    data = plaid_post("/link/token/create", payload)
    return data.get("link_token")

def plaid_exchange_public_token(public_token):
    """Exchange a public token for a permanent access token."""
    data = plaid_post("/item/public_token/exchange", {"public_token": public_token})
    return data.get("access_token"), data.get("item_id")

def plaid_get_institution_name(institution_id):
    """Get human-readable institution name."""
    try:
        data = plaid_post("/institutions/get_by_id", {
            "institution_id": institution_id,
            "country_codes": ["US"],
        })
        return data["institution"]["name"]
    except Exception:
        return "Unknown Bank"

def plaid_sync_item(access_token, item_id, institution_name):
    """
    Sync transactions for one linked bank using /transactions/sync.
    Returns (new_count, updated_accounts_list).
    """
    engine = get_db_connection()

    # Retrieve stored cursor for this item
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

        data = plaid_post("/transactions/sync", payload)
        added       = data.get("added", [])
        modified    = data.get("modified", [])
        removed     = data.get("removed", [])
        has_more    = data.get("has_more", False)
        next_cursor = data.get("next_cursor", "")

        rows_to_insert = []
        for txn in added + modified:
            if txn.get("pending"):
                continue  # skip pending, re-import when settled
            amount = txn.get("amount", 0)
            # Plaid signs: positive = debit (money out), negative = credit (money in)
            # We keep that convention. 
            rows_to_insert.append({
                "transaction_id": txn["transaction_id"],
                "date":           txn.get("date"),
                "name":           txn.get("merchant_name") or txn.get("name", ""),
                "amount":         amount,
                "category":       "Uncategorized",
                "bucket":         "INCOME" if amount < 0 else "SPEND",
                "source":         f"Plaid – {institution_name}",
            })

        if rows_to_insert:
            added_total += save_to_neon(pd.DataFrame(rows_to_insert))

        # Remove transactions Plaid says were deleted
        if removed:
            with engine.connect() as conn:
                for r in removed:
                    conn.execute(
                        text("DELETE FROM transactions WHERE transaction_id = :tid"),
                        {"tid": r["transaction_id"]}
                    )
                conn.commit()

        cursor = next_cursor

    # Persist updated cursor
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO budget_settings (key_name, str_value)
            VALUES (:k, :v)
            ON CONFLICT (key_name) DO UPDATE SET str_value = :v
        """), {"k": f"plaid_cursor_{item_id}", "v": cursor})
        conn.commit()

    # Update account balances from /accounts/get
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
                        current_balance   = EXCLUDED.current_balance,
                        available_balance = EXCLUDED.available_balance,
                        last_synced       = NOW()
                """), {
                    "aid":   acc["account_id"],
                    "iid":   item_id,
                    "name":  acc["name"],
                    "oname": acc.get("official_name"),
                    "type":  acc["type"],
                    "sub":   acc.get("subtype"),
                    "cur":   acc["balances"].get("current"),
                    "avail": acc["balances"].get("available"),
                })
            conn.commit()
    except Exception as e:
        st.warning(f"Could not refresh account balances: {e}")

    run_auto_categorization()
    return added_total

# --- MAIN APP ---
init_db()
tab1, tab_life, tab_net, tab_insights, tab2, tab3, tab_plaid = st.tabs([
    "📊 Weekly", "📈 Life Balance", "🏦 Net Worth", "💡 Insights", "⚡ Rules", "📂 Upload", "🔗 Connected Banks"
])

# === TAB 1: WEEKLY DASHBOARD ===
with tab1:
    col_date, col_set = st.columns([2, 1])
    with col_date:
        view_date = st.date_input("📅 View Week Containing:", value=datetime.now())
        start_of_week = view_date - timedelta(days=view_date.weekday())
        end_of_week = start_of_week + timedelta(days=6)
        st.caption(f"Showing: {start_of_week.strftime('%b %d')} - {end_of_week.strftime('%b %d')}")

    with col_set:
        with st.expander("⚙️ Settings"):
            est_income = get_budget_setting("est_income", 4000.0)
            est_bills = get_budget_setting("est_bills", 1500.0)
            new_income = st.number_input("Income", value=est_income)
            new_bills = st.number_input("Fixed Bills", value=est_bills)
            if st.button("Save Settings"):
                set_budget_setting("est_income", new_income); set_budget_setting("est_bills", new_bills); st.rerun()
            st.divider()
            if st.button("🗑️ Delete THIS WEEK"):
                with get_db_connection().connect() as conn:
                    conn.execute(text("DELETE FROM transactions WHERE date >= :s AND date <= :e"), {"s":start_of_week, "e":end_of_week})
                    conn.commit()
                st.rerun()

    with get_db_connection().connect() as conn:
        df = pd.read_sql("SELECT * FROM transactions", conn)
        
    if df.empty:
        st.info("No data.")
    else:
        df['date'] = pd.to_datetime(df['date'])
        week_df = df[(df['date'] >= pd.Timestamp(start_of_week)) & (df['date'] <= pd.Timestamp(end_of_week))].copy()
        
        spend_only_df = week_df[week_df['bucket'] == 'SPEND'].copy()
        week_spend = spend_only_df['amount'].sum()
        
        discretionary_pool = new_income - new_bills
        weekly_allowance = discretionary_pool / 4
        remaining = weekly_allowance - abs(week_spend)
        
        c1, c2, c3 = st.columns(3)
        c1.metric("Weekly Allowance", f"${weekly_allowance:,.0f}")
        c2.metric("Spent This Week", f"${abs(week_spend):,.2f}")
        c3.metric("Remaining", f"${remaining:,.2f}", delta_color="normal" if remaining > 0 else "inverse")
        st.progress(min(abs(week_spend) / weekly_allowance, 1.0) if weekly_allowance > 0 else 0)
        
        st.divider()
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("🕵️ Spending Detective (Editable)")
            if not spend_only_df.empty:
                edited_detective = st.data_editor(spend_only_df[['transaction_id', 'date', 'name', 'amount', 'bucket']], 
                    column_config={"transaction_id": None, "bucket": st.column_config.SelectboxColumn("Bucket", options=["SPEND", "BILL", "INCOME", "TRANSFER"], required=True)},
                    hide_index=True, use_container_width=True, key="detective_editor")
                if st.button("💾 Save Fixes", type="primary"):
                    with get_db_connection().connect() as conn:
                        for i, row in edited_detective.iterrows():
                            conn.execute(text("UPDATE transactions SET bucket = :b WHERE transaction_id = :id"), {"b": row['bucket'], "id": row['transaction_id']}); conn.commit()
                    st.rerun()
            else: st.success("No spending!")
        with c2:
            st.subheader("All Transactions")
            st.dataframe(week_df[['date', 'name', 'amount', 'bucket']].sort_values('date', ascending=False), hide_index=True, use_container_width=True)

# === TAB 2: LIFE BALANCE (RUNNING TOTAL) ===
with tab_life:
    st.header("📈 The Life Balance")
    st.caption("Are you winning or losing over time?")
    
    saved_start = get_str_setting("budget_start_date", "2025-01-01")
    c_set, c_chart = st.columns([1, 3])
    
    with c_set:
        start_date_input = st.date_input("Start Tracking From:", value=pd.to_datetime(saved_start))
        if st.button("Update Start Date"):
            set_budget_setting("budget_start_date", start_date_input.strftime('%Y-%m-%d'), is_str=True)
            st.rerun()
            
    with c_chart:
        if not df.empty:
            mask = df['date'] >= pd.Timestamp(start_date_input)
            life_df = df[mask].copy()
            
            wk_allow = (est_income - est_bills) / 4
            daily_allow = wk_allow / 7
            
            date_range = pd.date_range(start=start_date_input, end=datetime.now())
            daily_data = pd.DataFrame(index=date_range)
            daily_data['allowance'] = daily_allow
            
            daily_spend = life_df[life_df['bucket'] == 'SPEND'].groupby('date')['amount'].sum().abs()
            daily_data = daily_data.join(daily_spend).fillna(0)
            daily_data = daily_data.rename(columns={'amount': 'spend'})
            
            daily_data['cum_allowance'] = daily_data['allowance'].cumsum()
            daily_data['cum_spend'] = daily_data['spend'].cumsum()
            daily_data['running_balance'] = daily_data['cum_allowance'] - daily_data['cum_spend']
            
            last_bal = daily_data['running_balance'].iloc[-1]
            st.metric("Total Life Balance", f"${last_bal:,.2f}", delta="Surplus" if last_bal > 0 else "Deficit")
            
            fig = px.line(daily_data, y='running_balance', title="Running Surplus/Deficit Over Time")
            fig.add_hline(y=0, line_dash="dash", line_color="gray")
            st.plotly_chart(fig, use_container_width=True)

# === TAB 3: NET WORTH & LOANS ===
with tab_net:
    st.header("🏦 Net Worth & Loans")
    
    with st.expander("📝 Update Account Balances", expanded=True):
        with get_db_connection().connect() as conn:
            accounts_df = pd.read_sql("SELECT * FROM net_worth_accounts ORDER BY type", conn)
        
        if accounts_df.empty:
            default_data = pd.DataFrame([
                {"name": "Checking Account", "type": "Asset", "balance": 0.00},
                {"name": "Savings Account", "type": "Asset", "balance": 0.00},
                {"name": "Loan 1", "type": "Liability", "balance": 0.00},
                {"name": "Loan 2", "type": "Liability", "balance": 0.00}
            ])
            with get_db_connection().connect() as conn:
                for _, row in default_data.iterrows():
                    conn.execute(text("INSERT INTO net_worth_accounts (name, type, balance) VALUES (:n, :t, :b)"), {"n":row['name'], "t":row['type'], "b":row['balance']})
                conn.commit()
            st.rerun()

        edited_acc = st.data_editor(accounts_df, column_config={
            "account_id": None,
            "type": st.column_config.SelectboxColumn(options=["Asset", "Liability"]),
            "balance": st.column_config.NumberColumn(format="$%.2f")
        }, hide_index=True, num_rows="dynamic")
        
        if st.button("💾 Save Balances"):
            with get_db_connection().connect() as conn:
                conn.execute(text("DELETE FROM net_worth_accounts"))
                for _, row in edited_acc.iterrows():
                    conn.execute(text("INSERT INTO net_worth_accounts (name, type, balance) VALUES (:n, :t, :b)"), 
                                 {"n": row['name'], "t": row['type'], "b": row['balance']})
                conn.commit()
            st.success("Balances Saved!")
            st.rerun()

    assets = edited_acc[edited_acc['type'] == 'Asset']['balance'].sum()
    liabilities = edited_acc[edited_acc['type'] == 'Liability']['balance'].sum()
    net_worth = assets - liabilities
    
    m1, m2, m3 = st.columns(3)
    m1.metric("Total Assets", f"${assets:,.2f}")
    m2.metric("Total Debt", f"${liabilities:,.2f}")
    m3.metric("Net Worth", f"${net_worth:,.2f}", delta_color="normal" if net_worth > 0 else "inverse")
    
    chart_data = pd.DataFrame({"Type": ["Assets", "Liabilities"], "Amount": [assets, liabilities]})
    st.plotly_chart(px.bar(chart_data, x="Type", y="Amount", color="Type", title="Assets vs Liabilities"), use_container_width=True)

# === TAB 4: INSIGHTS ===
with tab_insights:
    st.header("💡 Insights")
    if not week_df.empty:
        wants_cats = ["Dining Out", "Shopping", "Gambling", "Subscriptions", "Personal Loan"]
        wants_df = week_df[(week_df['category'].isin(wants_cats)) & (week_df['bucket'] == 'SPEND') & (week_df['amount'] < 0)]
        total_wants = abs(wants_df['amount'].sum())
        c1, c2 = st.columns([1, 2])
        c1.metric("Spent on 'Wants'", f"${total_wants:,.2f}")
        
        all_cats = week_df[(week_df['amount'] < 0) & (week_df['bucket'] == 'SPEND')]['category'].unique()
        sel_cats = c2.multiselect("Filter Categories:", options=all_cats, default=all_cats)
        filt_spend = week_df[(week_df['category'].isin(sel_cats)) & (week_df['bucket'] == 'SPEND') & (week_df['amount'] < 0)]['amount'].sum()
        c2.metric("New Remaining", f"${(weekly_allowance - abs(filt_spend)):,.2f}")

# === TAB 5: RULES & INBOX ===
with tab2:
    st.header("⚡ Rules & Inbox")
    
    # ADDED "Travel" to this list:
    CAT_OPTIONS = ["Groceries", "Dining Out", "Rent", "Utilities", "Shopping", "Transport", "Travel", "Income", "Subscriptions", "Credit Card Pay", "Home Improvement", "Pets", "RX", "Savings", "Gambling", "Personal Loan", "Uncategorized"]
    
    # --- 1. ADD NEW RULE ---
    with st.expander("➕ Add Auto-Rule"):
        with st.form("add"):
            c1,c2,c3=st.columns([2,1,1]); nk=c1.text_input("If Name Contains..."); nc=c2.selectbox("Set Category", CAT_OPTIONS); nb=c3.selectbox("Set Bucket", ["SPEND","BILL","INCOME","TRANSFER"])
            if st.form_submit_button("Save Rule"):
                with get_db_connection().connect() as c: c.execute(text("INSERT INTO category_rules (keyword, category, bucket) VALUES (:k,:c,:b)"), {"k":nk,"c":nc,"b":nb}); c.commit()
                run_auto_categorization(); st.rerun()

    # --- 2. ACTION ITEMS ---
    st.divider()
    st.subheader("🚨 Inbox: New Uploads")
    
    with get_db_connection().connect() as conn:
        todo = pd.read_sql("SELECT * FROM transactions WHERE category='Uncategorized'", conn)
    
    if not todo.empty:
        st.info(f"You have {len(todo)} new transactions to sort.")
        ed_todo = st.data_editor(todo, column_config={
            "transaction_id": None,
            "category": st.column_config.SelectboxColumn(options=CAT_OPTIONS, required=True),
            "bucket": st.column_config.SelectboxColumn(options=["SPEND","BILL","INCOME","TRANSFER"], required=True)
        }, hide_index=True, use_container_width=True)
        
        if st.button("💾 Save Inbox Changes"):
            with get_db_connection().connect() as c:
                for _, r in ed_todo.iterrows():
                    if r['category'] != 'Uncategorized': 
                        c.execute(text("UPDATE transactions SET category=:c, bucket=:b WHERE transaction_id=:i"),
                                  {"c":r['category'],"b":r['bucket'],"i":r['transaction_id']})
                c.commit()
            st.success("Saved!"); st.rerun()
    else:
        st.success("🎉 Inbox Zero! All transactions are categorized.")

    # --- 3. FULL HISTORY ---
    st.divider()
    st.subheader("🔍 Full History")
    s = st.text_input("Search History:", "")

    with get_db_connection().connect() as c:
        if s:
            search_param = f"%{s}%"
            h = pd.read_sql(
                text("SELECT * FROM transactions WHERE name ILIKE :search OR amount::text LIKE :search ORDER BY date DESC LIMIT 200"),
                c,
                params={"search": search_param}
            )
        else:
            h = pd.read_sql(
                text("SELECT * FROM transactions ORDER BY date DESC LIMIT 50"),
                c
            )
    
    ed=st.data_editor(h, column_config={
        "transaction_id": None,
        "category": st.column_config.SelectboxColumn(options=CAT_OPTIONS),
        "bucket": st.column_config.SelectboxColumn(options=["SPEND","BILL","INCOME","TRANSFER"])
    }, hide_index=True, use_container_width=True)
    
    if st.button("Update History"): 
        with get_db_connection().connect() as c: 
            for _,r in ed.iterrows(): c.execute(text("UPDATE transactions SET category=:c, bucket=:b WHERE transaction_id=:i"),{"c":r['category'],"b":r['bucket'],"i":r['transaction_id']}); c.commit()
        st.rerun()
# === TAB 6: UPLOAD ===
with tab3:
    st.header("Upload"); bc = st.selectbox("Bank", ["Chase","Citi","Sofi","Chime","Loan/Other"]); f = st.file_uploader("CSV/PDF", type=['csv','pdf'])
    if f: 
        df = clean_bank_file(f, bc); st.dataframe(df.head(), hide_index=True)
        if st.button("Confirm"): c=save_to_neon(df); st.success(f"{c} added!"); st.rerun()

# === TAB 7: CONNECTED BANKS (PLAID) ===
with tab_plaid:
    st.header("🔗 Connected Banks")
    st.caption("Connect your bank accounts once — transactions sync automatically.")

    if not PLAID_CLIENT_ID or not PLAID_SECRET:
        st.error("⚠️ Plaid credentials not found. Add PLAID_CLIENT_ID, PLAID_SECRET, and PLAID_ENV to your .env or Streamlit secrets.")
        st.stop()

    # ------------------------------------------------------------------ #
    # SECTION 1 — Currently connected banks                               #
    # ------------------------------------------------------------------ #
    with get_db_connection().connect() as conn:
        items_df    = pd.read_sql("SELECT * FROM plaid_items ORDER BY linked_at DESC", conn)
        accounts_df = pd.read_sql("SELECT * FROM plaid_accounts ORDER BY type, name", conn)

    if items_df.empty:
        st.info("No banks connected yet. Use the button below to link your first account.")
    else:
        st.subheader("Your Connected Accounts")
        for _, item in items_df.iterrows():
            with st.expander(f"🏦 {item['institution_name']}  —  linked {pd.to_datetime(item['linked_at']).strftime('%b %d, %Y')}", expanded=True):
                item_accounts = accounts_df[accounts_df['item_id'] == item['item_id']]

                if not item_accounts.empty:
                    display_cols = ['name', 'type', 'subtype', 'current_balance', 'available_balance', 'last_synced']
                    display_cols = [c for c in display_cols if c in item_accounts.columns]
                    st.dataframe(
                        item_accounts[display_cols].rename(columns={
                            'name': 'Account', 'type': 'Type', 'subtype': 'Subtype',
                            'current_balance': 'Balance', 'available_balance': 'Available',
                            'last_synced': 'Last Synced'
                        }),
                        hide_index=True, use_container_width=True
                    )

                col_sync, col_remove = st.columns([1, 4])
                with col_sync:
                    if st.button("🔄 Sync Now", key=f"sync_{item['item_id']}"):
                        with st.spinner(f"Syncing {item['institution_name']}..."):
                            try:
                                new_count = plaid_sync_item(
                                    item['access_token'],
                                    item['item_id'],
                                    item['institution_name']
                                )
                                st.success(f"✅ {new_count} new transactions imported!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Sync failed: {e}")
                with col_remove:
                    if st.button("🗑️ Disconnect", key=f"remove_{item['item_id']}"):
                        try:
                            plaid_post("/item/remove", {"access_token": item['access_token']})
                        except Exception:
                            pass  # Best-effort removal on Plaid side
                        with get_db_connection().connect() as conn:
                            conn.execute(text("DELETE FROM plaid_items WHERE item_id = :id"), {"id": item['item_id']})
                            conn.commit()
                        st.success("Bank disconnected.")
                        st.rerun()

    st.divider()

    # ------------------------------------------------------------------ #
    # SECTION 2 — Sync ALL banks at once                                  #
    # ------------------------------------------------------------------ #
    if not items_df.empty:
        if st.button("🔄 Sync All Banks", type="primary"):
            total_new = 0
            for _, item in items_df.iterrows():
                with st.spinner(f"Syncing {item['institution_name']}..."):
                    try:
                        n = plaid_sync_item(item['access_token'], item['item_id'], item['institution_name'])
                        total_new += n
                    except Exception as e:
                        st.warning(f"Could not sync {item['institution_name']}: {e}")
            st.success(f"✅ Sync complete — {total_new} new transactions imported!")
            st.rerun()
        st.divider()

    # ------------------------------------------------------------------ #
    # SECTION 3 — Link a new bank via Plaid Link                          #
    # ------------------------------------------------------------------ #
    st.subheader("➕ Link a New Bank")

    # Check if we're returning from Plaid Link with a public token
    public_token_param = st.query_params.get("plaid_public_token", "")

    if public_token_param and public_token_param not in ["", "null"]:
        with st.spinner("Finishing bank connection..."):
            try:
                access_token, item_id = plaid_exchange_public_token(public_token_param)
                item_info = plaid_post("/item/get", {"access_token": access_token})
                institution_id = item_info["item"].get("institution_id", "")
                institution_name = plaid_get_institution_name(institution_id) if institution_id else "Your Bank"

                with get_db_connection().connect() as conn:
                    conn.execute(text("""
                        INSERT INTO plaid_items (item_id, access_token, institution_name)
                        VALUES (:iid, :tok, :name)
                        ON CONFLICT (item_id) DO UPDATE SET
                            access_token     = EXCLUDED.access_token,
                            institution_name = EXCLUDED.institution_name
                    """), {"iid": item_id, "tok": access_token, "name": institution_name})
                    conn.commit()

                st.query_params.clear()
                new_count = plaid_sync_item(access_token, item_id, institution_name)
                st.success(f"🎉 {institution_name} connected! {new_count} transactions imported.")
                st.rerun()
            except Exception as e:
                st.error(f"Failed to connect bank: {e}")
                st.query_params.clear()
    else:
        # Generate a fresh link token
        try:
            link_token = plaid_create_link_token()
        except Exception as e:
            st.error(f"Could not create Plaid link token: {e}")
            link_token = None

        if link_token:

            # Build the URL to the static Plaid Link page, passing the
            # link_token and the current app URL as query params
            import urllib.parse
            return_url  = f"https://{st.context.headers.get('host', 'localhost')}"
            plaid_page_url = (
                f"app/static/plaid_link.html"
                f"?link_token={urllib.parse.quote(link_token)}"
                f"&return_url={urllib.parse.quote(return_url)}"
            )

            st.markdown(f"""
            <a href="{plaid_page_url}" target="_blank" style="
                display: inline-block;
                background: #4CAF50;
                color: white;
                padding: 14px 32px;
                font-size: 16px;
                font-weight: bold;
                border-radius: 8px;
                text-decoration: none;
                margin-top: 8px;">
                🏦 Connect a Bank Account
            </a>
            """, unsafe_allow_html=True)

            st.caption("A new tab will open with a secure bank login. After connecting, you'll be returned to this page automatically.")
            st.caption("🔒 Bank-grade encryption. Plaid is trusted by Venmo, Coinbase, and Robinhood.")

    # ------------------------------------------------------------------ #
    # SECTION 4 — Setup instructions                                      #
    # ------------------------------------------------------------------ #
    with st.expander("📋 Setup Instructions", expanded=False):
        st.markdown("""
        **To activate Plaid sync, add these to your `.env` file or Streamlit secrets:**

        ```
        PLAID_CLIENT_ID=your_client_id_here
        PLAID_SECRET=your_secret_here
        PLAID_ENV=development   # use 'sandbox' for testing, 'development' or 'production' for real banks
        ```

        **Environments:**
        - `sandbox` — test with fake credentials (username: `user_good`, password: `pass_good`)
        - `development` — real banks, up to 100 live Items free
        - `production` — real banks, requires Plaid approval

        **After connecting a bank:**
        - Hit **Sync Now** or **Sync All Banks** to pull latest transactions
        - New transactions land in the **Rules & Inbox** tab for categorization
        - Your auto-rules will apply automatically on every sync
        """)
