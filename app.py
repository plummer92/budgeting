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

# --- DB INIT (Now with Settings Table) ---
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
        # NEW: Table to store your Budget Inputs (Income/Bills)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS budget_settings (
                key_name TEXT PRIMARY KEY,
                value NUMERIC
            );
        """))
        conn.commit()

# --- HELPERS ---
def get_budget_setting(key, default_val):
    """Fetches a setting like 'est_income' from DB"""
    with get_db_connection().connect() as conn:
        result = conn.execute(text("SELECT value FROM budget_settings WHERE key_name = :k"), {"k": key}).fetchone()
        if result:
            return float(result[0])
        return float(default_val)

def set_budget_setting(key, value):
    """Saves a setting to DB"""
    with get_db_connection().connect() as conn:
        conn.execute(text("""
            INSERT INTO budget_settings (key_name, value) VALUES (:k, :v)
            ON CONFLICT (key_name) DO UPDATE SET value = :v
        """), {"k": key, "v": value})
        conn.commit()

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

# --- FILE PROCESSORS ---
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

def process_chime_csv(df):
    df = df.rename(columns={'transaction date': 'date', 'description': 'name'})
    df['source'] = 'Chime'
    return df

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
                                transactions.append({
                                    'date': date_part,
                                    'name': " ".join(description_parts),
                                    'amount': amount,
                                    'source': 'Chime PDF'
                                })
    except Exception as e: st.warning(f"PDF Error: {e}")
    if not transactions: return pd.DataFrame(columns=['date', 'name', 'amount', 'source'])
    return pd.DataFrame(transactions)

def clean_bank_file(uploaded_file, bank_choice):
    try:
        if uploaded_file.name.endswith('.csv'):
            df = pd.read_csv(uploaded_file, encoding='utf-8-sig')
            df.columns = df.columns.str.strip().str.lower().str.replace('\ufeff', '')
            if bank_choice == "Chase": df = process_chase(df)
            elif bank_choice == "Citi": df = process_citi(df)
            elif bank_choice == "Sofi": df = process_sofi(df)
            elif bank_choice == "Chime": df = process_chime_csv(df)
        elif uploaded_file.name.endswith('.pdf') and bank_choice == "Chime":
            df = process_chime_pdf(uploaded_file)
        else:
            st.error("Unsupported file."); st.stop()

        if 'amount' not in df.columns: df['amount'] = 0.0
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
tab1, tab_month, tab2, tab3 = st.tabs(["📊 Weekly Dashboard", "📅 Monthly Overview", "⚡ Rules", "📂 Upload"])

# === TAB 1: WEEKLY DASHBOARD (With Calc Explorer) ===
with tab1:
    col_date, col_set = st.columns([2, 1])
    with col_date:
        view_date = st.date_input("📅 View Week Containing:", value=datetime.now())
        start_of_week = view_date - timedelta(days=view_date.weekday())
        end_of_week = start_of_week + timedelta(days=6)
        st.caption(f"Showing: {start_of_week.strftime('%b %d')} - {end_of_week.strftime('%b %d')}")

    # --- CALCULATION EXPLORER ---
    with col_set:
        with st.expander("⚙️ Budget Settings"):
            # Load from DB
            est_income = get_budget_setting("est_income", 4000.0)
            est_bills = get_budget_setting("est_bills", 1500.0)
            
            new_income = st.number_input("Est. Monthly Income", value=est_income)
            new_bills = st.number_input("Est. Monthly Fixed Bills", value=est_bills)
            
            if st.button("Save Settings"):
                set_budget_setting("est_income", new_income)
                set_budget_setting("est_bills", new_bills)
                st.success("Saved!")
                st.rerun()

    with get_db_connection().connect() as conn:
        df = pd.read_sql("SELECT * FROM transactions", conn)
        
    if df.empty:
        st.info("No data.")
    else:
        df['date'] = pd.to_datetime(df['date'])
        week_df = df[(df['date'] >= pd.Timestamp(start_of_week)) & (df['date'] <= pd.Timestamp(end_of_week))].copy()
        week_spend = week_df[(week_df['bucket'] == 'SPEND')]['amount'].sum()
        
        # --- THE MATH ---
        discretionary_pool = new_income - new_bills
        weekly_allowance = discretionary_pool / 4
        remaining = weekly_allowance - abs(week_spend)
        
        # DISPLAY METRICS
        c1, c2, c3 = st.columns(3)
        c1.metric("Weekly Allowance", f"${weekly_allowance:,.0f}")
        c2.metric("Spent This Week", f"${abs(week_spend):,.2f}")
        c3.metric("Remaining", f"${remaining:,.2f}", delta_color="normal" if remaining > 0 else "inverse")
        
        st.progress(min(abs(week_spend) / weekly_allowance, 1.0) if weekly_allowance > 0 else 0)
        
        # --- THE EXPLORER ---
        with st.expander("🧮 Calculation Explorer (How we got these numbers)"):
            st.markdown(f"""
            **1. The Monthly Pool**
            We start with your Income (**${new_income:,.0f}**) and subtract your Fixed Bills (**${new_bills:,.0f}**).
            > Result: **${discretionary_pool:,.0f}** available for "Fun/Spending" this month.
            
            **2. The Weekly Slice**
            We divide that pool by **4 weeks** to be safe.
            > ${discretionary_pool:,.0f} ÷ 4 = **${weekly_allowance:,.0f} per week**.
            
            **3. The Reality Check**
            You have spent **${abs(week_spend):,.2f}** so far this week.
            > ${weekly_allowance:,.0f} - ${abs(week_spend):,.2f} = **${remaining:,.2f} left**.
            """)

        st.divider()
        # Charts
        c1, c2 = st.columns(2)
        with c1:
            spend_df = week_df[(week_df['amount'] < 0) & (week_df['bucket'] == 'SPEND')].copy()
            if not spend_df.empty:
                spend_df['amount'] = spend_df['amount'].abs()
                st.plotly_chart(px.pie(spend_df, values='amount', names='category', hole=0.4, title="This Week's Spending"), use_container_width=True)
            else: st.info("No spending this week.")
        with c2:
            st.dataframe(week_df[['date', 'name', 'amount', 'category']].sort_values('date', ascending=False), hide_index=True, use_container_width=True)

# === TAB 2: MONTHLY OVERVIEW (NEW!) ===
with tab_month:
    st.header("📅 Monthly Overview")
    
    # 1. Select Month
    sel_date = st.date_input("Select Month:", value=datetime.now(), key="month_picker")
    start_month = sel_date.replace(day=1)
    # Logic to get end of month
    next_month = (start_month.replace(day=28) + timedelta(days=4)).replace(day=1)
    end_month = next_month - timedelta(days=1)
    
    st.caption(f"Analyzing: {start_month.strftime('%B 1')} - {end_month.strftime('%B %d, %Y')}")
    
    if not df.empty:
        # Filter for Month
        month_df = df[(df['date'] >= pd.Timestamp(start_month)) & (df['date'] <= pd.Timestamp(end_month))].copy()
        
        if month_df.empty:
            st.info("No transactions found for this month.")
        else:
            # 2. High Level Stats
            # Income = Positive amounts in INCOME bucket
            total_income = month_df[(month_df['amount'] > 0) & (month_df['bucket'] == 'INCOME')]['amount'].sum()
            
            # Bills = Negative amounts in BILL bucket
            total_bills = month_df[(month_df['amount'] < 0) & (month_df['bucket'] == 'BILL')]['amount'].sum()
            
            # Spend = Negative amounts in SPEND bucket
            total_spend = month_df[(month_df['amount'] < 0) & (month_df['bucket'] == 'SPEND')]['amount'].sum()
            
            # Savings Transfers (Optional view)
            total_savings = month_df[(month_df['category'] == 'Savings')]['amount'].sum()

            # Net
            net_result = total_income + total_bills + total_spend # Bills/Spend are negative
            
            # Metrics
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Total Income", f"${total_income:,.2f}")
            m2.metric("Fixed Bills", f"${abs(total_bills):,.2f}")
            m3.metric("Discretionary Spend", f"${abs(total_spend):,.2f}")
            m4.metric("Net Result (Saved)", f"${net_result:,.2f}", delta_color="normal" if net_result > 0 else "inverse")
            
            st.divider()
            
            # 3. Monthly Charts
            mc1, mc2 = st.columns(2)
            with mc1:
                # Bar Chart: Spending by Category
                # Get all outflows (Spend + Bills)
                outflows = month_df[month_df['amount'] < 0].copy()
                outflows['amount'] = outflows['amount'].abs()
                if not outflows.empty:
                    fig = px.bar(outflows, x='category', y='amount', color='bucket', title="Where did the money go?")
                    st.plotly_chart(fig, use_container_width=True)
            
            with mc2:
                # Pie Chart: Spending Bucket Ratios
                if not outflows.empty:
                    fig2 = px.pie(outflows, values='amount', names='bucket', title="Bills vs. Spending")
                    st.plotly_chart(fig2, use_container_width=True)


# === TAB 3: RULES ===
with tab2:
    st.header("⚡ Rules & Edits")
    CAT_OPTIONS = ["Groceries", "Dining Out", "Rent", "Utilities", "Shopping", "Transport", "Income", "Subscriptions", "Credit Card Pay", "Home Improvement", "Pets", "RX", "Savings", "Gambling"]
    
    with st.form("add_rule"):
        c1, c2, c3 = st.columns([2, 1, 1])
        with c1: nk = st.text_input("Keyword", placeholder="e.g. Walmart")
        with c2: nc = st.selectbox("Cat", CAT_OPTIONS)
        with c3: nb = st.selectbox("Bkt", ["SPEND", "BILL", "INCOME", "TRANSFER"])
        if st.form_submit_button("➕ Add") and nk:
            with get_db_connection().connect() as conn:
                conn.execute(text("INSERT INTO category_rules (keyword, category, bucket) VALUES (:k,:c,:b)"), {"k":nk,"c":nc,"b":nb}); conn.commit()
            run_auto_categorization(); st.rerun()
            
    st.subheader("Uncategorized"); todo = pd.read_sql("SELECT * FROM transactions WHERE category='Uncategorized'", get_db_connection())
    if not todo.empty:
        ed = st.data_editor(todo, column_config={"category":st.column_config.SelectboxColumn(options=CAT_OPTIONS),"bucket":st.column_config.SelectboxColumn(options=["SPEND","BILL","INCOME","TRANSFER"])}, hide_index=True)
        if st.button("Save Todo"):
            with get_db_connection().connect() as conn:
                for i,r in ed.iterrows(): 
                    if r['category']!='Uncategorized': conn.execute(text("UPDATE transactions SET category=:c, bucket=:b WHERE transaction_id=:i"),{"c":r['category'],"b":r['bucket'],"i":r['transaction_id']}); conn.commit()
            st.rerun()

# === TAB 4: UPLOAD ===
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
