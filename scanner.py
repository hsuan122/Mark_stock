import os
import io
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
# 2. 全市場股票池抓取 (台股全上市櫃 + 美股 S&P500 / Nasdaq100)
# ==========================================
def get_full_market_stock_list():
    stock_dict = {}
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

    # A. 台股【全上市】普通股 (排除權證/ETF，僅濾除日量 < 50 張的無流動性殭屍股)
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=15)
        if res.status_code == 200:
            twse_df = pd.DataFrame(res.json())
            twse_df = twse_df[twse_df['Code'].str.match(r'^[1-9]\d{3}$')]
            twse_df['ClosingPrice'] = pd.to_numeric(twse_df['ClosingPrice'].str.replace(',', ''), errors='coerce')
            twse_df['TradeVolume'] = pd.to_numeric(twse_df['TradeVolume'].str.replace(',', ''), errors='coerce')
            valid_twse = twse_df[(twse_df['ClosingPrice'] >= 10) & (twse_df['TradeVolume'] >= 50000)]
            for _, row in valid_twse.iterrows():
                stock_dict[f"{row['Code']}.TW"] = row['Name'].strip()
    except Exception as e:
        print(f"⚠️ 上市清單抓取異常: {e}")

    # B. 台股【全上櫃】普通股 (股價 >= 10 元 且 日量 >= 50 張)
    try:
        res_otc = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", timeout=15)
        if res_otc.status_code == 200:
            tpex_df = pd.DataFrame(res_otc.json())
            tpex_df = tpex_df[tpex_df['SecuritiesCompanyCode'].str.match(r'^[1-9]\d{3}$')]
            tpex_df['Close'] = pd.to_numeric(tpex_df['Close'].str.replace(',', ''), errors='coerce')
            tpex_df['TradingShares'] = pd.to_numeric(tpex_df['TradingShares'].str.replace(',', ''), errors='coerce')
            valid_tpex = tpex_df[(tpex_df['Close'] >= 10) & (tpex_df['TradingShares'] >= 50000)]
            for _, row in valid_tpex.iterrows():
                stock_dict[f"{row['SecuritiesCompanyCode']}.TWO"] = row['CompanyName'].strip()
    except Exception as e:
        print(f"⚠️ 上櫃清單抓取異常: {e}")

    # C. 美股【S&P 500 + Nasdaq 100 全成分股】自動抓取
    us_tickers = set()
    try:
        sp_res = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", headers=headers, timeout=15)
        if sp_res.status_code == 200:
            sp_table = pd.read_html(io.StringIO(sp_res.text))[0]
            for sym in sp_table['Symbol'].tolist():
                us_tickers.add(str(sym).replace('.', '-').strip())
    except Exception as e:
        print(f"⚠️ S&P 500 清單抓取異常: {e}")

    try:
        nq_res = requests.get("https://en.wikipedia.org/wiki/Nasdaq-100", headers=headers, timeout=15)
        if nq_res.status_code == 200:
            for tbl in pd.read_html(io.StringIO(nq_res.text)):
                if 'Ticker' in tbl.columns:
                    for sym in tbl['Ticker'].tolist():
                        us_tickers.add(str(sym).replace('.', '-').strip())
                    break
    except Exception as e:
        print(f"⚠️ Nasdaq 100 清單抓取異常: {e}")

    # 補充美股中型高動能成長股與保底名單
    extra_us = [
        "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA", "AVGO", "AMD", "PLTR",
        "NFLX", "CRM", "PANW", "CRWD", "NOW", "ANET", "MU", "ARM", "SMCI", "APP",
        "VST", "CEG", "GEV", "UBER", "SHOP", "SPOT", "AXON", "MELI", "COIN", "HOOD",
        "RKLB", "SOFI", "AFRM", "UPST", "REDD", "DUOL", "CAVA", "BROS", "CELH", "ONON",
        "DECK", "TOST", "IOT", "NET", "DDOG", "SNOW", "ZS", "MDB", "MSTR", "CVNA"
    ]
    us_tickers.update(extra_us)
    for sym in us_tickers:
        stock_dict[sym] = sym

    print(f"✅ 全市場載入完成：共 {len(stock_dict)} 檔台美股準備高速掃描！")
    return stock_dict

