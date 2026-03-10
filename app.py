import streamlit as st
from utils import init_db

st.set_page_config(page_title="My Budget Master", layout="wide", page_icon="💰")
init_db()

st.title("💰 My Budget Master")
st.markdown("Welcome to your personal financial command center. Use the sidebar to navigate.")

col1, col2, col3, col4, col5 = st.columns(5)
col1.page_link("pages/1_Weekly.py",         label="📊 Weekly",         icon="📊")
col2.page_link("pages/2_Life_Balance.py",   label="📈 Life Balance",    icon="📈")
col3.page_link("pages/3_Net_Worth.py",      label="🏦 Net Worth",       icon="🏦")
col4.page_link("pages/4_Insights.py",       label="💡 Insights",        icon="💡")
col5.page_link("pages/5_Envelopes.py",      label="✉️ Envelopes",       icon="✉️")

col6, col7, col8, col9, col10 = st.columns(5)
col6.page_link("pages/6_Rules.py",          label="⚡ Rules",           icon="⚡")
col7.page_link("pages/7_Upload.py",         label="📂 Upload",          icon="📂")
col8.page_link("pages/8_Connected_Banks.py",label="🔗 Banks",           icon="🔗")
col9.page_link("pages/9_Bill_Calendar.py",  label="📅 Bill Calendar",   icon="📅")
col10.page_link("pages/10_Recurring.py",    label="🔁 Recurring",       icon="🔁")
