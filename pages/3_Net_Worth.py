import streamlit as st
import pandas as pd
import plotly.express as px
from sqlalchemy import text
from utils import get_db_connection, init_db, show_sidebar_alerts

st.set_page_config(page_title="Net Worth", layout="wide", page_icon="🏦")
init_db()
show_sidebar_alerts()

st.title("🏦 Net Worth & Loans")

with st.expander("📝 Update Account Balances", expanded=True):
    with get_db_connection().connect() as conn:
        accounts_df = pd.read_sql("SELECT * FROM net_worth_accounts ORDER BY type", conn)

    if accounts_df.empty:
        default_data = pd.DataFrame([
            {"name": "Checking Account", "type": "Asset",     "balance": 0.00},
            {"name": "Savings Account",  "type": "Asset",     "balance": 0.00},
            {"name": "Loan 1",           "type": "Liability", "balance": 0.00},
            {"name": "Loan 2",           "type": "Liability", "balance": 0.00},
        ])
        with get_db_connection().connect() as conn:
            for _, row in default_data.iterrows():
                conn.execute(text("INSERT INTO net_worth_accounts (name, type, balance) VALUES (:n, :t, :b)"),
                             {"n": row['name'], "t": row['type'], "b": row['balance']})
            conn.commit()
        st.rerun()

    edited_acc = st.data_editor(
        accounts_df,
        column_config={
            "account_id": None,
            "type":    st.column_config.SelectboxColumn(options=["Asset", "Liability"]),
            "balance": st.column_config.NumberColumn(format="$%.2f"),
        },
        hide_index=True, num_rows="dynamic"
    )

    if st.button("💾 Save Balances"):
        with get_db_connection().connect() as conn:
            conn.execute(text("DELETE FROM net_worth_accounts"))
            for _, row in edited_acc.iterrows():
                conn.execute(text("INSERT INTO net_worth_accounts (name, type, balance) VALUES (:n, :t, :b)"),
                             {"n": row['name'], "t": row['type'], "b": row['balance']})
            conn.commit()
        st.success("Balances saved!")
        st.rerun()

assets      = edited_acc[edited_acc['type'] == 'Asset']['balance'].sum()
liabilities = edited_acc[edited_acc['type'] == 'Liability']['balance'].sum()
net_worth   = assets - liabilities

m1, m2, m3 = st.columns(3)
m1.metric("Total Assets", f"${assets:,.2f}")
m2.metric("Total Debt",   f"${liabilities:,.2f}")
m3.metric("Net Worth",    f"${net_worth:,.2f}",
          delta_color="normal" if net_worth >= 0 else "inverse")

chart_data = pd.DataFrame({"Type": ["Assets", "Liabilities"], "Amount": [assets, liabilities]})
st.plotly_chart(
    px.bar(chart_data, x="Type", y="Amount", color="Type", title="Assets vs Liabilities"),
    use_container_width=True
)
