import streamlit as st
import pandas as pd
import os
import hashlib
import re
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from datetime import datetime, timedelta
import plotly.express as px
import plotly.graph_objects as go
import pdfplumber

load_dotenv()

# --- CONFIG ---
st.set_page_config(page_title="My Budget Master", layout="wide", page_icon="💰")

# --- DATABASE CONNECTION ---
def get_db_connection():
    db_url = os.getenv("DATABASE_URL")
    if not db_url and "DATABASE_URL" in st.secrets:
        db_url = st.secrets["DATABASE_URL"]
    if not db_url:
        st.error("❌ Database URL not found!")
        st.stop()
    return create_engine(db_url.replace("postgres://", "postgresql://"))

# --- DB INIT ---
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
                value NUMERIC,
                str_value TEXT
            );
        """))
        # NEW: Net Worth Accounts Table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS net_worth_accounts (
                account_id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL, -- 'Asset' or 'Liability'
                balance NUMERIC NOT NULL
            );
        """))
        conn.commit()

# --- HELPERS ---
def get_budget_setting(key, default_val):
    with get_db_connection().connect() as conn:
        result = conn.execute(text("SELECT value FROM budget_settings WHERE key_name = :k"), {"k": key}).fetchone()
        if result and result[0] is not None: return float(result[0])
        return float(default_val)

def get_str_setting(key, default_val):
    with get_db_connection().connect() as conn:
        result = conn.execute(text("SELECT str_value FROM budget_settings WHERE key_name = :k"), {"k": key}).fetchone()
        if result and result[0] is not None: return str(result[0])
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
            except: pass
        conn.commit()
    return count

# --- MAIN APP ---
init_db()
tab1, tab_life, tab_net, tab_insights, tab2, tab3 = st.tabs(["📊 Weekly", "📈 Life Balance", "🏦 Net Worth", "💡 Insights", "⚡ Rules", "📂 Upload"])

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
    
    # Settings for Start Date
    saved_start = get_str_setting("budget_start_date", "2025-01-01")
    c_set, c_chart = st.columns([1, 3])
    
    with c_set:
        start_date_input = st.date_input("Start Tracking From:", value=pd.to_datetime(saved_start))
        if st.button("Update Start Date"):
            set_budget_setting("budget_start_date", start_date_input.strftime('%Y-%m-%d'), is_str=True)
            st.rerun()
            
    with c_chart:
        if not df.empty:
            # Logic: Calculate Cumulative Allowance vs Cumulative Spend
            mask = df['date'] >= pd.Timestamp(start_date_input)
            life_df = df[mask].copy()
            
            # 1. Get Weekly Allowance
            wk_allow = (est_income - est_bills) / 4
            daily_allow = wk_allow / 7
            
            # 2. Generate Daily Range
            date_range = pd.date_range(start=start_date_input, end=datetime.now())
            daily_data = pd.DataFrame(index=date_range)
            daily_data['allowance'] = daily_allow
            
            # 3. Merge Spending
            daily_spend = life_df[life_df['bucket'] == 'SPEND'].groupby('date')['amount'].sum().abs()
            daily_data = daily_data.join(daily_spend).fillna(0)
            daily_data = daily_data.rename(columns={'amount': 'spend'})
            
            # 4. Running Totals
            daily_data['cum_allowance'] = daily_data['allowance'].cumsum()
            daily_data['cum_spend'] = daily_data['spend'].cumsum()
            daily_data['running_balance'] = daily_data['cum_allowance'] - daily_data['cum_spend']
            
            # 5. Chart
            last_bal = daily_data['running_balance'].iloc[-1]
            st.metric("Total Life Balance", f"${last_bal:,.2f}", delta="Surplus" if last_bal > 0 else "Deficit")
            
            fig = px.line(daily_data, y='running_balance', title="Running Surplus/Deficit Over Time")
            fig.add_hline(y=0, line_dash="dash", line_color="gray")
            st.plotly_chart(fig, use_container_width=True)

