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
# 2. 全市場 9,000+ 檔股票池抓取 (台股全上市櫃 + 美股 NYSE/NASDAQ/AMEX 全市場)
# ==========================================
def get_entire_market_universe():
    tw_dict = {}
    us_dict = {}
    total_raw_count = 0

    # A. 台股【全上市】普通股
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=15)
        if res.status_code == 200:
            twse_df = pd.DataFrame(res.json())
            twse_df = twse_df[twse_df['Code'].str.match(r'^[1-9]\d{3}$')]
            total_raw_count += len(twse_df)
            twse_df['ClosingPrice'] = pd.to_numeric(twse_df['ClosingPrice'].str.replace(',', ''), errors='coerce')
            twse_df['TradeVolume'] = pd.to_numeric(twse_df['TradeVolume'].str.replace(',', ''), errors='coerce')
            valid_twse = twse_df[(twse_df['ClosingPrice'] > 0) & (twse_df['TradeVolume'] >= 30000)]
            for _, row in valid_twse.iterrows():
                tw_dict[f"{row['Code']}.TW"] = row['Name'].strip()
    except Exception as e:
        print(f"⚠️ 上市清單抓取異常: {e}")

    # B. 台股【全上櫃】普通股
    try:
        res_otc = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", timeout=15)
        if res_otc.status_code == 200:
            tpex_df = pd.DataFrame(res_otc.json())
            tpex_df = tpex_df[tpex_df['SecuritiesCompanyCode'].str.match(r'^[1-9]\d{3}$')]
            total_raw_count += len(tpex_df)
            tpex_df['Close'] = pd.to_numeric(tpex_df['Close'].str.replace(',', ''), errors='coerce')
            tpex_df['TradingShares'] = pd.to_numeric(tpex_df['TradingShares'].str.replace(',', ''), errors='coerce')
            valid_tpex = tpex_df[(tpex_df['Close'] > 0) & (tpex_df['TradingShares'] >= 30000)]
            for _, row in valid_tpex.iterrows():
                tw_dict[f"{row['SecuritiesCompanyCode']}.TWO"] = row['CompanyName'].strip()
    except Exception as e:
        print(f"⚠️ 上櫃清單抓取異常: {e}")

    # C. 美股【NYSE + NASDAQ + AMEX 全市場】串接 NASDAQ 官方 Screener API
    nasdaq_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Origin': 'https://www.nasdaq.com',
        'Referer': 'https://www.nasdaq.com/'
    }
    try:
        us_url = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&download=true"
        us_res = requests.get(us_url, headers=nasdaq_headers, timeout=20)
        if us_res.status_code == 200:
            rows = us_res.json().get('data', {}).get('rows', [])
            total_raw_count += len(rows)
            us_df = pd.DataFrame(rows)
            us_df = us_df[us_df['symbol'].str.match(r'^[A-Z]{1,5}$', na=False)]
            us_df['price'] = pd.to_numeric(us_df['lastsale'].astype(str).str.replace('$', '').str.replace(',', ''), errors='coerce')
            us_df['vol'] = pd.to_numeric(us_df['volume'].astype(str).str.replace(',', ''), errors='coerce')
            us_df['mcap'] = pd.to_numeric(us_df['marketCap'].astype(str).str.replace(',', ''), errors='coerce')
            us_df['dollar_vol'] = us_df['price'] * us_df['vol']

            active_us = us_df[(us_df['price'] >= 8.0) & (us_df['mcap'] >= 150_000_000) & (us_df['vol'] >= 100_000)]
            active_us = active_us.sort_values(by='dollar_vol', ascending=False).head(1600)
            for _, row in active_us.iterrows():
                sym = str(row['symbol']).strip()
                us_dict[sym] = sym
    except Exception as e:
        print(f"⚠️ NASDAQ API 異常，切換備援清單: {e}")

    if len(us_dict) < 100:
        try:
            sp_res = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", headers=nasdaq_headers, timeout=15)
            if sp_res.status_code == 200:
                sp_table = pd.read_html(io.StringIO(sp_res.text))[0]
                for sym in sp_table['Symbol'].tolist():
                    s = str(sym).replace('.', '-').strip()
                    us_dict[s] = s
                total_raw_count += len(us_dict)
        except Exception:
            pass

    all_dict = {**tw_dict, **us_dict}
    print(f"✅ 全市場母體共 {total_raw_count} 檔 (台股 {len(tw_dict)} 檔 + 美股活躍股 {len(us_dict)} 檔進入 K 線深度檢驗)！")
    return all_dict, total_raw_count

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
# 4. 《超級績效》嚴格遞減 VCP 識別器 (強制 T1 > T2 > T3，否則直接淘汰！)
# ==========================================
def seg_amplitude(h_s, l_s):
    h_max = float(h_s.max())
    l_min = float(l_s.min())
    return (h_max - l_min) / h_max if h_max > 0 else 0.0

