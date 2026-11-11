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

    # A. 台股【全上市】普通股
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=15)
        if res.status_code == 200:
            twse_df = pd.DataFrame(res.json())
            twse_df = twse_df[twse_df['Code'].str.match(r'^[1-9]\d{3}$')]
            twse_df['TradeVolume'] = pd.to_numeric(twse_df['TradeVolume'].str.replace(',', ''), errors='coerce')
            valid_twse = twse_df[twse_df['TradeVolume'] >= 100000]
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
            tpex_df['TradingShares'] = pd.to_numeric(tpex_df['TradingShares'].str.replace(',', ''), errors='coerce')
            valid_tpex = tpex_df[tpex_df['TradingShares'] >= 100000]
            for _, row in valid_tpex.iterrows():
                stock_dict[f"{row['SecuritiesCompanyCode']}.TWO"] = row['CompanyName'].strip()
    except Exception as e:
        print(f"⚠️ 上櫃清單抓取異常: {e}")

    # C. 美股【S&P 500 + Nasdaq 100 + 熱門動能股】
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
# 4. Mark 筆記公式：(波段低點 / 波段高點) - 1
# ==========================================
def mark_vcp_drop(high_s, low_s):
    # 計算該區間內高點與低點的波動收縮百分比：(Swing Low / Swing High) - 1
    h_max = float(high_s.max())
    l_min = float(low_s.min())
    if h_max <= 0:
        return 0.0
    return abs((l_min / h_max) - 1.0)

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

