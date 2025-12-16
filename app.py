import streamlit as st
import pandas as pd
import os
import hashlib
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

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
                transaction_id TEXT PRIMARY KEY, 
                date DATE, 
                name TEXT, 
                merchant_name TEXT, 
                amount NUMERIC, 
                category TEXT, 
                bucket TEXT, 
                pending BOOLEAN,
                manual_category TEXT, 
                manual_bucket TEXT,
                source TEXT
            );
        """))
        conn.commit()

# --- BANK PROCESSORS ---
def process_chase(df):
    """Handles Chase CSV format"""
    df.columns = df.columns.str.strip().str.lower()
    
    # UPDATED MAPPING based on your error message
    col_map = {
        'post date': 'date',          # Found in your file
        'transaction date': 'date',   # Found in your file
        'posting date': 'date',       # Older Chase format
        'name': 'name',               # Found in your file
        'description': 'name',        # Older Chase format
        'amount': 'amount'
    }
    df = df.rename(columns=col_map)
    
    # Validation
    if 'date' not in df.columns or 'amount' not in df.columns:
        st.error(f"❌ Chase Error: Missing columns. Found: {list(df.columns)}")
        st.stop()
        
    df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
    df['source'] = 'Chase'
    return df

def process_citi(df):
    """Handles Citi CSV format"""
    df.columns = df.columns.str.strip().str.lower()
    
    col_map = {
        'date': 'date',
        'description': 'name',
        'merchant name': 'name'
    }
    df = df.rename(columns=col_map)
    
    # Ensure Debit/Credit exist
    if 'debit' not in df.columns: df['debit'] = 0
    if 'credit' not in df.columns: df['credit'] = 0
    
    # Calculate Amount
    if 'amount' not in df.columns:
        df['amount'] = df['credit'].fillna(0) - df['debit'].fillna(0)
    
    df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
    df['source'] = 'Citi'
    return df

# --- MAIN CLEANER ---
def clean_bank_csv(uploaded_file, bank_choice):
    try:
        df = pd.read_csv(uploaded_file)
        
        # Route to processor
        if bank_choice == "Chase":
            df = process_chase(df)
        elif bank_choice == "Citi":
            df = process_citi(df)
            
        # Cleanup Amount
        if df['amount'].dtype == 'object':
            df['amount'] = df['amount'].astype(str).str.replace('$', '').str.replace(',', '')
            df['amount'] = pd.to_numeric(df['amount'])
            
        # Generate ID
        def generate_id(row):
            d = str(row.get('date', ''))
            n = str(row.get('name', ''))
            a = str(row.get('amount', ''))
            return hashlib.md5(f"{d}{n}{a}".encode()).hexdigest()

        df['transaction_id'] = df.apply(generate_id, axis=1)
        df['bucket'] = 'SPEND'
        df['category'] = 'Uncategorized'
        
        required_cols = ['transaction_id', 'date', 'name', 'amount', 'category', 'bucket', 'source']
        for col in required_cols:
            if col not in df.columns: df[col] = None 
            
        return df[required_cols].dropna(subset=['date'])
        
    except Exception as e:
        st.error(f"Error processing {bank_choice}: {e}")
        st.stop()

# --- SAVE TO DB ---
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
            except Exception:
                pass
        conn.commit()
    return count

# --- APP LAYOUT ---
st.set_page_config(page_title="Budget Upload", layout="wide")
init_db()

st.title("📂 Bank Upload Dashboard")

with st.sidebar:
    st.header("1. Select Bank")
    bank_choice = st.selectbox("Choose Bank Format", ["Chase", "Citi"])
    
    st.header("2. Upload File")
    uploaded_file = st.file_uploader(f"Upload {bank_choice} CSV", type=['csv'])
    
    if uploaded_file:
        st.info(f"Processing as {bank_choice}...")
        clean_df = clean_bank_csv(uploaded_file, bank_choice)
        st.dataframe(clean_df.head(3), hide_index=True)
        
        if st.button("Confirm Upload", type="primary"):
            count = save_to_neon(clean_df)
            st.success(f"Success! Added {count} rows.")
            st.rerun()

    st.divider()
    if st.button("⚠️ Delete All Transactions (Reset)"):
        with get_db_connection().connect() as conn:
            conn.execute(text("DELETE FROM transactions"))
            conn.commit()
        st.warning("Database wiped clean.")
        st.rerun()

try:
    df = pd.read_sql("SELECT * FROM transactions ORDER BY date DESC LIMIT 50", get_db_connection())
    if not df.empty:
        st.subheader("Latest Transactions")
        st.dataframe(df, use_container_width=True)
except Exception as e:
    st.error(f"Error loading view: {e}")