# ==========================================
# 3. Telegram 推播函式
# ==========================================
def send_telegram_msg(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"❌ 網路連線錯誤：{e}")

def send_categorized_reports(breakout_list, vcp_watch_list, total_scanned):
    if not breakout_list and not vcp_watch_list:
        send_telegram_msg(f"📊 <b>【全市場 VCP 樞紐點雷達 V4】</b>\n已掃描全市場 {total_scanned} 檔標的，今日無符合嚴格樞紐點條件之股票。")
        return

    all_blocks = []
    if breakout_list:
        all_blocks.append("🚀 <b>【第一類：今日剛從樞紐點起漲／突破】</b>\n<i>(前日緊貼均線整理、無爆量長黑，今日首根轉強)</i>")
        all_blocks.extend(breakout_list)
    if vcp_watch_list:
        all_blocks.append("\n🎯 <b>【第二類：VCP 樞紐點量縮築底觀察池】</b>\n<i>(處於第二階段、波動收斂且量縮乾涸，等待突破點)</i>")
        all_blocks.extend(vcp_watch_list)

    chunk_size = 10
    total_pages = (len(all_blocks) + chunk_size - 1) // chunk_size
    for i in range(0, len(all_blocks), chunk_size):
        page = (i // chunk_size) + 1
        chunk = all_blocks[i:i + chunk_size]
        header = f"📊 <b>【全市場 VCP 選股日報 V4】({page}/{total_pages})</b>\n<i>已掃描 {total_scanned} 檔｜已剔除乖離過大與爆量出貨股</i>\n\n"
        send_telegram_msg(header + "\n\n".join(chunk))

# ==========================================
# 4. 多執行緒批次下載 + 嚴格 VCP 樞紐點篩選
# ==========================================
def analyze_full_market(stock_dict):
    symbols = list(stock_dict.keys())
    raw_data = {}
    tw_rs_scores = {}
    us_rs_scores = {}

    # 使用 16 個月資料即可滿足 200MA 上升 3 個月 (263天) 與 52 週高低點 (252天)，大幅提升全市場下載速度
    start_date = (pd.Timestamp.now() - pd.DateOffset(months=16)).strftime('%Y-%m-%d')
    batch_size = 150

    print("📥 正在啟動多執行緒高速下載全市場 K 線...")
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        try:
            data = yf.download(batch, start=start_date, group_by="ticker", threads=True, progress=False)
            for sym in batch:
                try:
                    df = data[sym].dropna(how='all') if len(batch) > 1 else data.dropna(how='all')
                    if len(df) < 260:
                        continue
                    c = df['Close']
                    if c.iloc[-1] < 10:
                        continue
                    raw_data[sym] = df
                    
                    # 計算歐奈爾 RS 加權漲幅
                    r3m = (c.iloc[-1] / c.iloc[-63]) - 1
                    r6m = (c.iloc[-1] / c.iloc[-126]) - 1
                    r9m = (c.iloc[-1] / c.iloc[-189]) - 1
                    r12m = (c.iloc[-1] / c.iloc[-252]) - 1
                    score = 0.4 * r3m + 0.2 * r6m + 0.2 * r9m + 0.2 * r12m
                    
                    if sym.endswith(".TW") or sym.endswith(".TWO"):
                        tw_rs_scores[sym] = score
                    else:
                        us_rs_scores[sym] = score
                except Exception:
                    continue
        except Exception as e:
            print(f"批次下載略過: {e}")

    rs_rating_map = {}
    if tw_rs_scores:
        rs_rating_map.update((pd.Series(tw_rs_scores).rank(pct=True) * 99).round(0).astype(int).to_dict())
    if us_rs_scores:
        rs_rating_map.update((pd.Series(us_rs_scores).rank(pct=True) * 99).round(0).astype(int).to_dict())

    breakout_results = []
    vcp_watch_results = []

    for symbol, df in raw_data.items():
        open_p = df['Open']
        close = df['Close']
        high = df['High']
        low = df['Low']
        vol = df['Volume']

        o_now, c_now, h_now, l_now = open_p.iloc[-1], close.iloc[-1], high.iloc[-1], low.iloc[-1]
        c_prev = close.iloc[-2]
        rs_rating = rs_rating_map.get(symbol, 50)
        name = stock_dict.get(symbol, symbol)
        is_tw = symbol.endswith(".TW") or symbol.endswith(".TWO")

        # 均線計算
        ma20 = close.rolling(20).mean()
        ma50 = close.rolling(50).mean()
        ma150 = close.rolling(150).mean()
        ma200 = close.rolling(200).mean()
        vol_ma20 = vol.rolling(20).mean()
        vol_ma50 = vol.rolling(50).mean()

        # 20日均量至少大於 300 張(台股) / 30萬股(美股)，確保基本交易流動性
        if vol_ma20.iloc[-1] < 300000:
            continue

        # ----------------------------------------------------
        # 【濾網 1】Mark Minervini 第二階段趨勢模板 + 排除深跌股
        # ----------------------------------------------------
        high_52w = high.rolling(252).max().iloc[-1]
        low_52w = low.rolling(252).min().iloc[-1]
        ma200_rising_3m = (ma200.iloc[-1] > ma200.iloc[-21] > ma200.iloc[-42] > ma200.iloc[-63])

        peak_6m_idx = high.iloc[-126:].idxmax()
        peak_6m_val = high.loc[peak_6m_idx]
        low_since_peak = low.loc[peak_6m_idx:].min()
        max_base_drawdown = (peak_6m_val - low_since_peak) / peak_6m_val

        minervini_pass = (
            (c_now > ma50.iloc[-1] > ma150.iloc[-1] > ma200.iloc[-1]) and
            ma200_rising_3m and
            (c_now >= low_52w * 1.30) and
            (c_now >= high_52w * 0.80) and      # 距離一年高點 20% 以內
            (max_base_drawdown <= 0.30)         # 整理期最大回檔不得超過 30%
        )

        rs_threshold = 82 if is_tw else 80
        if not (minervini_pass and rs_rating >= rs_threshold):
            continue

        # ----------------------------------------------------
        # 【濾網 2】防追高乖離率過濾 (徹底淘汰騰雲、佳大這種已漲一大段的股票！)
        # ----------------------------------------------------
        # 真正處於「樞紐點(Pivot)」的股票，起漲前一日必須緊貼 20MA 與 50MA 整理
        dist_20ma_prev = (c_prev - ma20.iloc[-2]) / ma20.iloc[-2]
        dist_50ma_prev = (c_prev - ma50.iloc[-2]) / ma50.iloc[-2]
        dist_50ma_now = (c_now - ma50.iloc[-1]) / ma50.iloc[-1]

        # 若前一日距離 20MA 超過 7%，或距離 50MA 超過 16%，代表早已噴出遠離底部樞紐點，直接剔除！
        if (dist_20ma_prev > 0.07) or (dist_50ma_prev > 0.16) or (dist_50ma_now > 0.22):
            continue

        # ----------------------------------------------------
        # 【濾網 3】近 15 日無爆量出貨長黑 / 爆量長上影線 (徹底淘汰佳大 11/05 那種出貨股！)
        # ----------------------------------------------------
        recent_15 = df.iloc[-15:]
        v_ma20_15 = vol_ma20.iloc[-15:]
        # 條件 A：爆量收黑 (收盤 < 開盤 且 量 > 1.5倍均量)
        bearish_vol_bar = ((recent_15['Close'] < recent_15['Open'] * 0.985) & (recent_15['Volume'] > v_ma20_15 * 1.5)).any()
        # 條件 B：爆量留長上影線倒貨 (從當日最高點殺跌超過 4.5% 且 量 > 1.6倍均量，如佳大 11/05 從 80.3 殺到 74.7)
        climax_wick_bar = (((recent_15['High'] - recent_15['Close']) / recent_15['High'] > 0.045) & (recent_15['Volume'] > v_ma20_15 * 1.6)).any()

        if bearish_vol_bar or climax_wick_bar:
            continue

        # 近 20 日上漲均量必須 >= 下跌均量
        price_diff = close.diff().iloc[-20:]
        vol_20 = vol.iloc[-20:]
        up_vol_avg = vol_20[price_diff > 0].mean() if (price_diff > 0).any() else 0
        down_vol_avg = vol_20[price_diff <= 0].mean() if (price_diff <= 0).any() else 1
        if up_vol_avg < down_vol_avg:
            continue

        # ----------------------------------------------------
        # 【濾網 4】計算打底至少 3 週以上的波段高點 VWAP 與箱型樞紐點
        # ----------------------------------------------------
        # 波段高點必須落在過去 12 ~ 65 個交易日之間 (禁止拿最近 3~5 天剛創的高點當錨點！)
        base_window = df.iloc[-65:-10]
        peak_date = base_window['High'].idxmax()
        pivot_high = base_window['High'].max()

        # 過去 10 天(不含今日)必須在該波段高點附近或下方橫盤整理，未提前噴出超過 3%
        if high.iloc[-11:-1].max() > pivot_high * 1.03:
            continue

        sub_df = df.loc[peak_date:]
        tp = (sub_df['High'] + sub_df['Low'] + sub_df['Close']) / 3
        anchored_vwap = (tp * sub_df['Volume']).cumsum() / sub_df['Volume'].cumsum()
        vwap_now = anchored_vwap.iloc[-1]

        # 計算近 10 日波動收斂度與量縮程度
        prev_swing = (high.iloc[-30:-10].max() - low.iloc[-30:-10].min()) / low.iloc[-30:-10].min()
        recent_10d_range = (high.iloc[-11:-1].max() - low.iloc[-11:-1].min()) / low.iloc[-11:-1].min()
        vol_ma5_prev = vol.iloc[-6:-1].mean()
        vol_contract_ratio = vol_ma5_prev / vol_ma50.iloc[-2] if vol_ma50.iloc[-2] > 0 else 1.0

        # 判斷是否具備「緊密橫盤打底」結構 (近 10 日高低振幅 <= 11%)
        is_tight_base = (recent_10d_range <= 0.11) and (recent_10d_range <= prev_swing * 0.85)

        # 判斷今日是否為飽滿紅 K (收盤 > 開盤、上漲、且收在 K 棒上 60% 位置)
        is_strong_red_k = (c_now > o_now) and (c_now > c_prev) and ((c_now - l_now) >= (h_now - l_now) * 0.6 if h_now > l_now else True)
        vol_ratio = vol.iloc[-1] / vol_ma20.iloc[-2] if vol_ma20.iloc[-2] > 0 else 0

        # ----------------------------------------------------
        # 訊號分類 A：🚀 今日剛突破樞紐點 (必須有緊密底部 + 今日帶量飽滿紅K)
        # ----------------------------------------------------
        box_20d_top = high.iloc[-21:-1].max()
        pivot_breakout = is_tight_base and is_strong_red_k and (c_now > box_20d_top) and (c_prev <= box_20d_top) and (vol_ratio >= 1.5)
        vwap_breakout = is_tight_base and is_strong_red_k and (c_now > vwap_now) and (c_prev <= anchored_vwap.iloc[-2]) and (vol_ratio >= 1.4)

        ema10 = close.ewm(span=10, adjust=False).mean()
        ema20 = close.ewm(span=20, adjust=False).mean()
        recent_pullback = (low.iloc[-6:-1] <= ema20.iloc[-6:-1] * 1.01).any()
        wedge_pop = is_tight_base and is_strong_red_k and recent_pullback and (c_now > ema10.iloc[-1]) and (c_now > ema20.iloc[-1]) and (c_prev <= ema10.iloc[-2]) and (vol_ratio >= 1.3)

        # ----------------------------------------------------
        # 訊號分類 B：🎯 VCP 樞紐點量縮築底 (尚未噴出，正在量縮等突破)
        # ----------------------------------------------------
        vcp_coiling = (
            is_tight_base and
            (vol_contract_ratio <= 0.70) and            # 近 5 日均量萎縮至 50 日均量的 70% 以下
            (c_now >= ma20.iloc[-1] * 0.98) and         # 股價穩守在 20MA 附近
            (c_now <= box_20d_top * 1.005) and          # 尚未正式突破箱頂
            (c_now >= box_20d_top * 0.93)               # 距離樞紐點箱頂僅一步之遙 (7%以內)
        )

        # 計算 2x ATR 停損價 (最大不超過 8%)
        tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
        atr20 = tr.rolling(20).mean().iloc[-1]
        stop_distance = min(2 * atr20, c_now * 0.08)
        stop_price = c_now - stop_distance
        stop_pct = (stop_distance / c_now) * 100

        flag = "🇹🇼" if is_tw else "🇺🇸"
        title = f"{flag} {symbol} {name}" if name != symbol else f"{flag} {symbol}"

        if pivot_breakout or vwap_breakout or wedge_pop:
            tags = []
            if pivot_breakout:
                tags.append(f"📦突破樞紐箱頂({box_20d_top:.2f})")
            if vwap_breakout:
                tags.append("📈突破波段VWAP")
            if wedge_pop:
                tags.append("⚡Wedge Pop站回均線")
            tags.append(f"🔥紅K量增({vol_ratio:.1f}倍)")

            msg = (
                f"<b>{title}</b> (收盤: {c_now:.2f} | <b>RS: {rs_rating}</b>)\n"
                f" ├ 訊號：{' | '.join(tags)}\n"
                f" ├ 結構：前10日振幅僅 {recent_10d_range*100:.1f}% | 距50MA +{dist_50ma_now*100:.1f}%\n"
                f" └ 🛡️停損：<b>{stop_price:.2f}</b> (-{stop_pct:.1f}%) | 樞紐VWAP: {vwap_now:.2f}"
            )
            breakout_results.append((rs_rating, msg))

        elif vcp_coiling:
            msg = (
                f"<b>{title}</b> (收盤: {c_now:.2f} | <b>RS: {rs_rating}</b>)\n"
                f" ├ 狀態：🎯VCP量縮築底 (10日振幅 {recent_10d_range*100:.1f}% | 量縮至均量 {vol_contract_ratio*100:.0f}%)\n"
                f" ├ 關鍵樞紐點：突破 <b>{box_20d_top:.2f}</b> 轉強 (距50MA +{dist_50ma_now*100:.1f}%)\n"
                f" └ 🛡️預估停損：<b>{stop_price:.2f}</b> (-{stop_pct:.1f}%) | 樞紐VWAP: {vwap_now:.2f}"
            )
            vcp_watch_results.append((rs_rating, msg))

    breakout_results.sort(key=lambda x: x[0], reverse=True)
    vcp_watch_results.sort(key=lambda x: x[0], reverse=True)
    
    # 各取 RS 最高的前 15 檔精選，避免洗版
    return [x[1] for x in breakout_results[:15]], [x[1] for x in vcp_watch_results[:15]], len(raw_data)

if __name__ == "__main__":
    stock_dict = get_full_market_stock_list()
    breakout_list, vcp_watch_list, total_scanned = analyze_full_market(stock_dict)
    send_categorized_reports(breakout_list, vcp_watch_list, total_scanned)
