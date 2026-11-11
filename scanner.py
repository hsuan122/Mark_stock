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

    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=15)
        if res.status_code == 200:
            twse_df = pd.DataFrame(res.json())
            twse_df = twse_df[twse_df['Code'].str.match(r'^[1-9]\d{3}$')]
            twse_df['ClosingPrice'] = pd.to_numeric(twse_df['ClosingPrice'].str.replace(',', ''), errors='coerce')
            twse_df['TradeVolume'] = pd.to_numeric(twse_df['TradeVolume'].str.replace(',', ''), errors='coerce')
            valid_twse = twse_df[(twse_df['ClosingPrice'] > 0) & (twse_df['TradeVolume'] >= 100000)]
            for _, row in valid_twse.iterrows():
                stock_dict[f"{row['Code']}.TW"] = row['Name'].strip()
    except Exception as e:
        print(f"⚠️ 上市清單抓取異常: {e}")

    try:
        res_otc = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", timeout=15)
        if res_otc.status_code == 200:
            tpex_df = pd.DataFrame(res_otc.json())
            tpex_df = tpex_df[tpex_df['SecuritiesCompanyCode'].str.match(r'^[1-9]\d{3}$')]
            tpex_df['Close'] = pd.to_numeric(tpex_df['Close'].str.replace(',', ''), errors='coerce')
            tpex_df['TradingShares'] = pd.to_numeric(tpex_df['TradingShares'].str.replace(',', ''), errors='coerce')
            valid_tpex = tpex_df[(tpex_df['Close'] > 0) & (tpex_df['TradingShares'] >= 100000)]
            for _, row in valid_tpex.iterrows():
                stock_dict[f"{row['SecuritiesCompanyCode']}.TWO"] = row['CompanyName'].strip()
    except Exception as e:
        print(f"⚠️ 上櫃清單抓取異常: {e}")

    us_tickers = set()
    try:
        sp_res = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", headers=headers, timeout=15)
        if sp_res.status_code == 200:
            sp_table = pd.read_html(io.StringIO(sp_res.text))[0]
            for sym in sp_table['Symbol'].tolist():
                us_tickers.add(str(sym).replace('.', '-').strip())
    except Exception:
        pass

    try:
        nq_res = requests.get("https://en.wikipedia.org/wiki/Nasdaq-100", headers=headers, timeout=15)
        if nq_res.status_code == 200:
            for tbl in pd.read_html(io.StringIO(nq_res.text)):
                if 'Ticker' in tbl.columns:
                    for sym in tbl['Ticker'].tolist():
                        us_tickers.add(str(sym).replace('.', '-').strip())
                    break
    except Exception:
        pass

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

    print(f"✅ 全市場載入完成：共 {len(stock_dict)} 檔台美股準備掃描！")
    return stock_dict

# ==========================================
# 3. 抓取入選股票的近期 EPS 與年增率
# ==========================================
def get_eps_info(symbol):
    try:
        tk = yf.Ticker(symbol)
        info = tk.info or {}
        ttm_eps = info.get('trailingEps')
        q_growth = info.get('earningsQuarterlyGrowth')

        latest_q_eps = None
        try:
            q_stmt = tk.quarterly_income_stmt
            if q_stmt is not None and not q_stmt.empty:
                for row_name in ['Basic EPS', 'Diluted EPS']:
                    if row_name in q_stmt.index:
                        eps_row = q_stmt.loc[row_name].dropna()
                        if len(eps_row) >= 1:
                            latest_q_eps = float(eps_row.iloc[0])
                        if q_growth is None and len(eps_row) >= 5 and eps_row.iloc[4] != 0:
                            q_growth = (eps_row.iloc[0] - eps_row.iloc[4]) / abs(eps_row.iloc[4])
                        break
        except Exception:
            pass

        parts = []
        if latest_q_eps is not None:
            parts.append(f"最新季EPS: <b>{latest_q_eps:.2f}</b>")
        if ttm_eps is not None:
            parts.append(f"近四季: <b>{ttm_eps:.2f}</b>")
        if q_growth is not None:
            sign = "+" if q_growth >= 0 else ""
            parts.append(f"季年增: <b>{sign}{q_growth*100:.1f}%</b>")

        return " ｜ ".join(parts) if parts else "財報更新中"
    except Exception:
        return "財報更新中"

