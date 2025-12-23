import streamlit as st
import pandas as pd
import os
import hashlib
import re
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from datetime import datetime, timedelta
import plotly.express as px
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
                value NUMERIC
            );
        """))
        conn.commit()

# --- HELPERS ---
def get_budget_setting(key, default_val):
    with get_db_connection().connect() as conn:
        result = conn.execute(text("SELECT value FROM budget_settings WHERE key_name = :k"), {"k": key}).fetchone()
        if result: return float(result[0])
        return float(default_val)

def set_budget_setting(key, value):
    with get_db_connection().connect() as conn:
        conn.execute(text("""
            INSERT INTO budget_settings (key_name, value) VALUES (:k, :v)
            ON CONFLICT (key_name) DO UPDATE SET value = :v
        """), {"k": key, "v": value})
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
    """
    Text-Mode Parser for Chime PDFs (Handles borderless tables)
    """
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
                        # Regex for Date (MM/DD/YYYY or Mon DD)
                        match = re.match(r'^(\d{1,2}/\d{1,2}/\d{2,4}|[A-Z][a-z]{2}\s\d{1,2})\s+(.*)', line)
                        if match:
                            date_part = match.group(1)
                            rest_of_line = match.group(2)
                            if '/' not in date_part: date_part = f"{date_part}, {default_year}"
                            
                            # Scan backwards for amount
                            tokens = rest_of_line.split()
                            amount = 0.0
                            description_parts = []
                            found_amount = False
                            
                            for i in range(len(tokens) - 1, -1, -1):
                                clean_token = tokens[i].replace('$', '').replace(',', '').replace('(', '-').replace(')', '')
                                # Check if token is a valid number
                                if re.match(r'^-?\d+\.\d{2}$', clean_token) and not found_amount:
                                    amount = float(clean_token)
                                    found_amount = True
                                    description_parts = tokens[:i]
                                    break
                            
                            if found_amount:
                                transactions.append({
                                    'date': date_part, 
                                    'name': " ".join(description_parts), 
                                    'amount': amount, 
                                    'source': 'Chime PDF'
                                })
    except Exception as e: st.warning(f"PDF Parsing Warning: {e}")
    
    if not transactions: return pd.DataFrame(columns=['date', 'name', 'amount', 'source'])
    return pd.DataFrame(transactions)

def clean_bank_file(uploaded_file, bank_choice):
    try:
        # Lowercase check for file extension
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
            
            elif bank_choice == "Chime":
                df = df.rename(columns={'transaction date': 'date', 'description': 'name'})
            
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
        
        # Generate Unique ID
        df['transaction_id'] = df.apply(lambda row: hashlib.md5(f"{row.get('date')}{row.get('name')}{row.get('amount')}".encode()).hexdigest(), axis=1)
        
        df['bucket'] = 'SPEND'
        df['category'] = 'Uncategorized'
        
        req = ['transaction_id', 'date', 'name', 'amount', 'category', 'bucket', 'source']
        for c in req: 
            if c not in df.columns: df[c] = None
        return df[req].dropna(subset=['date'])
        
    except Exception as e: st.error(f"Error processing file: {e}"); st.stop()

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
tab1, tab_insights, tab_month, tab2, tab3 = st.tabs(["📊 Dashboard", "💡 Insights", "📅 Monthly", "⚡ Rules", "📂 Upload"])

# === TAB 1: WEEKLY DASHBOARD (Editable Detective) ===
with tab1:
    col_date, col_set = st.columns([2, 1])
    with col_date:
        view_date = st.date_input("📅 View Week Containing:", value=datetime.now())
        start_of_week = view_date - timedelta(days=view_date.weekday())
        end_of_week = start_of_week + timedelta(days=6)
        st.caption(f"Showing: {start_of_week.strftime('%b %d')} - {end_of_week.strftime('%b %d')}")

    with col_set:
        with st.expander("⚙️ Settings & Danger Zone"):
            est_income = get_budget_setting("est_income", 4000.0)
            est_bills = get_budget_setting("est_bills", 1500.0)
            new_income = st.number_input("Income", value=est_income)
            new_bills = st.number_input("Fixed Bills", value=est_bills)
            if st.button("Save Settings"):
                set_budget_setting("est_income", new_income); set_budget_setting("est_bills", new_bills); st.rerun()
            
            st.divider()
            if st.button("🗑️ Delete THIS WEEK'S Data"):
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
            st.caption("Spot an error? Change 'SPEND' to 'TRANSFER' here.")
            if not spend_only_df.empty:
                edited_detective = st.data_editor(
                    spend_only_df[['transaction_id', 'date', 'name', 'amount', 'bucket']], 
                    column_config={
                        "transaction_id": None, 
                        "bucket": st.column_config.SelectboxColumn("Bucket", options=["SPEND", "BILL", "INCOME", "TRANSFER"], required=True)
                    },
                    hide_index=True, use_container_width=True, key="detective_editor"
                )
                if st.button("💾 Save Corrections", type="primary"):
                    with get_db_connection().connect() as conn:
                        for i, row in edited_detective.iterrows():
                            conn.execute(text("UPDATE transactions SET bucket = :b WHERE transaction_id = :id"), 
                                         {"b": row['bucket'], "id": row['transaction_id']})
                            conn.commit()
                    st.success("Updated!"); st.rerun()
            else: st.success("No spending this week!")
                
        with c2:
            st.subheader("All Transactions")
            st.dataframe(week_df[['date', 'name', 'amount', 'bucket']].sort_values('date', ascending=False), hide_index=True, use_container_width=True)

# === TAB 2: INSIGHTS (Wants Analysis) ===
with tab_insights:
    st.header("💡 Spending Insights")
    if week_df.empty:
        st.info("No data.")
    else:
        wants_cats = ["Dining Out", "Shopping", "Gambling", "Subscriptions", "Entertainment", "Personal Loan"]
        wants_df = week_df[(week_df['category'].isin(wants_cats)) & (week_df['bucket'] == 'SPEND') & (week_df['amount'] < 0)].copy()
        total_wants = abs(wants_df['amount'].sum())
        
        col1, col2 = st.columns([1, 2])
        with col1:
            st.metric("Spent on 'Wants'", f"${total_wants:,.2f}")
            st.write("Includes Dining Out, Shopping, Gambling, etc.")
        with col2:
            st.write("### ✂️ The 'What If' Machine")
            all_cats = week_df[(week_df['amount'] < 0) & (week_df['bucket'] == 'SPEND')]['category'].unique()
            sel_cats = st.multiselect("Included Categories:", options=all_cats, default=all_cats)
            filtered_spend = week_df[(week_df['category'].isin(sel_cats)) & (week_df['bucket'] == 'SPEND') & (week_df['amount'] < 0)]['amount'].sum()
            new_rem = weekly_allowance - abs(filtered_spend)
            st.metric("New Remaining Balance", f"${new_rem:,.2f}")

# === TAB 3: MONTHLY (Interactive Drill-Down) ===
with tab_month:
    st.header("📅 Monthly Overview")
    sel_date = st.date_input("Select Month:", value=datetime.now(), key="month_picker")
    start_month = sel_date.replace(day=1)
    next_month = (start_month.replace(day=28) + timedelta(days=4)).replace(day=1)
    end_month = next_month - timedelta(days=1)
    
    if not df.empty:
        month_df = df[(df['date'] >= pd.Timestamp(start_month)) & (df['date'] <= pd.Timestamp(end_month))].copy()
        if not month_df.empty:
            inc_df = month_df[(month_df['bucket'] == 'INCOME') & (month_df['amount'] > 0)]
            bill_df = month_df[(month_df['bucket'] == 'BILL') & (month_df['amount'] < 0)]
            spend_df = month_df[(month_df['bucket'] == 'SPEND') & (month_df['amount'] < 0)]
            
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Income", f"${inc_df['amount'].sum():,.0f}")
            m2.metric("Bills", f"${abs(bill_df['amount'].sum()):,.0f}")
            m3.metric("Spending", f"${abs(spend_df['amount'].sum()):,.0f}")
            net = inc_df['amount'].sum() + bill_df['amount'].sum() + spend_df['amount'].sum()
            m4.metric("Net Saved", f"${net:,.2f}", delta_color="normal" if net > 0 else "inverse")
            st.divider()
            
            st.subheader("🔍 Deep Dive")
            view_mode = st.radio("View:", ["Discretionary Spending", "Fixed Bills", "Income"], horizontal=True)
            if view_mode == "Discretionary Spending": target_df = spend_df.copy()
            elif view_mode == "Fixed Bills": target_df = bill_df.copy()
            else: target_df = inc_df.copy()
            
            if not target_df.empty:
                target_df['amount'] = target_df['amount'].abs()
                c1, c2 = st.columns([1, 2])
                with c1:
                    fig = px.pie(target_df, values='amount', names='category', hole=0.4)
                    fig.update_layout(margin=dict(t=0, b=0, l=0, r=0), height=300)
                    st.plotly_chart(fig, use_container_width=True)
                with c2:
                    avail_cats = sorted(target_df['category'].unique().tolist())
                    sel_cat = st.selectbox("📂 Filter by Category:", ["Show All"] + avail_cats)
                    display_df = target_df if sel_cat == "Show All" else target_df[target_df['category'] == sel_cat]
                    st.dataframe(display_df[['date', 'name', 'category', 'amount']].sort_values('date', ascending=False), hide_index=True, use_container_width=True)
            else: st.info(f"No {view_mode} found.")

# === TAB 4: RULES & HISTORY ===
with tab2:
    st.header("⚡ Rules & Edits")
    CAT_OPTIONS = ["Groceries", "Dining Out", "Rent", "Utilities", "Shopping", "Transport", "Income", "Subscriptions", "Credit Card Pay", "Home Improvement", "Pets", "RX", "Savings", "Gambling", "Personal Loan"]
    
    with st.expander("➕ Add New Auto-Rule"):
        with st.form("add_rule"):
            c1, c2, c3 = st.columns([2, 1, 1])
            with c1: nk = st.text_input("Name Contains...", placeholder="e.g. Walmart")
            with c2: nc = st.selectbox("Category", CAT_OPTIONS)
            with c3: nb = st.selectbox("Bucket", ["SPEND", "BILL", "INCOME", "TRANSFER"])
            if st.form_submit_button("Save") and nk:
                with get_db_connection().connect() as conn:
                    conn.execute(text("INSERT INTO category_rules (keyword, category, bucket) VALUES (:k,:c,:b)"), {"k":nk,"c":nc,"b":nb}); conn.commit()
                run_auto_categorization(); st.rerun()

    st.divider(); st.subheader("🚨 Action Items"); todo = pd.read_sql("SELECT * FROM transactions WHERE category='Uncategorized'", get_db_connection())
    if not todo.empty:
        ed = st.data_editor(todo, column_config={"category":st.column_config.SelectboxColumn(options=CAT_OPTIONS),"bucket":st.column_config.SelectboxColumn(options=["SPEND","BILL","INCOME","TRANSFER"])}, hide_index=True)
        if st.button("Save Actions"):
            with get_db_connection().connect() as conn:
                for i,r in ed.iterrows(): 
                    if r['category']!='Uncategorized': conn.execute(text("UPDATE transactions SET category=:c, bucket=:b WHERE transaction_id=:i"),{"c":r['category'],"b":r['bucket'],"i":r['transaction_id']}); conn.commit()
            st.rerun()

    st.divider(); st.subheader("🔍 Full History (Unlimited Search)")
    search_term = st.text_input("Search:", "")
    query = "SELECT * FROM transactions WHERE category != 'Uncategorized'"
    if search_term: 
        query += f" AND (name ILIKE '%{search_term}%' OR amount::text LIKE '%{search_term}%') ORDER BY date DESC"
    else: 
        query += " ORDER BY date DESC LIMIT 50"
        
    with get_db_connection().connect() as conn: h_df = pd.read_sql(text(query), conn)
    ed_h = st.data_editor(h_df, column_config={"category":st.column_config.SelectboxColumn(options=CAT_OPTIONS),"bucket":st.column_config.SelectboxColumn(options=["SPEND","BILL","INCOME","TRANSFER"])}, hide_index=True)
    if st.button("Update History"):
        with get_db_connection().connect() as conn:
            for i,r in ed_h.iterrows(): conn.execute(text("UPDATE transactions SET category=:c, bucket=:b WHERE transaction_id=:i"),{"c":r['category'],"b":r['bucket'],"i":r['transaction_id']}); conn.commit()
        st.rerun()

# === TAB 5: UPLOAD ===
with tab3:
    st.header("Upload"); bc = st.selectbox("Bank", ["Chase","Citi","Sofi","Chime"]); f = st.file_uploader("CSV/PDF", type=['csv','pdf'])
    if f: 
        df = clean_bank_file(f, bc); st.dataframe(df.head(), hide_index=True)
        if st.button("Confirm"): c=save_to_neon(df); st.success(f"{c} added!"); st.rerun()
    st.divider()
    if st.button("Delete Old Data"):
        d = st.date_input("Before:", value=pd.to_datetime("2025-10-01"))
        with get_db_connection().connect() as conn: conn.execute(text("DELETE FROM transactions WHERE date < :d"), {"d":d}); conn.commit()
        st.success("Deleted!"); st.rerun()
