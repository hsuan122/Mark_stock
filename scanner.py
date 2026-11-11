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
# 2. 全市場 7,000+ 檔股票池抓取 (台股全上市櫃 + 美股 NYSE/NASDAQ/AMEX 全市場)
# ==========================================
def get_entire_market_universe():
    tw_dict = {}
    us_dict = {}
    total_raw_count = 0

    # A. 台股【全上市】普通股 (TWSE 官方 API)
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=15)
        if res.status_code == 200:
            twse_df = pd.DataFrame(res.json())
            twse_df = twse_df[twse_df['Code'].str.match(r'^[1-9]\d{3}$')]
            total_raw_count += len(twse_df)
            twse_df['ClosingPrice'] = pd.to_numeric(twse_df['ClosingPrice'].str.replace(',', ''), errors='coerce')
            twse_df['TradeVolume'] = pd.to_numeric(twse_df['TradeVolume'].str.replace(',', ''), errors='coerce')
            # 僅排除日成交量不到 30 張的無流動性殭屍股
            valid_twse = twse_df[(twse_df['ClosingPrice'] > 0) & (twse_df['TradeVolume'] >= 30000)]
            for _, row in valid_twse.iterrows():
                tw_dict[f"{row['Code']}.TW"] = row['Name'].strip()
    except Exception as e:
        print(f"⚠️ 上市清單抓取異常: {e}")

    # B. 台股【全上櫃】普通股 (TPEx 官方 API)
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

    # C. 美股【NYSE + NASDAQ + AMEX 全市場約 5,500+ 檔】直接串接 NASDAQ 官方 Screener API
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
            # 僅保留純字母普通股代號 (排除特別股 ^、權證 / WS)
            us_df = us_df[us_df['symbol'].str.match(r'^[A-Z]{1,5}$', na=False)]
            us_df['price'] = pd.to_numeric(us_df['lastsale'].astype(str).str.replace('$', '').str.replace(',', ''), errors='coerce')
            us_df['vol'] = pd.to_numeric(us_df['volume'].astype(str).str.replace(',', ''), errors='coerce')
            us_df['mcap'] = pd.to_numeric(us_df['marketCap'].astype(str).str.replace(',', ''), errors='coerce')
            us_df['dollar_vol'] = us_df['price'] * us_df['vol']

            # 依《超級績效》第5章原則：排除美股低於 $8 的仙股(Penny Stocks)與市值過小的殭屍股，取全美股日成交金額前 1,600 大活躍股進入深度K線運算
            active_us = us_df[(us_df['price'] >= 8.0) & (us_df['mcap'] >= 150_000_000) & (us_df['vol'] >= 100_000)]
            active_us = active_us.sort_values(by='dollar_vol', ascending=False).head(1600)
            for _, row in active_us.iterrows():
                sym = str(row['symbol']).strip()
                us_dict[sym] = sym
    except Exception as e:
        print(f"⚠️ NASDAQ 全市場 API 異常，切換為備援清單: {e}")

    # 若遇網路阻擋之保底機制 (Wikipedia S&P 500 + Nasdaq 100)
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
    print(f"✅ 全市場掃描母體共 {total_raw_count} 檔 (台股 {len(tw_dict)} 檔 + 美股活躍股 {len(us_dict)} 檔進入 K 線深度檢驗)！")
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
# 4. 《超級績效》動態週期 VCP 演算法 (支援 3 週 ~ 24 週打底期，不再死鎖 45 天！)
# ==========================================
def analyze_dynamic_vcp(high_s, low_s, close_s):
    """
    自動尋找過去 15 ~ 120 個交易日 (3~24週) 的波段最高點作為基底起點，
    並依該股票實際打底天數動態切分為 T1(左段) -> T2(中段) -> T3(右側近5~8日)，
    回傳：(打底週數, T1跌幅, T2跌幅, T3右側波幅, 右側精準樞紐價, 基底總深度)
    """
    # 尋找過去 15 ~ 120 天內的最高峰作為 VCP 基底起點
    window_120 = high_s.iloc[-120:-1]
    peak_idx_pos = window_120.values.argmax()
    base_days = len(window_120) - peak_idx_pos  # 距離最高峰的交易日數

    # 若最高峰就在最近 15 天內 (屬於短線強勢小旗型)，則以過去 36 天作為觀察窗
    if base_days < 18:
        base_days = 36

    base_highs = high_s.iloc[-(base_days + 1):-1]
    base_lows = low_s.iloc[-(base_days + 1):-1]
    base_peak = float(base_highs.max())
    base_min = float(base_lows.min())
    base_depth = (base_peak - base_min) / base_peak if base_peak > 0 else 0.0

    # 將整個打底期動態切為三段：左段 T1 (前 45%)、中段 T2 (中 35%)、右段 T3 (最後 20%，至少取近 6 日)
    t3_len = max(6, min(10, int(base_days * 0.22)))
    rem_days = base_days - t3_len
    t1_len = max(6, int(rem_days * 0.55))
    t2_len = max(5, rem_days - t1_len)

    h_t1 = base_highs.iloc[:t1_len]
    l_t1 = base_lows.iloc[:t1_len]
    h_t2 = base_highs.iloc[t1_len:t1_len + t2_len]
    l_t2 = base_lows.iloc[t1_len:t1_len + t2_len]
    h_t3 = base_highs.iloc[-t3_len:]
    l_t3 = base_lows.iloc[-t3_len:]

    t1_drop = (float(h_t1.max()) - float(l_t1.min())) / float(h_t1.max()) if len(h_t1) > 0 and h_t1.max() > 0 else base_depth
    t2_drop = (float(h_t2.max()) - float(l_t2.min())) / float(h_t2.max()) if len(h_t2) > 0 and h_t2.max() > 0 else t1_drop
    t3_drop = (float(h_t3.max()) - float(l_t3.min())) / float(h_t3.max()) if len(h_t3) > 0 and h_t3.max() > 0 else t2_drop

    # 右側樞紐買點 (Pivot High)：若右側近 12 日高點接近基底大高點 (差距 10% 內)，取右側小高點為樞紐；否則取近 25 日高點
    right_12d_high = float(high_s.iloc[-13:-1].max())
    if right_12d_high >= base_peak * 0.90:
        pivot_price = right_12d_high
    else:
        pivot_price = float(high_s.iloc[-26:-1].max())

    base_weeks = round(base_days / 5.0, 1)
    return base_weeks, t1_drop, t2_drop, t3_drop, pivot_price, base_depth

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

