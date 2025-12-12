import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from datetime import timedelta

# --- 0. 網頁設定 ---
st.set_page_config(page_title="ETF 策略回測 - 完整說明版", layout="wide")

# --- 頂部說明區 ---
st.title("🛡️ 核心定投 + 增強循環回測系統")

with st.expander("📖 第一次使用？點我看「策略說明書」"):
    st.markdown("""
    ### 這個策略是做什麼的？
    這是一個結合 **「長期穩健投資 (Beta)」** 與 **「短線擇時進攻 (Alpha)」** 的混合策略。
    
    #### 兩大主角：
    1.  **核心資產 (A)**：通常選 **VOO (標普500)**。負責保本與長期複利，採用分批定投。
    2.  **增強資產 (B)**：通常選 **QQQ** 或 **SPY**。負責在大跌時進場「撿便宜」，並模擬期權的高槓桿效果。

    #### 兩種模式選擇：
    * **🚀 利潤滾雪球模式**：B 賺到的錢，全部拿去買 A。A 只進不出，適合想極大化長期資產的人。
    * **⚖️ 固定比例再平衡模式**：強迫維持 70% A + 30% B。資產配置較固定，適合風險控管嚴格的人。
    
    #### ⚠️ 關鍵保護機制 (隱藏規則)：
    * **趨勢濾網**：當價格跌破 **200日均線 (年線)** 時，視為空頭市場，**停止所有買入動作** (只賣不買)。
    * **強制平倉**：為了避免期權時間價值耗損，B 資產若持有超過 **10 個月**，無論賺賠強制賣出。
    """)

# --- 1. 側邊欄：參數設定 ---
st.sidebar.header("1. 資金與時間設定")
start_date = st.sidebar.date_input("開始日期", pd.to_datetime("2015-01-01"))
end_date = st.sidebar.date_input("結束日期", pd.to_datetime("today"))
initial_capital = st.sidebar.number_input("初始總資金 (USD)", value=100000, step=10000, help="回測開始時的本金，例如 10 萬美金。")

st.sidebar.markdown("---")
st.sidebar.header("2. 資產配置 (Portfolio)")

ticker_core = st.sidebar.text_input("核心資產 (A)", value="VOO", help="建議輸入穩健的大盤 ETF，如 VOO, SPY, VTI。").upper()
weight_core = st.sidebar.slider("核心倉位佔比 (%)", 0, 100, 70, help="資金的多少比例分配給 A 資產。")

ticker_sat = st.sidebar.text_input("增強資產 (B)", value="QQQ", help="建議輸入波動較大的成長型 ETF，如 QQQ, TQQQ, SOXL。").upper()
weight_sat = st.sidebar.slider("增強倉位上限 (%)", 0, 100, 25, help="最多用多少比例的資金來做 B 資產的操作。")

# 計算每月定投金額
dca_months = 12
monthly_dca_amt = (initial_capital * (weight_core / 100)) / dca_months

st.sidebar.markdown("---")
st.sidebar.header("3. 再平衡模式 (Rebalance)")
rebalance_mode = st.sidebar.radio(
    "選擇資金運作邏輯",
    ("🚀 利潤滾雪球 (原本模式 - 只進不出)", "⚖️ 固定比例再平衡 (嚴格執行 70/30)"),
    help="「利潤滾雪球」會把 B 賺的錢轉去買 A；「固定比例」則會定期賣強買弱，維持固定佔比。"
)

rebalance_freq = "無"
if "固定比例" in rebalance_mode:
    rebalance_freq = st.sidebar.selectbox("再平衡頻率", ["季 (Quarterly)", "半年 (Semi-Annually)", "年 (Annually)"], help="多久執行一次強制調整比例。建議選擇「半年」或「季」。")

st.sidebar.markdown("---")
st.sidebar.header("4. 進攻策略 (B資產)")
st.sidebar.caption("這裡模擬「大跌時買入 Call 期權」的行為")

use_ma_filter = st.sidebar.checkbox("啟用 200MA 濾網", value=True, help="強烈建議勾選！跌破年線時停止買入，能避開像 2008 或 2022 這種大空頭。")

leverage = st.sidebar.number_input("模擬槓桿倍數", value=4.0, min_value=1.0, step=0.5, help="1.0 代表買現貨。4.0 代表模擬期權，漲跌幅會放大 4 倍。")
drop_threshold = st.sidebar.number_input("觸發買入跌幅 (%)", value=1.5, min_value=0.5, step=0.1, help="B 資產單日跌超過這個幅度，才視為「大跌」，進場撿便宜。")
batch_pct = st.sidebar.number_input("單次買入資金 (%)", value=3.0, step=0.5, help="每次訊號出現時，投入總資金的多少百分比。")

