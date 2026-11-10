import os
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import warnings
warnings.filterwarnings("ignore")

# ==========================================
# 1. 讀取 Telegram 設定 (優先讀取 GitHub Secrets，若無則使用下方預設值)
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "請換成你的_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "請換成你的_CHAT_ID")

# ==========================================
# 2. 自動取得台股(上市+上櫃熱門股)與美股清單
# ==========================================
def get_auto_stock_list():
    stock_dict = {}  # 格式: {"2330.TW": "台積電", "NVDA": "NVDA"}
    
    # A. 自動抓取台股【上市】成交重心股 (股價 >= 30 元，取成交量前 100 大)
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=15)
        if res.status_code == 200:
            twse_data = pd.DataFrame(res.json())
            # 僅保留 4 碼普通股 (排除權證與 ETF)
            twse_data = twse_data[twse_data['Code'].str.match(r'^[1-9]\d{3}$')]
            twse_data['ClosingPrice'] = pd.to_numeric(twse_data['ClosingPrice'].str.replace(',', ''), errors='coerce')
            twse_data['TradeVolume'] = pd.to_numeric(twse_data['TradeVolume'].str.replace(',', ''), errors='coerce')
            # 篩選股價 >= 30 元，依成交量排序取前 100 檔
            twse_top = twse_data[twse_data['ClosingPrice'] >= 30].sort_values(by='TradeVolume', ascending=False).head(100)
            for _, row in twse_top.iterrows():
                stock_dict[f"{row['Code']}.TW"] = row['Name'].strip()
    except Exception as e:
        print(f"⚠️ 上市清單抓取異常，啟用備用清單: {e}")

    # B. 自動抓取台股【上櫃】成交重心股 (股價 >= 30 元，取成交量前 50 大)
    try:
        res_otc = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", timeout=15)
        if res_otc.status_code == 200:
            tpex_data = pd.DataFrame(res_otc.json())
            tpex_data = tpex_data[tpex_data['SecuritiesCompanyCode'].str.match(r'^[1-9]\d{3}$')]
            tpex_data['Close'] = pd.to_numeric(tpex_data['Close'].str.replace(',', ''), errors='coerce')
            tpex_data['TradingShares'] = pd.to_numeric(tpex_data['TradingShares'].str.replace(',', ''), errors='coerce')
            tpex_top = tpex_data[tpex_data['Close'] >= 30].sort_values(by='TradingShares', ascending=False).head(50)
            for _, row in tpex_top.iterrows():
                stock_dict[f"{row['SecuritiesCompanyCode']}.TWO"] = row['CompanyName'].strip()
    except Exception as e:
        print(f"⚠️ 上櫃清單抓取異常: {e}")

    # C. 加入美股核心科技與動能領頭羊清單
    us_leaders = [
        "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA", "AVGO", "AMD", "PLTR",
        "NFLX", "CRM", "PANW", "CRWD", "NOW", "ANET", "MU", "ARM", "SMCI", "APP",
        "VST", "CEG", "GEV", "UBER", "SHOP", "SPOT", "AXON", "MELI", "COIN", "HOOD"
    ]
    for sym in us_leaders:
        stock_dict[sym] = sym

    print(f"✅ 成功載入共 {len(stock_dict)} 檔台美股熱門標的準備掃描！")
    return stock_dict