def send_minervini_reports(cat1_breakout, cat2_vcp, cat3_tennis, total_universe, kline_scanned, template_passed):
    all_blocks = []
    if cat1_breakout:
        all_blocks.append("🚀 <b>【一、樞紐點帶量突破買點 (Pivot Breakout)】</b>\n<i>(《超級績效》準則：VCP收斂完成 + 今日紅K帶量突破樞紐高點)</i>")
        all_blocks.extend(cat1_breakout)
    if cat2_vcp:
        all_blocks.append("\n🎯 <b>【二、標準 VCP 波動遞減收斂池 (T1 > T2 > T3)】</b>\n<i>(《超級績效》準則：3~24週動態基底波幅由左至右遞減、量縮等突破)</i>")
        all_blocks.extend(cat2_vcp)
    if cat3_tennis:
        all_blocks.append("\n🎾 <b>【三、第二階段回測支撐／網球反彈 (Pullback & Tennis Ball)】</b>\n<i>(《超級績效》準則：符合八大趨勢模板 + 回測10/20MA守穩或右側構築中)</i>")
        all_blocks.extend(cat3_tennis)

    if not all_blocks:
        send_telegram_msg(f"📊 <b>【《超級績效》全市場選股雷達】</b>\n全市場母體 {total_universe} 檔，深度K線掃描 {kline_scanned} 檔，通過八大模板 {template_passed} 檔。")
        return

    chunk_size = 7
    total_pages = (len(all_blocks) + chunk_size - 1) // chunk_size
    for i in range(0, len(all_blocks), chunk_size):
        page = (i // chunk_size) + 1
        chunk = all_blocks[i:i + chunk_size]
        header = (
            f"📊 <b>【《超級績效》全市場選股日報】({page}/{total_pages})</b>\n"
            f"<i>全市場母體 {total_universe} 檔｜深度K線掃描 {kline_scanned} 檔｜符合八大趨勢模板 {template_passed} 檔</i>\n\n"
        )
        send_telegram_msg(header + "\n\n".join(chunk))

# ==========================================
# 6. 《超級績效》原書純血引擎
# ==========================================
def analyze_market_minervini(stock_dict):
    symbols = list(stock_dict.keys())
    raw_data = {}
    tw_rs_scores = {}
    us_rs_scores = {}

    # 下載 15 個月資料 (約 315 個交易日，足以精準計算 52 週高低點與 200MA 趨勢，且下載速度極快)
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
                    
                    # 歐奈爾 / 《超級績效》RS 相對強弱加權計分
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
        # 👑 第一關：《超級績效》原書第 5 章「八大趨勢模板 (8-Point Trend Template)」
        # ====================================================
        high_52w = high.iloc[-252:].max()
        low_52w = low.iloc[-252:].min()

        # 原書八大條件：
        # 1. 股價 > 150MA 且 > 200MA
        # 2. 150MA > 200MA
        # 3. 200MA 持續上升至少 1~2 個月以上
        # 4. 50MA > 150MA 且 > 200MA
        # 5. 股價 > 50MA
        # 6. 股價高於 52 週低點至少 30%
        # 7. 股價距離 52 週高點 25% 以內
        # 8. RS 排名 >= 80 (原書要求不低於 70，以 80~90 以上為佳)
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

        # 排除短期均線 5<10<20 蓋頭下彎且跌破月線的短空股 (如上海商銀)
        if (ma5.iloc[-1] < ma10.iloc[-1] < ma20.iloc[-1]) and (c_now < ma20.iloc[-1]):
            continue

        template_passed_count += 1
        doubled_from_low = (c_now >= low_52w * 2.0)

        # ====================================================
        # 👑 第二關：《超級績效》第 10 章「價量行為與出貨過濾」
        # ====================================================
        is_green_bar = (close < open_p) | (close < close.shift(1))
        is_true_red_bar = (close >= open_p) & (close > close.shift(1))

        # 1. 過去 60 天最大量的前 2 根 K 棒不能是下跌黑 K / 綠 K (淘汰寶雅、中鼎那種天量出貨圖)
        top2_vol_dates = vol.iloc[-60:].nlargest(2).index
        if ((close.loc[top2_vol_dates] < open_p.loc[top2_vol_dates]) | (close.loc[top2_vol_dates] < close.shift(1).loc[top2_vol_dates])).any():
            continue

        # 2. 今日不可為帶量長黑下跌 (淘汰藥華藥那種當日倒貨棒)
        vol_ratio_today = vol.iloc[-1] / vol_ma20.iloc[-2] if vol_ma20.iloc[-2] > 0 else 1.0
        if ((c_now < o_now * 0.99) or (c_now < c_prev * 0.99)) and (vol_ratio_today >= 1.25):
            continue

        # 3. 計算近 30 日紅 K 均量 vs 綠 K 均量，以及近 5 日量縮比率
        red_v30 = vol.iloc[-30:][is_true_red_bar.iloc[-30:]]
        green_v30 = vol.iloc[-30:][is_green_bar.iloc[-30:]]
        avg_red_v = red_v30.mean() if len(red_v30) > 0 else 0
        avg_green_v = green_v30.mean() if len(green_v30) > 0 else 1
        up_down_vol_ratio = avg_red_v / avg_green_v if avg_green_v > 0 else 1.0

        vol_5d_avg = vol.iloc[-5:].mean()
        vol_shrink_pct = (vol_5d_avg / vol_ma50.iloc[-1]) * 100 if vol_ma50.iloc[-1] > 0 else 100

        # 漲量 > 跌量，或近期明顯量縮整理
        if (up_down_vol_ratio < 0.90) and (vol_shrink_pct > 85):
            continue

        # ====================================================
        # 👑 第三關：《超級績效》動態週期 VCP (支援 3 ~ 24 週打底)
        # ====================================================
        base_weeks, t1_drop, t2_drop, t3_drop, pivot_high, base_depth = analyze_dynamic_vcp(high, low, close)

        # 原書準則：一般 VCP 基底總回檔深度不超過 35% (淘汰大甲那種跌 42% 過深的結構)
        if base_depth > 0.35:
            continue

        dist_to_pivot = (c_now - pivot_high) / pivot_high
        # 若已突破樞紐點超過 +6% 以上，代表已錯過最佳買點不追高 (淘汰騰雲)
        if dist_to_pivot > 0.06 or dist_to_pivot < -0.16:
            continue

        # 停損計算 (2x ATR，最大不超過 8%~10%，完全符合原書停損守則)
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
        # 👑 第四關：三大實戰分流 (計分排序制，保證台股與美股皆有精選名單！)
        # ====================================================
        # 1. 🚀 【今日突破樞紐買點】：今日帶量紅K突破 pivot_high，且突破前右側已收斂
        if is_red_today and (c_now > pivot_high) and (c_prev <= pivot_high) and (vol_ratio_today >= 1.3) and (t3_drop <= t1_drop * 0.80):
            score = rs_rating + int(vol_ratio_today * 15) + (20 if doubled_from_low else 0)
            status_str = f"今日紅K帶量({vol_ratio_today:.1f}倍)突破樞紐點 <b>{pivot_high:.2f}</b>"
            item = (score, symbol, title, c_now, rs_rating, status_str, vcp_str, support_str, stop_price, stop_pct)
            (tw_breakout if is_tw else us_breakout).append(item)

        # 2. 🎯 【標準 VCP 波動遞減收斂池】：
        # 只要動態週期滿足「右側 T3 < 左側 T1」且「T3 <= T2 (或 T3 已壓縮至 7.5% 內)」且股價穩守月線之上等待突破！
        elif (
            (c_now <= pivot_high) and
            (c_now >= ma20.iloc[-1] * 0.99) and
            (t3_drop < t1_drop * 0.80) and
            ((t3_drop <= t2_drop * 1.02) or (t3_drop <= 0.075)) and
            (t3_drop <= 0.10)
        ):
            dist_pct = ((pivot_high - c_now) / c_now) * 100
            # 評分：右側 T3 越窄、離樞紐點越近、量越縮，排名越前面！
            score = rs_rating + int((0.12 - t3_drop) * 400) + int((0.15 - abs(dist_to_pivot)) * 150) + (15 if vol_shrink_pct <= 85 else 0)
            status_str = f"差 +{dist_pct:.1f}% 突破樞紐 <b>{pivot_high:.2f}</b>｜近期量為均量 {vol_shrink_pct:.0f}%"
            item = (score, symbol, title, c_now, rs_rating, status_str, vcp_str, support_str, stop_price, stop_pct)
            (tw_vcp if is_tw else us_vcp).append(item)

        # 3. 🎾 【第二階段回測支撐／網球反彈池】：
        # 通過八大趨勢模板與無出貨檢驗，正在回測 10MA/20MA 守穩或構築右肩的強勢股
        elif (c_now <= pivot_high) and (c_now >= ma20.iloc[-1] * 0.98) and (t3_drop <= t1_drop):
            dist_pct = ((pivot_high - c_now) / c_now) * 100
            score = rs_rating + int((0.15 - t3_drop) * 200) + (15 if is_red_today else 0)
            action_tag = "回測月線紅K彈升(網球反彈)" if (low.iloc[-4:].min() <= ma20.iloc[-1] * 1.02 and is_red_today) else "站穩均線右肩收斂中"
            status_str = f"{action_tag}｜上檔樞紐 <b>{pivot_high:.2f}</b> (差 +{dist_pct:.1f}%)"
            item = (score, symbol, title, c_now, rs_rating, status_str, vcp_str, support_str, stop_price, stop_pct)
            (tw_tennis if is_tw else us_tennis).append(item)

    # 依 VCP 品質分數由高到低排序，台股與美股各自獨立保障名額
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
                f" ├ 📐 <b>VCP結構</b>：{vcp_s}\n"
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
        template_passed_count
    )

if __name__ == "__main__":
    stock_dict, total_universe = get_entire_market_universe()
    cat1, cat2, cat3, kline_scanned, template_passed = analyze_market_minervini(stock_dict)
    send_minervini_reports(cat1, cat2, cat3, total_universe, kline_scanned, template_passed)
