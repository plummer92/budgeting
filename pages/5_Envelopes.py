import streamlit as st
import pandas as pd
from datetime import datetime
from sqlalchemy import text
from utils import get_db_connection, CAT_OPTIONS, init_db

st.set_page_config(page_title="Envelopes", layout="wide", page_icon="✉️")
init_db()

st.title("✉️ Envelope Budgeting")
st.caption("Give every dollar a job. Money sits in envelopes until you spend it.")

current_month = datetime.now().strftime('%Y-%m')
month_label   = datetime.now().strftime('%B %Y')

# ── Load data ─────────────────────────────────────────────────────────────────
with get_db_connection().connect() as conn:
    envelopes_df = pd.read_sql("SELECT * FROM envelopes ORDER BY sort_order, name", conn)
    funding_df   = pd.read_sql(
        "SELECT envelope_id, SUM(amount) as funded FROM envelope_funding WHERE month = %s GROUP BY envelope_id",
        conn, params=(current_month,)
    )
    transactions_df = pd.read_sql(
        "SELECT category, SUM(ABS(amount)) as spent FROM transactions WHERE bucket='SPEND' AND DATE_TRUNC('month', date) = DATE_TRUNC('month', CURRENT_DATE) GROUP BY category",
        conn
    )

# ── Section 1: Summary metrics ────────────────────────────────────────────────
if not envelopes_df.empty:
    total_budgeted = envelopes_df['budgeted'].sum()
    total_funded   = funding_df['funded'].sum() if not funding_df.empty else 0
    total_spent    = transactions_df['spent'].sum() if not transactions_df.empty else 0
    total_left     = total_funded - total_spent

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Budgeted",  f"${total_budgeted:,.2f}")
    m2.metric("Funded This Month", f"${total_funded:,.2f}")
    m3.metric("Spent This Month",  f"${total_spent:,.2f}")
    m4.metric("Left to Spend",     f"${total_left:,.2f}",
              delta_color="normal" if total_left >= 0 else "inverse")

    st.divider()

# ── Section 2: Envelope cards ─────────────────────────────────────────────────
if envelopes_df.empty:
    st.info("No envelopes yet. Create your first one below.")
else:
    st.subheader(f"📬 Your Envelopes — {month_label}")

    env = envelopes_df.copy()
    if not funding_df.empty:
        env = env.merge(funding_df, on='envelope_id', how='left')
    else:
        env['funded'] = 0.0
    env['funded'] = env['funded'].fillna(0)

    if not transactions_df.empty:
        env = env.merge(transactions_df, left_on='category', right_on='category', how='left')
    else:
        env['spent'] = 0.0
    env['spent']     = env['spent'].fillna(0)
    env['available'] = env['funded'] - env['spent']

    cols = st.columns(3)
    for i, (_, row) in enumerate(env.iterrows()):
        with cols[i % 3]:
            budget  = row['budgeted'] if row['budgeted'] > 0 else 1
            pct     = min(row['spent'] / budget, 1.0)
            remaining = row['available']
            color   = "#4CAF50" if pct < 0.75 else "#FF9800" if pct < 1.0 else "#f44336"
            emoji   = "✅" if pct < 0.75 else "⚠️" if pct < 1.0 else "🔴"

            st.markdown(f"""
            <div style="background:white; border-radius:12px; padding:16px; margin-bottom:12px;
                        box-shadow:0 2px 8px rgba(0,0,0,0.08); border-left:4px solid {color};">
                <div style="font-weight:bold; font-size:15px; margin-bottom:4px;">
                    {emoji} {row['name']}
                </div>
                <div style="color:#888; font-size:12px; margin-bottom:8px;">
                    {row['category'] or 'No category linked'} &middot; resets {row['reset_period']}
                </div>
                <div style="background:#f0f0f0; border-radius:4px; height:8px; margin-bottom:8px;">
                    <div style="background:{color}; width:{pct*100:.0f}%; height:8px; border-radius:4px;"></div>
                </div>
                <div style="display:flex; justify-content:space-between; font-size:13px;">
                    <span>Spent: <b>${row['spent']:,.2f}</b></span>
                    <span>Budget: <b>${row['budgeted']:,.2f}</b></span>
                </div>
                <div style="text-align:center; margin-top:6px; font-size:14px; font-weight:bold; color:{color};">
                    {'$' + f"{remaining:,.2f} left" if remaining >= 0 else '⚠️ Over by $' + f"{abs(remaining):,.2f}"}
                </div>
            </div>
            """, unsafe_allow_html=True)

    if envelopes_df['budgeted'].sum() > 0:
        overall_pct = min(env['spent'].sum() / envelopes_df['budgeted'].sum(), 1.0)
        st.progress(overall_pct,
                    text=f"Overall: ${env['spent'].sum():,.2f} spent of ${envelopes_df['budgeted'].sum():,.2f} budgeted ({overall_pct*100:.0f}%)")

