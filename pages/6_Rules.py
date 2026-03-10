import streamlit as st
import pandas as pd
from sqlalchemy import text
from utils import get_db_connection, run_auto_categorization, CAT_OPTIONS, init_db, show_sidebar_alerts

st.set_page_config(page_title="Rules & Inbox", layout="wide", page_icon="⚡")
init_db()
show_sidebar_alerts()

st.title("⚡ Rules & Inbox")

# ── Add Rule ──────────────────────────────────────────────────────────────────
with st.expander("➕ Add Auto-Categorization Rule"):
    with st.form("add_rule"):
        c1, c2, c3 = st.columns([2, 1, 1])
        nk = c1.text_input("If transaction name contains...")
        nc = c2.selectbox("Set Category", CAT_OPTIONS)
        nb = c3.selectbox("Set Bucket", ["SPEND","BILL","INCOME","TRANSFER"])
        if st.form_submit_button("Save Rule"):
            if nk:
                with get_db_connection().connect() as conn:
                    conn.execute(text("INSERT INTO category_rules (keyword, category, bucket) VALUES (:k,:c,:b)"),
                                 {"k": nk, "c": nc, "b": nb})
                    conn.commit()
                run_auto_categorization()
                st.success(f"Rule saved! Any matching uncategorized transactions have been updated.")
                st.rerun()
            else:
                st.error("Please enter a keyword.")

# Show existing rules
with get_db_connection().connect() as conn:
    rules_df = pd.read_sql("SELECT * FROM category_rules ORDER BY rule_id", conn)

if not rules_df.empty:
    with st.expander(f"📋 Existing Rules ({len(rules_df)})"):
        edited_rules = st.data_editor(rules_df, column_config={"rule_id": None},
                                      hide_index=True, use_container_width=True, num_rows="dynamic")
        if st.button("💾 Save Rule Changes"):
            with get_db_connection().connect() as conn:
                conn.execute(text("DELETE FROM category_rules"))
                for _, row in edited_rules.iterrows():
                    if pd.notna(row.get('keyword')) and row['keyword']:
                        conn.execute(text("INSERT INTO category_rules (keyword, category, bucket) VALUES (:k,:c,:b)"),
                                     {"k": row['keyword'], "c": row['category'], "b": row['bucket']})
                conn.commit()
            run_auto_categorization()
            st.success("Rules updated!")
            st.rerun()

st.divider()

# ── Inbox ──────────────────────────────────────────────────────────────────────
st.subheader("🚨 Inbox: Uncategorized Transactions")

with get_db_connection().connect() as conn:
    todo = pd.read_sql("SELECT * FROM transactions WHERE category='Uncategorized' ORDER BY date DESC", conn)

if not todo.empty:
    st.info(f"You have **{len(todo)}** transactions to categorize.")

    if st.button("🤖 Re-run Auto-Rules"):
        run_auto_categorization()
        st.rerun()

    ed_todo = st.data_editor(
        todo,
        column_config={
            "transaction_id": None,
            "manual_category": None,
            "manual_bucket": None,
            "pending": None,
            "merchant_name": None,
            "category": st.column_config.SelectboxColumn("Category", options=CAT_OPTIONS, required=True),
            "bucket":   st.column_config.SelectboxColumn("Bucket",   options=["SPEND","BILL","INCOME","TRANSFER"], required=True),
        },
        hide_index=True, use_container_width=True
    )
    if st.button("💾 Save Inbox Changes", type="primary"):
        with get_db_connection().connect() as conn:
            for _, r in ed_todo.iterrows():
                if r['category'] != 'Uncategorized':
                    conn.execute(text("UPDATE transactions SET category=:c, bucket=:b WHERE transaction_id=:i"),
                                 {"c": r['category'], "b": r['bucket'], "i": r['transaction_id']})
            conn.commit()
        st.success("Saved!")
        st.rerun()
else:
    st.success("🎉 Inbox Zero! All transactions are categorized.")

st.divider()

# ── Full History ──────────────────────────────────────────────────────────────
st.subheader("🔍 Full Transaction History")
s = st.text_input("Search by name or amount:", "")

with get_db_connection().connect() as conn:
    if s:
        h = pd.read_sql(
            text("SELECT * FROM transactions WHERE name ILIKE :search OR amount::text LIKE :search ORDER BY date DESC LIMIT 200"),
            conn, params={"search": f"%{s}%"}
        )
    else:
        h = pd.read_sql(text("SELECT * FROM transactions ORDER BY date DESC LIMIT 50"), conn)

ed = st.data_editor(
    h,
    column_config={
        "transaction_id": None,
        "manual_category": None,
        "manual_bucket": None,
        "pending": None,
        "merchant_name": None,
        "category": st.column_config.SelectboxColumn("Category", options=CAT_OPTIONS),
        "bucket":   st.column_config.SelectboxColumn("Bucket",   options=["SPEND","BILL","INCOME","TRANSFER"]),
    },
    hide_index=True, use_container_width=True
)
if st.button("Update History"):
    with get_db_connection().connect() as conn:
        for _, r in ed.iterrows():
            conn.execute(text("UPDATE transactions SET category=:c, bucket=:b WHERE transaction_id=:i"),
                         {"c": r['category'], "b": r['bucket'], "i": r['transaction_id']})
        conn.commit()
    st.success("Updated!")
    st.rerun()
