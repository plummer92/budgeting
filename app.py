import streamlit as st
import pandas as pd
import os
import hashlib
import re
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from datetime import datetime, timedelta
import plotly.express as px
import pdfplumber # Make sure this is in requirements.txt!

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

# --- CSV PROCESSORS ---
def process_chase(df):
    if 'post date' in df.columns: df = df.rename(columns={'post date': 'date'})
    elif 'transaction date' in df.columns: df = df.rename(columns={'transaction date': 'date'})
    if 'description' in df.columns: df = df.rename(columns={'description': 'name'})
    elif 'merchant' in df.columns: df = df.rename(columns={'merchant': 'name'})
    df['source'] = 'Chase'
    return df

def process_citi(df):
    df = df.rename(columns={'date': 'date', 'description': 'name', 'merchant name': 'name'})
    if 'debit' not in df.columns: df['debit'] = 0
    if 'credit' not in df.columns: df['credit'] = 0
    if 'amount' not in df.columns: df['amount'] = df['credit'].fillna(0) - df['debit'].fillna(0)
    df['source'] = 'Citi'
    return df

def process_sofi(df):
    if 'payment date' in df.columns: df = df.rename(columns={'payment date': 'date'})
    elif 'posted date' in df.columns: df = df.rename(columns={'posted date': 'date'})
    df = df.rename(columns={'description': 'name'})
    df['source'] = 'Sofi'
    return df

def process_chime_pdf(uploaded_file):
    """
    Extracts transactions from Chime PDF Statements.
    Supports formats: "Sep 28" AND "09/28/2025"
    """
    transactions = []
    
    # Try to guess year from filename
    filename = uploaded_file.name
    year_match = re.search(r'20\d{2}', filename)
    default_year = year_match.group(0) if year_match else str(datetime.now().year)

    with pdfplumber.open(uploaded_file) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    # Clean the row
                    clean_row = [str(x).strip() if x else '' for x in row]
                    
                    # Need at least Date and Amount
                    if len(clean_row) < 2: continue
                    
                    # 1. DETECT DATE (Col 0)
                    date_str = clean_row[0]
                    
                    # Regex for "Sep 28" OR "09/30/2025"
                    # Matches: (Mon DD) OR (M/D/YY) OR (M/D/YYYY)
                    date_match = re.match(r'([A-Z][a-z]{2}\s\d{1,2})|(\d{1,2}/\d{1,2}/\d{2,4})', date_str)
                    
                    if not date_match:
                        continue
                        
                    # 2. FIND DESCRIPTION
                    # Sometimes Chime has an empty column between Date and Desc
                    # We look for the first non-empty text after column 0
                    description = "Unknown"
                    for col_idx in range(1, len(clean_row)):
                        if clean_row[col_idx] and '$' not in clean_row[col_idx]:
                            description = clean_row[col_idx]
                            break
                    
                    # 3. FIND AMOUNT
                    # Search backwards from the end for something with a '.' or '$'
                    amount = 0.0
                    for col_in_reverse in reversed(clean_row):
                        if ('$' in col_in_reverse or '.' in col_in_reverse) and len(col_in_reverse) < 20:
                            try:
                                clean_amt = col_in_reverse.replace('$', '').replace(',', '').replace(' ', '')
                                if '(' in clean_amt:
                                    amount = -float(clean_amt.replace('(', '').replace(')', ''))
                                else:
                                    amount = float(clean_amt)
                                break # Found it
                            except:
                                continue

                    # 4. APPEND
                    # If date is like "9/30/2025", use it directly. 
                    # If "Sep 30", add the year.
                    final_date = date_str
                    if '/' not in date_str:
                        final_date = f"{date_str}, {default_year}"

                    transactions.append({
                        'date': final_date,
                        'name': description,
                        'amount': amount,
                        'source': 'Chime PDF'
                    })
    
    # If parsing failed completely, return empty with columns to prevent crash
    if not transactions:
        return pd.DataFrame(columns=['date', 'name', 'amount', 'source'])
                        
    return pd.DataFrame(transactions)

