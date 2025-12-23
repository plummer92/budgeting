# === TAB 3: MONTHLY OVERVIEW (With Drill-Down) ===
with tab_month:
    st.header("📅 Monthly Overview")
    
    col_sel, col_empty = st.columns([1, 3])
    with col_sel:
        sel_date = st.date_input("Select Month:", value=datetime.now(), key="month_picker")
    
    start_month = sel_date.replace(day=1)
    next_month = (start_month.replace(day=28) + timedelta(days=4)).replace(day=1)
    end_month = next_month - timedelta(days=1)
    
    st.caption(f"Analyzing: {start_month.strftime('%b 1')} - {end_month.strftime('%b %d, %Y')}")
    
    if not df.empty:
        month_df = df[(df['date'] >= pd.Timestamp(start_month)) & (df['date'] <= pd.Timestamp(end_month))].copy()
        
        if month_df.empty:
            st.info("No transactions found for this month.")
        else:
            # METRICS
            inc_df = month_df[(month_df['bucket'] == 'INCOME') & (month_df['amount'] > 0)]
            bill_df = month_df[(month_df['bucket'] == 'BILL') & (month_df['amount'] < 0)]
            spend_df = month_df[(month_df['bucket'] == 'SPEND') & (month_df['amount'] < 0)]
            
            total_income = inc_df['amount'].sum()
            total_bills = bill_df['amount'].sum()
            total_spend = spend_df['amount'].sum()
            net_saved = total_income + total_bills + total_spend
            
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("💰 Income", f"${total_income:,.0f}")
            m2.metric("🧾 Fixed Bills", f"${abs(total_bills):,.0f}")
            m3.metric("💸 Spending", f"${abs(total_spend):,.0f}")
            m4.metric("🏦 Net Saved", f"${net_saved:,.2f}", delta_color="normal" if net_saved > 0 else "inverse")
            
            st.divider()
            
            # --- DRILL DOWN SECTION ---
            st.subheader("🔍 Deep Dive")
            
            # 1. Choose View
            view_mode = st.radio("View:", ["Discretionary Spending", "Fixed Bills", "Income"], horizontal=True)
            
            if view_mode == "Discretionary Spending": target_df = spend_df.copy()
            elif view_mode == "Fixed Bills": target_df = bill_df.copy()
            else: target_df = inc_df.copy()
            
            if not target_df.empty:
                target_df['amount'] = target_df['amount'].abs()
                
                # 2. Split Layout
                c1, c2 = st.columns([1, 2])
                
                with c1:
                    st.markdown("### 📊 Breakdown")
                    # Pie Chart
                    fig = px.pie(target_df, values='amount', names='category', hole=0.4)
                    fig.update_layout(margin=dict(t=0, b=0, l=0, r=0), height=300)
                    st.plotly_chart(fig, use_container_width=True)
                    
                    # Category Totals Table
                    st.caption("Totals by Category:")
                    cat_summary = target_df.groupby('category')['amount'].sum().sort_values(ascending=False).reset_index()
                    cat_summary.columns = ['Category', 'Total']
                    st.dataframe(cat_summary, hide_index=True, use_container_width=True, column_config={"Total": st.column_config.NumberColumn(format="$%.2f")})

                with c2:
                    st.markdown("### 🧾 Receipts")
                    
                    # 3. THE CATEGORY PICKER (The Feature You Asked For)
                    available_cats = sorted(target_df['category'].unique().tolist())
                    sel_cat = st.selectbox("📂 Filter by Category (Click to Drill Down):", ["Show All"] + available_cats)
                    
                    # Filter Logic
                    if sel_cat != "Show All":
                        display_df = target_df[target_df['category'] == sel_cat]
                    else:
                        display_df = target_df
                    
                    # Transaction Table
                    st.dataframe(
                        display_df[['date', 'name', 'category', 'amount']].sort_values('date', ascending=False),
                        hide_index=True,
                        use_container_width=True,
                        column_config={"amount": st.column_config.NumberColumn(format="$%.2f")}
                    )
            else:
                st.info(f"No {view_mode} found.")
