import streamlit as st
import pandas as pd
import psycopg2
import os
import hashlib
from dotenv import load_dotenv

load_dotenv()

# --- DATABASE CONNECTION ---
def get_db_connection():
    # Check Streamlit Secrets first (Cloud), then local .env
    db_url = os.getenv("DATABASE_URL")
    if not db_url and "DATABASE_URL" in st.secrets:
        db_url = st.secrets["DATABASE_URL"]
    
    if not db_url:
        st.error("❌ Database URL not found! Did you set the secrets in Streamlit Cloud?")
        st.stop()
        
    return psycopg2.connect(db_url)

# --- 0. INITIALIZE DB (Fixes the "Relation does not exist" error) ---
def init_db():
    """Creates the table if it doesn't exist yet."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
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
                manual_bucket TEXT
            );
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        st.error(f"Database Initialization Error: {e}")
# --- 1. THE SMARTER NORMALIZER ---
def clean_bank_csv(uploaded_file):
    df = pd.read_csv(uploaded_file)
    
    # 1. CLEAN HEADERS: lowercase, strip spaces
    df.columns = df.columns.str.strip().str.lower()
    
    # DEBUG: Show the user what columns were actually found
    st.write("🔍 Found these columns in your CSV:", df.columns.tolist())
    
    # 2. MAP COLUMNS: Add every variation you can think of here!
    column_map = {
        # DATE variations
        'posting date': 'date',       # Chase
        'post date': 'date',
        'trans. date': 'date',
        'transaction date': 'date',
        'effective date': 'date',
        'date': 'date',               # Citi / Wells
        
        # NAME variations
        'description': 'name',        # Chase / Citi
        'merchant name': 'name', 
        'original description': 'name',
        'transaction description': 'name',
        
        # AMOUNT variations
        'amount': 'amount',
        'credit': 'credit',
        'debit': 'debit'
    }
    
    # Rename columns using the map
    df = df.rename(columns=column_map)
    
    # 3. Handle Citi-style "Debit" and "Credit" split
    if 'amount' not in df.columns:
        if 'debit' in df.columns and 'credit' in df.columns:
            df['amount'] = df['credit'].fillna(0) - df['debit'].fillna(0)
        elif 'debit' in df.columns:
             df['amount'] = df['debit'].fillna(0) * -1
             
    # 4. Crash prevention
    if 'amount' not in df.columns:
        st.error(f"❌ Could not find an Amount column! We found: {list(df.columns)}")
        st.stop()
    if 'date' not in df.columns:
        st.error(f"❌ Could not find a Date column! We found: {list(df.columns)}")
        st.stop()
    
    # 5. Cleanup Amount
    if df['amount'].dtype == 'object':
        df['amount'] = df['amount'].astype(str).str.replace('$', '').str.replace(',', '')
        df['amount'] = pd.to_numeric(df['amount'])
        
    # 6. Generate ID
    def generate_id(row):
        # Convert date to string to prevent "None" errors in hash
        d = str(row.get('date', ''))
        n = str(row.get('name', ''))
        a = str(row.get('amount', ''))
        raw = f"{d}{n}{a}"
        return hashlib.md5(raw.encode()).hexdigest()

    df['transaction_id'] = df.apply(generate_id, axis=1)
    df['bucket'] = 'SPEND'
    df['category'] = 'Uncategorized'
    
    # 7. Select Final Columns
    required_cols = ['transaction_id', 'date', 'name', 'amount', 'category', 'bucket']
    for col in required_cols:
        if col not in df.columns:
            df[col] = None 
            
    return df[required_cols]
        
    # 6. Generate ID
    def generate_id(row):
        raw = f"{row.get('date', '')}{row.get('name', '')}{row.get('amount', '')}"
        return hashlib.md5(raw.encode()).hexdigest()

    df['transaction_id'] = df.apply(generate_id, axis=1)
    df['bucket'] = 'SPEND'
    df['category'] = 'Uncategorized'
    
    # 7. Select Final Columns
    required_cols = ['transaction_id', 'date', 'name', 'amount', 'category', 'bucket']
    for col in required_cols:
        if col not in df.columns:
            df[col] = None 
            
    return df[required_cols]
        
    # Generate ID
    def generate_id(row):
        # Create unique ID based on row data
        raw = f"{row.get('date', '')}{row.get('name', '')}{row.get('amount', '')}"
        return hashlib.md5(raw.encode()).hexdigest()

    df['transaction_id'] = df.apply(generate_id, axis=1)
    df['bucket'] = 'SPEND'
    df['category'] = 'Uncategorized'
    
    # Select only columns we need (and handle missing ones gracefully)
    required_cols = ['transaction_id', 'date', 'name', 'amount', 'category', 'bucket']
    for col in required_cols:
        if col not in df.columns:
            df[col] = None # Fill missing with None
            
    return df[required_cols]

# --- 2. SAVE TO NEON ---
def save_to_neon(df):
    conn = get_db_connection()
    cur = conn.cursor()
    
    added_count = 0
    for _, row in df.iterrows():
        try:
            cur.execute("""
                INSERT INTO transactions (
                    transaction_id, date, name, merchant_name, 
                    amount, category, bucket, pending
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (transaction_id) DO NOTHING;
            """, (
                row['transaction_id'], row['date'], row['name'], row['name'],
                row['amount'], row['category'], row['bucket'], False
            ))
            if cur.rowcount > 0:
                added_count += 1
        except Exception as e:
            st.warning(f"Skipped row error: {e}")
            
    conn.commit()
    conn.close()
    return added_count

# --- MAIN APP LOGIC ---
st.set_page_config(page_title="Budget Upload", layout="wide")

# 1. RUN INIT (Crucial Step!)
init_db()

st.title("📂 Bank Upload Dashboard")

# 2. SIDEBAR UPLOAD
with st.sidebar:
    st.header("Upload Data")
    uploaded_file = st.file_uploader("Upload Bank CSV", type=['csv'])
    
    if uploaded_file is not None:
        try:
            st.info("Processing file...")
            clean_df = clean_bank_csv(uploaded_file)
            st.dataframe(clean_df.head(3), hide_index=True) 
            
            if st.button("Confirm Upload"):
                count = save_to_neon(clean_df)
                st.success(f"Success! Added {count} new transactions.")
                st.balloons()
                # Rerun to show new data immediately
                st.rerun()
                
        except Exception as e:
            st.error(f"Error reading CSV: {e}")

# 3. VIEW DATA (Safe Loading)
try:
    conn = get_db_connection()
    # Check if table has data
    df = pd.read_sql("SELECT * FROM transactions ORDER BY date DESC LIMIT 50", conn)
    
    if df.empty:
        st.info("No transactions found. Upload a CSV to get started!")
    else:
        st.subheader("Latest Transactions")
        st.dataframe(df)
        
    conn.close()
except Exception as e:
    st.error(f"Error loading data: {e}")
