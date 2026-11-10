import os
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import warnings
warnings.filterwarnings("ignore")

# ==========================================
# 1. 讀取 Telegram 設定
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "請換成你的_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "請換成你的_CHAT_ID")

# ==========================================
# 2. 自動取得台股(股價>=10元，擴大母體)與美股清單
# ==========================================
def get_auto_stock_list():
    stock_dict = {}
    
    # A. 台股【上市】：股價 >= 10 元，改依「成交金額(TradeValue)」取前 200 大，避免漏掉量縮整理股
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=15)
        if res.status_code == 200:
            twse_data = pd.DataFrame(res.json())
            twse_data = twse_data[twse_data['Code'].str.match(r'^[1-9]\d{3}$')]
            twse_data['ClosingPrice'] = pd.to_numeric(twse_data['ClosingPrice'].str.replace(',', ''), errors='coerce')
            twse_data['TradeValue'] = pd.to_numeric(twse_data['TradeValue'].str.replace(',', ''), errors='coerce')
            twse_top = twse_data[twse_data['ClosingPrice'] >= 10].sort_values(by='TradeValue', ascending=False).head(200)
            for _, row in twse_top.iterrows():
                stock_dict[f"{row['Code']}.TW"] = row['Name'].strip()
    except Exception as e:
        print(f"⚠️ 上市清單抓取異常: {e}")

    # B. 台股【上櫃】：股價 >= 10 元，取前 100 大
    try:
        res_otc = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", timeout=15)
        if res_otc.status_code == 200:
            tpex_data = pd.DataFrame(res_otc.json())
            tpex_data = tpex_data[tpex_data['SecuritiesCompanyCode'].str.match(r'^[1-9]\d{3}$')]
            tpex_data['Close'] = pd.to_numeric(tpex_data['Close'].str.replace(',', ''), errors='coerce')
            tpex_data['TransactionAmount'] = pd.to_numeric(tpex_data['TransactionAmount'].str.replace(',', ''), errors='coerce')
            tpex_top = tpex_data[tpex_data['Close'] >= 10].sort_values(by='TransactionAmount', ascending=False).head(100)
            for _, row in tpex_top.iterrows():
                stock_dict[f"{row['SecuritiesCompanyCode']}.TWO"] = row['CompanyName'].strip()
    except Exception as e:
        print(f"⚠️ 上櫃清單抓取異常: {e}")

    # C. 美股核心科技與動能領頭羊 (擴充至 40 檔)
    us_leaders = [
        "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA", "AVGO", "AMD", "PLTR",
        "NFLX", "CRM", "PANW", "CRWD", "NOW", "ANET", "MU", "ARM", "SMCI", "APP",
        "VST", "CEG", "GEV", "UBER", "SHOP", "SPOT", "AXON", "MELI", "COIN", "HOOD",
        "LLY", "COST", "WMT", "ORCL", "ISRG", "BKNG", "GS", "JPM", "CAT", "DE"
    ]
    for sym in us_leaders:
        stock_dict[sym] = sym

    print(f"✅ 成功載入共 {len(stock_dict)} 檔台美股標的準備掃描！")
    return stock_dict

# ==========================================
# 3. Telegram 分段推播函式
# ==========================================
def send_telegram_msg(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"❌ 網路連線錯誤：{e}")

