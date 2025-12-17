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

# --- DATABASE ---
def get_db_connection():
    db_url = os.getenv("DATABASE_URL")
    if not db_url and "DATABASE_URL" in st.secrets:
        db_url = st.secrets["DATABASE_URL"]
    if not db_url:
        st.error("❌ Database URL not found!")
        st.stop()
    return create_engine(db_url.replace("postgres://", "postgresql://"))

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
        
        # 2. Rules Table (NEW)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS category_rules (
                rule_id SERIAL PRIMARY KEY,
                keyword TEXT NOT NULL,
                category TEXT NOT NULL,
                bucket TEXT NOT NULL
            );
        """))
        conn.commit()

# --- DATA PROCESSING (Your Working Code) ---
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

# --- TABS LOGIC ---

init_db()
tab1, tab2, tab3 = st.tabs(["📊 Dashboard", "📝 Edit Categories", "📂 Upload Data"])

# === TAB 1: DASHBOARD ===
with tab1:
    st.header("Weekly Envelope Status")
    
    # 1. Get Data for Current Month (FIXED)
    current_month = datetime.now().strftime('%Y-%m')
    
    # We use a context manager (with ... as conn) for safety
    with get_db_connection().connect() as conn:
        # We use text() and :month to safely handle the '%' wildcard
        query = text("SELECT * FROM transactions WHERE date::text LIKE :month")
        df = pd.read_sql(query, conn, params={"month": f"{current_month}%"})
    
    if df.empty:
        st.info("No data found for this month.")
    else:
        # ... (The rest of your code remains exactly the same)
        # 2. Calculate "The Envelope"
        income = df[df['amount'] > 0]['amount'].sum()
        # ...
        # 2. Calculate "The Envelope"
        # Logic: Income - Bills = Spending Money
        income = df[df['amount'] > 0]['amount'].sum()
        bills = df[(df['amount'] < 0) & (df['bucket'] == 'BILL')]['amount'].sum()
        spending = df[(df['amount'] < 0) & (df['bucket'] == 'SPEND')]['amount'].sum()
        
        # Hardcoded estimate for demo (You can make these inputs later)
        ESTIMATED_INCOME = 4000 
        ESTIMATED_BILLS = 1500
        
        # Math
        pool = ESTIMATED_INCOME - ESTIMATED_BILLS
        weeks_in_month = 4
        weekly_allowance = pool / weeks_in_month
        
        # Determine current week spending
        today = datetime.now()
        start_week = today - timedelta(days=today.weekday())
        week_spend = df[
            (pd.to_datetime(df['date']) >= start_week) & 
            (df['bucket'] == 'SPEND')
        ]['amount'].sum()
        
        # 3. Display Metrics
        col1, col2, col3 = st.columns(3)
        col1.metric("Weekly Allowance", f"${weekly_allowance:,.0f}")
        col2.metric("Spent This Week", f"${abs(week_spend):,.2f}")
        col3.metric("Remaining", f"${(weekly_allowance - abs(week_spend)):,.2f}", 
                    delta_color="normal" if (weekly_allowance - abs(week_spend)) > 0 else "inverse")
        
        # 4. Progress Bar
        progress = min(abs(week_spend) / weekly_allowance, 1.0) if weekly_allowance > 0 else 0
        st.progress(progress)
        st.caption(f"You have used {int(progress*100)}% of your weekly budget.")
        
        # 5. Charts
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Spending by Category")
            # Filter only negative amounts (spending)
            spend_df = df[df['amount'] < 0].copy()
            spend_df['amount'] = spend_df['amount'].abs()
            fig = px.pie(spend_df, values='amount', names='category', hole=0.4)
            st.plotly_chart(fig, use_container_width=True)
            
        with c2:
            st.subheader("Recent Activity")
            st.dataframe(df[['date', 'name', 'amount', 'category', 'bucket']].sort_values('date', ascending=False).head(10), hide_index=True)

# === TAB 2: EDIT CATEGORIES ===
with tab2:
    st.header("Transaction Manager")
    st.write("Edit categories here. Changes save automatically.")
    
    # Load all data
    edit_df = pd.read_sql("SELECT * FROM transactions ORDER BY date DESC LIMIT 100", get_db_connection())
    
    # Editable Grid
    edited_data = st.data_editor(
        edit_df,
        column_config={
            "category": st.column_config.SelectboxColumn(
                "Category",
                options=["Groceries", "Dining Out", "Rent", "Utilities", "Shopping", "Transport", "Income", "Uncategorized"],
                required=True,
            ),
            "bucket": st.column_config.SelectboxColumn(
                "Bucket",
                options=["SPEND", "BILL", "INCOME"],
                required=True,
            )
        },
        disabled=["transaction_id", "source", "date", "name", "amount"],
        hide_index=True,
        key="editor"
    )
    
    # Save Button logic
    if st.button("💾 Save Changes"):
        engine = get_db_connection()
        with engine.connect() as conn:
            # We iterate through the edited dataframe in session state
            # Note: For production, we usually check 'st.session_state["editor"]["edited_rows"]'
            # But creating a loop is safer for simple apps.
            for index, row in edited_data.iterrows():
                conn.execute(text("""
                    UPDATE transactions 
                    SET category = :cat, bucket = :bucket
                    WHERE transaction_id = :tid
                """), {"cat": row['category'], "bucket": row['bucket'], "tid": row['transaction_id']})
                conn.commit()
        st.success("Updates saved!")
        st.rerun()

# === TAB 3: UPLOAD (Kept Safe) ===
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
