import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from sqlalchemy import text
from utils import get_db_connection, get_budget_setting, set_budget_setting, init_db, show_sidebar_alerts

st.set_page_config(page_title="Weekly Dashboard", layout="wide", page_icon="📊")
init_db()
show_sidebar_alerts()

st.title("📊 Weekly Dashboard")

col_date, col_set = st.columns([2, 1])
with col_date:
    view_date = st.date_input("📅 View Week Containing:", value=datetime.now())
    start_of_week = view_date - timedelta(days=view_date.weekday())
    end_of_week   = start_of_week + timedelta(days=6)
    st.caption(f"Showing: {start_of_week.strftime('%b %d')} — {end_of_week.strftime('%b %d')}")

with col_set:
    with st.expander("⚙️ Settings"):
        est_income = get_budget_setting("est_income", 4000.0)
        est_bills  = get_budget_setting("est_bills",  1500.0)
        new_income = st.number_input("Monthly Income",    value=est_income)
        new_bills  = st.number_input("Monthly Fixed Bills", value=est_bills)
        if st.button("Save Settings"):
            set_budget_setting("est_income", new_income)
            set_budget_setting("est_bills",  new_bills)
            st.rerun()
        st.divider()
        if st.button("🗑️ Delete THIS WEEK's Transactions", type="secondary"):
            with get_db_connection().connect() as conn:
                conn.execute(text("DELETE FROM transactions WHERE date >= :s AND date <= :e"),
                             {"s": start_of_week, "e": end_of_week})
                conn.commit()
            st.rerun()

with get_db_connection().connect() as conn:
    df = pd.read_sql("SELECT * FROM transactions", conn)

if df.empty:
    st.info("No transaction data yet. Upload a bank file or connect a bank to get started.")
    st.stop()

df['date']  = pd.to_datetime(df['date'])
week_df     = df[(df['date'] >= pd.Timestamp(start_of_week)) & (df['date'] <= pd.Timestamp(end_of_week))].copy()
spend_df    = week_df[week_df['bucket'] == 'SPEND'].copy()
week_spend  = spend_df['amount'].sum()

weekly_allowance = (new_income - new_bills) / 4
remaining        = weekly_allowance - abs(week_spend)

c1, c2, c3 = st.columns(3)
c1.metric("Weekly Allowance",  f"${weekly_allowance:,.0f}")
c2.metric("Spent This Week",   f"${abs(week_spend):,.2f}")
c3.metric("Remaining",         f"${remaining:,.2f}",
          delta_color="normal" if remaining >= 0 else "inverse")

st.progress(min(abs(week_spend) / weekly_allowance, 1.0) if weekly_allowance > 0 else 0)

st.divider()
c1, c2 = st.columns(2)

with c1:
    st.subheader("🕵️ Spending Detective")
    if not spend_df.empty:
        edited = st.data_editor(
            spend_df[['transaction_id', 'date', 'name', 'amount', 'category', 'bucket']],
            column_config={
                "transaction_id": None,
                "bucket":   st.column_config.SelectboxColumn("Bucket",   options=["SPEND","BILL","INCOME","TRANSFER"], required=True),
                "category": st.column_config.SelectboxColumn("Category", options=["Groceries","Dining Out","Rent","Utilities","Shopping","Transport","Travel","Income","Subscriptions","Credit Card Pay","Home Improvement","Pets","RX","Savings","Gambling","Personal Loan","Uncategorized"]),
            },
            hide_index=True, use_container_width=True, key="detective_editor"
        )
        if st.button("💾 Save Changes", type="primary"):
            with get_db_connection().connect() as conn:
                for _, row in edited.iterrows():
                    conn.execute(text("UPDATE transactions SET bucket=:b, category=:c WHERE transaction_id=:id"),
                                 {"b": row['bucket'], "c": row['category'], "id": row['transaction_id']})
                conn.commit()
            st.success("Saved!")
            st.rerun()
    else:
        st.success("No spending this week!")

with c2:
    st.subheader("📋 All Transactions This Week")
    if not week_df.empty:
        st.dataframe(
            week_df[['date','name','amount','bucket','category']].sort_values('date', ascending=False),
            hide_index=True, use_container_width=True
        )
    else:
        st.info("No transactions found for this week.")