def find_strict_vcp_structure(high_s, low_s):
    """
    掃描《超級績效》標準打底週期 (5週 ~ 22週，即 25 ~ 110 個交易日)，
    尋找符合「T1 > T2 > T3 嚴格遞減」的 VCP 結構。
    右側 T3 固定包含最近 5 個交易日，確保不會把近幾日暴漲暴跌的股票誤判為收斂！
    若沒有任何週期符合 T1 > T2 > T3，回傳 is_valid = False。
    """
    # 計算最近 5 日 (含今日) 與 突破前 5 日 (不含今日) 的右側緊密度
    t3_curr_5d = seg_amplitude(high_s.iloc[-5:], low_s.iloc[-5:])
    t3_prev_5d = seg_amplitude(high_s.iloc[-6:-1], low_s.iloc[-6:-1])

    best_match = None
    best_ratio_score = -1.0

    # 測試 5週(25日)、7週(35日)、10週(50日)、13週(65日)、17週(85日)、22週(110日) 基底
    for base_days in [25, 35, 50, 65, 85, 110]:
        if len(high_s) < base_days + 5:
            continue

        base_h = high_s.iloc[-(base_days + 1):-1]
        base_l = low_s.iloc[-(base_days + 1):-1]
        base_peak = float(base_h.max())
        base_min = float(base_l.min())
        base_depth = (base_peak - base_min) / base_peak if base_peak > 0 else 1.0

        # 基底總深度不可超過 35%
        if base_depth > 0.35:
            continue

        # 右段 T3 取最近 6 日，剩餘天數切分為左段 T1 (前 55%) 與中段 T2 (後 45%)
        t3_days = 6
        rem = base_days - t3_days
        t1_days = int(rem * 0.55)
        t2_days = rem - t1_days

        t1_amp = seg_amplitude(base_h.iloc[:t1_days], base_l.iloc[:t1_days])
        t2_amp = seg_amplitude(base_h.iloc[t1_days:t1_days + t2_days], base_l.iloc[t1_days:t1_days + t2_days])
        # 右側 T3 同時考量包含今日的近 5 日波幅，防止近 5 日大波動混入
        t3_amp = max(seg_amplitude(base_h.iloc[-t3_days:], base_l.iloc[-t3_days:]), t3_curr_5d)
        t3_breakout_amp = max(seg_amplitude(base_h.iloc[-t3_days:], base_l.iloc[-t3_days:]), t3_prev_5d)

        # 【核心鐵律】強制要求 T1 > T2 * 1.12 且 T2 > T3 * 1.12 (每一波至少收斂 12% 以上)，且 T3 <= 9.5%！
        is_strict_watch = (t1_amp > t2_amp * 1.12) and (t2_amp > t3_amp * 1.12) and (t3_amp <= 0.095)
        is_strict_brk = (t1_amp > t2_amp * 1.12) and (t2_amp > t3_breakout_amp * 1.12) and (t3_breakout_amp <= 0.095)

        if is_strict_watch or is_strict_brk:
            eff_t3 = t3_amp if is_strict_watch else t3_breakout_amp
            # 收斂比例越顯著、右側 T3 越窄，得分越高
            ratio_score = (t1_amp / (eff_t3 + 0.01)) + (0.10 - eff_t3) * 20
            if ratio_score > best_ratio_score:
                best_ratio_score = ratio_score
                right_12d_high = float(high_s.iloc[-13:-1].max())
                pivot_price = right_12d_high if right_12d_high >= base_peak * 0.90 else float(high_s.iloc[-26:-1].max())
                best_match = (
                    True,
                    round(base_days / 5.0, 1),
                    t1_amp,
                    t2_amp,
                    eff_t3,
                    pivot_price,
                    is_strict_watch,
                    is_strict_brk
                )

    if best_match is not None:
        return best_match
    return (False, 0, 0, 0, 0, 0, False, False)

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

