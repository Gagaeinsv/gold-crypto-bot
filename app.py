import streamlit as st
import pandas as pd
from datetime import datetime
from services.storage_engine import StorageEngine

st.set_page_config(
    page_title="Trading Analytics Dashboard",
    page_icon="📊",
    layout="wide"
)

# Custom header
st.title("📊 Trading Analytics Dashboard (Sprint 0)")
st.write("Real-time monitoring of parsed signals and trade performance metrics.")

# Initialize SQLite storage engine
db = StorageEngine()

# Sidebar panel for manual testing/injection
st.sidebar.header("🔧 Admin Control Panel")
st.sidebar.write("Manually inject trading signals for system testing:")

test_asset = st.sidebar.text_input("Asset Ticker", value="BTCUSDT").upper().strip()
test_dir = st.sidebar.selectbox("Direction", options=["BUY", "SELL"])
test_price = st.sidebar.number_input("Entry Price", min_value=0.0, value=95000.0, step=0.01)

if st.sidebar.button("Inject Test Trade"):
    if test_asset:
        trade_id = db.save_trade(test_asset, test_dir, test_price)
        st.sidebar.success(f"Trade #{trade_id} injected successfully!")
        st.rerun()
    else:
        st.sidebar.error("Asset name cannot be empty.")

# Fetch computed analytics metrics
metrics = db.get_metrics()

# Render Metric Cards
col1, col2, col3 = st.columns(3)
with col1:
    st.metric(
        label="Overall Win Rate", 
        value=f"{metrics['win_rate']}%",
        help="Percentage of closed trades with positive PnL"
    )
with col2:
    st.metric(
        label="7-Day Cumulative PnL", 
        value=f"{metrics['cumulative_weekly_pnl']}%",
        delta=f"{metrics['cumulative_weekly_pnl']}%" if metrics['cumulative_weekly_pnl'] != 0 else None,
        help="Sum of PnL percentages for trades closed in the last 7 days"
    )
with col3:
    st.metric(
        label="Active Win Streak", 
        value=metrics['win_streak'],
        help="Consecutive winning closed trades counting backwards from the latest closed trade"
    )

st.write("---")

# Fetch all historical trades
trades = db.get_all_trades()
df = pd.DataFrame(trades)

# If database is empty
if df.empty:
    st.info("No trades found in the database. Use the sidebar to inject some test trades or configure the Telegram Ingestion Engine.")
else:
    # Convert columns to datetime
    df['created_at'] = pd.to_datetime(df['created_at'])
    df['closed_at'] = pd.to_datetime(df['closed_at'])
    
    # Cumulative PnL Line Chart
    st.subheader("📈 Cumulative PnL Growth Over Time")
    closed_df = df[df['status'] == 'CLOSED'].copy()
    if closed_df.empty:
        st.info("No closed trades to compute cumulative PnL chart.")
    else:
        # Sort chronologically to calculate cumulative PnL sum
        closed_df = closed_df.sort_values('closed_at')
        closed_df['Cumulative PnL (%)'] = closed_df['pnl_percentage'].cumsum()
        
        # Plot native line chart
        chart_data = closed_df.set_index('closed_at')[['Cumulative PnL (%)']]
        st.line_chart(chart_data)

    # Search & Filter Area
    st.subheader("📋 Trade Log & Analytics Table")
    
    f_col1, f_col2, f_col3 = st.columns(3)
    with f_col1:
        search_query = st.text_input("🔍 Search by Asset Ticker", value="").upper().strip()
    with f_col2:
        status_filter = st.selectbox("Status", options=["All", "OPEN", "CLOSED"])
    with f_col3:
        direction_filter = st.selectbox("Direction", options=["All", "BUY", "SELL"])

    # Apply filters
    filtered_df = df.copy()
    if search_query:
        filtered_df = filtered_df[filtered_df['asset'].str.contains(search_query, na=False)]
    if status_filter != "All":
        filtered_df = filtered_df[filtered_df['status'] == status_filter]
    if direction_filter != "All":
        filtered_df = filtered_df[filtered_df['direction'] == direction_filter]

    if filtered_df.empty:
        st.warning("No trades match the selected search/filter criteria.")
    else:
        # Format table columns for visual presentation
        display_df = filtered_df.copy()
        display_df['entry_price'] = display_df['entry_price'].map('{:,.4f}'.format)
        display_df['exit_price'] = display_df['exit_price'].apply(lambda x: f"{x:,.4f}" if pd.notnull(x) else "—")
        display_df['pnl_percentage'] = display_df['pnl_percentage'].apply(lambda x: f"{x:+.2f}%" if pd.notnull(x) else "—")
        display_df['created_at'] = display_df['created_at'].dt.strftime('%Y-%m-%d %H:%M:%S')
        display_df['closed_at'] = display_df['closed_at'].apply(lambda x: x.strftime('%Y-%m-%d %H:%M:%S') if pd.notnull(x) else "—")

        # Cell styling helper for highlighting rows/cells
        def style_rows(row):
            val = filtered_df.loc[row.name, 'pnl_percentage']
            status = filtered_df.loc[row.name, 'status']
            styles = [''] * len(row)
            
            pnl_idx = row.index.get_loc('pnl_percentage')
            status_idx = row.index.get_loc('status')
            
            if status == 'CLOSED':
                if val > 0:
                    styles[pnl_idx] = 'background-color: rgba(46, 117, 89, 0.35); color: #4ade80;' # Soft green
                elif val < 0:
                    styles[pnl_idx] = 'background-color: rgba(185, 28, 28, 0.25); color: #f87171;' # Soft red
            else:
                styles[status_idx] = 'background-color: rgba(30, 58, 138, 0.4); color: #60a5fa;' # Soft blue
                
            return styles

        # Display dataframe
        st.dataframe(
            display_df.style.apply(style_rows, axis=1),
            use_container_width=True,
            column_config={
                "id": st.column_config.NumberColumn("ID"),
                "asset": "Asset",
                "direction": "Direction",
                "entry_price": "Entry Price",
                "exit_price": "Exit Price",
                "pnl_percentage": "PnL %",
                "status": "Status",
                "created_at": "Opened At",
                "closed_at": "Closed At"
            }
        )
