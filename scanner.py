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

    # A. 台股【全上市】普通股 (僅排除日量 < 100 張的無流動性殭屍股)
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

    # B. 台股【全上櫃】普通股
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

    # C. 美股【S&P 500 + Nasdaq 100 + 熱門成長股】
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
# 3. 抓取入選股票的近期 EPS 與年增率 (僅對最終入選股查詢，速度快)
# ==========================================
def get_eps_info(symbol):
    try:
        tk = yf.Ticker(symbol)
        info = tk.info or {}
        ttm_eps = info.get('trailingEps')
        q_growth = info.get('earningsQuarterlyGrowth')

        # 嘗試從季損益表抓取最新單季 EPS
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
            parts.append(f"近四季合計: <b>{ttm_eps:.2f}</b>")
        if q_growth is not None:
            sign = "+" if q_growth >= 0 else ""
            parts.append(f"單季年增: <b>{sign}{q_growth*100:.1f}%</b>")

        return " ｜ ".join(parts) if parts else "財報更新中"
    except Exception:
        return "財報更新中"

# ==========================================
# 4. Telegram 推播函式
# ==========================================
def send_telegram_msg(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"❌ 網路連線錯誤：{e}")

def send_minervini_reports(breakouts, vcp_watchlist, total_scanned, template_passed):
    all_blocks = []
    if breakouts:
        all_blocks.append("🚀 <b>【一、今日突破樞紐買點 (Pivot Breakout)】</b>\n<i>(符合 Mark 模板 + 漲量多跌量縮 + VCP 收斂後紅K突破)</i>")
        all_blocks.extend(breakouts)
    if vcp_watchlist:
        all_blocks.append("\n🎯 <b>【二、每週 VCP 波動收縮觀察池 (Weekly Watchlist)】</b>\n<i>(符合 Mark 模板 + 嚴格漲量多跌量縮、無爆量綠K，等待突破)</i>")
        all_blocks.extend(vcp_watchlist)

    if not all_blocks:
        send_telegram_msg(f"📊 <b>【Mark Minervini 主角選股雷達 V6】</b>\n已掃描 {total_scanned} 檔，通過趨勢模板共 {template_passed} 檔，今日無同時符合「嚴格漲量多跌量縮 + 無綠K爆量 + VCP遞減」之標的。")
        return

    chunk_size = 7
    total_pages = (len(all_blocks) + chunk_size - 1) // chunk_size
    for i in range(0, len(all_blocks), chunk_size):
        page = (i // chunk_size) + 1
        chunk = all_blocks[i:i + chunk_size]
        header = (
            f"📊 <b>【Mark Minervini 選股日報 V6】({page}/{total_pages})</b>\n"
            f"<i>掃描 {total_scanned} 檔｜通過趨勢模板 {template_passed} 檔｜已徹底封殺綠K爆量股</i>\n\n"
        )
        send_telegram_msg(header + "\n\n".join(chunk))

# ==========================================
# 5. 主角：Mark Minervini 嚴格價量與 VCP 引擎
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
    vcp_candidates = []
    template_passed_count = 0

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

        ma20 = close.rolling(20).mean()
        ma50 = close.rolling(50).mean()
        ma150 = close.rolling(150).mean()
        ma200 = close.rolling(200).mean()
        vol_ma20 = vol.rolling(20).mean()
        vol_ma50 = vol.rolling(50).mean()

        if vol_ma20.iloc[-1] < 250000:
            continue

        # ====================================================
        # 👑 【第一關】Mark Minervini 趨勢模板 (Trend Template)
        # ====================================================
        high_52w = high.iloc[-252:].max()
        low_52w = low.iloc[-252:].min()
        ma200_rising_3m = (ma200.iloc[-1] > ma200.iloc[-21] > ma200.iloc[-42] > ma200.iloc[-63])
        rolling_52w_high = high.rolling(252).max()
        made_high_in_6m = (high.iloc[-126:] >= rolling_52w_high.iloc[-126:] * 0.99).any()

        trend_template_pass = (
            (c_now > ma50.iloc[-1] > ma150.iloc[-1] > ma200.iloc[-1]) and
            ma200_rising_3m and
            (c_now >= low_52w * 1.30) and
            (c_now >= high_52w * 0.78) and
            made_high_in_6m and
            (rs_rating >= 80)
        )
        if not trend_template_pass:
            continue

        template_passed_count += 1
        doubled_from_low = (c_now >= low_52w * 2.0)

        # 排除基底從高點崩跌超過 30% 的結構破壞股
        peak_6m = high.iloc[-126:].max()
        peak_6m_pos = high.iloc[-126:].values.argmax()
        low_after_peak = low.iloc[-126 + peak_6m_pos:].min()
        if ((peak_6m - low_after_peak) / peak_6m) > 0.30:
            continue

        # ====================================================
        # 👑 【第二關】徹底落實「漲量多、跌量縮」與「封殺綠 K 爆量」
        # ====================================================
        # 關鍵修正：對齊看盤軟體！只要「收盤 < 開盤 (黑K/綠K，如中鼎10/05開高走低)」或「收盤 < 昨收」，一律視為綠K下跌量！
        is_green_bar = (close < open_p) | (close < close.shift(1))
        # 真紅K定義：收盤 > 開盤 且 收盤 > 昨收 且 未留超長上影線
        is_true_red_bar = (close > open_p) & (close > close.shift(1)) & ((high - close) <= (close - low) * 1.5)

        # 鐵律 1：過去 60 個交易日內，成交量最大的前 3 根 K 棒，絕對不能有綠 K！(直接淘汰寶雅 08/12 的 8.4萬張綠K、中鼎 10/05 的 2.29萬張綠K)
        recent_60_vol = vol.iloc[-60:]
        top3_vol_dates = recent_60_vol.nlargest(3).index
        if is_green_bar.loc[top3_vol_dates].any():
            continue

        # 鐵律 2：過去 30 個交易日內，嚴禁出現任何「成交量 >= 20日均量 1.55 倍」的綠 K 或長上影線倒貨 K 棒！
        recent_30_green = is_green_bar.iloc[-30:]
        recent_30_vol_spike = vol.iloc[-30:] >= (vol_ma20.iloc[-30:] * 1.55)
        upper_wick_dump_30 = ((high.iloc[-30:] - close.iloc[-30:]) / high.iloc[-30:] > 0.038) & (vol.iloc[-30:] >= vol_ma20.iloc[-30:] * 1.45)
        if (recent_30_green & recent_30_vol_spike).any() or upper_wick_dump_30.any():
            continue

        # 鐵律 3：嚴格「漲量多、跌量縮」(過去 25 日內，真紅K平均量必須 >= 綠K平均量的 1.20 倍！)
        red_vols_25 = vol.iloc[-25:][is_true_red_bar.iloc[-25:]]
        green_vols_25 = vol.iloc[-25:][is_green_bar.iloc[-25:]]
        avg_red_vol = red_vols_25.mean() if len(red_vols_25) > 0 else 0
        avg_green_vol = green_vols_25.mean() if len(green_vols_25) > 0 else 1
        if avg_red_vol < (avg_green_vol * 1.20):
            continue

        # 鐵律 4：近期回檔/整理中的綠 K 必須明顯量縮 (近 10 日內綠 K 平均量 < 20 日均量的 85%)
        green_vols_10 = vol.iloc[-10:][is_green_bar.iloc[-10:]]
        if len(green_vols_10) > 0 and (green_vols_10.mean() > vol_ma20.iloc[-1] * 0.85):
            continue

        # ====================================================
        # 👑 【第三關】嚴格 VCP 波動收縮遞減 (左 > 中 > 右)
        # ====================================================
        c1_drop = abs((low.iloc[-46:-31].min() / high.iloc[-46:-31].max()) - 1)
        c2_drop = abs((low.iloc[-31:-16].min() / high.iloc[-31:-16].max()) - 1)
        c3_drop = abs((low.iloc[-16:-1].min() / high.iloc[-16:-1].max()) - 1)
        recent_5d_drop = abs((low.iloc[-6:-1].min() / high.iloc[-6:-1].max()) - 1)

        pivot_high = high.iloc[-21:-1].max()
        dist_from_pivot = (c_now - pivot_high) / pivot_high
        if dist_from_pivot > 0.06:  # 突破樞紐點超過 6% 視為已噴出不追高
            continue

        # 強制規定：右段波動(c3)必須同時小於左段(c1)與中段(c2)，絕不允許像 LLY/中鼎 那樣往右發散變大！
        strict_vcp_shrinking = (
            (c3_drop < c1_drop * 0.85) and
            (c3_drop <= c2_drop * 0.90) and
            (c3_drop <= 0.10) and
            (recent_5d_drop <= 0.06)
        )
        if not strict_vcp_shrinking:
            continue

        vol_5d_avg = vol.iloc[-6:-1].mean()
        vol_ratio_today = vol.iloc[-1] / vol_ma20.iloc[-2] if vol_ma20.iloc[-2] > 0 else 1.0

        # 計算 2x ATR 停損價 (最大 10%)
        tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
        atr20 = tr.rolling(20).mean().iloc[-1]
        stop_distance = min(2 * atr20, c_now * 0.10)
        stop_price = c_now - stop_distance
        stop_pct = (stop_distance / c_now) * 100

        # ====================================================
        # 🧩 【配角指標計算】
        # ====================================================
        supporting_tags = [f"📊紅K均量為綠K之{avg_red_vol/avg_green_vol:.1f}倍"]
        if doubled_from_low:
            supporting_tags.append("👑低點翻倍")

        peak_60_idx = high.iloc[-61:-1].idxmax()
        sub_df = df.loc[peak_60_idx:]
        tp = (sub_df['High'] + sub_df['Low'] + sub_df['Close']) / 3
        vwap_series = (tp * sub_df['Volume']).cumsum() / sub_df['Volume'].cumsum()
        vwap_now = vwap_series.iloc[-1]
        if c_now > vwap_now:
            supporting_tags.append(f"✅高點VWAP之上({vwap_now:.1f})")

        ema10 = close.ewm(span=10, adjust=False).mean()
        ema20 = close.ewm(span=20, adjust=False).mean()
        if (low.iloc[-6:-1] < ema20.iloc[-6:-1]).any() and (c_now > ema10.iloc[-1]) and (c_now > ema20.iloc[-1]):
            supporting_tags.append("⚡Wedge Pop")

        if is_true_red_bar.iloc[-1] and (vol_ratio_today >= 1.8):
            supporting_tags.append(f"🔥Zanger爆量({vol_ratio_today:.1f}倍)")

        flag = "🇹🇼" if is_tw else "🇺🇸"
        title = f"{flag} {symbol} {name}" if name != symbol else f"{flag} {symbol}"
        vcp_str = f"左-{c1_drop*100:.1f}% ➔ 中-{c2_drop*100:.1f}% ➔ 右-{c3_drop*100:.1f}% (近5日-{recent_5d_drop*100:.1f}%)"
        support_str = " | ".join(supporting_tags)

        # 分類 1：🚀 今日真紅 K 帶量突破樞紐買點
        if is_true_red_bar.iloc[-1] and (c_now > pivot_high) and (c_prev <= pivot_high) and (vol_ratio_today >= 1.4):
            score = rs_rating + (20 if doubled_from_low else 0)
            breakout_candidates.append((score, symbol, title, c_now, rs_rating, vol_ratio_today, pivot_high, vcp_str, support_str, stop_price, stop_pct, 0))

        # 分類 2：🎯 每週 VCP 量縮收斂觀察池 (近5日均量 <= 50日均量的 75%，且股價緊貼樞紐點下方 7% 內)
        elif (vol_5d_avg <= vol_ma50.iloc[-2] * 0.75) and (c_now <= pivot_high) and (c_now >= pivot_high * 0.93) and (c_now >= ma20.iloc[-1] * 0.98):
            score = rs_rating + (15 if doubled_from_low else 0) + int((1 - recent_5d_drop) * 20)
            vol_shrink_pct = (vol_5d_avg / vol_ma50.iloc[-2]) * 100
            vcp_candidates.append((score, symbol, title, c_now, rs_rating, vol_shrink_pct, pivot_high, vcp_str, support_str, stop_price, stop_pct, 1))

    breakout_candidates.sort(key=lambda x: x[0], reverse=True)
    vcp_candidates.sort(key=lambda x: x[0], reverse=True)

    # 僅對最終精選出的前 10 檔突破股與前 12 檔 VCP 觀察股查詢最新 EPS，確保極速完成
    print("💰 正在為入選標的查詢最新季報 EPS 數據...")
    final_breakouts = []
    for item in breakout_candidates[:10]:
        _, sym, title, c_now, rs, v_ratio, piv, vcp_s, sup_s, sp, sp_pct, _ = item
        eps_str = get_eps_info(sym)
        msg = (
            f"<b>{title}</b> (收盤: {c_now:.2f} | <b>RS: {rs}</b>)\n"
            f" ├ 👑 <b>Mark突破買點</b>：紅K帶量({v_ratio:.1f}倍)突破樞紐點 <b>{piv:.2f}</b>\n"
            f" ├ 📐 VCP遞減：{vcp_s}\n"
            f" ├ 💰 獲利表現：{eps_str}\n"
            f" ├ 💡 價量與配角：{sup_s}\n"
            f" └ 🛡️ 2xATR停損：<b>{sp:.2f}</b> (-{sp_pct:.1f}%)"
        )
        final_breakouts.append(msg)

    final_vcp = []
    for item in vcp_candidates[:12]:
        _, sym, title, c_now, rs, v_shrink, piv, vcp_s, sup_s, sp, sp_pct, _ = item
        eps_str = get_eps_info(sym)
        msg = (
            f"<b>{title}</b> (收盤: {c_now:.2f} | <b>RS: {rs}</b>)\n"
            f" ├ 🎯 <b>VCP量縮築底</b>：近5日量縮至均量 {v_shrink:.0f}%｜等待突破樞紐 <b>{piv:.2f}</b>\n"
            f" ├ 📐 VCP遞減：{vcp_s}\n"
            f" ├ 💰 獲利表現：{eps_str}\n"
            f" ├ 💡 價量與配角：{sup_s}\n"
            f" └ 🛡️ 預估停損：<b>{sp:.2f}</b> (-{sp_pct:.1f}%)"
        )
        final_vcp.append(msg)

    return final_breakouts, final_vcp, len(raw_data), template_passed_count

if __name__ == "__main__":
    stock_dict = get_full_market_stock_list()
    breakouts, vcp_watch, total_scanned, template_passed = analyze_market_minervini(stock_dict)
    send_minervini_reports(breakouts, vcp_watch, total_scanned, template_passed)