st.divider()

# ── Section 3: Create & edit envelopes ───────────────────────────────────────
col_add, col_edit = st.columns(2)

with col_add:
    st.subheader("➕ Create Envelope")
    with st.form("new_envelope"):
        env_name     = st.text_input("Envelope Name", placeholder="e.g. Groceries, Date Night, Car Fund")
        env_budget   = st.number_input("Monthly Budget ($)", min_value=0.0, step=10.0)
        env_category = st.selectbox("Link to Transaction Category", ["(none)"] + CAT_OPTIONS)
        env_period   = st.selectbox("Resets Every", ["monthly", "weekly", "yearly"])
        if st.form_submit_button("✉️ Create Envelope", type="primary"):
            if not env_name:
                st.error("Please enter a name.")
            else:
                try:
                    with get_db_connection().connect() as conn:
                        conn.execute(text("""
                            INSERT INTO envelopes (name, budgeted, category, reset_period)
                            VALUES (:name, :budget, :cat, :period)
                            ON CONFLICT (name) DO UPDATE SET
                                budgeted=EXCLUDED.budgeted,
                                category=EXCLUDED.category,
                                reset_period=EXCLUDED.reset_period
                        """), {
                            "name":   env_name,
                            "budget": env_budget,
                            "cat":    env_category if env_category != "(none)" else None,
                            "period": env_period,
                        })
                        conn.commit()
                    st.success(f"✉️ '{env_name}' created!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")

with col_edit:
    st.subheader("✏️ Edit / Delete Envelopes")
    if not envelopes_df.empty:
        edited_env = st.data_editor(
            envelopes_df[['envelope_id', 'name', 'budgeted', 'category', 'reset_period']],
            column_config={
                "envelope_id":  None,
                "budgeted":     st.column_config.NumberColumn("Budget $", format="$%.2f"),
                "category":     st.column_config.SelectboxColumn("Category", options=[""] + CAT_OPTIONS),
                "reset_period": st.column_config.SelectboxColumn("Resets", options=["monthly","weekly","yearly"]),
            },
            hide_index=True, use_container_width=True, num_rows="dynamic", key="env_editor"
        )
        if st.button("💾 Save Changes"):
            with get_db_connection().connect() as conn:
                existing_ids = set(envelopes_df['envelope_id'].tolist())
                edited_ids   = set(edited_env['envelope_id'].dropna().tolist())
                for del_id in existing_ids - edited_ids:
                    conn.execute(text("DELETE FROM envelopes WHERE envelope_id = :id"), {"id": del_id})
                for _, row in edited_env.iterrows():
                    if pd.notna(row.get('envelope_id')):
                        conn.execute(text("""
                            UPDATE envelopes SET name=:n, budgeted=:b, category=:c, reset_period=:r
                            WHERE envelope_id=:id
                        """), {"n": row['name'], "b": row['budgeted'],
                               "c": row['category'] or None, "r": row['reset_period'],
                               "id": row['envelope_id']})
                conn.commit()
            st.success("Saved!")
            st.rerun()
    else:
        st.info("Create your first envelope on the left.")

st.divider()

# ── Section 4: Fund envelopes ─────────────────────────────────────────────────
st.subheader(f"💸 Fund Envelopes for {month_label}")
st.caption("Record how much you're putting into each envelope this month. Defaults to your budgeted amount.")

if not envelopes_df.empty:
    with st.form("fund_envelopes"):
        h1, h2, h3 = st.columns([2, 1, 1])
        h1.markdown("**Envelope**")
        h2.markdown("**Budgeted**")
        h3.markdown("**Fund Amount**")

        amounts = {}
        for _, row in envelopes_df.iterrows():
            c1, c2, c3 = st.columns([2, 1, 1])
            c1.write(row['name'])
            c2.write(f"${row['budgeted']:,.2f}")
            amounts[row['envelope_id']] = c3.number_input(
                "", min_value=0.0, value=float(row['budgeted']),
                step=10.0, key=f"fund_{row['envelope_id']}", label_visibility="collapsed"
            )

        if st.form_submit_button("💰 Save Funding", type="primary"):
            with get_db_connection().connect() as conn:
                for env_id, amount in amounts.items():
                    # Upsert: replace any existing funding for this envelope this month
                    conn.execute(text("""
                        DELETE FROM envelope_funding WHERE envelope_id=:eid AND month=:month
                    """), {"eid": env_id, "month": current_month})
                    conn.execute(text("""
                        INSERT INTO envelope_funding (envelope_id, amount, month)
                        VALUES (:eid, :amt, :month)
                    """), {"eid": env_id, "amt": amount, "month": current_month})
                conn.commit()
            st.success(f"✅ Envelopes funded for {month_label}!")
            st.rerun()
