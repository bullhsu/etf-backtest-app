import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import google.generativeai as genai
from datetime import timedelta

# --- 0. 網頁設定 ---
st.set_page_config(page_title="AI 智能投資回測系統", layout="wide", page_icon="📈")

# --- 初始化 Session State ---
if "data_fetched" not in st.session_state:
    st.session_state.data_fetched = False
if "messages" not in st.session_state:
    st.session_state.messages = []
if "backtest_results" not in st.session_state:
    st.session_state.backtest_results = None
if "params" not in st.session_state:
    st.session_state.params = {}

# --- 頂部說明區 ---
st.title("🛡️ AI 核心定投 + 增強循環回測系統")

with st.expander("📖 第一次使用？點我看「策略說明書」"):
    st.markdown("""
    ### 這個策略是做什麼的？
    這是一個結合 **「長期穩健投資 (Beta)」** 與 **「短線擇時進攻 (Alpha)」** 的混合策略。
    
    #### 兩大主角：
    1.  **核心資產 (A)**：通常選 **VOO (標普500)**。負責保本與長期複利，採用分批定投。
    2.  **增強資產 (B)**：通常選 **QQQ** 或 **SPY**。負責在大跌時進場「撿便宜」，並模擬期權的高槓桿效果。

    #### ⚠️ 關鍵保護機制：
    * **趨勢濾網**：當價格跌破 **200日均線 (年線)** 時，視為空頭市場，**停止所有買入動作**。
    * **強制平倉**：B 資產若持有超過 **9 個月**，無論賺賠強制賣出。
    """)

# --- 1. 側邊欄：參數設定 ---
st.sidebar.header("1. 資金與時間設定")
start_date = st.sidebar.date_input("開始日期", pd.to_datetime("2015-01-01"))
end_date = st.sidebar.date_input("結束日期", pd.to_datetime("today"))
initial_capital = st.sidebar.number_input("初始總資金 (USD)", value=100000, step=10000)

st.sidebar.markdown("---")
st.sidebar.header("2. 資產配置 (Portfolio)")
ticker_core = st.sidebar.text_input("核心資產 (A)", value="VOO").upper()
weight_core = st.sidebar.slider("核心倉位佔比 (%)", 0, 100, 70)
ticker_sat = st.sidebar.text_input("增強資產 (B)", value="QQQ").upper()
weight_sat = st.sidebar.slider("增強倉位上限 (%)", 0, 100, 25)

# 計算每月定投金額
dca_months = 12
monthly_dca_amt = (initial_capital * (weight_core / 100)) / dca_months

st.sidebar.markdown("---")
st.sidebar.header("3. 再平衡模式")
rebalance_mode = st.sidebar.radio(
    "選擇資金運作邏輯",
    ("🚀 利潤滾雪球 (只進不出)", "⚖️ 固定比例再平衡 (嚴格執行 70/30)")
)
rebalance_freq = "無"
if "固定比例" in rebalance_mode:
    rebalance_freq = st.sidebar.selectbox("再平衡頻率", ["季 (Quarterly)", "半年 (Semi-Annually)", "年 (Annually)"])

st.sidebar.markdown("---")
st.sidebar.header("4. 進攻策略 (B資產)")
use_ma_filter = st.sidebar.checkbox("啟用 200MA 濾網", value=True)
leverage = st.sidebar.number_input("模擬槓桿倍數", value=3.0, min_value=1.0, step=0.5)
drop_threshold = st.sidebar.number_input("觸發買入跌幅 (%)", value=1.5, min_value=0.5, step=0.1)
batch_pct = st.sidebar.number_input("單次買入資金 (%)", value=3.0, step=0.5)

st.sidebar.subheader("止盈規則")
target_0_4 = st.sidebar.number_input("0~4 個月止盈 (%)", value=50)
target_5_6 = st.sidebar.number_input("5~6 個月止盈 (%)", value=30)
target_7_9 = st.sidebar.number_input("7~9 個月止盈 (%)", value=10)

# --- AI 設定區 ---
st.sidebar.divider()
st.sidebar.subheader("🤖 AI 投資顧問 (Gemini)")
google_api_key = st.sidebar.text_input("Google API Key", type="password", help="請輸入 Gemini API Key 以啟用分析功能")
st.sidebar.caption("還沒有 Key? [點此免費申請](https://aistudio.google.com/app/apikey)")

# --- 2. 工具函數 ---

