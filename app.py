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
        conn.commit()

# --- HELPERS ---
def run_auto_categorization():
    engine = get_db_connection()
    count = 0
    with engine.connect() as conn:
        rules = pd.read_sql("SELECT * FROM category_rules", conn)
        if rules.empty: return 0
        for _, rule in rules.iterrows():
            keyword = f"%{rule['keyword']}%"
            result = conn.execute(text("""
                UPDATE transactions 
                SET category = :cat, bucket = :bucket
                WHERE name ILIKE :kw 
                AND category = 'Uncategorized'
            """), {"cat": rule['category'], "bucket": rule['bucket'], "kw": keyword})
            count += result.rowcount
        conn.commit()
    return count

def clean_bank_csv(uploaded_file, bank_choice):
    try:
        df = pd.read_csv(uploaded_file, encoding='utf-8-sig')
        df.columns = df.columns.str.strip().str.lower().str.replace('\ufeff', '')
        
        if bank_choice == "Chase":
            if 'post date' in df.columns: df = df.rename(columns={'post date': 'date'})
            elif 'transaction date' in df.columns: df = df.rename(columns={'transaction date': 'date'})
            if 'description' in df.columns: df = df.rename(columns={'description': 'name'})
            elif 'merchant' in df.columns: df = df.rename(columns={'merchant': 'name'})
            df['source'] = 'Chase'
            
        elif bank_choice == "Citi":
            df = df.rename(columns={'date': 'date', 'description': 'name', 'merchant name': 'name'})
            if 'debit' not in df.columns: df['debit'] = 0
            if 'credit' not in df.columns: df['credit'] = 0
            if 'amount' not in df.columns: df['amount'] = df['credit'].fillna(0) - df['debit'].fillna(0)
            df['source'] = 'Citi'

        if df['amount'].dtype == 'object':
            df['amount'] = df['amount'].astype(str).str.replace('$', '').str.replace(',', '')
            df['amount'] = pd.to_numeric(df['amount'])
            
        df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
        
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

# --- MAIN APP ---
init_db()
tab1, tab2, tab3 = st.tabs(["📊 Dashboard", "⚡ Rules & Edits", "📂 Upload Data"])

# === TAB 1: DASHBOARD ===
with tab1:
    today = datetime.now()
    start_of_week = today - timedelta(days=today.weekday())
    end_of_week = start_of_week + timedelta(days=6)
    
    date_label = f"{start_of_week.strftime('%b %d')} - {end_of_week.strftime('%b %d')}"
    st.header(f"Weekly Envelope Status ({date_label})")
    
    current_month = datetime.now().strftime('%Y-%m')
    with get_db_connection().connect() as conn:
        query = text("SELECT * FROM transactions WHERE date::text LIKE :month")
        df = pd.read_sql(query, conn, params={"month": f"{current_month}%"})
    
    if df.empty:
        st.info("No data for this month.")
    else:
        # Spending Logic (Ignore TRANSFERS)
        week_spend = df[
            (pd.to_datetime(df['date']) >= start_of_week) & 
            (pd.to_datetime(df['date']) <= end_of_week) &
            (df['bucket'] == 'SPEND')  # Only count SPEND bucket
        ]['amount'].sum()
        
        # Income Logic (Ignore Payments/Transfers)
        income = df[(df['amount'] > 0) & (df['bucket'] == 'INCOME')]['amount'].sum()
        
        # Hardcoded Budget
        weekly_allowance = (4000 - 1500) / 4 
        
        col1, col2, col3 = st.columns(3)
        col1.metric("Weekly Allowance", f"${weekly_allowance:,.0f}")
        col2.metric("Spent This Week", f"${abs(week_spend):,.2f}")
        col3.metric("Remaining", f"${(weekly_allowance - abs(week_spend)):,.2f}")
        st.progress(min(abs(week_spend) / weekly_allowance, 1.0) if weekly_allowance > 0 else 0)
        
        c1, c2 = st.columns(2)
        with c1:
            # Chart: Exclude Transfers
            spend_df = df[(df['amount'] < 0) & (df['bucket'] == 'SPEND')].copy()
            if not spend_df.empty:
                spend_df['amount'] = spend_df['amount'].abs()
                st.plotly_chart(px.pie(spend_df, values='amount', names='category', hole=0.4), use_container_width=True)
        with c2:
            st.dataframe(df[['date', 'name', 'amount', 'category']].sort_values('date', ascending=False).head(10), hide_index=True)