def send_minervini_reports(cat1_breakout, cat2_vcp, cat3_tennis, total_universe, kline_scanned, template_passed, vcp_passed):
    all_blocks = []
    if cat1_breakout:
        all_blocks.append("🚀 <b>【一、樞紐點帶量突破買點 (Pivot Breakout)】</b>\n<i>(嚴格符合 T1 > T2 > T3 遞減 + 今日紅K帶量突破樞紐高點)</i>")
        all_blocks.extend(cat1_breakout)
    if cat2_vcp:
        all_blocks.append("\n🎯 <b>【二、標準 VCP 嚴格遞減收斂池 (T1 > T2 > T3 ≤ 9.5%)】</b>\n<i>(100% 滿足 T1 > T2 > T3 波幅遞減、量縮整理，緊貼樞紐等突破)</i>")
        all_blocks.extend(cat2_vcp)
    if cat3_tennis:
        all_blocks.append("\n🎾 <b>【三、VCP 遞減 + 回測月線守穩／網球反彈】</b>\n<i>(同樣嚴格滿足 T1 > T2 > T3 遞減，且近期回測 20MA 支撐守穩收紅)</i>")
        all_blocks.extend(cat3_tennis)

    if not all_blocks:
        send_telegram_msg(f"📊 <b>【《超級績效》全市場選股雷達】</b>\n母體 {total_universe} 檔｜K線掃描 {kline_scanned} 檔｜過模板 {template_passed} 檔。")
        return

    chunk_size = 7
    total_pages = (len(all_blocks) + chunk_size - 1) // chunk_size
    for i in range(0, len(all_blocks), chunk_size):
        page = (i // chunk_size) + 1
        chunk = all_blocks[i:i + chunk_size]
        header = (
            f"📊 <b>【《超級績效》全市場 VCP 日報】({page}/{total_pages})</b>\n"
            f"<i>母體 {total_universe} 檔｜掃描 {kline_scanned} 檔｜過八大模板 {template_passed} 檔｜嚴格 T1>T2>T3 共 {vcp_passed} 檔</i>\n\n"
        )
        send_telegram_msg(header + "\n\n".join(chunk))

# ==========================================
# 6. 《超級績效》主引擎 (全分類強制 T1 > T2 > T3)
# ==========================================
def analyze_market_minervini(stock_dict):
    symbols = list(stock_dict.keys())
    raw_data = {}
    tw_rs_scores = {}
    us_rs_scores = {}

    start_date = (pd.Timestamp.now() - pd.DateOffset(months=15)).strftime('%Y-%m-%d')
    batch_size = 180

    print(f"📥 正在高速批次下載 {len(symbols)} 檔台美股歷史 K 線...")
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        try:
            data = yf.download(batch, start=start_date, group_by="ticker", threads=True, progress=False)
            for sym in batch:
                try:
                    df = data[sym].copy() if len(batch) > 1 else data.copy()
                    df = df[['Open', 'High', 'Low', 'Close', 'Volume']].dropna()
                    df = df[df['Volume'] > 0]
                    if len(df) < 252:
                        continue
                    
                    c = df['Close']
                    raw_data[sym] = df
                    
                    r3m = (c.iloc[-1] / c.iloc[-63]) - 1
                    r6m = (c.iloc[-1] / c.iloc[-126]) - 1
                    r9m = (c.iloc[-1] / c.iloc[-189]) - 1
                    r12m = (c.iloc[-1] / c.iloc[-250]) - 1
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

    tw_breakout, us_breakout = [], []
    tw_vcp, us_vcp = [], []
    tw_tennis, us_tennis = [], []
    template_passed_count = 0
    vcp_passed_count = 0

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
        vol_ma20 = vol.rolling(20).mean()
        vol_ma50 = vol.rolling(50).mean()

        if vol_ma20.iloc[-1] < 200000:
            continue

        # ====================================================
        # 👑 第一關：《超級績效》八大趨勢模板 (8-Point Trend Template)
        # ====================================================
        high_52w = high.iloc[-252:].max()
        low_52w = low.iloc[-252:].min()
        ma200_trending_up = (ma200.iloc[-1] > ma200.iloc[-22]) and (ma200.iloc[-22] >= ma200.iloc[-44])

        trend_template_pass = (
            (c_now > ma150.iloc[-1]) and (c_now > ma200.iloc[-1]) and
            (ma150.iloc[-1] > ma200.iloc[-1]) and
            ma200_trending_up and
            (ma50.iloc[-1] > ma150.iloc[-1]) and (ma50.iloc[-1] > ma200.iloc[-1]) and
            (c_now > ma50.iloc[-1]) and
            (c_now >= low_52w * 1.30) and
            (c_now >= high_52w * 0.75) and
            (rs_rating >= 80)
        )
        if not trend_template_pass:
            continue

        # 排除短期均線 5<10<20 蓋頭下彎且跌破月線的短空股
        if (ma5.iloc[-1] < ma10.iloc[-1] < ma20.iloc[-1]) and (c_now < ma20.iloc[-1]):
            continue

        template_passed_count += 1
        doubled_from_low = (c_now >= low_52w * 2.0)

        # ====================================================
        # 👑 第二關：價量行為與天量綠 K 封殺
        # ====================================================
        is_green_bar = (close < open_p) | (close < close.shift(1))
        is_true_red_bar = (close >= open_p) & (close > close.shift(1))

        top2_vol_dates = vol.iloc[-60:].nlargest(2).index
        if ((close.loc[top2_vol_dates] < open_p.loc[top2_vol_dates]) | (close.loc[top2_vol_dates] < close.shift(1).loc[top2_vol_dates])).any():
            continue

        vol_ratio_today = vol.iloc[-1] / vol_ma20.iloc[-2] if vol_ma20.iloc[-2] > 0 else 1.0
        if ((c_now < o_now * 0.99) or (c_now < c_prev * 0.99)) and (vol_ratio_today >= 1.25):
            continue

        red_v30 = vol.iloc[-30:][is_true_red_bar.iloc[-30:]]
        green_v30 = vol.iloc[-30:][is_green_bar.iloc[-30:]]
        avg_red_v = red_v30.mean() if len(red_v30) > 0 else 0
        avg_green_v = green_v30.mean() if len(green_v30) > 0 else 1
        up_down_vol_ratio = avg_red_v / avg_green_v if avg_green_v > 0 else 1.0

        vol_5d_avg = vol.iloc[-5:].mean()
        vol_shrink_pct = (vol_5d_avg / vol_ma50.iloc[-1]) * 100 if vol_ma50.iloc[-1] > 0 else 100

        if (up_down_vol_ratio < 0.95) and (vol_shrink_pct > 85):
            continue

        # ====================================================
        # 👑 第三關：【全體強制鐵律】必須符合 T1 > T2 > T3 嚴格遞減！
        # 不符合者直接 continue 淘汰，絕不允許進入任何一個分類！
        # ====================================================
        is_valid_vcp, base_weeks, t1_drop, t2_drop, t3_drop, pivot_high, ok_watch, ok_brk = find_strict_vcp_structure(high, low)
        if not is_valid_vcp:
            continue

        dist_to_pivot = (c_now - pivot_high) / pivot_high
        if dist_to_pivot > 0.055 or dist_to_pivot < -0.12:
            continue

        vcp_passed_count += 1

        # 計算 2x ATR 停損價 (最大 8%)
        tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
        atr20 = tr.rolling(20).mean().iloc[-1]
        stop_distance = min(2 * atr20, c_now * 0.08)
        stop_price = c_now - stop_distance
        stop_pct = (stop_distance / c_now) * 100

        # 配角輔助指標
        tags = [f"📊漲/跌量比 {up_down_vol_ratio:.1f}x"]
        if doubled_from_low:
            tags.append("👑低點翻倍")
        peak_60_idx = high.iloc[-61:-1].idxmax()
        sub_df = df.loc[peak_60_idx:]
        tp = (sub_df['High'] + sub_df['Low'] + sub_df['Close']) / 3
        vwap_series = (tp * sub_df['Volume']).cumsum() / sub_df['Volume'].cumsum()
        vwap_now = vwap_series.iloc[-1]
        if c_now >= vwap_now:
            tags.append(f"✅VWAP之上({vwap_now:.1f})")

        flag = "🇹🇼" if is_tw else "🇺🇸"
        title = f"{flag} {symbol} {name}" if name != symbol else f"{flag} {symbol}"
        support_str = " | ".join(tags)
        vcp_str = f"打底 {base_weeks}週：T1(-{t1_drop*100:.1f}%) ➔ T2(-{t2_drop*100:.1f}%) ➔ T3右側(-<b>{t3_drop*100:.1f}%</b>)"
        is_red_today = (c_now > o_now) and (c_now > c_prev) and ((c_now - l_now) >= (h_now - l_now) * 0.5 if h_now > l_now else True)

        # ====================================================
        # 👑 第四關：三大實戰分流 (所有入選股票 100% 已通過 T1 > T2 > T3！)
        # ====================================================
        # 1. 🚀 【今日突破樞紐買點】
        if ok_brk and is_red_today and (c_now > pivot_high) and (c_prev <= pivot_high) and (vol_ratio_today >= 1.3):
            score = rs_rating + int(vol_ratio_today * 15) + int((0.10 - t3_drop) * 300)
            status_str = f"今日紅K帶量({vol_ratio_today:.1f}倍)突破樞紐點 <b>{pivot_high:.2f}</b>"
            item = (score, symbol, title, c_now, rs_rating, status_str, vcp_str, support_str, stop_price, stop_pct)
            (tw_breakout if is_tw else us_breakout).append(item)

        # 2. 🎾 【VCP 遞減 + 回測 20MA 守穩彈升 (網球反彈)】
        elif ok_watch and (c_now <= pivot_high) and (c_now >= ma20.iloc[-1] * 0.995) and (low.iloc[-4:].min() <= ma20.iloc[-1] * 1.015) and is_red_today:
            dist_pct = ((pivot_high - c_now) / c_now) * 100
            score = rs_rating + int((0.10 - t3_drop) * 400) + 20
            status_str = f"回測月線紅K守穩彈升｜上檔樞紐 <b>{pivot_high:.2f}</b> (差 +{dist_pct:.1f}%)"
            item = (score, symbol, title, c_now, rs_rating, status_str, vcp_str, support_str, stop_price, stop_pct)
            (tw_tennis if is_tw else us_tennis).append(item)

        # 3. 🎯 【標準 VCP 嚴格遞減收斂池】
        elif ok_watch and (c_now <= pivot_high) and (c_now >= ma20.iloc[-1] * 0.99):
            dist_pct = ((pivot_high - c_now) / c_now) * 100
            score = rs_rating + int((0.10 - t3_drop) * 500) + (20 if vol_shrink_pct <= 80 else 0)
            status_str = f"差 +{dist_pct:.1f}% 突破樞紐 <b>{pivot_high:.2f}</b>｜近5日量為均量 {vol_shrink_pct:.0f}%"
            item = (score, symbol, title, c_now, rs_rating, status_str, vcp_str, support_str, stop_price, stop_pct)
            (tw_vcp if is_tw else us_vcp).append(item)

    for lst in [tw_breakout, us_breakout, tw_vcp, us_vcp, tw_tennis, us_tennis]:
        lst.sort(key=lambda x: x[0], reverse=True)

    selected_breakouts = tw_breakout[:4] + us_breakout[:4]
    selected_vcp = tw_vcp[:6] + us_vcp[:5]
    selected_tennis = tw_tennis[:4] + us_tennis[:4]

    print("💰 正在為入選台美股查詢最新季報 EPS 數據...")
    def format_block(items, prefix_icon):
        out = []
        for item in items:
            _, sym, title, c_now, rs, status_s, vcp_s, sup_s, sp, sp_pct = item
            eps_str = get_eps_info(sym)
            out.append(
                f"<b>{title}</b> (收盤: {c_now:.2f} | <b>RS: {rs}</b>)\n"
                f" ├ {prefix_icon} <b>狀態</b>：{status_s}\n"
                f" ├ 📐 <b>VCP遞減</b>：{vcp_s}\n"
                f" ├ 💰 <b>獲利表現</b>：{eps_str}\n"
                f" ├ 💡 <b>價量配角</b>：{sup_s}\n"
                f" └ 🛡️ <b>建議停損</b>：<b>{sp:.2f}</b> (-{sp_pct:.1f}%)"
            )
        return out

    return (
        format_block(selected_breakouts, "🚀"),
        format_block(selected_vcp, "🎯"),
        format_block(selected_tennis, "🎾"),
        len(raw_data),
        template_passed_count,
        vcp_passed_count
    )

if __name__ == "__main__":
    stock_dict, total_universe = get_entire_market_universe()
    cat1, cat2, cat3, kline_scanned, template_passed, vcp_passed = analyze_market_minervini(stock_dict)
    send_minervini_reports(cat1, cat2, cat3, total_universe, kline_scanned, template_passed, vcp_passed)