@st.cache_data
def get_data(tickers, start, end):
    """下載數據"""
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
    if equity_series.empty: return 0, 0, 0
    total_return = (equity_series.iloc[-1] / equity_series.iloc[0]) - 1
    days = (equity_series.index[-1] - equity_series.index[0]).days
    years = days / 365.25
    cagr = (equity_series.iloc[-1] / equity_series.iloc[0]) ** (1/years) - 1 if years > 0 else 0
    rolling_max = equity_series.cummax()
    drawdown = (equity_series - rolling_max) / rolling_max
    max_dd = drawdown.min()
    return total_return, cagr, max_dd

# --- AI 對話函數 ---
def chat_with_ai(user_input, context_text=""):
    if not google_api_key:
        return "⚠️ 請先輸入 API Key。"
    
    try:
        genai.configure(api_key=google_api_key)
        # 嘗試使用最新的 Flash 模型
        model = genai.GenerativeModel('gemini-2.5-flash') 
        
        system_prompt = f"""
        你是一位專業的華爾街量化交易員。
        目前正在與使用者討論一個「核心定投 + 衛星動能」的策略回測結果。
        
        【當前策略數據背景】
        {context_text}
        
        請用繁體中文回答。回答要簡潔、專業，並基於上述數據。
        """
        
        messages = [{"role": "user", "parts": [system_prompt + "\n\n使用者問題: " + user_input]}]
        response = model.generate_content(messages)
        return response.text
    except Exception as e:
        return f"❌ AI 思考時發生錯誤: {str(e)} (請檢查 Key 或網路)"

# --- 處理使用者輸入的通用函數 ---
def handle_user_input(prompt_text):
    if not st.session_state.backtest_results:
        st.warning("請先執行回測！")
        return

    # 1. 顯示並儲存使用者訊息
    st.session_state.messages.append({"role": "user", "content": prompt_text})
    
    # 2. 準備 Context
    results = st.session_state.backtest_results
    context_str = f"策略數據: {results['metrics_dict']}, 年度報酬: {results['yearly_str']}"
    
    # 3. 呼叫 AI 並儲存回應
    ai_reply = chat_with_ai(prompt_text, context_str)
    st.session_state.messages.append({"role": "assistant", "content": ai_reply})

