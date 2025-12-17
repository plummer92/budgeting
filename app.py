import streamlit as st
import pandas as pd
import os
import hashlib
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from datetime import datetime, timedelta
import plotly.express as px

load_dotenv()

# --- CONFIG ---
st.set_page_config(page_title="My Weekly Budget", layout="wide", page_icon="💰")

# --- DATABASE CONNECTION ---
def get_db_connection():
    db_url = os.getenv("DATABASE_URL")
    if not db_url and "DATABASE_URL" in st.secrets:
        db_url = st.secrets["DATABASE_URL"]
    if not db_url:
        st.error("❌ Database URL not found!")
        st.stop()
    return create_engine(db_url.replace("postgres://", "postgresql://"))

# --- DB INIT (With Rules Table) ---
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
        
        # 2. Rules Table (This was likely missing or broken before)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS category_rules (
                rule_id SERIAL PRIMARY KEY,
                keyword TEXT NOT NULL,
                category TEXT NOT NULL,
                bucket TEXT NOT NULL
            );
        """))
        conn.commit()

# --- HELPER FUNCTIONS ---
def run_auto_categorization():
    """Applies rules to uncategorized transactions"""
    engine = get_db_connection()
    count = 0
    with engine.connect() as conn:
        # Get rules
        rules = pd.read_sql("SELECT * FROM category_rules", conn)
        if rules.empty:
            return 0
            
        for _, rule in rules.iterrows():
            keyword = f"%{rule['keyword']}%"
            # Postgres ILIKE is case-insensitive
            result = conn.execute(text("""
                UPDATE transactions 
                SET category = :cat, bucket = :bucket
                WHERE name ILIKE :kw 
                AND category = 'Uncategorized'
            """), {"cat": rule['category'], "bucket": rule['bucket'], "kw": keyword})
            count += result.rowcount
        conn.commit()
    return count

# --- DATA CLEANING (Same as before) ---
def process_chase(df):
    df.columns = df.columns.str.strip().str.lower().str.replace('\ufeff', '')
    if 'post date' in df.columns: df = df.rename(columns={'post date': 'date'})
    elif 'transaction date' in df.columns: df = df.rename(columns={'transaction date': 'date'})
    if 'description' in df.columns: df = df.rename(columns={'description': 'name'})
    elif 'merchant' in df.columns: df = df.rename(columns={'merchant': 'name'})
    
    df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
    df['source'] = 'Chase'
    return df

def process_citi(df):
    df.columns = df.columns.str.strip().str.lower().str.replace('\ufeff', '')
    df = df.rename(columns={'date': 'date', 'description': 'name', 'merchant name': 'name'})
    if 'debit' not in df.columns: df['debit'] = 0
    if 'credit' not in df.columns: df['credit'] = 0
    if 'amount' not in df.columns: df['amount'] = df['credit'].fillna(0) - df['debit'].fillna(0)
    
    df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
    df['source'] = 'Citi'
    return df

def clean_bank_csv(uploaded_file, bank_choice):
    try:
        df = pd.read_csv(uploaded_file, encoding='utf-8-sig')
        if bank_choice == "Chase": df = process_chase(df)
        elif bank_choice == "Citi": df = process_citi(df)
        
        if df['amount'].dtype == 'object':
            df['amount'] = df['amount'].astype(str).str.replace('$', '').str.replace(',', '')
            df['amount'] = pd.to_numeric(df['amount'])
            
        def generate_id(row):
            return hashlib.md5(f"{row.get('date')}{row.get('name')}{row.get('amount')}".encode()).hexdigest()

        df['transaction_id'] = df.apply(generate_id, axis=1)
        df['bucket'] = 'SPEND'
        df['category'] = 'Uncategorized'
        
        req = ['transaction_id', 'date', 'name', 'amount', 'category', 'bucket', 'source']
        for c in req: 
            if c not in df.columns: df[c] = None
        return df[req].dropna(subset=['date'])
    except Exception as e:
        st.error(f"Error: {e}")
        st.stop()

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

# --- APP START ---
init_db() # Run DB check on startup

# TABS
tab1, tab2, tab3 = st.tabs(["📊 Dashboard", "⚡ Rules & Edits", "📂 Upload Data"])

# === TAB 1: DASHBOARD ===
with tab1:
    st.header("Weekly Envelope Status")
    
    current_month = datetime.now().strftime('%Y-%m')
    
    # Safe Query with Params
    with get_db_connection().connect() as conn:
        query = text("SELECT * FROM transactions WHERE date::text LIKE :month")
        df = pd.read_sql(query, conn, params={"month": f"{current_month}%"})
    
    if df.empty:
        st.info(f"No transactions found for {datetime.now().strftime('%B %Y')}. Upload data in Tab 3!")
    else:
        # Calculate Logic
        income = df[df['amount'] > 0]['amount'].sum()
        bills = df[(df['amount'] < 0) & (df['bucket'] == 'BILL')]['amount'].sum()
        spending = df[(df['amount'] < 0) & (df['bucket'] == 'SPEND')]['amount'].sum()
        
        # NOTE: You can make these dynamic inputs later
        ESTIMATED_INCOME = 4000 
        ESTIMATED_BILLS = 1500
        
        pool = ESTIMATED_INCOME - ESTIMATED_BILLS
        weeks_in_month = 4
        weekly_allowance = pool / weeks_in_month
        
        today = datetime.now()
        start_week = today - timedelta(days=today.weekday())
        week_spend = df[
            (pd.to_datetime(df['date']) >= start_week) & 
            (df['bucket'] == 'SPEND')
        ]['amount'].sum()
        
        # Metrics
        col1, col2, col3 = st.columns(3)
        col1.metric("Weekly Allowance", f"${weekly_allowance:,.0f}")
        col2.metric("Spent This Week", f"${abs(week_spend):,.2f}")
        col3.metric("Remaining", f"${(weekly_allowance - abs(week_spend)):,.2f}", 
                    delta_color="normal" if (weekly_allowance - abs(week_spend)) > 0 else "inverse")
        
        # Progress
        progress = min(abs(week_spend) / weekly_allowance, 1.0) if weekly_allowance > 0 else 0
        st.progress(progress)
        
        # Charts
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Spending by Category")
            spend_df = df[df['amount'] < 0].copy()
            if not spend_df.empty:
                spend_df['amount'] = spend_df['amount'].abs()
                fig = px.pie(spend_df, values='amount', names='category', hole=0.4)
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.caption("No spending yet.")
            
        with c2:
            st.subheader("Recent Activity")
            st.dataframe(df[['date', 'name', 'amount', 'category', 'bucket']].sort_values('date', ascending=False).head(10), hide_index=True)

# === TAB 2: RULES & EDITS ===
with tab2:
    st.header("⚡ Auto-Categorization Rules")
    
    col1, col2 = st.columns([2, 1])
    
    with col1:
        st.caption("Define rules (e.g., if Name contains 'Netflix', set to 'Subscriptions')")
        
        # Load rules safely
        try:
            rules_df = pd.read_sql("SELECT * FROM category_rules ORDER BY rule_id", get_db_connection())
        except:
            # Fallback if table is broken: Empty DF with correct columns
            rules_df = pd.DataFrame(columns=["rule_id", "keyword", "category", "bucket"])

        # EDITOR
        edited_rules = st.data_editor(
            rules_df,
            num_rows="dynamic",
            column_config={
                "rule_id": st.column_config.NumberColumn(disabled=True),
                "keyword": "If Name Contains...",
                "category": st.column_config.SelectboxColumn("Set Category", options=["Groceries", "Dining Out", "Rent", "Utilities", "Shopping", "Transport", "Income", "Subscriptions"]),
                "bucket": st.column_config.SelectboxColumn("Set Bucket", options=["SPEND", "BILL", "INCOME"])
            },
            hide_index=True,
            key="rules_editor"
        )

        if st.button("💾 Save Rules & Run Automation"):
            engine = get_db_connection()
            with engine.connect() as conn:
                # 1. Clear old rules (Simple override)
                conn.execute(text("TRUNCATE TABLE category_rules RESTART IDENTITY"))
                
                # 2. Insert new rules
                for _, row in edited_rules.iterrows():
                    # Check for empty rows
                    if row.get('keyword'): 
                        conn.execute(text("""
                            INSERT INTO category_rules (keyword, category, bucket)
                            VALUES (:kw, :cat, :bucket)
                        """), {"kw": row['keyword'], "cat": row['category'], "bucket": row['bucket']})
                conn.commit()
            
            # 3. Run Automation
            matches = run_auto_categorization()
            st.success(f"Rules saved! Automatically categorized {matches} transactions.")
            st.rerun()

    with col2:
        st.info("💡 Tip: Rules only apply to 'Uncategorized' items.")
        # BUTTON TO FIX YOUR BROKEN DB
        if st.button("⚠️ Fix Database Schema"):
            with get_db_connection().connect() as conn:
                conn.execute(text("DROP TABLE IF EXISTS category_rules"))
                conn.commit()
            st.warning("Rules table reset. Please reload.")
            st.rerun()

    st.divider()
    
    st.header("📝 Manual Transaction Editor")
    edit_df = pd.read_sql("SELECT * FROM transactions ORDER BY date DESC LIMIT 100", get_db_connection())
    
    edited_data = st.data_editor(
        edit_df,
        column_config={
            "category": st.column_config.SelectboxColumn("Category", options=["Groceries", "Dining Out", "Rent", "Utilities", "Shopping", "Transport", "Income", "Subscriptions", "Uncategorized"]),
            "bucket": st.column_config.SelectboxColumn("Bucket", options=["SPEND", "BILL", "INCOME"])
        },
        disabled=["transaction_id", "source", "date", "name", "amount"],
        hide_index=True,
        key="tx_editor"
    )
    
    if st.button("💾 Save Manual Edits"):
        engine = get_db_connection()
        with engine.connect() as conn:
            for index, row in edited_data.iterrows():
                conn.execute(text("""
                    UPDATE transactions 
                    SET category = :cat, bucket = :bucket
                    WHERE transaction_id = :tid
                """), {"cat": row['category'], "bucket": row['bucket'], "tid": row['transaction_id']})
                conn.commit()
        st.success("Manual edits saved!")
        st.rerun()

# === TAB 3: UPLOAD ===
with tab3:
    st.header("Upload New Data")
    bank_choice = st.selectbox("Select Bank", ["Chase", "Citi"])
    uploaded_file = st.file_uploader(f"Upload {bank_choice} CSV", type=['csv'])
    
    if uploaded_file:
        clean_df = clean_bank_csv(uploaded_file, bank_choice)
        st.dataframe(clean_df.head(), hide_index=True)
        if st.button("Confirm Upload", type="primary"):
            count = save_to_neon(clean_df)
            st.success(f"Added {count} rows!")
            st.rerun()