# --- PDF PROCESSOR (NEW!) ---
def process_chime_pdf(uploaded_file):
    """
    Extracts transactions from Chime PDF Statements.
    Assumes standard Chime format: Date | Description | ... | Amount
    """
    transactions = []
    
    # Try to guess year from filename (e.g., "September-2025")
    filename = uploaded_file.name
    year_match = re.search(r'20\d{2}', filename)
    default_year = year_match.group(0) if year_match else str(datetime.now().year)

    with pdfplumber.open(uploaded_file) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    # Chime rows usually start with a date like "Sep 28"
                    # We clean the row to remove None/Empty
                    clean_row = [str(x).strip() if x else '' for x in row]
                    
                    if len(clean_row) < 3: continue
                    
                    # 1. Detect Date (Col 0)
                    date_str = clean_row[0]
                    # Regex for "Mon DD" (e.g. Sep 28)
                    if not re.match(r'[A-Z][a-z]{2}\s\d{1,2}', date_str):
                        continue
                        
                    # 2. Detect Amount (Usually last column with $)
                    amount_str = clean_row[-1]
                    if '$' not in amount_str and '.' not in amount_str:
                        # Sometimes amount is 2nd to last if there is a Balance column
                        amount_str = clean_row[-2]

                    try:
                        # Clean currency string
                        clean_amount = amount_str.replace('$', '').replace(',', '').replace(' ', '')
                        
                        # Handle Negatives: Chime sometimes uses ($10.00) or -10.00
                        if '(' in clean_amount:
                            amount = -float(clean_amount.replace('(', '').replace(')', ''))
                        else:
                            amount = float(clean_amount)
                        
                        # Chime logic: Purchases are usually listed as positive numbers in "Spending" sections
                        # or negative in general ledgers. 
                        # SAFETY: If Description contains "Payment" or "Deposit", it's income (+).
                        # If it's a merchant, it's spend (-).
                        # Let's trust the sign for now, but user might need to flip it in review.
                        
                        transactions.append({
                            'date': f"{date_str}, {default_year}", # Add year to string
                            'name': clean_row[1], # Description is usually col 2
                            'amount': amount,
                            'source': 'Chime PDF'
                        })
                    except:
                        continue
                        
    return pd.DataFrame(transactions)

def clean_bank_file(uploaded_file, bank_choice):
    try:
        # 1. HANDLE CSV
        if uploaded_file.name.endswith('.csv'):
            df = pd.read_csv(uploaded_file, encoding='utf-8-sig')
            df.columns = df.columns.str.strip().str.lower().str.replace('\ufeff', '')
            
            if bank_choice == "Chase": df = process_chase(df)
            elif bank_choice == "Citi": df = process_citi(df)
            elif bank_choice == "Sofi": df = process_sofi(df)
            elif bank_choice == "Chime": df = process_chime_csv(df)
            
        # 2. HANDLE PDF
        elif uploaded_file.name.endswith('.pdf'):
            if bank_choice == "Chime":
                df = process_chime_pdf(uploaded_file)
            else:
                st.error("PDF support is currently only for Chime.")
                st.stop()
        else:
            st.error("Unsupported file type.")
            st.stop()

        # CLEANUP & STANDARDIZE
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
        st.error(f"Error processing file: {e}")
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
    col_date, col_title = st.columns([1, 3])
    with col_date:
        view_date = st.date_input("📅 View Week Containing:", value=datetime.now())
    
    start_of_week = view_date - timedelta(days=view_date.weekday())
    end_of_week = start_of_week + timedelta(days=6)
    
    with col_title:
        date_label = f"{start_of_week.strftime('%b %d')} - {end_of_week.strftime('%b %d')}"
        st.title(f"Weekly Status: {date_label}")
    
    with get_db_connection().connect() as conn:
        df = pd.read_sql("SELECT * FROM transactions", conn)
        
    if df.empty:
        st.info("No data found. Go to Tab 3 to upload.")
    else:
        df['date'] = pd.to_datetime(df['date'])
        week_df = df[(df['date'] >= pd.Timestamp(start_of_week)) & (df['date'] <= pd.Timestamp(end_of_week))].copy()
        
        week_spend = week_df[(week_df['bucket'] == 'SPEND')]['amount'].sum()
        weekly_allowance = (4000 - 1500) / 4 
        
        col1, col2, col3 = st.columns(3)
        col1.metric("Weekly Allowance", f"${weekly_allowance:,.0f}")
        col2.metric("Spent This Week", f"${abs(week_spend):,.2f}")
        col3.metric("Remaining", f"${(weekly_allowance - abs(week_spend)):,.2f}")
        
        st.progress(min(abs(week_spend) / weekly_allowance, 1.0) if weekly_allowance > 0 else 0)
        
        st.divider()
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Spending Breakdown")
            spend_df = week_df[(week_df['amount'] < 0) & (week_df['bucket'] == 'SPEND')].copy()
            if not spend_df.empty:
                spend_df['amount'] = spend_df['amount'].abs()
                st.plotly_chart(px.pie(spend_df, values='amount', names='category', hole=0.4), use_container_width=True)
        with c2:
            st.subheader("Transaction Log")
            st.dataframe(week_df[['date', 'name', 'amount', 'category']].sort_values('date', ascending=False), hide_index=True, use_container_width=True)

