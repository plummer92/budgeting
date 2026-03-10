import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime
from utils import get_db_connection, get_budget_setting, get_str_setting, set_budget_setting, init_db

st.set_page_config(page_title="Life Balance", layout="wide", page_icon="📈")
init_db()

st.title("📈 The Life Balance")
st.caption("Are you winning or losing over time?")

est_income = get_budget_setting("est_income", 4000.0)
est_bills  = get_budget_setting("est_bills",  1500.0)
saved_start = get_str_setting("budget_start_date", "2025-01-01")

c_set, c_chart = st.columns([1, 3])

with c_set:
    start_date_input = st.date_input("Start Tracking From:", value=pd.to_datetime(saved_start))
    if st.button("Update Start Date"):
        set_budget_setting("budget_start_date", start_date_input.strftime('%Y-%m-%d'), is_str=True)
        st.rerun()

with get_db_connection().connect() as conn:
    df = pd.read_sql("SELECT * FROM transactions", conn)

with c_chart:
    if df.empty:
        st.info("No transaction data yet.")
    else:
        df['date'] = pd.to_datetime(df['date'])
        life_df    = df[df['date'] >= pd.Timestamp(start_date_input)].copy()

        wk_allow   = (est_income - est_bills) / 4
        daily_allow = wk_allow / 7

        date_range  = pd.date_range(start=start_date_input, end=datetime.now())
        daily_data  = pd.DataFrame(index=date_range)
        daily_data['allowance'] = daily_allow

        daily_spend = life_df[life_df['bucket'] == 'SPEND'].groupby('date')['amount'].sum().abs()
        daily_data  = daily_data.join(daily_spend).fillna(0).rename(columns={'amount': 'spend'})

        daily_data['cum_allowance']  = daily_data['allowance'].cumsum()
        daily_data['cum_spend']      = daily_data['spend'].cumsum()
        daily_data['running_balance'] = daily_data['cum_allowance'] - daily_data['cum_spend']

        last_bal = daily_data['running_balance'].iloc[-1]
        st.metric("Total Life Balance", f"${last_bal:,.2f}",
                  delta="Surplus" if last_bal >= 0 else "Deficit",
                  delta_color="normal" if last_bal >= 0 else "inverse")

        fig = px.line(daily_data, y='running_balance', title="Running Surplus / Deficit Over Time")
        fig.add_hline(y=0, line_dash="dash", line_color="gray")
        fig.update_layout(yaxis_title="Balance ($)", xaxis_title="Date")
        st.plotly_chart(fig, use_container_width=True)
