import streamlit as st
from utils import init_db

st.set_page_config(page_title="My Budget Master", layout="wide", page_icon="💰")

init_db()

st.title("💰 My Budget Master")
st.markdown("""
Welcome to your personal financial command center.  
Use the sidebar to navigate between sections.
""")

col1, col2, col3, col4 = st.columns(4)
col1.page_link("pages/1_Weekly.py",          label="📊 Weekly Dashboard",  icon="📊")
col2.page_link("pages/2_Life_Balance.py",     label="📈 Life Balance",       icon="📈")
col3.page_link("pages/3_Net_Worth.py",        label="🏦 Net Worth",          icon="🏦")
col4.page_link("pages/4_Insights.py",         label="💡 Insights",           icon="💡")

col5, col6, col7, col8 = st.columns(4)
col5.page_link("pages/5_Envelopes.py",        label="✉️ Envelopes",          icon="✉️")
col6.page_link("pages/6_Rules.py",            label="⚡ Rules & Inbox",      icon="⚡")
col7.page_link("pages/7_Upload.py",           label="📂 Upload",             icon="📂")
col8.page_link("pages/8_Connected_Banks.py",  label="🔗 Connected Banks",    icon="🔗")