# ==========================================
# 3. Telegram 分段推播函式 (防止超過 4096 字元上限)
# ==========================================
def send_telegram_msg(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        res = requests.post(url, json=payload, timeout=15)
        if res.status_code == 200:
            print("✅ Telegram 訊息發送成功！")
        else:
            print(f"❌ 發送失敗：{res.text}")
    except Exception as e:
        print(f"❌ 網路連線錯誤：{e}")

def send_batch_reports(matched_list):
    if not matched_list:
        send_telegram_msg("📊 <b>【選股日報】</b>\n今日無標的同時符合「Minervini 模板 + RS≥80」且觸發訊號，系統運作正常！")
        return

    # 每 10 檔股票切成一則訊息發送，避免超過 Telegram 單則字數上限
    chunk_size = 10
    total_pages = (len(matched_list) + chunk_size - 1) // chunk_size
    for i in range(0, len(matched_list), chunk_size):
        page = (i // chunk_size) + 1
        chunk = matched_list[i:i + chunk_size]
        header = f"📊 <b>【TradingView 7大指標 × Minervini 選股日報】({page}/{total_pages})</b>\n"
        header += f"<i>篩選門檻：符合 Minervini 多頭模板 + RS ≥ 80</i>\n\n"
        send_telegram_msg(header + "\n\n".join(chunk))

# ==========================================
# 4. 計算 7 大指標 + VCP 收縮 + 2x ATR 自動停損
# ==========================================
def analyze_stocks(stock_dict):
    raw_data = {}
    rs_scores = {}

    print("📥 正在批次下載歷史 K 線資料...")
    for symbol in stock_dict.keys():
        try:
            df = yf.Ticker(symbol).history(period="2y")
            if len(df) < 252:
                continue
            raw_data[symbol] = df
            
            # 計算歐奈爾 RS 加權漲幅
            c = df['Close']
            r3m = (c.iloc[-1] / c.iloc[-63]) - 1
            r6m = (c.iloc[-1] / c.iloc[-126]) - 1
            r9m = (c.iloc[-1] / c.iloc[-189]) - 1
            r12m = (c.iloc[-1] / c.iloc[-252]) - 1
            rs_scores[symbol] = 0.4 * r3m + 0.2 * r6m + 0.2 * r9m + 0.2 * r12m
        except Exception:
            continue

    if not rs_scores:
        return []

    rs_series = pd.Series(rs_scores)
    rs_rating_map = (rs_series.rank(pct=True) * 99).round(0).astype(int).to_dict()

    scored_results = []

    for symbol, df in raw_data.items():
        close = df['Close']
        high = df['High']
        low = df['Low']
        vol = df['Volume']

        c_now = close.iloc[-1]
        c_prev = close.iloc[-2]
        rs_rating = rs_rating_map.get(symbol, 50)
        name = stock_dict.get(symbol, symbol)

        if c_now < 30:
            continue

        # 1. Mark Minervini 趨勢模板 (含 200MA 上升滿 3 個月)
        ma50 = close.rolling(50).mean()
        ma150 = close.rolling(150).mean()
        ma200 = close.rolling(200).mean()
        high_52w = high.rolling(252).max().iloc[-1]
        low_52w = low.rolling(252).min().iloc[-1]

        ma200_rising_3m = (ma200.iloc[-1] > ma200.iloc[-21] > ma200.iloc[-42] > ma200.iloc[-63])
        rolling_52w_high = high.rolling(252).max()
        made_high_in_6m = (high.iloc[-126:] >= rolling_52w_high.iloc[-126:] * 0.995).any()

        minervini_pass = (
            (c_now > ma50.iloc[-1] > ma150.iloc[-1] > ma200.iloc[-1]) and
            ma200_rising_3m and
            (c_now >= low_52w * 1.30) and
            (c_now >= high_52w * 0.75) and
            made_high_in_6m
        )
        doubled_from_low = c_now >= low_52w * 2.0

        # 只保留符合 Minervini 多頭模板 且 RS >= 80 的強勢領頭羊
        if not (minervini_pass and rs_rating >= 80):
            continue

        # 2. VCP 波動收縮率與價量健康度
        prev_swing_drop = abs((low.iloc[-40:-15].min() / high.iloc[-40:-15].max()) - 1)
        recent_swing_drop = abs((low.iloc[-15:].min() / high.iloc[-15:].max()) - 1)
        vcp_contracting = (recent_swing_drop <= prev_swing_drop * 0.6) and (recent_swing_drop <= 0.12)

        price_diff = close.diff().iloc[-20:]
        vol_20 = vol.iloc[-20:]
        up_vol_avg = vol_20[price_diff > 0].mean() if (price_diff > 0).any() else 0
        down_vol_avg = vol_20[price_diff <= 0].mean() if (price_diff <= 0).any() else 1
        healthy_volume = up_vol_avg > down_vol_avg

        # 3. Auto Anchored Swing VWAP (Martin Luk)
        swing_window = df.iloc[-61:-1]
        peak_date = swing_window['High'].idxmax()
        sub_df = df.loc[peak_date:]
        tp = (sub_df['High'] + sub_df['Low'] + sub_df['Close']) / 3
        anchored_vwap = (tp * sub_df['Volume']).cumsum() / sub_df['Volume'].cumsum()
        vwap_now = anchored_vwap.iloc[-1]
        cross_vwap = (c_now > vwap_now) and (c_prev <= anchored_vwap.iloc[-2]) if len(anchored_vwap) > 1 else False

        # 4. Darvas Box 突破 & Zanger 爆量
        box_top = high.iloc[-21:-1].max()
        box_bottom = low.iloc[-21:-1].min()
        is_tight_box = ((box_top - box_bottom) / box_bottom) <= 0.22
        darvas_breakout = (c_now > box_top) and (c_prev <= box_top) and is_tight_box

        vol_ma20 = vol.iloc[-21:-1].mean()
        vol_ratio = vol.iloc[-1] / vol_ma20 if vol_ma20 > 0 else 0
        zanger_vol_burst = vol_ratio >= 2.0

        # 5. Wedge Pop (WP) / Drop (WD) & Williams Vix Fix
        ema10 = close.ewm(span=10, adjust=False).mean()
        ema20 = close.ewm(span=20, adjust=False).mean()
        recent_pullback = (low.iloc[-6:-1] < ema20.iloc[-6:-1]).any()
        wedge_pop = recent_pullback and (c_now > ema10.iloc[-1]) and (c_now > ema20.iloc[-1]) and (c_prev <= ema10.iloc[-2] or c_prev <= ema20.iloc[-2])

        overextended = ((high.iloc[-6:-1] - ema20.iloc[-6:-1]) / ema20.iloc[-6:-1] > 0.12).any()
        wedge_drop = overextended and (c_now < ema10.iloc[-1]) and (c_prev >= ema10.iloc[-2])

        highest_c22 = close.rolling(22).max()
        wvf = ((highest_c22 - low) / highest_c22) * 100
        wvf_upper_band = wvf.rolling(20).mean() + 2.0 * wvf.rolling(20).std()
        wvf_range_high = wvf.rolling(50).max() * 0.85
        vix_fix_green = (wvf.iloc[-1] >= wvf_upper_band.iloc[-1]) or (wvf.iloc[-1] >= wvf_range_high.iloc[-1])

        # 6. 自動計算停損價 (2x 20日ATR，最大不超過 10%)
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr20 = tr.rolling(20).mean().iloc[-1]
        
        stop_distance = min(2 * atr20, c_now * 0.10)
        stop_price = c_now - stop_distance
        stop_pct = (stop_distance / c_now) * 100

        # 整理觸發標籤
        signals = []
        if doubled_from_low:
            signals.append("👑低點翻倍")
        if vcp_contracting and healthy_volume:
            signals.append(f"🎯VCP收斂({recent_swing_drop*100:.1f}%)")
        if darvas_breakout:
            signals.append("📦突破箱頂")
        if cross_vwap:
            signals.append("📈站上高點VWAP")
        if zanger_vol_burst:
            signals.append(f"🔥爆量({vol_ratio:.1f}倍)")
        if wedge_pop:
            signals.append("⚡Wedge Pop")
        if vix_fix_green:
            signals.append("🟢Vix_Fix見底")
        if wedge_drop:
            signals.append("⚠️Wedge Drop")

        # 只有觸發至少一項關鍵訊號才列入通知，保持日報精準度
        if signals:
            title = f"{symbol} {name}" if name != symbol else symbol
            msg = (
                f"<b>{title}</b> (收盤: {c_now:.2f} | <b>RS: {rs_rating}</b>)\n"
                f" ├ 訊號：{' | '.join(signals)}\n"
                f" └ 🛡️停損：<b>{stop_price:.2f}</b> (-{stop_pct:.1f}%) | 高點VWAP: {vwap_now:.2f}"
            )
            scored_results.append((rs_rating, msg))

    # 依照 RS 分數由高到低排序
    scored_results.sort(key=lambda x: x[0], reverse=True)
    return [item[1] for item in scored_results]

if __name__ == "__main__":
    stock_dict = get_auto_stock_list()
    matched_list = analyze_stocks(stock_dict)
    send_batch_reports(matched_list)