st.sidebar.subheader("止盈/出場規則")
target_0_4 = st.sidebar.number_input("0~4 個月止盈 (%)", value=50, help="持有初期，獲利達此目標即賣出。")
target_5_6 = st.sidebar.number_input("5~6 個月止盈 (%)", value=30, help="持有中期，降低獲利目標。")
target_7_9 = st.sidebar.number_input("7~9 個月止盈 (%)", value=10, help="持有後期，只要有賺就跑。")

# 顯示隱藏規則提醒
st.sidebar.info("""
**📜 內建強制規則 (不可改)：**
1. **時間止損**：B 資產若持有超過 **9 個月**，無論賺賠強制賣出 (模擬期權到期)。
2. **趨勢濾網**：若勾選 200MA，跌破年線時**只賣不買**。
""")

# --- 2. 工具函數 ---

@st.cache_data
def get_data(tickers, start, end):
    """下載數據並處理格式問題"""
    df = yf.download(tickers, start=start, end=end, auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        try:
            if 'Close' in df.columns:
                df = df['Close']
            elif 'Close' in df.columns.get_level_values(0): 
                 df = df.xs('Close', axis=1, level=0)
        except: pass
    if len(tickers) == 1 and 'Close' in df.columns:
        df = df[['Close']]
        df.columns = tickers
    return df

def calculate_metrics(equity_series):
    """計算 CAGR, MDD 等指標"""
    total_return = (equity_series.iloc[-1] / equity_series.iloc[0]) - 1
    days = (equity_series.index[-1] - equity_series.index[0]).days
    years = days / 365.25
    cagr = (equity_series.iloc[-1] / equity_series.iloc[0]) ** (1/years) - 1 if years > 0 else 0
    rolling_max = equity_series.cummax()
    drawdown = (equity_series - rolling_max) / rolling_max
    max_dd = drawdown.min()
    return total_return, cagr, max_dd

# --- 3. 核心回測邏輯 ---

def run_backtest(df, params):
    try:
        price_core = df[params['ticker_core']]
        price_sat = df[params['ticker_sat']]
    except: return pd.DataFrame()

    dates = df.index
    ma_trend = price_core.rolling(window=200).mean()
    
    history = []
    sat_batches = [] 
    
    # 資金池初始化
    cash_core = params['capital'] * (params['w_core'] / 100)
    cash_sat = params['capital'] * (params['w_sat'] / 100)
    cash_buffer = params['capital'] - cash_core - cash_sat
    
    shares_core = 0
    dca_count = 0
    last_dca_month = -1
    last_rebalance_month = dates[0].month
    
    # 決定再平衡月份間隔
    rb_interval = 0
    if "季" in params['rb_freq']: rb_interval = 3
    elif "半年" in params['rb_freq']: rb_interval = 6
    elif "年" in params['rb_freq']: rb_interval = 12
    
    for i in range(1, len(dates)):
        today = dates[i]
        p_core = price_core.iloc[i]
        p_sat = price_sat.iloc[i]
        p_sat_yst = price_sat.iloc[i-1]
        
        # 0. 預先計算當前總資產
        val_sat_batches_temp = sum([b['cost'] * (1 + max((p_sat - b['entry_price'])/b['entry_price']*params['leverage'], -1.0)) for b in sat_batches])
        current_total_equity = (shares_core * p_core) + cash_core + val_sat_batches_temp + cash_sat + cash_buffer

        # MA Filter (趨勢判斷)
        is_bull = True
        if params['use_ma'] and not pd.isna(ma_trend.iloc[i-1]):
            is_bull = price_core.iloc[i-1] > ma_trend.iloc[i-1]
        
        # --- 1. 定期再平衡邏輯 ---
        is_rebalance_day = False
        if "固定比例" in params['rb_mode'] and rb_interval > 0:
            if today.month != last_rebalance_month and (today.month % rb_interval == 0):
                is_rebalance_day = True
                last_rebalance_month = today.month

        if is_rebalance_day:
            target_core = current_total_equity * (params['w_core'] / 100)
            target_sat = current_total_equity * (params['w_sat'] / 100)
            
            # A. 調整 Core
            current_core_total = (shares_core * p_core) + cash_core
            diff_core = target_core - current_core_total
            
            if diff_core < 0: # 賣出 A
                sell_val = abs(diff_core)
                if cash_core >= sell_val:
                    cash_core -= sell_val
                    cash_buffer += sell_val 
                else:
                    sell_shares_val = sell_val - cash_core
                    cash_core = 0
                    shares_to_sell = sell_shares_val / p_core
                    if shares_core >= shares_to_sell:
                        shares_core -= shares_to_sell
                    cash_buffer += sell_shares_val

            # B. 調整 Sat
            current_sat_total = val_sat_batches_temp + cash_sat
            diff_sat = target_sat - current_sat_total
            
            if diff_sat < 0: # 賣出 B (抽走現金)
                sell_amount = abs(diff_sat)
                if cash_sat >= sell_amount:
                    cash_sat -= sell_amount
                    cash_buffer += sell_amount
                else:
                    cash_buffer += cash_sat
                    cash_sat = 0
            elif diff_sat > 0: # 補錢給 B
                amount_to_refill = min(diff_sat, cash_buffer) 
                cash_sat += amount_to_refill
                cash_buffer -= amount_to_refill

        # --- 2. 核心 DCA ---
        if today.month != last_dca_month:
            if dca_count < params['dca_months'] and is_bull and cash_core >= params['monthly_dca_amt']:
                shares_core += params['monthly_dca_amt'] / p_core
                cash_core -= params['monthly_dca_amt']
                dca_count += 1
            last_dca_month = today.month
            
        if "原本模式" in params['rb_mode']:
            needed = (params['dca_months'] - dca_count) * params['monthly_dca_amt']
            surplus = cash_core - needed
            if surplus > 100 and is_bull:
                shares_core += surplus / p_core
                cash_core -= surplus
        
        # --- 3. 增強倉位交易 ---
        if "固定比例" in params['rb_mode']:
            batch_amt = current_total_equity * (params['batch_pct'] / 100)
        else:
            batch_amt = params['capital'] * (params['batch_pct'] / 100)

        # A. 止盈
        for batch in sat_batches[::-1]:
            days = (today - batch['date']).days
            months = days / 30.0
            raw_ret = (p_sat - batch['entry_price']) / batch['entry_price']
            lev_ret = raw_ret * params['leverage']
            
            sell = False
            if months > 9: sell = True
            elif months <= 4 and lev_ret * 100 > params['tg_0_4']: sell = True
            elif 4 < months <= 6 and lev_ret * 100 > params['tg_5_6']: sell = True
            elif 6 < months <= 9 and lev_ret * 100 > params['tg_7_9']: sell = True
            
            if sell:
                final_ret = max(lev_ret, -1.0)
                ret_total = batch['cost'] * (1 + final_ret)
                profit = ret_total - batch['cost']
                
                if "固定比例" in params['rb_mode']:
                    cash_sat += ret_total
                else:
                    cash_sat += min(ret_total, batch['cost'])
                    if profit > 0: cash_core += profit
                
                sat_batches.remove(batch)

        # B. 買入
        drop = (p_sat / p_sat_yst) - 1
        is_drop = (drop * 100) < -params['drop_thresh']
        cost_sat = sum(b['cost'] for b in sat_batches)
        
        if "固定比例" in params['rb_mode']:
            max_sat = current_total_equity * (params['w_sat'] / 100)
        else:
            max_sat = params['capital'] * (params['w_sat'] / 100)
            
        if is_bull and is_drop and (cost_sat + batch_amt <= max_sat) and (cash_sat >= batch_amt):
            sat_batches.append({'date': today, 'entry_price': p_sat, 'cost': batch_amt})
            cash_sat -= batch_amt

        # --- 4. 紀錄 ---
        val_sat_inv = sum([b['cost'] * (1 + max((p_sat - b['entry_price'])/b['entry_price']*params['leverage'], -1.0)) for b in sat_batches])
        val_core = shares_core * p_core
        total = val_core + cash_core + val_sat_inv + cash_sat + cash_buffer
        
        history.append({
            'Date': today, 'Total Asset': total,
            'Core Invested': val_core, 'Core Cash': cash_core,
            'Sat Invested': val_sat_inv, 'Sat Cash': cash_sat
        })

    return pd.DataFrame(history).set_index('Date')

# --- 4. 主程式執行 ---

if st.sidebar.button("🚀 執行完整分析 (點我)", type="primary"):
    tickers = list(set([ticker_core, ticker_sat]))
    with st.spinner("正在下載數據與運算... (請稍等約 5-10 秒)"):
        df_data = get_data(tickers, start_date, end_date)
        if df_data.empty:
            st.error("❌ 下載失敗，請檢查股票代號是否正確。")
        else:
            params = {
                'ticker_core': ticker_core, 'ticker_sat': ticker_sat,
                'capital': initial_capital, 'w_core': weight_core, 'w_sat': weight_sat,
                'dca_months': dca_months, 'monthly_dca_amt': monthly_dca_amt,
                'use_ma': use_ma_filter, 'leverage': leverage,
                'drop_thresh': drop_threshold, 'batch_pct': batch_pct,
                'tg_0_4': target_0_4, 'tg_5_6': target_5_6, 'tg_7_9': target_7_9,
                'rb_mode': rebalance_mode, 'rb_freq': rebalance_freq
            }
            
            res = run_backtest(df_data, params)
            
            if not res.empty:
                # 基準比較
                bench_prices = df_data[ticker_core].loc[res.index]
                bench_equity = bench_prices * (initial_capital / bench_prices.iloc[0])
                
                strat_m = calculate_metrics(res['Total Asset'])
                bench_m = calculate_metrics(bench_equity)
                
                # --- 結果顯示 ---
                st.divider()
                st.subheader(f"📊 回測結果分析 ({rebalance_mode})")
                
                # 指標卡片
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("最終資產 (USD)", f"${res['Total Asset'].iloc[-1]:,.0f}", help="回測結束時的總金額")
                c2.metric("總報酬率", f"{strat_m[0]*100:.1f}%", f"{(strat_m[0]-bench_m[0])*100:.1f}% vs B&H", help="策略總共賺了多少 % (下方小字是跟「單純買入持有」相比)")
                c3.metric("年化報酬率 (CAGR)", f"{strat_m[1]*100:.1f}%", help="平均每年的複利成長率")
                c4.metric("最大回撤 (Risk)", f"{strat_m[2]*100:.1f}%", f"{(strat_m[2]-bench_m[2])*100:.1f}%", delta_color="inverse", help="歷史上最慘曾經從高點跌掉多少 % (數字越小越好)")
                
                # 圖表 1: 權益曲線
                st.subheader("📈 資產成長曲線")
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=res.index, y=res['Total Asset'], name="你的策略", line=dict(color='blue', width=2)))
                fig.add_trace(go.Scatter(x=bench_equity.index, y=bench_equity, name=f"基準 (All-in {ticker_core})", line=dict(color='gray', dash='dot')))
                st.plotly_chart(fig, use_container_width=True)
                st.caption("💡 **解讀**：藍線如果高於灰線，代表策略跑贏了大盤。觀察藍線在 2020 或 2022 年是否比灰線更平穩，這代表抗跌能力。")
                
                # 圖表 2: 堆疊圖
                st.subheader("💰 資產堆疊圖 (資金流向)")
                fig_stack = go.Figure()
                fig_stack.add_trace(go.Scatter(x=res.index, y=res['Core Invested'], name=f"A 持倉 ({ticker_core})", stackgroup='one', fillcolor='rgba(0,0,255,0.5)'))
                fig_stack.add_trace(go.Scatter(x=res.index, y=res['Core Cash'], name="A 待投現金/利潤", stackgroup='one', fillcolor='rgba(173,216,230,0.5)'))
                fig_stack.add_trace(go.Scatter(x=res.index, y=res['Sat Invested'], name=f"B 持倉 ({ticker_sat})", stackgroup='one', fillcolor='rgba(0,128,0,0.6)'))
                fig_stack.add_trace(go.Scatter(x=res.index, y=res['Sat Cash'], name="B 閒置現金", stackgroup='one', fillcolor='rgba(144,238,144,0.3)'))
                st.plotly_chart(fig_stack, use_container_width=True)
                st.markdown("""
                **💡 圖表解讀：**
                * **深藍色區域 (A)**：你的核心資產部位。在「滾雪球模式」下，這裡應該會越來越大。
                * **深綠色區域 (B)**：你的進攻部位。這塊區域呈現鋸齒狀是正常的（代表買進後止盈賣出）。
                * **淺綠色區域 (B現金)**：若這塊很大，代表手上有很多子彈沒打出去（可能是買入條件太嚴苛，或剛再平衡賣出）。
                """)

            else:
                st.warning("回測區間內沒有數據，請嘗試調整日期。")