# === TAB 3: NET WORTH & LOANS ===
with tab_net:
    st.header("🏦 Net Worth & Loans")
    
    # 1. ACCOUNTS TABLE
    with st.expander("📝 Update Account Balances", expanded=True):
        with get_db_connection().connect() as conn:
            accounts_df = pd.read_sql("SELECT * FROM net_worth_accounts ORDER BY type", conn)
        
        if accounts_df.empty:
            # Default Data
            default_data = pd.DataFrame([
                {"name": "Chime Checking", "type": "Asset", "balance": 1500.00},
                {"name": "Chime Savings", "type": "Asset", "balance": 5000.00},
                {"name": "Car Loan", "type": "Liability", "balance": 12000.00},
                {"name": "Personal Loan", "type": "Liability", "balance": 5000.00}
            ])
            with get_db_connection().connect() as conn:
                for _, row in default_data.iterrows():
                    conn.execute(text("INSERT INTO net_worth_accounts (name, type, balance) VALUES (:n, :t, :b)"), {"n":row['name'], "t":row['type'], "b":row['balance']})
                conn.commit()
            st.rerun()

        # Editable Table
        edited_acc = st.data_editor(accounts_df, column_config={
            "account_id": None,
            "type": st.column_config.SelectboxColumn(options=["Asset", "Liability"]),
            "balance": st.column_config.NumberColumn(format="$%.2f")
        }, hide_index=True, num_rows="dynamic")
        
        if st.button("💾 Save Balances"):
            with get_db_connection().connect() as conn:
                conn.execute(text("DELETE FROM net_worth_accounts")) # Simple wipe and replace
                for _, row in edited_acc.iterrows():
                    conn.execute(text("INSERT INTO net_worth_accounts (name, type, balance) VALUES (:n, :t, :b)"), 
                                 {"n": row['name'], "t": row['type'], "b": row['balance']})
                conn.commit()
            st.success("Balances Saved!")
            st.rerun()

    # 2. METRICS
    assets = edited_acc[edited_acc['type'] == 'Asset']['balance'].sum()
    liabilities = edited_acc[edited_acc['type'] == 'Liability']['balance'].sum()
    net_worth = assets - liabilities
    
    m1, m2, m3 = st.columns(3)
    m1.metric("Total Assets", f"${assets:,.2f}")
    m2.metric("Total Debt", f"${liabilities:,.2f}")
    m3.metric("Net Worth", f"${net_worth:,.2f}", delta_color="normal" if net_worth > 0 else "inverse")
    
    # 3. CHART
    chart_data = pd.DataFrame({
        "Type": ["Assets", "Liabilities"],
        "Amount": [assets, liabilities]
    })
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

# === TAB 5: RULES ===
with tab2:
    st.header("⚡ Rules"); CAT_OPTIONS = ["Groceries", "Dining Out", "Rent", "Utilities", "Shopping", "Transport", "Income", "Subscriptions", "Credit Card Pay", "Home Improvement", "Pets", "RX", "Savings", "Gambling", "Personal Loan"]
    with st.expander("➕ Add Rule"):
        with st.form("add"):
            c1,c2,c3=st.columns([2,1,1]); nk=c1.text_input("Keyword"); nc=c2.selectbox("Cat", CAT_OPTIONS); nb=c3.selectbox("Bkt", ["SPEND","BILL","INCOME","TRANSFER"])
            if st.form_submit_button("Save"):
                with get_db_connection().connect() as c: c.execute(text("INSERT INTO category_rules (keyword, category, bucket) VALUES (:k,:c,:b)"), {"k":nk,"c":nc,"b":nb}); c.commit()
                run_auto_categorization(); st.rerun()
    st.divider(); st.subheader("🔍 Full History"); s=st.text_input("Search:", "")
    q = "SELECT * FROM transactions WHERE category != 'Uncategorized'" + (f" AND (name ILIKE '%{s}%' OR amount::text LIKE '%{s}%')" if s else " ORDER BY date DESC LIMIT 50")
    with get_db_connection().connect() as c: h=pd.read_sql(text(q),c)
    ed=st.data_editor(h, column_config={"category":st.column_config.SelectboxColumn(options=CAT_OPTIONS),"bucket":st.column_config.SelectboxColumn(options=["SPEND","BILL","INCOME","TRANSFER"])}, hide_index=True)
    if st.button("Update"): 
        with get_db_connection().connect() as c: 
            for _,r in ed.iterrows(): c.execute(text("UPDATE transactions SET category=:c, bucket=:b WHERE transaction_id=:i"),{"c":r['category'],"b":r['bucket'],"i":r['transaction_id']}); c.commit()
        st.rerun()

# === TAB 6: UPLOAD ===
with tab3:
    st.header("Upload"); bc = st.selectbox("Bank", ["Chase","Citi","Sofi","Chime","Loan/Other"]); f = st.file_uploader("CSV/PDF", type=['csv','pdf'])
    if f: 
        df = clean_bank_file(f, bc); st.dataframe(df.head(), hide_index=True)
        if st.button("Confirm"): c=save_to_neon(df); st.success(f"{c} added!"); st.rerun()
