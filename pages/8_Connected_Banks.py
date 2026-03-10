import streamlit as st
import pandas as pd
import urllib.parse
from sqlalchemy import text
from utils import (get_db_connection, plaid_create_link_token, plaid_exchange_public_token,
                   plaid_get_institution_name, plaid_post, plaid_sync_item,
                   PLAID_CLIENT_ID, PLAID_SECRET, init_db)

st.set_page_config(page_title="Connected Banks", layout="wide", page_icon="🔗")
init_db()
show_sidebar_alerts()

st.title("🔗 Connected Banks")
st.caption("Connect your bank accounts once — transactions sync automatically.")

if not PLAID_CLIENT_ID or not PLAID_SECRET:
    st.error("⚠️ Plaid credentials not found. Add PLAID_CLIENT_ID, PLAID_SECRET, and PLAID_ENV to your Streamlit secrets.")
    st.stop()

# ── Load connected banks ──────────────────────────────────────────────────────
with get_db_connection().connect() as conn:
    items_df    = pd.read_sql("SELECT * FROM plaid_items ORDER BY linked_at DESC", conn)
    accounts_df = pd.read_sql("SELECT * FROM plaid_accounts ORDER BY type, name", conn)

# ── Show connected banks ──────────────────────────────────────────────────────
if items_df.empty:
    st.info("No banks connected yet. Use the button below to link your first account.")
else:
    st.subheader("Your Connected Accounts")
    for _, item in items_df.iterrows():
        with st.expander(f"🏦 {item['institution_name']}  —  linked {pd.to_datetime(item['linked_at']).strftime('%b %d, %Y')}", expanded=True):
            item_accounts = accounts_df[accounts_df['item_id'] == item['item_id']]
            if not item_accounts.empty:
                display_cols = [c for c in ['name','type','subtype','current_balance','available_balance','last_synced'] if c in item_accounts.columns]
                st.dataframe(
                    item_accounts[display_cols].rename(columns={
                        'name':'Account','type':'Type','subtype':'Subtype',
                        'current_balance':'Balance','available_balance':'Available','last_synced':'Last Synced'
                    }),
                    hide_index=True, use_container_width=True
                )
            col_sync, col_remove, _ = st.columns([1, 1, 4])
            with col_sync:
                if st.button("🔄 Sync Now", key=f"sync_{item['item_id']}"):
                    with st.spinner(f"Syncing {item['institution_name']}..."):
                        try:
                            n = plaid_sync_item(item['access_token'], item['item_id'], item['institution_name'])
                            st.success(f"✅ {n} new transactions imported!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Sync failed: {e}")
            with col_remove:
                if st.button("🗑️ Disconnect", key=f"remove_{item['item_id']}"):
                    try:
                        plaid_post("/item/remove", {"access_token": item['access_token']})
                    except Exception:
                        pass
                    with get_db_connection().connect() as conn:
                        conn.execute(text("DELETE FROM plaid_items WHERE item_id = :id"), {"id": item['item_id']})
                        conn.commit()
                    st.success("Disconnected.")
                    st.rerun()

    st.divider()
    if st.button("🔄 Sync All Banks", type="primary"):
        total = 0
        for _, item in items_df.iterrows():
            with st.spinner(f"Syncing {item['institution_name']}..."):
                try:
                    total += plaid_sync_item(item['access_token'], item['item_id'], item['institution_name'])
                except Exception as e:
                    st.warning(f"Could not sync {item['institution_name']}: {e}")
        st.success(f"✅ Done — {total} new transactions imported!")
        st.rerun()

    st.divider()

# ── Handle return from Plaid Link ─────────────────────────────────────────────
public_token_param = st.query_params.get("plaid_public_token", "")
if public_token_param and public_token_param not in ["", "null"]:
    with st.spinner("Finishing bank connection..."):
        try:
            access_token, item_id = plaid_exchange_public_token(public_token_param)
            item_info      = plaid_post("/item/get", {"access_token": access_token})
            institution_id = item_info["item"].get("institution_id", "")
            institution_name = plaid_get_institution_name(institution_id) if institution_id else "Your Bank"
            with get_db_connection().connect() as conn:
                conn.execute(text("""
                    INSERT INTO plaid_items (item_id, access_token, institution_name)
                    VALUES (:iid, :tok, :name)
                    ON CONFLICT (item_id) DO UPDATE SET
                        access_token=EXCLUDED.access_token, institution_name=EXCLUDED.institution_name
                """), {"iid": item_id, "tok": access_token, "name": institution_name})
                conn.commit()
            st.query_params.clear()
            new_count = plaid_sync_item(access_token, item_id, institution_name)
            st.success(f"🎉 {institution_name} connected! {new_count} transactions imported.")
            st.rerun()
        except Exception as e:
            st.error(f"Failed to connect bank: {e}")
            st.query_params.clear()
else:
    # ── Link a new bank ───────────────────────────────────────────────────────
    st.subheader("➕ Link a New Bank")
    try:
        link_token = plaid_create_link_token()
    except Exception as e:
        st.error(f"Could not create Plaid link token: {e}")
        link_token = None

    if link_token:
        host       = st.context.headers.get('host', 'localhost')
        return_url = f"https://{host}"
        PLAID_LINK_PAGE = "https://plummer92.github.io/budgeting/static/plaid_link.html"
        plaid_page_url  = (
            f"{PLAID_LINK_PAGE}"
            f"?link_token={urllib.parse.quote(link_token)}"
            f"&return_url={urllib.parse.quote(return_url)}"
        )
        st.markdown(f"""
        <a href="{plaid_page_url}" style="
            display:inline-block; background:#4CAF50; color:white;
            padding:14px 32px; font-size:16px; font-weight:bold;
            border-radius:8px; text-decoration:none; margin-top:8px;">
            🏦 Connect a Bank Account
        </a>
        """, unsafe_allow_html=True)
        st.caption("You'll be taken to a secure login page, then redirected back here automatically.")
        st.caption("🔒 Bank-grade encryption. Plaid is trusted by Venmo, Coinbase, and Robinhood.")

    with st.expander("📋 Setup Instructions"):
        st.markdown("""
        **Add these to your Streamlit secrets:**
        ```
        PLAID_CLIENT_ID = "your_client_id"
        PLAID_SECRET    = "your_secret"
        PLAID_ENV       = "production"
        ```
        - `sandbox` — test with fake credentials (`user_good` / `pass_good`)
        - `production` — real banks (requires Plaid approval)
        """)
