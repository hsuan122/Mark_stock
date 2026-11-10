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

    # A. 台股【全上市】普通股 (僅排除日量 < 100 張的殭屍股)
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
# 4. Telegram 推播函式
# ==========================================
def send_telegram_msg(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"❌ 網路連線錯誤：{e}")

def send_minervini_reports(breakouts, vcp_watchlist, stage2_leaders, total_scanned, template_passed, clean_passed):
    all_blocks = []
    if breakouts:
        all_blocks.append("🚀 <b>【一、今日紅K突破買點 (Pivot / VWAP / WP)】</b>\n<i>(通過無綠K爆量檢驗 + 今日帶量突破轉強)</i>")
        all_blocks.extend(breakouts)
    if vcp_watchlist:
        all_blocks.append("\n🎯 <b>【二、標準 VCP 波動收縮量縮池 (Weekly VCP)】</b>\n<i>(右側波幅明顯收斂 + 成交量萎縮低於均量，等突破樞紐)</i>")
        all_blocks.extend(vcp_watchlist)
    if stage2_leaders:
        all_blocks.append("\n📋 <b>【三、Mark 模板乾淨籌碼觀察池 (依波動最窄排序)】</b>\n<i>(通過漲量多跌量縮、無爆量綠K，目前於樞紐點下方整理)</i>")
        all_blocks.extend(stage2_leaders)

    if not all_blocks:
        send_telegram_msg(f"📊 <b>【Mark Minervini 選股雷達 V7】</b>\n掃描 {total_scanned} 檔，通過模板 {template_passed} 檔。")
        return

    chunk_size = 7
    total_pages = (len(all_blocks) + chunk_size - 1) // chunk_size
    for i in range(0, len(all_blocks), chunk_size):
        page = (i // chunk_size) + 1
        chunk = all_blocks[i:i + chunk_size]
        header = (
            f"📊 <b>【Mark Minervini 選股日報 V7】({page}/{total_pages})</b>\n"
            f"<i>掃描 {total_scanned} 檔｜過模板 {template_passed} 檔｜無綠K爆量乾淨圖形 {clean_passed} 檔</i>\n\n"
        )
        send_telegram_msg(header + "\n\n".join(chunk))

# ==========================================
# 5. 主角：Mark Minervini 篩選 + 醜圖封殺 + 智慧收斂排序
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
    clean_stage2_pool = []
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

        # ====================================================
        # 🛡️ 【第二關】四道醜圖封殺令 (精準剔除寶雅、中鼎、大甲、藥華藥、佳大、騰雲)
        # ====================================================
        # 封殺令 1：近半年從高點最大回檔不得超過 32% (淘汰大甲那種跌 42% 的結構破壞股)
        peak_6m_pos = high.iloc[-126:].values.argmax()
        peak_6m = high.iloc[-126:].max()
        low_after_peak = low.iloc[-126 + peak_6m_pos:].min()
        if ((peak_6m - low_after_peak) / peak_6m) > 0.32:
            continue

        # 定義看盤軟體上的「綠 K / 黑 K (收盤 < 開盤)」與「實體下跌綠 K」
        is_black_candle = close < open_p
        is_ugly_green_vol = (close < open_p * 0.992) | (close < close.shift(1) * 0.985)
        is_red_candle = (close >= open_p) & (close >= close.shift(1))

        # 封殺令 2：過去 60 天內，最大量的前 2 根 K 棒絕不能是黑K/綠K！(直接淘汰寶雅 8.4萬張綠K、中鼎 2.29萬張綠K)
        top2_vol_dates = vol.iloc[-60:].nlargest(2).index
        if (is_black_candle.loc[top2_vol_dates] | (close.loc[top2_vol_dates] < close.shift(1).loc[top2_vol_dates])).any():
            continue

        # 過去 30 天內，不得出現「成交量 >= 20日均量 2.0 倍」的大綠K 或 爆量長上影線倒貨 (淘汰佳大 11/05)
        vol_spike_2x = vol.iloc[-30:] >= (vol_ma20.iloc[-30:] * 2.0)
        wick_dump = ((high.iloc[-15:] - close.iloc[-15:]) / high.iloc[-15:] > 0.045) & (vol.iloc[-15:] >= vol_ma20.iloc[-15:] * 1.6)
        if (is_ugly_green_vol.iloc[-30:] & vol_spike_2x).any() or wick_dump.any():
            continue

        # 今日不得為出量下跌黑K (淘汰藥華藥 11/10)
        vol_ratio_today = vol.iloc[-1] / vol_ma20.iloc[-2] if vol_ma20.iloc[-2] > 0 else 1.0
        if ((c_now < o_now) or (c_now < c_prev)) and (vol_ratio_today >= 1.2):
            continue

        # 封殺令 3：漲量多、跌量縮 (過去 25 日紅K平均量 >= 綠K平均量，或近 5 日量縮至 20日均量 80% 以下)
        red_v25 = vol.iloc[-25:][is_red_candle.iloc[-25:]]
        green_v25 = vol.iloc[-25:][is_black_candle.iloc[-25:]]
        avg_red_v = red_v25.mean() if len(red_v25) > 0 else 0
        avg_green_v = green_v25.mean() if len(green_v25) > 0 else 1
        vol_5d_avg = vol.iloc[-5:].mean()
        vol_shrink_ratio = vol_5d_avg / vol_ma20.iloc[-1] if vol_ma20.iloc[-1] > 0 else 1.0

        if (avg_red_v < avg_green_v * 0.95) and (vol_shrink_ratio > 0.75):
            continue

        # 封殺令 4：不追高原則 (距離前 20 日樞紐高點不得超過 +7%，淘汰騰雲那種已噴遠的股票)
        pivot_high = high.iloc[-21:-1].max()
        dist_from_pivot = (c_now - pivot_high) / pivot_high
        if dist_from_pivot > 0.07 or dist_from_pivot < -0.18:
            continue

        clean_passed_count += 1

        # ====================================================
        # 📐 【第三關】改良版 VCP 波動收縮計算 (基底最大振幅 vs 近10日/近5日收窄度)
        # ====================================================
        # 計算過去 45 天整理基底的最大高低落差 (Base Depth)，對比近 15 天與近 5 天的收窄幅度
        base_45d_drop = abs((low.iloc[-46:-1].min() / high.iloc[-46:-1].max()) - 1)
        recent_15d_drop = abs((low.iloc[-16:-1].min() / high.iloc[-16:-1].max()) - 1)
        recent_5d_drop = abs((low.iloc[-6:-1].min() / high.iloc[-6:-1].max()) - 1)

        # 只要近期 15 日或 5 日波幅明顯比 45 日基底收斂，且近 5 日波幅 <= 8.5%，即符合右側收縮！
        is_vcp_tight = (
            (recent_15d_drop <= base_45d_drop * 0.75 or recent_5d_drop <= base_45d_drop * 0.55) and
            (recent_5d_drop <= 0.085)
        )

        # 計算 2x ATR 停損價 (最大 10%)
        tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
        atr20 = tr.rolling(20).mean().iloc[-1]
        stop_distance = min(2 * atr20, c_now * 0.10)
        stop_price = c_now - stop_distance
        stop_pct = (stop_distance / c_now) * 100

        # ====================================================
        # 🧩 【配角指標計算】
        # ====================================================
        supporting_tags = [f"📊紅/綠均量比 {avg_red_v/avg_green_v:.1f}x"]
        if doubled_from_low:
            supporting_tags.append("👑低點翻倍")

        peak_60_idx = high.iloc[-61:-1].idxmax()
        sub_df = df.loc[peak_60_idx:]
        tp = (sub_df['High'] + sub_df['Low'] + sub_df['Close']) / 3
        vwap_series = (tp * sub_df['Volume']).cumsum() / sub_df['Volume'].cumsum()
        vwap_now = vwap_series.iloc[-1]
        cross_vwap = (c_now > vwap_now) and (c_prev <= vwap_series.iloc[-2] if len(vwap_series) > 1 else False)
        if cross_vwap:
            supporting_tags.append(f"📈剛站上VWAP({vwap_now:.1f})")
        elif c_now > vwap_now:
            supporting_tags.append(f"✅VWAP之上({vwap_now:.1f})")

        ema10 = close.ewm(span=10, adjust=False).mean()
        ema20 = close.ewm(span=20, adjust=False).mean()
        wedge_pop = (low.iloc[-6:-1] <= ema20.iloc[-6:-1] * 1.01).any() and (c_now > ema10.iloc[-1]) and (c_now > ema20.iloc[-1]) and (c_prev <= ema10.iloc[-2])
        if wedge_pop:
            supporting_tags.append("⚡Wedge Pop")

        if is_red_candle.iloc[-1] and (vol_ratio_today >= 1.8):
            supporting_tags.append(f"🔥Zanger爆量({vol_ratio_today:.1f}倍)")

        flag = "🇹🇼" if is_tw else "🇺🇸"
        title = f"{flag} {symbol} {name}" if name != symbol else f"{flag} {symbol}"
        vcp_str = f"基底-{base_45d_drop*100:.1f}% ➔ 近15日-{recent_15d_drop*100:.1f}% ➔ 近5日-{recent_5d_drop*100:.1f}%"
        support_str = " | ".join(supporting_tags)

        is_strong_red_today = (c_now > o_now) and (c_now > c_prev) and ((c_now - l_now) >= (h_now - l_now) * 0.5 if h_now > l_now else True)

        # 分類 1：🚀 今日紅 K 帶量突破 (突破前高樞紐 / 剛站上波段VWAP / Wedge Pop)
        if is_strong_red_today and (vol_ratio_today >= 1.25) and ((c_now > pivot_high and c_prev <= pivot_high) or cross_vwap or wedge_pop):
            score = rs_rating + (20 if doubled_from_low else 0) + int(vol_ratio_today * 5)
            breakout_candidates.append((score, symbol, title, c_now, rs_rating, vol_ratio_today, pivot_high, vcp_str, support_str, stop_price, stop_pct))

        # 分類 2：🎯 標準 VCP 量縮築底池 (符合右側收斂 + 近期量縮 <= 20日均量的 90%)
        elif is_vcp_tight and (vol_shrink_ratio <= 0.90) and (c_now <= pivot_high):
            score = rs_rating + int((1 - recent_5d_drop) * 30) + (15 if doubled_from_low else 0)
            vcp_candidates.append((score, symbol, title, c_now, rs_rating, vol_shrink_ratio * 100, pivot_high, vcp_str, support_str, stop_price, stop_pct))

        # 分類 3：📋 乾淨籌碼 Stage 2 觀察池 (所有通過四道封殺令的好圖，依「近5日波幅最窄」排序作為保底)
        else:
            tightness_score = int((1 - recent_5d_drop) * 100) + rs_rating
            clean_stage2_pool.append((tightness_score, symbol, title, c_now, rs_rating, vol_shrink_ratio * 100, pivot_high, vcp_str, support_str, stop_price, stop_pct))

    breakout_candidates.sort(key=lambda x: x[0], reverse=True)
    vcp_candidates.sort(key=lambda x: x[0], reverse=True)
    clean_stage2_pool.sort(key=lambda x: x[0], reverse=True)

    print("💰 正在為入選標的查詢最新季報 EPS 數據...")
    final_breakouts = []
    for item in breakout_candidates[:8]:
        _, sym, title, c_now, rs, v_ratio, piv, vcp_s, sup_s, sp, sp_pct = item
        eps_str = get_eps_info(sym)
        final_breakouts.append(
            f"<b>{title}</b> (收盤: {c_now:.2f} | <b>RS: {rs}</b>)\n"
            f" ├ 🚀 <b>突破轉強</b>：今日紅K量增({v_ratio:.1f}倍)｜前高樞紐 <b>{piv:.2f}</b>\n"
            f" ├ 📐 波動收縮：{vcp_s}\n"
            f" ├ 💰 獲利表現：{eps_str}\n"
            f" ├ 💡 價量配角：{sup_s}\n"
            f" └ 🛡️ 2xATR停損：<b>{sp:.2f}</b> (-{sp_pct:.1f}%)"
        )

    final_vcp = []
    for item in vcp_candidates[:10]:
        _, sym, title, c_now, rs, v_shrink, piv, vcp_s, sup_s, sp, sp_pct = item
        eps_str = get_eps_info(sym)
        final_vcp.append(
            f"<b>{title}</b> (收盤: {c_now:.2f} | <b>RS: {rs}</b>)\n"
            f" ├ 🎯 <b>VCP量縮收斂</b>：近5日量為均量 {v_shrink:.0f}%｜突破 <b>{piv:.2f}</b> 轉強\n"
            f" ├ 📐 波動收縮：{vcp_s}\n"
            f" ├ 💰 獲利表現：{eps_str}\n"
            f" ├ 💡 價量配角：{sup_s}\n"
            f" └ 🛡️ 預估停損：<b>{sp:.2f}</b> (-{sp_pct:.1f}%)"
        )

    # 若前兩類合計不足 8 檔，自動補上「第三類：乾淨籌碼觀察池」最收斂的前幾名，保證天天有精選清單！
    final_stage2 = []
    need_count = max(5, 12 - (len(final_breakouts) + len(final_vcp)))
    for item in clean_stage2_pool[:need_count]:
        _, sym, title, c_now, rs, v_shrink, piv, vcp_s, sup_s, sp, sp_pct = item
        eps_str = get_eps_info(sym)
        final_stage2.append(
            f"<b>{title}</b> (收盤: {c_now:.2f} | <b>RS: {rs}</b>)\n"
            f" ├ 📋 <b>模板精選</b>：近5日量為均量 {v_shrink:.0f}%｜關鍵樞紐 <b>{piv:.2f}</b>\n"
            f" ├ 📐 波動收縮：{vcp_s}\n"
            f" ├ 💰 獲利表現：{eps_str}\n"
            f" ├ 💡 價量配角：{sup_s}\n"
            f" └ 🛡️ 預估停損：<b>{sp:.2f}</b> (-{sp_pct:.1f}%)"
        )

    return final_breakouts, final_vcp, final_stage2, len(raw_data), template_passed_count, clean_passed_count

if __name__ == "__main__":
    stock_dict = get_full_market_stock_list()
    breakouts, vcp_watch, stage2_leaders, total_scanned, template_passed, clean_passed = analyze_market_minervini(stock_dict)
    send_minervini_reports(breakouts, vcp_watch, stage2_leaders, total_scanned, template_passed, clean_passed)