# ==========================================
# 4. 計算區間內「從高點往下拉回的真實最大回檔 (True Pullback Drawdown)」
# ==========================================
def calc_max_pullback(high_s, low_s):
    running_peak = high_s.cummax()
    dd = (running_peak - low_s) / running_peak
    return float(dd.max()) if len(dd) > 0 else 0.0

# ==========================================
# 5. Telegram 推播函式
# ==========================================
def send_telegram_msg(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"❌ 網路連線錯誤：{e}")

def send_minervini_reports(breakouts, pivot_ready, shakeout_pocket, wider_vcp, total_scanned, template_passed, clean_passed):
    all_blocks = []
    if breakouts:
        all_blocks.append("🚀 <b>【一、今日正式突破窄幅樞紐點 (可直接進場型)】</b>\n<i>(突破前5日已收斂至 ≤8% 窄幅箱型 + 今日帶量紅K突破樞紐)</i>")
        all_blocks.extend(breakouts)
    if pivot_ready:
        all_blocks.append("\n🎯 <b>【二、標準 VCP 窄幅樞紐成型池 (近5日振幅 ≤ 8%)】</b>\n<i>(穩守月線之上、近5日極度收斂且量縮，突破樞紐價即可進場)</i>")
        all_blocks.extend(pivot_ready)
    if shakeout_pocket:
        all_blocks.append("\n🪝 <b>【三、20MA 量縮洗盤下影線／口袋支點】</b>\n<i>(短均線向上 + 回測月線量縮拉下影線守穩，低風險早期買點)</i>")
        all_blocks.extend(shakeout_pocket)
    if wider_vcp:
        all_blocks.append("\n⏳ <b>【四、右側收斂中・等待振幅壓縮至8%內 (切勿提前追高)】</b>\n<i>(雖符合多頭模板但近期振幅仍 >8%，須等橫盤收窄後再進場)</i>")
        all_blocks.extend(wider_vcp)

    if not all_blocks:
        send_telegram_msg(f"📊 <b>【Mark Minervini 樞紐點雷達 V10】</b>\n掃描 {total_scanned} 檔，過模板 {template_passed} 檔。")
        return

    chunk_size = 7
    total_pages = (len(all_blocks) + chunk_size - 1) // chunk_size
    for i in range(0, len(all_blocks), chunk_size):
        page = (i // chunk_size) + 1
        chunk = all_blocks[i:i + chunk_size]
        header = (
            f"📊 <b>【Mark 精準 VCP 收斂日報 V10】({page}/{total_pages})</b>\n"
            f"<i>掃描 {total_scanned} 檔｜過模板 {template_passed} 檔｜短均線向上乾淨圖形 {clean_passed} 檔</i>\n\n"
        )
        send_telegram_msg(header + "\n\n".join(chunk))

# ==========================================
# 6. 主角：Mark Minervini 引擎 (修正短均線下彎 + 真實回檔收斂計算)
# ==========================================
def analyze_market_minervini(stock_dict):
    symbols = list(stock_dict.keys())
    raw_data = {}
    tw_rs_scores = {}
    us_rs_scores = {}

    start_date = (pd.Timestamp.now() - pd.DateOffset(months=24)).strftime('%Y-%m-%d')
    batch_size = 120

    print("📥 正在批次下載全市場歷史 K 線...")
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        try:
            data = yf.download(batch, start=start_date, group_by="ticker", threads=True, progress=False)
            for sym in batch:
                try:
                    df = data[sym].copy() if len(batch) > 1 else data.copy()
                    df = df[['Open', 'High', 'Low', 'Close', 'Volume']].dropna()
                    df = df[df['Volume'] > 0]
                    if len(df) < 265:
                        continue
                    
                    c = df['Close']
                    raw_data[sym] = df
                    
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

    breakout_candidates = []
    pivot_ready_candidates = []
    shakeout_candidates = []
    wider_vcp_candidates = []
    template_passed_count = 0
    clean_passed_count = 0

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

        ma5 = close.rolling(5).mean()
        ma10 = close.rolling(10).mean()
        ma20 = close.rolling(20).mean()
        ma50 = close.rolling(50).mean()
        ma150 = close.rolling(150).mean()
        ma200 = close.rolling(200).mean()
        vol_ma10 = vol.rolling(10).mean()
        vol_ma20 = vol.rolling(20).mean()

        if vol_ma20.iloc[-1] < 250000:
            continue

        # ====================================================
        # 👑 【第一關】Mark 趨勢模板 + 剔除短期均線蓋頭下彎股 (淘汰上海商銀！)
        # ====================================================
        high_52w = high.iloc[-252:].max()
        low_52w = low.iloc[-252:].min()
        ma200_rising_3m = (ma200.iloc[-1] > ma200.iloc[-21]) and (ma200.iloc[-21] > ma200.iloc[-42]) and (ma200.iloc[-42] > ma200.iloc[-63])
        rolling_52w_high = high.rolling(252).max()
        made_high_in_6m = (high.iloc[-126:] >= rolling_52w_high.iloc[-126:] * 0.99).any()

        trend_template_pass = (
            (c_now > ma50.iloc[-1] > ma150.iloc[-1] > ma200.iloc[-1]) and
            ma200_rising_3m and
            (c_now >= low_52w * 1.30) and
            (c_now >= high_52w * 0.75) and
            made_high_in_6m and
            (rs_rating >= 80)
        )
        if not trend_template_pass:
            continue

        template_passed_count += 1
        doubled_from_low = (c_now >= low_52w * 2.0)

        # 【新增硬性過濾】避免上海商銀這種 5MA < 10MA < 20MA 且股價壓在 20MA 下方的短空趨勢股！
        # 規定：收盤價必須站在 20MA 之上 (差距容許 -0.5%)，且 20MA 必須高於 10 天前 (月線向上)，且不可 5<10<20 蓋頭往下
        short_ma_downtrend = (ma5.iloc[-1] < ma10.iloc[-1] < ma20.iloc[-1]) and (c_now < ma10.iloc[-1])
        if (c_now < ma20.iloc[-1] * 0.995) or (ma20.iloc[-1] < ma20.iloc[-10]) or short_ma_downtrend:
            continue

        # ====================================================
        # 🛡️ 【第二關】真出貨醜圖封殺令
        # ====================================================
        peak_6m = high.iloc[-126:].max()
        peak_6m_pos = high.iloc[-126:].values.argmax()
        low_after_peak = low.iloc[-126 + peak_6m_pos:].min()
        if ((peak_6m - low_after_peak) / peak_6m) > 0.32:
            continue

        is_black_candle = close < open_p
        is_ugly_green_vol = (close < open_p * 0.992) | (close < close.shift(1) * 0.985)
        is_red_candle = (close >= open_p) & (close >= close.shift(1))

        top2_vol_dates = vol.iloc[-60:].nlargest(2).index
        if (is_black_candle.loc[top2_vol_dates] | (close.loc[top2_vol_dates] < close.shift(1).loc[top2_vol_dates])).any():
            continue

        vol_spike_2x = vol.iloc[-25:] >= (vol_ma20.iloc[-25:] * 2.1)
        wick_dump = ((high.iloc[-12:] - close.iloc[-12:]) / high.iloc[-12:] > 0.05) & (vol.iloc[-12:] >= vol_ma20.iloc[-12:] * 1.8)
        if (is_ugly_green_vol.iloc[-25:] & vol_spike_2x).any() or wick_dump.any():
            continue

        vol_ratio_today = vol.iloc[-1] / vol_ma20.iloc[-2] if vol_ma20.iloc[-2] > 0 else 1.0
        if ((c_now < o_now * 0.99) or (c_now < c_prev * 0.99)) and (vol_ratio_today >= 1.25):
            continue

        red_v25 = vol.iloc[-25:][is_red_candle.iloc[-25:]]
        green_v25 = vol.iloc[-25:][is_black_candle.iloc[-25:]]
        avg_red_v = red_v25.mean() if len(red_v25) > 0 else 0
        avg_green_v = green_v25.mean() if len(green_v25) > 0 else 1
        vol_5d_avg = vol.iloc[-5:].mean()
        vol_shrink_ratio = vol_5d_avg / vol_ma20.iloc[-1] if vol_ma20.iloc[-1] > 0 else 1.0

        if (avg_red_v < avg_green_v * 0.95) and (vol_shrink_ratio > 0.80):
            continue

        clean_passed_count += 1

        # ====================================================
        # 📐 【第三關】修正版 VCP 真實回檔收縮計算 (不把上漲波段算進回檔！)
        # ====================================================
        # 1. 計算過去 50 日基底的「真實高點往下回檔深度 (Base Pullback)」与「近 15 日真實回檔深度」
        base_50d_pullback = calc_max_pullback(high.iloc[-51:-1], low.iloc[-51:-1])
        recent_15d_pullback = calc_max_pullback(high.iloc[-16:-1], low.iloc[-16:-1])
        
        # 2. 計算「近 5 日橫盤振幅 (5-Day Box Range)」：用來衡量突破前夕是否真正進入窄幅收斂 (目標 <= 8.5%)
        prev_5d_range = (high.iloc[-6:-1].max() - low.iloc[-6:-1].min()) / low.iloc[-6:-1].min()
        curr_5d_range = (high.iloc[-5:].max() - low.iloc[-5:].min()) / low.iloc[-5:].min()

        # 3. 精準樞紐點定位：
        base_high = high.iloc[-60:-1].max()
        tight_10d_high = high.iloc[-11:-1].max()
        tight_10d_low = low.iloc[-11:-1].min()
        tight_10d_range = (tight_10d_high - tight_10d_low) / tight_10d_low

        if (tight_10d_range <= 0.09) and (tight_10d_high >= base_high * 0.88):
            exact_pivot = tight_10d_high
            pivot_type_str = f"右側小箱頂 <b>{exact_pivot:.2f}</b> (前高 {base_high:.2f})"
        else:
            exact_pivot = high.iloc[-30:-1].max()
            pivot_type_str = f"前高樞紐 <b>{exact_pivot:.2f}</b>"

        # 計算 2x ATR 停損價 (最大 10%)
        tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
        atr20 = tr.rolling(20).mean().iloc[-1]
        stop_distance = min(2 * atr20, c_now * 0.10)
        stop_price = c_now - stop_distance
        stop_pct = (stop_distance / c_now) * 100

        # ====================================================
        # 🧩 【配角指標計算】
        # ====================================================
        supporting_tags = [f"📊紅/綠量比 {avg_red_v/avg_green_v:.1f}x"]
        if doubled_from_low:
            supporting_tags.append("👑低點翻倍")

        peak_60_idx = high.iloc[-61:-1].idxmax()
        sub_df = df.loc[peak_60_idx:]
        tp = (sub_df['High'] + sub_df['Low'] + sub_df['Close']) / 3
        vwap_series = (tp * sub_df['Volume']).cumsum() / sub_df['Volume'].cumsum()
        vwap_now = vwap_series.iloc[-1]
        if c_now > vwap_now:
            supporting_tags.append(f"✅VWAP之上({vwap_now:.1f})")

        ema10 = close.ewm(span=10, adjust=False).mean()
        ema20 = close.ewm(span=20, adjust=False).mean()
        if (low.iloc[-6:-1] <= ema20.iloc[-6:-1] * 1.01).any() and (c_now > ema10.iloc[-1]) and (c_now > ema20.iloc[-1]) and (c_prev <= ema10.iloc[-2]):
            supporting_tags.append("⚡Wedge Pop")

        if is_red_candle.iloc[-1] and (vol_ratio_today >= 1.8):
            supporting_tags.append(f"🔥Zanger爆量({vol_ratio_today:.1f}倍)")

        flag = "🇹🇼" if is_tw else "🇺🇸"
        title = f"{flag} {symbol} {name}" if name != symbol else f"{flag} {symbol}"
        vcp_str = f"基底回檔-{base_50d_pullback*100:.1f}% ➔ 近15日回檔-{recent_15d_pullback*100:.1f}% ➔ 近5日振幅 <b>{curr_5d_range*100:.1f}%</b>"
        support_str = " | ".join(supporting_tags)

        is_strong_red_today = (c_now > o_now) and (c_now > c_prev) and ((c_now - l_now) >= (h_now - l_now) * 0.55 if h_now > l_now else True)

        # ----------------------------------------------------
        # 分類 1：🚀 今日正式突破窄幅樞紐點 (突破前5日振幅 <= 8.5% + 今日首根紅K突破 exact_pivot，且未過度延伸超過 +5%)
        # ----------------------------------------------------
        if (prev_5d_range <= 0.085) and is_strong_red_today and (c_now > exact_pivot) and (c_prev <= exact_pivot) and (c_now <= exact_pivot * 1.05) and (vol_ratio_today >= 1.3):
            vcp_b_str = f"基底回檔-{base_50d_pullback*100:.1f}% ➔ 突破前5日振幅僅 <b>{prev_5d_range*100:.1f}%</b>"
            score = rs_rating + (20 if doubled_from_low else 0) + int(vol_ratio_today * 5)
            breakout_candidates.append((score, symbol, title, c_now, rs_rating, vol_ratio_today, pivot_type_str, vcp_b_str, support_str, stop_price, stop_pct))

        # ----------------------------------------------------
        # 分類 2：🎯 標準 VCP 窄幅樞紐成型池 (強制要求：收盤價 <= 樞紐點 + 近 5 日振幅真正收斂 <= 8.5% + 回檔遞減！)
        # ----------------------------------------------------
        elif (c_now <= exact_pivot) and (c_now >= exact_pivot * 0.93) and (curr_5d_range <= 0.085) and (recent_15d_pullback <= base_50d_pullback * 0.80) and (vol_shrink_ratio <= 0.90):
            dist_pct = ((exact_pivot - c_now) / c_now) * 100
            # 越窄幅(curr_5d_range越小)、離樞紐越近，分數越高！
            score = rs_rating + int((0.10 - curr_5d_range) * 500) + int((0.08 - (exact_pivot - c_now)/exact_pivot) * 200)
            pivot_ready_candidates.append((score, symbol, title, c_now, rs_rating, vol_shrink_ratio * 100, pivot_type_str, dist_pct, vcp_str, support_str, stop_price, stop_pct))

        # ----------------------------------------------------
        # 分類 3：🪝 20MA 量縮洗盤下影線／口袋支點 (收盤 <= 樞紐點 + 回測 20MA 拉下影線量縮守穩)
        # ----------------------------------------------------
        elif (c_now <= exact_pivot) and (l_now <= ma20.iloc[-1] * 1.015) and (c_now >= ma20.iloc[-1]) and ((min(o_now, c_now) - l_now) / (h_now - l_now + 0.001) >= 0.35) and (vol.iloc[-1] <= vol_ma10.iloc[-1]):
            dist_pct = ((exact_pivot - c_now) / c_now) * 100
            score = rs_rating + int((0.12 - curr_5d_range) * 200)
            shakeout_candidates.append((score, symbol, title, c_now, rs_rating, ma20.iloc[-1], pivot_type_str, dist_pct, vcp_str, support_str, stop_price, stop_pct))

        # ----------------------------------------------------
        # 分類 4：⏳ 右側收斂中・等待振幅壓縮至 8% 內 (嚴格規定 c_now <= exact_pivot，排除大綜那種已突破股！)
        # ----------------------------------------------------
        elif (c_now <= exact_pivot) and (c_now >= exact_pivot * 0.90) and (recent_15d_pullback <= base_50d_pullback * 0.90):
            dist_pct = ((exact_pivot - c_now) / c_now) * 100
            score = int((1 - curr_5d_range) * 100) + rs_rating
            wider_vcp_candidates.append((score, symbol, title, c_now, rs_rating, vol_shrink_ratio * 100, pivot_type_str, dist_pct, vcp_str, support_str, stop_price, stop_pct))

    breakout_candidates.sort(key=lambda x: x[0], reverse=True)
    pivot_ready_candidates.sort(key=lambda x: x[0], reverse=True)
    shakeout_candidates.sort(key=lambda x: x[0], reverse=True)
    wider_vcp_candidates.sort(key=lambda x: x[0], reverse=True)

    print("💰 正在為入選標的查詢最新季報 EPS 數據...")
    final_breakouts = []
    for item in breakout_candidates[:6]:
        _, sym, title, c_now, rs, v_ratio, piv_str, vcp_s, sup_s, sp, sp_pct = item
        eps_str = get_eps_info(sym)
        final_breakouts.append(
            f"<b>{title}</b> (收盤: {c_now:.2f} | <b>RS: {rs}</b>)\n"
            f" ├ 🚀 <b>真樞紐突破(可進場)</b>：紅K帶量({v_ratio:.1f}倍)突破 {piv_str}\n"
            f" ├ 📐 收斂結構：{vcp_s}\n"
            f" ├ 💰 獲利表現：{eps_str}\n"
            f" ├ 💡 價量配角：{sup_s}\n"
            f" └ 🛡️ 2xATR停損：<b>{sp:.2f}</b> (-{sp_pct:.1f}%)"
        )

    final_pivot_ready = []
    for item in pivot_ready_candidates[:10]:
        _, sym, title, c_now, rs, v_shrink, piv_str, dist_p, vcp_s, sup_s, sp, sp_pct = item
        eps_str = get_eps_info(sym)
        final_pivot_ready.append(
            f"<b>{title}</b> (收盤: {c_now:.2f} | <b>RS: {rs}</b>)\n"
            f" ├ 🎯 <b>窄幅樞紐成型(差 +{dist_p:.1f}% 突破可買)</b>：{piv_str}｜近5日量 {v_shrink:.0f}%\n"
            f" ├ 📐 收斂結構：{vcp_s}\n"
            f" ├ 💰 獲利表現：{eps_str}\n"
            f" ├ 💡 價量配角：{sup_s}\n"
            f" └ 🛡️ 預估停損：<b>{sp:.2f}</b> (-{sp_pct:.1f}%)"
        )

    final_shakeout = []
    for item in shakeout_candidates[:5]:
        _, sym, title, c_now, rs, ma20_v, piv_str, dist_p, vcp_s, sup_s, sp, sp_pct = item
        eps_str = get_eps_info(sym)
        final_shakeout.append(
            f"<b>{title}</b> (收盤: {c_now:.2f} | <b>RS: {rs}</b>)\n"
            f" ├ 🪝 <b>20MA量縮洗盤守穩</b>：回測月線({ma20_v:.1f})拉下影線｜上檔 {piv_str}\n"
            f" ├ 📐 收斂結構：{vcp_s}\n"
            f" ├ 💰 獲利表現：{eps_str}\n"
            f" ├ 💡 價量配角：{sup_s}\n"
            f" └ 🛡️ 2xATR停損：<b>{sp:.2f}</b> (-{sp_pct:.1f}%)"
        )

    final_wider = []
    # 僅在窄幅池數量較少時，列出最多 5 檔「振幅最小但尚在 8%~12% 收斂中」的觀察股
    for item in wider_vcp_candidates[:5]:
        _, sym, title, c_now, rs, v_shrink, piv_str, dist_p, vcp_s, sup_s, sp, sp_pct = item
        eps_str = get_eps_info(sym)
        final_wider.append(
            f"<b>{title}</b> (收盤: {c_now:.2f} | <b>RS: {rs}</b>)\n"
            f" ├ ⏳ <b>尚在收斂中(差 +{dist_p:.1f}% 至前高)</b>：振幅未達8%內勿提前追高｜{piv_str}\n"
            f" ├ 📐 收斂結構：{vcp_s}\n"
            f" ├ 💰 獲利表現：{eps_str}\n"
            f" ├ 💡 價量配角：{sup_s}\n"
            f" └ 🛡️ 預估停損：<b>{sp:.2f}</b> (-{sp_pct:.1f}%)"
        )

    return final_breakouts, final_pivot_ready, final_shakeout, final_wider, len(raw_data), template_passed_count, clean_passed_count

if __name__ == "__main__":
    stock_dict = get_full_market_stock_list()
    breakouts, pivot_ready, shakeout_pocket, wider_vcp, total_scanned, template_passed, clean_passed = analyze_market_minervini(stock_dict)
    send_minervini_reports(breakouts, pivot_ready, shakeout_pocket, wider_vcp, total_scanned, template_passed, clean_passed)