# === TAB 2: RULES & EDITS ===
with tab2:
    st.header("⚡ Auto-Categorization Rules")
    
    try:
        unique_names = pd.read_sql("SELECT DISTINCT name FROM transactions ORDER BY name", get_db_connection())
        merchant_list = unique_names['name'].tolist()
    except: merchant_list = []
        
    with st.form("add_rule_form"):
        c1, c2, c3 = st.columns([2, 1, 1])
        with c1: new_keyword = st.selectbox("If Name Contains...", options=merchant_list, index=None, placeholder="Select merchant...")
        with c2: new_cat = st.selectbox("Set Category", options=["Groceries", "Dining Out", "Rent", "Utilities", "Shopping", "Transport", "Income", "Subscriptions", "Credit Card Pay"])
        with c3: new_bucket = st.selectbox("Set Bucket", options=["SPEND", "BILL", "INCOME", "TRANSFER"]) # <--- Added TRANSFER
            
        if st.form_submit_button("➕ Add Rule") and new_keyword:
            with get_db_connection().connect() as conn:
                conn.execute(text("INSERT INTO category_rules (keyword, category, bucket) VALUES (:kw, :cat, :bucket)"), 
                             {"kw": new_keyword, "cat": new_cat, "bucket": new_bucket})
                conn.commit()
            st.success(f"Rule added for '{new_keyword}'!")
            st.rerun()

    st.divider()

    st.subheader("📝 Action Items (Uncategorized)")
    todo_df = pd.read_sql("SELECT * FROM transactions WHERE category = 'Uncategorized' ORDER BY date DESC", get_db_connection())
    
    if todo_df.empty:
        st.success("🎉 You have no uncategorized transactions! Great job!")
    else:
        st.caption(f"You have {len(todo_df)} transactions to sort.")
        edited_todo = st.data_editor(
            todo_df,
            column_config={
                "category": st.column_config.SelectboxColumn("Category", options=["Groceries", "Dining Out", "Rent", "Utilities", "Shopping", "Transport", "Income", "Subscriptions", "Credit Card Pay"]),
                "bucket": st.column_config.SelectboxColumn("Bucket", options=["SPEND", "BILL", "INCOME", "TRANSFER"])
            },
            disabled=["transaction_id", "source", "date", "name", "amount"],
            hide_index=True,
            key="todo_editor"
        )
        
        if st.button("💾 Save & Clear Sorted Items"):
            with get_db_connection().connect() as conn:
                for index, row in edited_todo.iterrows():
                    if row['category'] != 'Uncategorized':
                        conn.execute(text("""
                            UPDATE transactions SET category = :cat, bucket = :bucket WHERE transaction_id = :tid
                        """), {"cat": row['category'], "bucket": row['bucket'], "tid": row['transaction_id']})
                        conn.commit()
            st.success("Saved! Items moved to history.")
            st.rerun()

    st.divider()

    with st.expander("✅ Categorized History (Click to View/Edit)"):
        done_df = pd.read_sql("SELECT * FROM transactions WHERE category != 'Uncategorized' ORDER BY date DESC LIMIT 50", get_db_connection())
        
        edited_done = st.data_editor(
            done_df,
            column_config={
                "category": st.column_config.SelectboxColumn("Category", options=["Groceries", "Dining Out", "Rent", "Utilities", "Shopping", "Transport", "Income", "Subscriptions", "Credit Card Pay"]),
                "bucket": st.column_config.SelectboxColumn("Bucket", options=["SPEND", "BILL", "INCOME", "TRANSFER"])
            },
            disabled=["transaction_id", "source", "date", "name", "amount"],
            hide_index=True,
            key="done_editor"
        )
        
        if st.button("💾 Update History"):
            with get_db_connection().connect() as conn:
                for index, row in edited_done.iterrows():
                    conn.execute(text("""
                        UPDATE transactions SET category = :cat, bucket = :bucket WHERE transaction_id = :tid
                    """), {"cat": row['category'], "bucket": row['bucket'], "tid": row['transaction_id']})
                    conn.commit()
            st.success("History updated!")
            st.rerun()

# === TAB 3: UPLOAD & SETTINGS ===
with tab3:
    st.header("Upload New Data")
    bank_choice = st.selectbox("Select Bank", ["Chase", "Citi"])
    uploaded_file = st.file_uploader("Upload CSV", type=['csv'])
    if uploaded_file:
        clean_df = clean_bank_csv(uploaded_file, bank_choice)
        st.dataframe(clean_df.head(), hide_index=True)
        if st.button("Confirm Upload", type="primary"):
            count = save_to_neon(clean_df)
            st.success(f"Added {count} rows!")
            st.rerun()

    st.divider()
    
    st.subheader("⚠️ Prune Old Data")
    col_a, col_b = st.columns([2, 1])
    with col_a:
        cutoff_date = st.date_input("Delete all transactions BEFORE:", value=pd.to_datetime("2025-10-01"))
    with col_b:
        st.write("") 
        st.write("") 
        if st.button("🗑️ Delete Old Data", type="primary"):
            with get_db_connection().connect() as conn:
                result = conn.execute(
                    text("DELETE FROM transactions WHERE date < :cutoff"), 
                    {"cutoff": cutoff_date}
                )
                conn.commit()
                deleted_rows = result.rowcount
            st.success(f"Cleaned up! Deleted {deleted_rows} old transactions.")
            st.rerun()