# === TAB 2: RULES & EDITS ===
with tab2:
    st.header("⚡ Auto-Categorization Rules")
    
    CAT_OPTIONS = ["Groceries", "Dining Out", "Rent", "Utilities", "Shopping", "Transport", "Income", "Subscriptions", "Credit Card Pay", "Home Improvement", "Pets", "RX"]
    
    with st.form("add_rule_form"):
        c1, c2, c3 = st.columns([2, 1, 1])
        with c1: new_keyword = st.text_input("If Name Contains...", placeholder="e.g. Chewy")
        with c2: new_cat = st.selectbox("Category", options=CAT_OPTIONS, index=10)
        with c3: new_bucket = st.selectbox("Bucket", options=["SPEND", "BILL", "INCOME", "TRANSFER"])
            
        if st.form_submit_button("➕ Add Rule") and new_keyword:
            with get_db_connection().connect() as conn:
                conn.execute(text("INSERT INTO category_rules (keyword, category, bucket) VALUES (:kw, :cat, :bucket)"), 
                             {"kw": new_keyword, "cat": new_cat, "bucket": new_bucket})
                conn.commit()
            st.success(f"Rule added for '{new_keyword}'!")
            run_auto_categorization()
            st.rerun()

    st.divider()

    st.subheader("📝 Action Items (Uncategorized)")
    todo_df = pd.read_sql("SELECT * FROM transactions WHERE category = 'Uncategorized' ORDER BY date DESC", get_db_connection())
    
    if todo_df.empty:
        st.success("🎉 All clear!")
    else:
        edited_todo = st.data_editor(todo_df, column_config={"category": st.column_config.SelectboxColumn("Category", options=CAT_OPTIONS), "bucket": st.column_config.SelectboxColumn("Bucket", options=["SPEND", "BILL", "INCOME", "TRANSFER"])}, disabled=["transaction_id", "source", "date", "name", "amount"], hide_index=True, key="todo_editor")
        if st.button("💾 Save Changes"):
            with get_db_connection().connect() as conn:
                for index, row in edited_todo.iterrows():
                    if row['category'] != 'Uncategorized':
                        conn.execute(text("UPDATE transactions SET category = :cat, bucket = :bucket WHERE transaction_id = :tid"), {"cat": row['category'], "bucket": row['bucket'], "tid": row['transaction_id']})
                        conn.commit()
            st.rerun()

    st.divider()

    with st.expander("✅ History (Click to Edit)"):
        done_df = pd.read_sql("SELECT * FROM transactions WHERE category != 'Uncategorized' ORDER BY date DESC LIMIT 100", get_db_connection())
        edited_done = st.data_editor(done_df, column_config={"category": st.column_config.SelectboxColumn("Category", options=CAT_OPTIONS), "bucket": st.column_config.SelectboxColumn("Bucket", options=["SPEND", "BILL", "INCOME", "TRANSFER"])}, disabled=["transaction_id", "source", "date", "name", "amount"], hide_index=True, key="done_editor")
        if st.button("💾 Update History"):
            with get_db_connection().connect() as conn:
                for index, row in edited_done.iterrows():
                    conn.execute(text("UPDATE transactions SET category = :cat, bucket = :bucket WHERE transaction_id = :tid"), {"cat": row['category'], "bucket": row['bucket'], "tid": row['transaction_id']})
                    conn.commit()
            st.success("Updated!")
            st.rerun()

# === TAB 3: UPLOAD & SETTINGS ===
with tab3:
    st.header("Upload Data")
    bank_choice = st.selectbox("Select Bank", ["Chase", "Citi", "Sofi", "Chime"])
    # ALLOW CSV AND PDF
    uploaded_file = st.file_uploader("Upload CSV or PDF", type=['csv', 'pdf'])
    
    if uploaded_file:
        clean_df = clean_bank_file(uploaded_file, bank_choice)
        st.dataframe(clean_df.head(), hide_index=True)
        if st.button("Confirm Upload", type="primary"):
            count = save_to_neon(clean_df)
            st.success(f"Added {count} rows!")
            st.rerun()

    st.divider()
    
    st.subheader("⚠️ Prune Old Data")
    col_a, col_b = st.columns([2, 1])
    with col_a:
        cutoff_date = st.date_input("Delete BEFORE:", value=pd.to_datetime("2025-10-01"))
    with col_b:
        st.write("") 
        st.write("") 
        if st.button("🗑️ Delete Old Data", type="primary"):
            with get_db_connection().connect() as conn:
                result = conn.execute(text("DELETE FROM transactions WHERE date < :cutoff"), {"cutoff": cutoff_date})
                conn.commit()
                deleted_rows = result.rowcount
            st.success(f"Deleted {deleted_rows} rows.")
            st.rerun()