def send_minervini_reports(cat1_breakout, cat2_vcp, cat3_tennis, total_scanned, template_passed):
    all_blocks = []
    if cat1_breakout:
        all_blocks.append("🚀 <b>【一、突破前高樞紐買點 (Pivot Breakout)】</b>\n<i>(Mark準則：VCP收斂完成 + 今日帶量突破前波小高點)</i>")
        all_blocks.extend(cat1_breakout)
    if cat2_vcp:
        all_blocks.append("\n🎯 <b>【二、標準 VCP 波動遞減收斂池 (T1 > T2 > 近5日)】</b>\n<i>(Mark準則：波幅由左至右嚴格縮小、下跌量縮，緊貼前高樞紐等突破)</i>")
        all_blocks.extend(cat2_vcp)
    if cat3_tennis:
        all_blocks.append("\n🎾 <b>【三、網球反彈與淺回檔 (Tennis Ball & Shallow Pullback)】</b>\n<i>(Mark準則：強勢股回測10/20MA量縮守穩或紅K彈升，等右肩突破)</i>")
        all_blocks.extend(cat3_tennis)

    if not all_blocks:
        send_telegram_msg(f"📊 <b>【Mark Minervini 純血選股雷達】</b>\n已掃描 {total_scanned} 檔，通過趨勢模板 {template_passed} 檔。")
        return

    chunk_size = 7
    total_pages = (len(all_blocks) + chunk_size - 1) // chunk_size
    for i in range(0, len(all_blocks), chunk_size):
        page = (i // chunk_size) + 1
        chunk = all_blocks[i:i + chunk_size]
        header = (
            f"📊 <b>【Mark Minervini 純血選股日報】({page}/{total_pages})</b>\n"
            f"<i>掃描 {total_scanned} 檔｜符合 Mark 趨勢模板 {template_passed} 檔 (台美股均衡入列)</i>\n\n"
        )
        send_telegram_msg(header + "\n\n".join(chunk))

# ==========================================
# 6. 純血版 Mark Minervini 選股引擎 (無重疊區間、保證近5日嚴格收斂)
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

    # 台股與美股分開存放，確保台股永遠不會被美股擠掉！
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

        if vol_ma20.iloc[-1] < 250000:
            continue

        # ====================================================
        # 👑 準則 1：Mark Minervini 趨勢模板 (Trend Template)
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

        # 排除上海商銀這種「短期均線空頭排列下彎 (5MA < 10MA < 20MA) 且壓在月線下」的走弱股
        if (ma5.iloc[-1] < ma10.iloc[-1] < ma20.iloc[-1]) and (c_now < ma20.iloc[-1]):
            continue
        # 50MA 季線本身也必須是向上趨勢
        if ma50.iloc[-1] < ma50.iloc[-10]:
            continue

        template_passed_count += 1
        doubled_from_low = (c_now >= low_52w * 2.0)

        # ====================================================
        # 👑 準則 2：Mark 價量準則 (漲量多跌量縮、排除巨量綠K出貨與崩跌基底)
        # ====================================================
        # 1. 整理基底從高點回檔不得超過 32% (淘汰大甲那種跌42%的結構破壞股)
        peak_6m = high.iloc[-126:].max()
        peak_6m_pos = high.iloc[-126:].values.argmax()
        low_after_peak = low.iloc[-126 + peak_6m_pos:].min()
        if ((peak_6m - low_after_peak) / peak_6m) > 0.32:
            continue

        # 2. 定義真紅 K (收盤>開盤 且 上漲) 與 綠 K / 下跌黑 K
        is_green_bar = (close < open_p) | (close < close.shift(1))
        is_true_red_bar = (close >= open_p) & (close > close.shift(1))

        # 過去 60 天最大量的前 2 根 K 棒絕不能是下跌綠 K (淘汰寶雅 8.4萬張綠K、中鼎 2.29萬張綠K)
        top2_vol_dates = vol.iloc[-60:].nlargest(2).index
        if ((close.loc[top2_vol_dates] < open_p.loc[top2_vol_dates]) | (close.loc[top2_vol_dates] < close.shift(1).loc[top2_vol_dates])).any():
            continue

        # 過去 20 天內不得出現「收黑下跌超過 1.5% 且爆出 2 倍均量」的出貨長黑棒 (淘汰藥華藥、佳大)
        heavy_dump_20d = ((close.iloc[-20:] < open_p.iloc[-20:] * 0.985) | (close.iloc[-20:] < close.shift(1).iloc[-20:] * 0.98)) & (vol.iloc[-20:] >= vol_ma20.iloc[-20:] * 2.0)
        if heavy_dump_20d.any():
            continue

        # 今日若為黑K下跌且出量 (>1.2倍均量)，直接排除
        vol_ratio_today = vol.iloc[-1] / vol_ma20.iloc[-2] if vol_ma20.iloc[-2] > 0 else 1.0
        if ((c_now < o_now) or (c_now < c_prev)) and (vol_ratio_today >= 1.2):
            continue

        # 計算近 30 日「上漲紅K均量 vs 下跌綠K均量」(Up Volume vs Down Volume)
        red_v30 = vol.iloc[-30:][is_true_red_bar.iloc[-30:]]
        green_v30 = vol.iloc[-30:][is_green_bar.iloc[-30:]]
        avg_red_v = red_v30.mean() if len(red_v30) > 0 else 0
        avg_green_v = green_v30.mean() if len(green_v30) > 0 else 1
        up_down_vol_ratio = avg_red_v / avg_green_v if avg_green_v > 0 else 1.0

        # 必須符合「上漲有量、下跌量縮」(紅綠均量比 >= 1.0，或近 5 日量縮至 20日均量的 80% 以下)
        vol_5d_avg = vol.iloc[-5:].mean()
        vol_shrink_pct = (vol_5d_avg / vol_ma20.iloc[-1]) * 100 if vol_ma20.iloc[-1] > 0 else 100
        if (up_down_vol_ratio < 1.0) and (vol_shrink_pct > 80):
            continue

        # ====================================================
        # 👑 準則 3：Mark VCP 收縮公式 (三個完全不重疊區間：T1 ➔ T2 ➔ T3)
        # 統一使用貼文公式：(Swing Low / Swing High) - 1
        # ====================================================
        # T1 (左段：前 21~45 日)、T2 (中段：前 6~20 日)、T3 (右段：最近 5 日)
        t1_drop = mark_vcp_drop(high.iloc[-45:-20], low.iloc[-45:-20])
        t2_drop = mark_vcp_drop(high.iloc[-20:-5], low.iloc[-20:-5])
        t3_drop = mark_vcp_drop(high.iloc[-5:], low.iloc[-5:])
        # 針對今日突破股，計算「突破前 5 日 (-6:-1)」的收縮度
        t3_pre_breakout = mark_vcp_drop(high.iloc[-6:-1], low.iloc[-6:-1])

        # 前波小高點 = 樞紐買點 (Previous Swing High)
        # 若近 12 日已在高檔形成窄幅箱型，取近 12 日高點為右側小樞紐；否則取近 25 日高點
        base_high_45 = high.iloc[-45:-1].max()
        swing_high_12 = high.iloc[-13:-1].max()
        if (t2_drop <= 0.12) and (swing_high_12 >= base_high_45 * 0.90):
            pivot_high = swing_high_12
            pivot_label = f"前波小高點 <b>{pivot_high:.2f}</b>"
        else:
            pivot_high = high.iloc[-25:-1].max()
            pivot_label = f"前高樞紐 <b>{pivot_high:.2f}</b>"

        dist_to_pivot = (c_now - pivot_high) / pivot_high
        # 若已突破樞紐點超過 +6% 以上，代表已脫離買點不追高
        if dist_to_pivot > 0.06 or dist_to_pivot < -0.15:
            continue

        # ====================================================
        # 👑 準則 4：Mark 停損公式 (2x 20日ATR，最大不超過 10%)
        # ====================================================
        tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
        atr20 = tr.rolling(20).mean().iloc[-1]
        stop_distance = min(2 * atr20, c_now * 0.10)
        stop_price = c_now - stop_distance
        stop_pct = (stop_distance / c_now) * 100

        # ====================================================
        # 🧩 配角輔助資訊 (VWAP / Wedge Pop / Zanger Vol / 翻倍標籤)
        # ====================================================
        tags = [f"📊漲/跌量比 {up_down_vol_ratio:.1f}x"]
        if doubled_from_low:
            tags.append("👑低點翻倍(+100%)")

        peak_60_idx = high.iloc[-61:-1].idxmax()
        sub_df = df.loc[peak_60_idx:]
        tp = (sub_df['High'] + sub_df['Low'] + sub_df['Close']) / 3
        vwap_series = (tp * sub_df['Volume']).cumsum() / sub_df['Volume'].cumsum()
        vwap_now = vwap_series.iloc[-1]
        if c_now >= vwap_now:
            tags.append(f"✅高點VWAP之上({vwap_now:.1f})")

        ema10 = close.ewm(span=10, adjust=False).mean()
        ema20 = close.ewm(span=20, adjust=False).mean()
        if (low.iloc[-6:-1] <= ema20.iloc[-6:-1] * 1.01).any() and (c_now > ema10.iloc[-1]) and (c_now > ema20.iloc[-1]) and (c_prev <= ema10.iloc[-2]):
            tags.append("⚡Wedge Pop")

        flag = "🇹🇼" if is_tw else "🇺🇸"
        title = f"{flag} {symbol} {name}" if name != symbol else f"{flag} {symbol}"
        support_str = " | ".join(tags)
        is_red_today = (c_now > o_now) and (c_now > c_prev) and ((c_now - l_now) >= (h_now - l_now) * 0.5 if h_now > l_now else True)

        # ====================================================
        # 👑 嚴格依 Mark 三大實戰型態分流 (保證近5日 T3 嚴格收斂！)
        # ====================================================
        # 1. 🚀 【突破前高樞紐買點】：突破前5日已收斂 (t3_pre <= t2 且 <= 8.5%) + 今日帶量紅K突破 pivot_high
        if (t3_pre_breakout < max(t1_drop, t2_drop) * 0.75) and (t3_pre_breakout <= 0.085) and is_red_today and (c_now > pivot_high) and (c_prev <= pivot_high) and (vol_ratio_today >= 1.3):
            vcp_str = f"T1(-{t1_drop*100:.1f}%) ➔ T2(-{t2_drop*100:.1f}%) ➔ 突破前5日(-<b>{t3_pre_breakout*100:.1f}%</b>)"
            score = rs_rating + (20 if doubled_from_low else 0) + int(vol_ratio_today * 10)
            item = (score, symbol, title, c_now, rs_rating, f"紅K帶量({vol_ratio_today:.1f}倍)突破 {pivot_label}", vcp_str, support_str, stop_price, stop_pct)
            if is_tw:
                tw_breakout.append(item)
            else:
                us_breakout.append(item)

        # 2. 🎯 【標準 VCP 波動遞減收斂池】：
        # 強制規定：T3(近5日) < T2(中段) 且 T3 <= 前波最大收縮的一半(或65%) 且 T3 <= 8.0%！
        # 如此一來，近 5 日數字在數學上 100% 絕對比前面小，絕不可能變大！
        elif (
            (c_now <= pivot_high) and
            (c_now >= ma20.iloc[-1]) and
            (ma20.iloc[-1] >= ma20.iloc[-5]) and
            (t3_drop < t2_drop * 0.90) and
            (t3_drop <= max(t1_drop, t2_drop) * 0.65) and
            (t3_drop <= 0.080) and
            (vol_shrink_pct <= 95)
        ):
            dist_pct = ((pivot_high - c_now) / c_now) * 100
            vcp_str = f"T1(-{t1_drop*100:.1f}%) ➔ T2(-{t2_drop*100:.1f}%) ➔ 近5日(-<b>{t3_drop*100:.1f}%</b>)"
            # 近 5 日越窄、離樞紐點越近，排序越前面
            score = rs_rating + int((0.10 - t3_drop) * 500) + (15 if doubled_from_low else 0)
            status_str = f"差 +{dist_pct:.1f}% 突破 {pivot_label}｜近5日量 {vol_shrink_pct:.0f}%"
            item = (score, symbol, title, c_now, rs_rating, status_str, vcp_str, support_str, stop_price, stop_pct)
            if is_tw:
                tw_vcp.append(item)
            else:
                us_vcp.append(item)

        # 3. 🎾 【網球反彈與淺回檔 (Tennis Ball Action & Shallow Pullback)】：
        # 強勢股回測 10MA/20MA 後迅速彈回均線之上收紅K (或拉出長下影線守穩月線)，且近5日波幅 < 中段波幅
        elif (
            (c_now <= pivot_high) and
            (c_now >= ma20.iloc[-1] * 0.99) and
            (low.iloc[-5:].min() <= ma20.iloc[-5:].max() * 1.02) and
            (is_red_today or ((min(o_now, c_now) - l_now) / (h_now - l_now + 0.001) >= 0.40)) and
            (t3_drop < max(t1_drop, t2_drop) * 0.85) and
            (t3_drop <= 0.095)
        ):
            dist_pct = ((pivot_high - c_now) / c_now) * 100
            vcp_str = f"T1(-{t1_drop*100:.1f}%) ➔ T2(-{t2_drop*100:.1f}%) ➔ 近5日(-<b>{t3_drop*100:.1f}%</b>)"
            score = rs_rating + int((0.10 - t3_drop) * 300) + (15 if doubled_from_low else 0)
            action_type = "回測20MA紅K網球彈升" if is_red_today else "回測20MA量縮長下影線"
            status_str = f"{action_type}｜上檔 {pivot_label} (差 +{dist_pct:.1f}%)"
            item = (score, symbol, title, c_now, rs_rating, status_str, vcp_str, support_str, stop_price, stop_pct)
            if is_tw:
                tw_tennis.append(item)
            else:
                us_tennis.append(item)

    # 排序並確保「台股 🇹🇼 與美股 🇺🇸 各自都有名額」，絕不讓美股把台股擠掉！
    for lst in [tw_breakout, us_breakout, tw_vcp, us_vcp, tw_tennis, us_tennis]:
        lst.sort(key=lambda x: x[0], reverse=True)

    selected_breakouts = tw_breakout[:4] + us_breakout[:3]
    selected_vcp = tw_vcp[:6] + us_vcp[:4]
    selected_tennis = tw_tennis[:5] + us_tennis[:3]

    print("💰 正在為入選台美股查詢最新季報 EPS 數據...")
    def format_block(items, prefix_icon):
        out = []
        for item in items:
            _, sym, title, c_now, rs, status_s, vcp_s, sup_s, sp, sp_pct = item
            eps_str = get_eps_info(sym)
            out.append(
                f"<b>{title}</b> (收盤: {c_now:.2f} | <b>RS: {rs}</b>)\n"
                f" ├ {prefix_icon} <b>狀態</b>：{status_s}\n"
                f" ├ 📐 <b>VCP收縮</b>：{vcp_s}\n"
                f" ├ 💰 <b>獲利表現</b>：{eps_str}\n"
                f" ├ 💡 <b>價量配角</b>：{sup_s}\n"
                f" └ 🛡️ <b>2xATR停損</b>：<b>{sp:.2f}</b> (-{sp_pct:.1f}%)"
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
    stock_dict = get_full_market_stock_list()
    cat1, cat2, cat3, total_scanned, template_passed = analyze_market_minervini(stock_dict)
    send_minervini_reports(cat1, cat2, cat3, total_scanned, template_passed)
