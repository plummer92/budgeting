import streamlit as st
import pandas as pd
import calendar
from datetime import datetime
from sqlalchemy import text
from utils import get_db_connection, init_db, show_sidebar_alerts

st.set_page_config(page_title="Bill Calendar", layout="wide", page_icon="📅")
init_db()
show_sidebar_alerts()

st.title("📅 Bill Calendar")
st.caption("See when every bill hits this month.")

# ── Month selector ────────────────────────────────────────────────────────────
today = datetime.now()
col_nav, _ = st.columns([1, 3])
with col_nav:
    selected_month = st.selectbox(
        "Month",
        options=[f"{today.year}-{m:02d}" for m in range(1, 13)],
        index=today.month - 1,
        format_func=lambda x: datetime.strptime(x, "%Y-%m").strftime("%B %Y")
    )

sel_year  = int(selected_month.split("-")[0])
sel_month = int(selected_month.split("-")[1])
month_label = datetime(sel_year, sel_month, 1).strftime("%B %Y")

# ── Load bills for selected month ─────────────────────────────────────────────
with get_db_connection().connect() as conn:
    bills_df = pd.read_sql(
        text("""
            SELECT date, name, amount, category
            FROM transactions
            WHERE bucket IN ('BILL', 'SPEND')
            AND category IN ('Rent','Utilities','Subscriptions','Credit Card Pay',
                             'Personal Loan','Insurance','Transport')
            AND EXTRACT(YEAR  FROM date) = :yr
            AND EXTRACT(MONTH FROM date) = :mo
            ORDER BY date
        """),
        conn,
        params={"yr": sel_year, "mo": sel_month}
    )

bills_df['date']   = pd.to_datetime(bills_df['date'])
bills_df['day']    = bills_df['date'].dt.day
bills_df['amount'] = bills_df['amount'].abs()

# ── Summary strip ─────────────────────────────────────────────────────────────
if not bills_df.empty:
    total_bills = bills_df['amount'].sum()
    paid_bills  = bills_df[bills_df['date'].dt.date <= today.date()]['amount'].sum()
    upcoming    = total_bills - paid_bills

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Bills",    f"${total_bills:,.2f}")
    m2.metric("Already Paid",   f"${paid_bills:,.2f}")
    m3.metric("Still Coming",   f"${upcoming:,.2f}")
    m4.metric("# of Bills",     str(len(bills_df)))
    st.divider()

# ── Calendar grid ─────────────────────────────────────────────────────────────
st.subheader(f"📆 {month_label}")

cal = calendar.monthcalendar(sel_year, sel_month)
day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Header row
header_cols = st.columns(7)
for i, d in enumerate(day_names):
    header_cols[i].markdown(
        f"<div style='text-align:center; font-weight:700; font-size:13px; "
        f"color:#555; padding-bottom:4px;'>{d}</div>",
        unsafe_allow_html=True
    )

# Build a dict: day -> list of bills
bills_by_day = {}
for _, row in bills_df.iterrows():
    d = row['day']
    bills_by_day.setdefault(d, []).append(row)

# Calendar rows
for week in cal:
    cols = st.columns(7)
    for col_idx, day in enumerate(week):
        with cols[col_idx]:
            if day == 0:
                st.markdown("<div style='height:90px'></div>", unsafe_allow_html=True)
                continue

            is_today  = (day == today.day and sel_month == today.month and sel_year == today.year)
            is_past   = datetime(sel_year, sel_month, day).date() < today.date()
            day_bills = bills_by_day.get(day, [])

            # Day number styling
            if is_today:
                day_style = "background:#1976d2; color:white; border-radius:50%; width:26px; height:26px; display:inline-flex; align-items:center; justify-content:center; font-weight:700; font-size:14px;"
            else:
                day_style = "font-weight:600; font-size:14px; color:#333;"

            cell_bg   = "#f9f9f9" if not day_bills else ("#f0f4ff" if not is_past else "#f5f5f5")
            cell_border = "2px solid #1976d2" if is_today else ("1px solid #ddd" if not day_bills else "1px solid #90caf9")

            bills_html = ""
            for b in day_bills:
                chip_color = "#e3f2fd" if not is_past else "#eeeeee"
                text_color = "#1565c0" if not is_past else "#757575"
                strike     = "text-decoration:line-through;" if is_past else ""
                bills_html += (
                    f"<div style='background:{chip_color}; color:{text_color}; {strike} "
                    f"border-radius:4px; padding:2px 5px; font-size:11px; margin-top:3px; "
                    f"white-space:nowrap; overflow:hidden; text-overflow:ellipsis;'>"
                    f"${b['amount']:,.0f} {b['name'][:14]}"
                    f"</div>"
                )

            st.markdown(f"""
            <div style="background:{cell_bg}; border:{cell_border}; border-radius:8px;
                        padding:6px 7px; min-height:80px; margin-bottom:4px;">
                <span style="{day_style}">{day}</span>
                {bills_html}
            </div>
            """, unsafe_allow_html=True)

st.divider()

# ── Bill list table ───────────────────────────────────────────────────────────
st.subheader(f"📋 All Bills — {month_label}")
if bills_df.empty:
    st.info("No bills found for this month. Bills are pulled from transactions with BILL bucket or Rent/Utilities/Subscriptions/Credit Card Pay categories.")
else:
    display = bills_df[['date', 'name', 'amount', 'category']].copy()
    display['date']   = display['date'].dt.strftime('%b %d')
    display['paid']   = bills_df['date'].dt.date <= today.date()
    display['status'] = display['paid'].map({True: '✅ Paid', False: '⏳ Upcoming'})
    st.dataframe(
        display[['date', 'name', 'amount', 'category', 'status']].rename(columns={
            'date': 'Date', 'name': 'Bill', 'amount': 'Amount',
            'category': 'Category', 'status': 'Status'
        }),
        hide_index=True,
        use_container_width=True,
        column_config={"Amount": st.column_config.NumberColumn(format="$%.2f")}
    )