def send_batch_reports(tw_list, us_list):
    all_matched = tw_list + us_list
    if not all_matched:
        send_telegram_msg("📊 <b>【選股日報 V3】</b>\n今日無標的同時符合「標準第二階段 + VCP量縮/紅K突破」，系統運作正常！")
        return

    chunk_size = 10
    total_pages = (len(all_matched) + chunk_size - 1) // chunk_size
    for i in range(0, len(all_matched), chunk_size):
        page = (i // chunk_size) + 1
        chunk = all_matched[i:i + chunk_size]
        header = f"📊 <b>【TradingView 7大指標 × VCP 選股日報 V3】({page}/{total_pages})</b>\n"
        header += f"<i>已排除黑K出貨量、高檔崩跌>32%及末升段標的</i>\n\n"
        send_telegram_msg(header + "\n\n".join(chunk))

# ==========================================
# 4. 核心選股邏輯 (修正 5 大盲點)
# ==========================================
def analyze_stocks(stock_dict):
    raw_data = {}
    tw_rs_scores = {}
    us_rs_scores = {}

    print("📥 正在批次下載歷史 K 線資料...")
    for symbol in stock_dict.keys():
        try:
            df = yf.Ticker(symbol).history(period="2y")
            if len(df) < 252:
                continue
            raw_data[symbol] = df
            
            c = df['Close']
            r3m = (c.iloc[-1] / c.iloc[-63]) - 1
            r6m = (c.iloc[-1] / c.iloc[-126]) - 1
            r9m = (c.iloc[-1] / c.iloc[-189]) - 1
            r12m = (c.iloc[-1] / c.iloc[-252]) - 1
            score = 0.4 * r3m + 0.2 * r6m + 0.2 * r9m + 0.2 * r12m
            
            # 【修正 1】台股與美股分開計算 RS 排名，避免美股被台股小型翻倍股擠掉
            if symbol.endswith(".TW") or symbol.endswith(".TWO"):
                tw_rs_scores[symbol] = score
            else:
                us_rs_scores[symbol] = score
        except Exception:
            continue

    rs_rating_map = {}
    if tw_rs_scores:
        rs_rating_map.update((pd.Series(tw_rs_scores).rank(pct=True) * 99).round(0).astype(int).to_dict())
    if us_rs_scores:
        rs_rating_map.update((pd.Series(us_rs_scores).rank(pct=True) * 99).round(0).astype(int).to_dict())

    tw_results = []
    us_results = []

    for symbol, df in raw_data.items():
        open_p = df['Open']
        close = df['Close']
        high = df['High']
        low = df['Low']
        vol = df['Volume']

        o_now = open_p.iloc[-1]
        c_now = close.iloc[-1]
        c_prev = close.iloc[-2]
        h_now = high.iloc[-1]
        l_now = low.iloc[-1]
        rs_rating = rs_rating_map.get(symbol, 50)
        name = stock_dict.get(symbol, symbol)

        # 【修正 2】放寬股價限制至 >= 10 元 (保留 10~30 元潛力股)
        if c_now < 10:
            continue

        # 【修正 3】判斷今日 K 棒品質：必須是紅 K 且收在整根 K 棒上半部，嚴格排除藥華藥/大甲那種開高走低大黑K
        is_red_k = (c_now > o_now) and (c_now > c_prev)
        close_in_upper_half = (c_now - l_now) >= (h_now - l_now) * 0.5 if (h_now > l_now) else True
        is_strong_bull_bar = is_red_k and close_in_upper_half

        # 1. Mark Minervini 趨勢模板 + 排除第三階段深跌股
        ma50 = close.rolling(50).mean()
        ma150 = close.rolling(150).mean()
        ma200 = close.rolling(200).mean()
        high_52w = high.rolling(252).max().iloc[-1]
        low_52w = low.rolling(252).min().iloc[-1]

        ma200_rising_3m = (ma200.iloc[-1] > ma200.iloc[-21] > ma200.iloc[-42] > ma200.iloc[-63])
        
        # 【修正 4】檢查從近半年最高點下來的「最大回檔深度」：若曾崩跌超過 32% (如大甲跌42%)，代表已走完第二階段或結構破壞，直接剔除！
        peak_6m_idx = high.iloc[-126:].idxmax()
        peak_6m_val = high.loc[peak_6m_idx]
        low_since_peak = low.loc[peak_6m_idx:].min()
        max_base_drawdown = (peak_6m_val - low_since_peak) / peak_6m_val

        minervini_pass = (
            (c_now > ma50.iloc[-1] > ma150.iloc[-1] > ma200.iloc[-1]) and
            ma200_rising_3m and
            (c_now >= low_52w * 1.30) and
            (c_now >= high_52w * 0.78) and      # 距離一年高點 22% 以內
            (max_base_drawdown <= 0.32)         # 整理期最大回檔不得超過 32%
        )

        # RS 門檻：台股 >= 80，美股 >= 75
        rs_threshold = 80 if (symbol.endswith(".TW") or symbol.endswith(".TWO")) else 75
        if not (minervini_pass and rs_rating >= rs_threshold):
            continue

        doubled_badge = "👑翻倍股" if (c_now >= low_52w * 2.0) else ""

        # 2. 【修正 5】真正的 VCP 波動收縮 + 量縮乾涸 (Volume Dry-Up)
        # 條件 A：近 10 日高低波幅 < 前 10~30 日波幅的 65%，且近 10 日波幅 <= 12%
        prev_swing = (high.iloc[-30:-10].max() - low.iloc[-30:-10].min()) / low.iloc[-30:-10].min()
        recent_swing = (high.iloc[-10:].max() - low.iloc[-10:].min()) / low.iloc[-10:].min()
        
        # 條件 B：近 20 日內「上漲日均量 > 下跌日均量」(沒有像藥華藥那樣的爆量長黑)
        price_diff = close.diff().iloc[-20:]
        vol_20 = vol.iloc[-20:]
        up_vol_avg = vol_20[price_diff > 0].mean() if (price_diff > 0).any() else 0
        down_vol_avg = vol_20[price_diff <= 0].mean() if (price_diff <= 0).any() else 1
        no_heavy_selling = (up_vol_avg >= down_vol_avg) and not ((c_now < o_now) and (vol.iloc[-1] > vol.iloc[-21:-1].mean() * 1.3))

        # 條件 C：量縮整理 (近 5 日均量 < 50 日均量的 80%)
        vol_ma5 = vol.iloc[-5:].mean()
        vol_ma50 = vol.iloc[-50:].mean()
        volume_dry_up = vol_ma5 <= (vol_ma50 * 0.80)

        vcp_ready = (recent_swing <= prev_swing * 0.75) and (recent_swing <= 0.12) and no_heavy_selling and volume_dry_up

        # 3. Auto Anchored Swing VWAP (Martin Luk)
        swing_window = df.iloc[-61:-1]
        peak_date = swing_window['High'].idxmax()
        sub_df = df.loc[peak_date:]
        tp = (sub_df['High'] + sub_df['Low'] + sub_df['Close']) / 3
        anchored_vwap = (tp * sub_df['Volume']).cumsum() / sub_df['Volume'].cumsum()
        vwap_now = anchored_vwap.iloc[-1]
        cross_vwap = is_strong_bull_bar and (c_now > vwap_now) and (c_prev <= anchored_vwap.iloc[-2]) if len(anchored_vwap) > 1 else False

        # 4. Darvas Box 突破 & Zanger 紅K爆量 (必須是強勢紅K才算數！)
        box_top = high.iloc[-21:-1].max()
        box_bottom = low.iloc[-21:-1].min()
        is_tight_box = ((box_top - box_bottom) / box_bottom) <= 0.20
        darvas_breakout = is_strong_bull_bar and (c_now > box_top) and (c_prev <= box_top) and is_tight_box

        vol_ma20 = vol.iloc[-21:-1].mean()
        vol_ratio = vol.iloc[-1] / vol_ma20 if vol_ma20 > 0 else 0
        zanger_vol_burst = is_strong_bull_bar and (vol_ratio >= 1.8) and (c_now >= vwap_now)

        # 5. Wedge Pop (WP) - 必須是紅K站回均線且無爆量長黑
        ema10 = close.ewm(span=10, adjust=False).mean()
        ema20 = close.ewm(span=20, adjust=False).mean()
        recent_pullback = (low.iloc[-6:-1] < ema20.iloc[-6:-1]).any()
        wedge_pop = is_strong_bull_bar and no_heavy_selling and recent_pullback and (c_now > ema10.iloc[-1]) and (c_now > ema20.iloc[-1]) and (c_prev <= ema10.iloc[-2] or c_prev <= ema20.iloc[-2])

        # 6. 自動計算停損價 (2x 20日ATR，最大不超過 10%)
        tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
        atr20 = tr.rolling(20).mean().iloc[-1]
        stop_distance = min(2 * atr20, c_now * 0.10)
        stop_price = c_now - stop_distance
        stop_pct = (stop_distance / c_now) * 100

        # 【修正 6】只有真正觸發「VCP量縮收斂」或「紅K攻擊訊號」才推播，單純翻倍但今天收黑K的股票一律踢除！
        action_signals = []
        if vcp_ready:
            action_signals.append(f"🎯VCP量縮收斂(10日波幅{recent_swing*100:.1f}%|量縮至{vol_ma5/vol_ma50*100:.0f}%)")
        if darvas_breakout:
            action_signals.append("📦紅K突破箱頂")
        if cross_vwap:
            action_signals.append("📈紅K站上高點VWAP")
        if zanger_vol_burst:
            action_signals.append(f"🔥紅K爆量攻擊({vol_ratio:.1f}倍)")
        if wedge_pop:
            action_signals.append("⚡Wedge Pop轉強")

        if action_signals:
            flag = "🇹🇼" if (symbol.endswith(".TW") or symbol.endswith(".TWO")) else "🇺🇸"
            title = f"{flag} {symbol} {name}" if name != symbol else f"{flag} {symbol}"
            if doubled_badge:
                title += f" ({doubled_badge})"
            msg = (
                f"<b>{title}</b> (收盤: {c_now:.2f} | <b>RS: {rs_rating}</b>)\n"
                f" ├ 訊號：{' | '.join(action_signals)}\n"
                f" └ 🛡️停損：<b>{stop_price:.2f}</b> (-{stop_pct:.1f}%) | 高點VWAP: {vwap_now:.2f}"
            )
            if symbol.endswith(".TW") or symbol.endswith(".TWO"):
                tw_results.append((rs_rating, msg))
            else:
                us_results.append((rs_rating, msg))

    tw_results.sort(key=lambda x: x[0], reverse=True)
    us_results.sort(key=lambda x: x[0], reverse=True)
    return [x[1] for x in tw_results], [x[1] for x in us_results]

if __name__ == "__main__":
    stock_dict = get_auto_stock_list()
    tw_list, us_list = analyze_stocks(stock_dict)
    send_batch_reports(tw_list, us_list)
