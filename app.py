import streamlit as st
import pandas as pd
import psycopg2
import os
import hashlib
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# --- DATABASE CONNECTION ---
def get_db_connection():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

# --- 1. THE NORMALIZER (Cleans messy Bank CSVs) ---
def clean_bank_csv(uploaded_file):
    """
    Standardizes different bank formats into our database format.
    """
    df = pd.read_csv(uploaded_file)
    
    # A. Standardize Column Names (Map your bank's headers here)
    # Edit this dictionary to match YOUR specific bank's CSV headers
    column_map = {
        'Posting Date': 'date',       # Chase
        'Date': 'date',               # Wells Fargo/Citi
        'Description': 'name',        # Chase
        'Merchant Name': 'name',      # Others
        'Amount': 'amount',           # Standard
        'Type': 'type'                # Debit/Credit
    }
    df = df.rename(columns=column_map)
    
    # B. Handle "Debit" and "Credit" columns (some banks split them)
    if 'Debit' in df.columns and 'Credit' in df.columns:
        df['amount'] = df['Credit'].fillna(0) - df['Debit'].fillna(0)
    
    # C. Ensure Amount is Numeric
    # Removes '$' and ',' if present
    if df['amount'].dtype == 'object':
        df['amount'] = df['amount'].astype(str).str.replace('$', '').str.replace(',', '')
        df['amount'] = pd.to_numeric(df['amount'])

    # D. Invert Amount Logic (Optional)
    # Some banks make spending positive, some negative. 
    # WE WANT: Spending = Negative, Income = Positive.
    # If your bank shows spending as positive, uncomment the line below:
    # df['amount'] = df['amount'] * -1 

    # E. Generate Synthetic ID (Prevent Duplicates)
    # ID = MD5(date + name + amount)
    def generate_id(row):
        raw_str = f"{row['date']}{row['name']}{row['amount']}"
        return hashlib.md5(raw_str.encode()).hexdigest()

    df['transaction_id'] = df.apply(generate_id, axis=1)
    
    # F. Add Default Buckets
    df['bucket'] = 'SPEND'
    df['category'] = 'Uncategorized'
    
    return df[['transaction_id', 'date', 'name', 'amount', 'category', 'bucket']]

# --- 2. UPLOAD TO NEON ---
def save_to_neon(df):
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Create table if not exists (Just in case)
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
    
    added_count = 0
    
    for _, row in df.iterrows():
        # Using ON CONFLICT DO NOTHING so re-uploading the same CSV is safe
        cur.execute("""
            INSERT INTO transactions (
                transaction_id, date, name, merchant_name, 
                amount, category, bucket, pending
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (transaction_id) DO NOTHING;
        """, (
            row['transaction_id'], 
            row['date'], 
            row['name'], 
            row['name'], # Use name as merchant_name initially
            row['amount'], 
            row['category'], 
            row['bucket'], 
            False # CSVs are usually posted, not pending
        ))
        if cur.rowcount > 0:
            added_count += 1
            
    conn.commit()
    conn.close()
    return added_count

# --- 3. UI LAYOUT ---
st.title("📂 Bank Upload Dashboard")

with st.sidebar:
    st.header("Upload Data")
    uploaded_file = st.file_uploader("Upload Bank CSV", type=['csv'])
    
    if uploaded_file is not None:
        try:
            st.info("Processing file...")
            clean_df = clean_bank_csv(uploaded_file)
            st.dataframe(clean_df.head(3), hide_index=True) # Preview
            
            if st.button("Confirm Upload"):
                count = save_to_neon(clean_df)
                st.success(f"Success! Added {count} new transactions.")
                st.balloons()
                
        except Exception as e:
            st.error(f"Error reading CSV: {e}")

# --- VIEW DATA ---
conn = get_db_connection()
df = pd.read_sql("SELECT * FROM transactions ORDER BY date DESC LIMIT 50", conn)
st.subheader("Latest Transactions")
st.dataframe(df)