# --- 3. 核心回測邏輯 ---
def run_backtest(df, params):
    try:
        price_core = df[params['ticker_core']]
        price_sat = df[params['ticker_sat']]
    except KeyError:
        return pd.DataFrame()

    dates = df.index
    ma_trend = price_core.rolling(window=200).mean()
    
    history = []
    sat_batches = [] 
    
    cash_core = params['capital'] * (params['w_core'] / 100)
    cash_sat = params['capital'] * (params['w_sat'] / 100)
    cash_buffer = params['capital'] - cash_core - cash_sat
    
    shares_core = 0
    dca_count = 0
    last_dca_month = -1
    last_rebalance_month = dates[0].month
    
    rb_interval = 0
    if "季" in params['rb_freq']: rb_interval = 3
    elif "半年" in params['rb_freq']: rb_interval = 6
    elif "年" in params['rb_freq']: rb_interval = 12
    
    for i in range(1, len(dates)):
        today = dates[i]
        p_core = price_core.iloc[i]
        p_sat = price_sat.iloc[i]
        p_sat_yst = price_sat.iloc[i-1]
        
        val_sat_batches_temp = sum([b['cost'] * (1 + max((p_sat - b['entry_price'])/b['entry_price']*params['leverage'], -1.0)) for b in sat_batches])
        current_total_equity = (shares_core * p_core) + cash_core + val_sat_batches_temp + cash_sat + cash_buffer

        is_bull = True
        if params['use_ma'] and not pd.isna(ma_trend.iloc[i-1]):
            is_bull = price_core.iloc[i-1] > ma_trend.iloc[i-1]
        
        # 再平衡
        is_rebalance_day = False
        if "固定比例" in params['rb_mode'] and rb_interval > 0:
            if today.month != last_rebalance_month and (today.month % rb_interval == 0):
                is_rebalance_day = True
                last_rebalance_month = today.month

        if is_rebalance_day:
            target_core = current_total_equity * (params['w_core'] / 100)
            target_sat = current_total_equity * (params['w_sat'] / 100)
            
            current_core_total = (shares_core * p_core) + cash_core
            diff_core = target_core - current_core_total
            if diff_core < 0:
                sell_val = abs(diff_core)
                if cash_core >= sell_val: cash_core -= sell_val; cash_buffer += sell_val 
                else:
                    sell_shares_val = sell_val - cash_core; cash_core = 0
                    shares_to_sell = sell_shares_val / p_core
                    if shares_core >= shares_to_sell: shares_core -= shares_to_sell
                    cash_buffer += sell_shares_val

            current_sat_total = val_sat_batches_temp + cash_sat
            diff_sat = target_sat - current_sat_total
            if diff_sat < 0:
                sell_amount = abs(diff_sat)
                if cash_sat >= sell_amount: cash_sat -= sell_amount; cash_buffer += sell_amount
                else: cash_buffer += cash_sat; cash_sat = 0
            elif diff_sat > 0:
                amount_to_refill = min(diff_sat, cash_buffer) 
                cash_sat += amount_to_refill; cash_buffer -= amount_to_refill

        # Core DCA
        if today.month != last_dca_month:
            if dca_count < params['dca_months'] and is_bull and cash_core >= params['monthly_dca_amt']:
                shares_core += params['monthly_dca_amt'] / p_core
                cash_core -= params['monthly_dca_amt']
                dca_count += 1
            last_dca_month = today.month
            
        if "利潤滾雪球" in params['rb_mode']:
            needed = (params['dca_months'] - dca_count) * params['monthly_dca_amt']
            surplus = cash_core - needed
            if surplus > 100 and is_bull: shares_core += surplus / p_core; cash_core -= surplus
        
        # Sat Trading
        if "固定比例" in params['rb_mode']: batch_amt = current_total_equity * (params['batch_pct'] / 100)
        else: batch_amt = params['capital'] * (params['batch_pct'] / 100)

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
                if "固定比例" in params['rb_mode']: cash_sat += ret_total
                else: cash_sat += min(ret_total, batch['cost']); 
                if "利潤滾雪球" in params['rb_mode'] and profit > 0: cash_core += profit

                sat_batches.remove(batch)

        drop = (p_sat / p_sat_yst) - 1
        is_drop = (drop * 100) < -params['drop_thresh']
        cost_sat = sum(b['cost'] for b in sat_batches)
        if "固定比例" in params['rb_mode']: max_sat = current_total_equity * (params['w_sat'] / 100)
        else: max_sat = params['capital'] * (params['w_sat'] / 100)
            
        if is_bull and is_drop and (cost_sat + batch_amt <= max_sat) and (cash_sat >= batch_amt):
            sat_batches.append({'date': today, 'entry_price': p_sat, 'cost': batch_amt})
            cash_sat -= batch_amt

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

# 觸發回測的按鈕
if st.sidebar.button("🚀 執行完整分析 (點我)", type="primary"):
    st.session_state.data_fetched = True
    st.session_state.messages = [] # 清空舊對話
    
    tickers = list(set([ticker_core, ticker_sat]))
    with st.spinner("正在下載數據與運算..."):
        df_data = get_data(tickers, start_date, end_date)
        if df_data.empty:
            st.error("❌ 下載失敗，請檢查股票代號。")
            st.session_state.data_fetched = False
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
            # 執行回測
            res = run_backtest(df_data, params)
            
            if not res.empty:
                # 計算 Benchmark
                bench_prices = df_data[ticker_core].loc[res.index]
                bench_equity = bench_prices * (initial_capital / bench_prices.iloc[0])
                strat_m = calculate_metrics(res['Total Asset'])
                bench_m = calculate_metrics(bench_equity)
                
                # 準備 AI Context
                res['Year'] = res.index.year
                yearly_ret = res['Total Asset'].resample('YE').last().pct_change()
                if yearly_ret.empty: yearly_ret = res['Total Asset'].resample('Y').last().pct_change()
                yearly_str = str(yearly_ret.tail(5).multiply(100).round(1).to_dict()) 
                
                metrics_dict = {
                    'total_ret': strat_m[0]*100, 'bench_ret': bench_m[0]*100,
                    'cagr': strat_m[1]*100, 'mdd': strat_m[2]*100
                }
                
                # 存入 Session State
                st.session_state.backtest_results = {
                    'res': res, 'bench_equity': bench_equity,
                    'strat_m': strat_m, 'bench_m': bench_m,
                    'metrics_dict': metrics_dict, 'yearly_str': yearly_str
                }
                st.session_state.params = params

                # 自動產生第一則 AI 分析
                if google_api_key:
                    initial_prompt = f"請分析此策略結果：MDD {metrics_dict['mdd']:.1f}%, CAGR {metrics_dict['cagr']:.1f}%, 近五年績效 {yearly_str}。"
                    with st.spinner("🤖 AI 正在撰寫初次報告..."):
                        ai_reply = chat_with_ai(initial_prompt, str(metrics_dict))
                        st.session_state.messages.append({"role": "assistant", "content": ai_reply})

