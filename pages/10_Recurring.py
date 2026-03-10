import streamlit as st
import pandas as pd
from sqlalchemy import text
from utils import get_db_connection, init_db, show_sidebar_alerts

st.set_page_config(page_title="Recurring Transactions", layout="wide", page_icon="🔁")
init_db()
show_sidebar_alerts()

st.title("🔁 Recurring Transaction Detector")
st.caption("Merchants that appear in multiple months — likely subscriptions or regular bills.")

# ── Load all transactions ─────────────────────────────────────────────────────
with get_db_connection().connect() as conn:
    df = pd.read_sql(
        "SELECT transaction_id, date, name, amount, category, bucket FROM transactions ORDER BY date DESC",
        conn
    )

if df.empty:
    st.info("No transactions yet.")
    st.stop()

df['date']   = pd.to_datetime(df['date'])
df['month']  = df['date'].dt.to_period('M').astype(str)
df['amount'] = df['amount'].abs()

# ── Detect recurring: merchants appearing in 2+ distinct months ───────────────
merchant_months = (
    df.groupby('name')['month']
    .nunique()
    .reset_index()
    .rename(columns={'month': 'months_seen'})
)
merchant_avg = (
    df.groupby('name')['amount']
    .mean()
    .reset_index()
    .rename(columns={'amount': 'avg_amount'})
)
merchant_last = (
    df.groupby('name')['date']
    .max()
    .reset_index()
    .rename(columns={'date': 'last_seen'})
)
merchant_cat = (
    df.groupby('name')['bucket']
    .first()
    .reset_index()
)

recurring = (
    merchant_months[merchant_months['months_seen'] >= 2]
    .merge(merchant_avg, on='name')
    .merge(merchant_last, on='name')
    .merge(merchant_cat, on='name')
    .sort_values('avg_amount', ascending=False)
)

# ── Summary ───────────────────────────────────────────────────────────────────
already_bills = recurring[recurring['bucket'].isin(['BILL', 'TRANSFER'])]
needs_review  = recurring[~recurring['bucket'].isin(['BILL', 'TRANSFER'])]

m1, m2, m3 = st.columns(3)
m1.metric("Recurring Merchants Found", str(len(recurring)))
m2.metric("Already Marked as Bill",    str(len(already_bills)))
m3.metric("Need Review",               str(len(needs_review)),
          delta_color="inverse" if len(needs_review) > 0 else "normal")

st.divider()

# ── Needs review ─────────────────────────────────────────────────────────────
if not needs_review.empty:
    st.subheader("⚠️ Possible Subscriptions / Bills — Not Yet Marked")
    st.caption("These merchants appear in 2+ months but aren't tagged as BILL. Review and mark them.")

    for _, row in needs_review.iterrows():
        col1, col2, col3, col4, col5 = st.columns([3, 1, 1, 1, 1])
        col1.write(f"**{row['name']}**")
        col2.write(f"${row['avg_amount']:,.2f}/mo avg")
        col3.write(f"{row['months_seen']} months")
        col4.write(row['last_seen'].strftime('%b %d'))

        if col5.button("Mark as BILL", key=f"bill_{row['name']}"):
            with get_db_connection().connect() as conn:
                conn.execute(text("""
                    UPDATE transactions SET bucket='BILL'
                    WHERE name = :n AND bucket NOT IN ('INCOME','TRANSFER')
                """), {"n": row['name']})
                conn.commit()
            st.success(f"✅ All '{row['name']}' transactions marked as BILL!")
            st.rerun()

    st.divider()

# ── Already marked ────────────────────────────────────────────────────────────
if not already_bills.empty:
    with st.expander(f"✅ Already Marked as BILL ({len(already_bills)} merchants)"):
        display = already_bills[['name', 'avg_amount', 'months_seen', 'last_seen']].copy()
        display['last_seen']   = display['last_seen'].dt.strftime('%b %d, %Y')
        display['avg_amount']  = display['avg_amount'].round(2)
        st.dataframe(
            display.rename(columns={
                'name': 'Merchant', 'avg_amount': 'Avg/Month',
                'months_seen': 'Months Seen', 'last_seen': 'Last Seen'
            }),
            hide_index=True,
            use_container_width=True,
            column_config={"Avg/Month": st.column_config.NumberColumn(format="$%.2f")}
        )

st.divider()

# ── Monthly subscription cost summary ────────────────────────────────────────
st.subheader("💳 Estimated Monthly Recurring Cost")
total_recurring = recurring['avg_amount'].sum()
total_bills_amt = already_bills['avg_amount'].sum()
total_unknown   = needs_review['avg_amount'].sum()

c1, c2, c3 = st.columns(3)
c1.metric("Total Recurring (all)",      f"${total_recurring:,.2f}/mo")
c2.metric("Confirmed Bills",            f"${total_bills_amt:,.2f}/mo")
c3.metric("Unconfirmed (needs review)", f"${total_unknown:,.2f}/mo",
          delta_color="inverse" if total_unknown > 0 else "normal")
