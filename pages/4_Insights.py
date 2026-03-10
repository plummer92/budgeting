import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime, timedelta
from utils import get_db_connection, get_budget_setting, init_db

st.set_page_config(page_title="Insights", layout="wide", page_icon="💡")
init_db()

st.title("💡 Insights")

est_income = get_budget_setting("est_income", 4000.0)
est_bills  = get_budget_setting("est_bills",  1500.0)
weekly_allowance = (est_income - est_bills) / 4

with get_db_connection().connect() as conn:
    df = pd.read_sql("SELECT * FROM transactions", conn)

if df.empty:
    st.info("No transaction data yet.")
    st.stop()

df['date'] = pd.to_datetime(df['date'])

# Default to current week
view_date     = st.date_input("📅 View Week Containing:", value=datetime.now())
start_of_week = view_date - timedelta(days=view_date.weekday())
end_of_week   = start_of_week + timedelta(days=6)
week_df = df[(df['date'] >= pd.Timestamp(start_of_week)) & (df['date'] <= pd.Timestamp(end_of_week))].copy()

if week_df.empty:
    st.info("No transactions found for this week.")
    st.stop()

# Wants vs Needs
st.subheader("🛍️ Wants vs Needs This Week")
wants_cats = ["Dining Out", "Shopping", "Gambling", "Subscriptions"]
wants_df   = week_df[(week_df['category'].isin(wants_cats)) & (week_df['bucket'] == 'SPEND')]
total_wants = abs(wants_df['amount'].sum())
total_spend = abs(week_df[week_df['bucket'] == 'SPEND']['amount'].sum())

c1, c2, c3 = st.columns(3)
c1.metric("Spent on Wants",  f"${total_wants:,.2f}")
c2.metric("Total Spending",  f"${total_spend:,.2f}")
c3.metric("Remaining Budget", f"${weekly_allowance - total_spend:,.2f}",
          delta_color="normal" if weekly_allowance - total_spend >= 0 else "inverse")

st.divider()

# Category breakdown
st.subheader("📊 Spending by Category This Week")
spend_cats = week_df[(week_df['bucket'] == 'SPEND') & (week_df['amount'] > 0)]
if not spend_cats.empty:
    cat_summary = spend_cats.groupby('category')['amount'].sum().abs().reset_index()
    cat_summary.columns = ['Category', 'Amount']
    cat_summary = cat_summary.sort_values('Amount', ascending=False)

    col_chart, col_table = st.columns([2, 1])
    with col_chart:
        fig = px.pie(cat_summary, names='Category', values='Amount', title="Where did the money go?")
        st.plotly_chart(fig, use_container_width=True)
    with col_table:
        st.dataframe(cat_summary, hide_index=True, use_container_width=True)

st.divider()

# Month over month comparison
st.subheader("📅 Month-over-Month Spending")
df['month'] = df['date'].dt.to_period('M').astype(str)
monthly = df[df['bucket'] == 'SPEND'].groupby(['month', 'category'])['amount'].sum().abs().reset_index()
monthly.columns = ['Month', 'Category', 'Amount']

all_cats = monthly['Category'].unique().tolist()
sel_cats = st.multiselect("Filter Categories:", options=all_cats, default=all_cats[:6] if len(all_cats) >= 6 else all_cats)
filtered = monthly[monthly['Category'].isin(sel_cats)]

if not filtered.empty:
    fig2 = px.bar(filtered, x='Month', y='Amount', color='Category', barmode='stack',
                  title="Monthly Spending by Category")
    st.plotly_chart(fig2, use_container_width=True)