# --- 5. 顯示結果區 (依賴 Session State) ---

if st.session_state.data_fetched and st.session_state.backtest_results:
    results = st.session_state.backtest_results
    res = results['res']
    strat_m = results['strat_m']
    bench_m = results['bench_m']
    params = st.session_state.params
    
    st.divider()
    st.subheader(f"📊 回測結果分析")
    
    # 指標
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("最終資產 (USD)", f"${res['Total Asset'].iloc[-1]:,.0f}")
    c2.metric("總報酬率", f"{strat_m[0]*100:.1f}%", f"{(strat_m[0]-bench_m[0])*100:.1f}% vs B&H")
    c3.metric("年化報酬 (CAGR)", f"{strat_m[1]*100:.1f}%")
    c4.metric("最大回撤 (MDD)", f"{strat_m[2]*100:.1f}%", delta=f"{(strat_m[2]-bench_m[2])*100:.1f}%", delta_color="inverse")
    
    # 圖表 1: 權益曲線
    st.subheader("📈 資產成長曲線")
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=res.index, y=res['Total Asset'], name="你的策略", line=dict(color='blue', width=2)))
    fig.add_trace(go.Scatter(x=results['bench_equity'].index, y=results['bench_equity'], name="基準 (Buy&Hold)", line=dict(color='gray', dash='dot')))
    st.plotly_chart(fig, use_container_width=True)

    # 圖表 2: 資產堆疊圖
    st.subheader("💰 資產堆疊圖 (資金流向)")
    fig_stack = go.Figure()
    fig_stack.add_trace(go.Scatter(x=res.index, y=res['Core Invested'], name=f"A 持倉 ({params['ticker_core']})", stackgroup='one', fillcolor='rgba(0,0,255,0.5)'))
    fig_stack.add_trace(go.Scatter(x=res.index, y=res['Core Cash'], name="A 現金", stackgroup='one', fillcolor='rgba(173,216,230,0.5)'))
    fig_stack.add_trace(go.Scatter(x=res.index, y=res['Sat Invested'], name=f"B 持倉 ({params['ticker_sat']})", stackgroup='one', fillcolor='rgba(0,128,0,0.6)'))
    fig_stack.add_trace(go.Scatter(x=res.index, y=res['Sat Cash'], name="B 現金", stackgroup='one', fillcolor='rgba(144,238,144,0.3)'))
    st.plotly_chart(fig_stack, use_container_width=True)
    
    # --- 6. AI 對話介面 ---
    st.divider()
    st.subheader("🤖 AI 投資顧問對話區")

    # 1. 先顯示歷史訊息 (這樣新訊息會在舊訊息下方)
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # 2. 快捷提問按鈕 (✅ 移動到這裡，歷史訊息的下方，輸入框的上方)
    st.markdown("---") # 加個分隔線區隔一下歷史訊息
    st.caption("💡 點擊下方按鈕可快速提問：")
    btn_col1, btn_col2, btn_col3 = st.columns(3)
    
    # 按鈕邏輯：點擊後立刻執行並強制刷新頁面，讓對話顯示出來
    if btn_col1.button("🛡️ 評估此策略的風險"):
        handle_user_input("請評估這個策略的風險水平，MDD 是否過高？有什麼潛在危機？")
        st.rerun()
    if btn_col2.button("💰 如何提高報酬率？"):
        handle_user_input("如果我想讓獲利更高，建議調整哪些參數？（例如槓桿或倉位）")
        st.rerun()
    if btn_col3.button("📉 2022年表現分析"):
        handle_user_input("請詳細分析 2022 年大空頭時，此策略的表現與原因。")
        st.rerun()

    # 3. 聊天輸入框 (永遠固定在最下方)
    if prompt := st.chat_input("💬 請輸入您的問題..."):
        handle_user_input(prompt)
        st.rerun()

elif st.session_state.data_fetched and not st.session_state.backtest_results:
    st.warning("⚠️ 沒有數據，請檢查代號或日期。")
