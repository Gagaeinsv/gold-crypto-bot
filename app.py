import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
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
    
    # ─── Category Selector at the Top ───
    st.subheader("🎯 Категорія сигналів")
    
    source_options = {
        "🟢 Всі сигнали": "All",
        "📢 Безкоштовні (Канал)": "ai",
        "💎 Преміум (Бот)": "user",
        "🛠️ Ручні / Тестові": "manual"
    }
    
    selected_label = st.radio(
        "Оберіть категорію для перегляду аналітики:",
        options=list(source_options.keys()),
        index=0,
        horizontal=True
    )
    selected_source = source_options[selected_label]
    
    # Filter main dataframe based on category choice
    if selected_source != "All":
        filtered_source_df = df[df['source'] == selected_source].copy()
    else:
        filtered_source_df = df.copy()

    # Dynamic metrics calculation based on selection
    def calculate_live_metrics(m_df):
        if m_df.empty:
            return {"win_rate": 0.0, "cumulative_weekly_pnl": 0.0, "win_streak": 0}
            
        closed_m_df = m_df[m_df['status'] == 'CLOSED'].copy()
        total_closed = len(closed_m_df)
        
        # 1. Win Rate
        if total_closed > 0:
            wins = len(closed_m_df[closed_m_df['pnl_percentage'] > 0])
            win_rate = (wins / total_closed) * 100.0
        else:
            win_rate = 0.0
            
        # 2. 7-Day Cumulative PnL
        seven_days_ago = datetime.utcnow() - timedelta(days=7)
        recent_closed = closed_m_df[closed_m_df['closed_at'] >= seven_days_ago]
        cumulative_weekly_pnl = recent_closed['pnl_percentage'].sum()
        
        # 3. Active Win Streak
        streak_df = closed_m_df.sort_values(by=['closed_at', 'id'], ascending=[False, False])
        win_streak = 0
        for pnl in streak_df['pnl_percentage']:
            if pnl is not None and pnl > 0:
                win_streak += 1
            else:
                break
                
        return {
            "win_rate": round(win_rate, 2),
            "cumulative_weekly_pnl": round(cumulative_weekly_pnl, 2),
            "win_streak": win_streak
        }

    metrics = calculate_live_metrics(filtered_source_df)

    # Render Metric Cards
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric(
            label="Overall Win Rate", 
            value=f"{metrics['win_rate']}%",
            help="Percentage of closed trades with positive PnL in this category"
        )
    with col2:
        st.metric(
            label="7-Day Cumulative PnL", 
            value=f"{metrics['cumulative_weekly_pnl']}%",
            delta=f"{metrics['cumulative_weekly_pnl']}%" if metrics['cumulative_weekly_pnl'] != 0 else None,
            help="Sum of PnL percentages for trades closed in the last 7 days in this category"
        )
    with col3:
        st.metric(
            label="Active Win Streak", 
            value=metrics['win_streak'],
            help="Consecutive winning closed trades counting backwards from the latest closed trade in this category"
        )

    st.write("---")

    # Cumulative PnL Line Chart
    st.subheader("📈 Cumulative PnL Growth Over Time")
    closed_df = filtered_source_df[filtered_source_df['status'] == 'CLOSED'].copy()
    if closed_df.empty:
        st.info("No closed trades to compute cumulative PnL chart in this category.")
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
    filtered_df = filtered_source_df.copy()
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

        # Map source to a human-readable name
        display_df['source'] = display_df['source'].map({
            'ai': '📢 Free (Channel)',
            'user': '💎 Premium (Bot)',
            'manual': '🛠️ Manual (Test)'
        })

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
                "closed_at": "Closed At",
                "source": "Category"
            }
        )
