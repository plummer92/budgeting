import streamlit as st
import pandas as pd
import psycopg2
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
import plotly.express as px

# Import your sync script so the button works
from main import sync

# Load environment variables
load_dotenv()

# --- CONFIGURATION ---
st.set_page_config(page_title="My Budget", page_icon="💰", layout="wide")

# --- DATABASE CONNECTION ---
def get_db_connection():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

# --- DATA LOADING ---
def load_data():
    conn = get_db_connection()
    
    # 1. Fetch Transactions
    df = pd.read_sql("""
        SELECT date, name, amount, category, bucket 
        FROM transactions 
        ORDER BY date DESC
    """, conn)
    
    conn.close()
    return df

# --- WEEKLY MATH LOGIC ---
def calculate_metrics(df):
    today = datetime.now().date()
    
    # 1. Identify Current Week
    start_of_week = today - timedelta(days=today.weekday()) # Monday
    
    # 2. Filter Data
    current_month_df = df[pd.to_datetime(df['date']).dt.month == today.month]
    current_week_df = df[pd.to_datetime(df['date']).dt.date >= start_of_week]
    
    # 3. Calculate Buckets
    total_income = current_month_df[current_month_df['amount'] < 0]['amount'].sum() * -1 # Assuming negative is income in Plaid logic, verify this!
    # Note: Usually Plaid: Positive = Spend, Negative = Income. 
    # Let's assume Positive = Spend for this visualization to be safe.
    
    bills_paid = df[(pd.to_datetime(df['date']).dt.month == today.month) & (df['bucket'] == 'BILL')]['amount'].sum()
    weekly_spent = current_week_df[current_week_df['bucket'] == 'SPEND']['amount'].sum()
    
    # 4. The "Envelope" Math
    # HARDCODED FOR DEMO (Replace with DB values later)
    EXPECTED_MONTHLY_INCOME = 5000 
    EXPECTED_BILLS = 2000
    WEEKS_REMAINING = 3
    
    # Adjusted Logic: (Income - Bills) / Weeks
    pool = EXPECTED_MONTHLY_INCOME - max(EXPECTED_BILLS, bills_paid)
    allowance = pool / 4 # simplified for demo
    
    remaining = allowance - weekly_spent
    
    return allowance, weekly_spent, remaining, bills_paid

# --- UI LAYOUT ---
st.title("💸 The Weekly Bucket")

# 1. Sidebar Control
with st.sidebar:
    st.header("Actions")
    if st.button("🔄 Sync with Bank"):
        with st.spinner("Syncing Plaid..."):
            try:
                sync() # Calls your main.py function
                st.success("Synced!")
                st.rerun()
            except Exception as e:
                st.error(f"Sync failed: {e}")
                
    st.divider()
    st.write("Debug Info:")
    st.caption(f"Database: Connected")
    st.caption(f"Env: {os.getenv('PLAID_ENV')}")

# 2. Load Data
df = load_data()
allowance, spent, remaining, bills_paid = calculate_metrics(df)

# 3. Top Metrics (The "Headlines")
col1, col2, col3 = st.columns(3)

with col1:
    st.metric(label="Weekly Remaining", value=f"${remaining:,.2f}", delta=f"${allowance:,.2f} Allowance")

with col2:
    st.metric(label="Weekly Spent", value=f"${spent:,.2f}", delta_color="inverse")

with col3:
    st.metric(label="Bills Paid (Month)", value=f"${bills_paid:,.2f}")

# 4. Progress Bar
st.subheader("Weekly Progress")
progress = min(spent / allowance, 1.0) if allowance > 0 else 0
bar_color = "green" if progress < 0.75 else "red"
st.progress(progress)
st.caption(f"You have spent {int(progress*100)}% of your weekly fund.")

# 5. Charts & Data
c1, c2 = st.columns([2, 1])

with c1:
    st.subheader("Spending Trend")
    # Simple Bar Chart of spending by Day
    daily_spend = df[df['bucket'] == 'SPEND'].groupby('date')['amount'].sum().reset_index()
    fig = px.bar(daily_spend, x='date', y='amount', title="Daily Spending")
    st.plotly_chart(fig, use_container_width=True)

with c2:
    st.subheader("Spending by Category")
    cat_spend = df[df['bucket'] == 'SPEND'].groupby('category')['amount'].sum().reset_index()
    fig2 = px.pie(cat_spend, values='amount', names='category', hole=0.4)
    st.plotly_chart(fig2, use_container_width=True)

# 6. Recent Transactions Table
st.subheader("Recent Transactions")
st.dataframe(
    df[['date', 'name', 'amount', 'category', 'bucket']].head(20),
    use_container_width=True
)
