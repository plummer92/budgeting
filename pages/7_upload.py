import streamlit as st
from utils import clean_bank_file, save_to_neon, run_auto_categorization, init_db

st.set_page_config(page_title="Upload", layout="wide", page_icon="📂")
init_db()

st.title("📂 Upload Bank File")
st.caption("Manually upload a CSV or PDF export from your bank.")

bc = st.selectbox("Select Your Bank", ["Chase", "Citi", "Sofi", "Chime", "Loan/Other"])
f  = st.file_uploader("Choose a CSV or PDF file", type=['csv', 'pdf'])

if f:
    df = clean_bank_file(f, bc)
    if df is not None and not df.empty:
        st.subheader("Preview (first 10 rows)")
        st.dataframe(df.head(10), hide_index=True, use_container_width=True)
        st.caption(f"{len(df)} transactions found in file.")

        if st.button("✅ Confirm & Import", type="primary"):
            count = save_to_neon(df)
            run_auto_categorization()
            st.success(f"🎉 {count} new transactions imported! Head to Rules & Inbox to categorize any new ones.")
