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
# 2. 全市場股票池抓取 (台股全上市櫃 + 美股全市場)
# ==========================================
def get_entire_market_universe():
    tw_dict = {}
    us_dict = {}
    total_raw_count = 0

    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=15)
        if res.status_code == 200:
            twse_df = pd.DataFrame(res.json())
            twse_df = twse_df[twse_df['Code'].str.match(r'^[1-9]\d{3}$')]
            total_raw_count += len(twse_df)
            twse_df['ClosingPrice'] = pd.to_numeric(twse_df['ClosingPrice'].str.replace(',', ''), errors='coerce')
            twse_df['TradeVolume'] = pd.to_numeric(twse_df['TradeVolume'].str.replace(',', ''), errors='coerce')
            valid_twse = twse_df[(twse_df['ClosingPrice'] > 0) & (twse_df['TradeVolume'] >= 50000)]
            for _, row in valid_twse.iterrows():
                tw_dict[f"{row['Code']}.TW"] = row['Name'].strip()
    except Exception as e:
        print(f"⚠️ 上市清單抓取異常: {e}")

    try:
        res_otc = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", timeout=15)
        if res_otc.status_code == 200:
            tpex_df = pd.DataFrame(res_otc.json())
            tpex_df = tpex_df[tpex_df['SecuritiesCompanyCode'].str.match(r'^[1-9]\d{3}$')]
            total_raw_count += len(tpex_df)
            tpex_df['Close'] = pd.to_numeric(tpex_df['Close'].str.replace(',', ''), errors='coerce')
            tpex_df['TradingShares'] = pd.to_numeric(tpex_df['TradingShares'].str.replace(',', ''), errors='coerce')
            valid_tpex = tpex_df[(tpex_df['Close'] > 0) & (tpex_df['TradingShares'] >= 50000)]
            for _, row in valid_tpex.iterrows():
                tw_dict[f"{row['SecuritiesCompanyCode']}.TWO"] = row['CompanyName'].strip()
    except Exception as e:
        print(f"⚠️ 上櫃清單抓取異常: {e}")

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

            active_us = us_df[(us_df['price'] >= 8.0) & (us_df['mcap'] >= 100_000_000) & (us_df['vol'] >= 120_000)]
            active_us = active_us.sort_values(by='vol', ascending=False).head(2200)
            for _, row in active_us.iterrows():
                sym = str(row['symbol']).strip()
                us_dict[sym] = sym
    except Exception as e:
        print(f"⚠️ NASDAQ API 異常: {e}")

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
    return all_dict, total_raw_count

# ==========================================
# 3. 查詢 EPS、營收成長與「財報是否超預期 (Earnings & Revenue Beat)」
# ==========================================
def get_eps_and_surprise_info(symbol):
    """
    回傳：(顯示字串, 是否財報/營收超預期 bool, 超預期標籤字串)
    """
    try:
        tk = yf.Ticker(symbol)
        info = tk.info or {}
        ttm_eps = info.get('trailingEps')
        q_growth = info.get('earningsQuarterlyGrowth')
        rev_growth = info.get('revenueGrowth')

        # 檢查分析師財報驚喜度 (Earnings Surprise %)
        surprise_pct = None
        try:
            eh = tk.earnings_history
            if eh is not None and not eh.empty and 'surprisePercent' in eh.columns:
                valid_eh = eh['surprisePercent'].dropna()
                if len(valid_eh) > 0:
                    surprise_pct = float(valid_eh.iloc[-1]) * 100.0
                    # 若欄位本身已是百分比數值(如 15.2 而非 0.152)，自動校正
                    if abs(surprise_pct) > 500:
                        surprise_pct = float(valid_eh.iloc[-1])
        except Exception:
            pass

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
            parts.append(f"EPS年增: <b>{sign}{q_growth*100:.1f}%</b>")
        if rev_growth is not None:
            r_sign = "+" if rev_growth >= 0 else ""
            parts.append(f"營收年增: <b>{r_sign}{rev_growth*100:.1f}%</b>")

        # 判定是否符合「財報/營收超預期」
        beat_tags = []
        is_beat = False
        if surprise_pct is not None and surprise_pct >= 5.0:
            is_beat = True
            beat_tags.append(f"🔥EPS超預期+{surprise_pct:.1f}%")
        if rev_growth is not None and rev_growth >= 0.25 and (q_growth is None or q_growth > 0):
            is_beat = True
            beat_tags.append(f"💥營收高成長+{rev_growth*100:.1f}%")
        elif q_growth is not None and q_growth >= 0.40 and (latest_q_eps is not None and latest_q_eps > 0):
            is_beat = True
            beat_tags.append(f"💥獲利爆發+{q_growth*100:.0f}%")

        beat_str = " | ".join(beat_tags)
        if beat_str:
            parts.append(f"<b>[{beat_str}]</b>")

        return (" ｜ ".join(parts) if parts else "財報更新中"), is_beat, beat_str
    except Exception:
        return "財報更新中", False, ""

# ==========================================
# 4. 《超級績效》技術面足跡 (XW A/B NT) 演算法 (保持與上一版完全相同)
# ==========================================
def analyze_minervini_footprint(high_s, low_s, close_s, vol_s, vol_ma50_s):
    lookback = min(180, len(high_s) - 2)
    window_h = high_s.iloc[-(lookback + 1):-1]
    peak_pos = int(window_h.values.argmax())
    days_since_peak = len(window_h) - peak_pos

    if days_since_peak < 12:
        sub_h = high_s.iloc[-95:-12]
        if len(sub_h) > 15:
            sub_peak = float(sub_h.max())
            curr_peak = float(window_h.max())
            if curr_peak <= sub_peak * 1.06:
                sub_pos = int(sub_h.values.argmax())
                days_since_peak = (len(sub_h) - sub_pos) + 11

    if days_since_peak < 15:
        return {"valid": False}

    base_h = high_s.iloc[-(days_since_peak + 1):-1]
    base_l = low_s.iloc[-(days_since_peak + 1):-1]
    base_peak = float(base_h.max())
    base_trough = float(base_l.min())

    first_drop_pct = ((base_peak - base_trough) / base_peak) * 100.0 if base_peak > 0 else 0.0
    if first_drop_pct > 36.0 or first_drop_pct < 6.0:
        return {"valid": False}

    right_5d_h = float(high_s.iloc[-6:-1].max())
    right_5d_l = float(low_s.iloc[-6:-1].min())
    curr_5d_h = float(high_s.iloc[-5:].max())
    curr_5d_l = float(low_s.iloc[-5:].min())

    last_drop_prev5 = ((right_5d_h - right_5d_l) / right_5d_h) * 100.0 if right_5d_h > 0 else 0.0
    last_drop_curr5 = ((curr_5d_h - curr_5d_l) / curr_5d_h) * 100.0 if curr_5d_h > 0 else 0.0
    final_tight_pct = min(last_drop_prev5, last_drop_curr5)

    mid_len = max(5, int(days_since_peak * 0.45))
    mid_h = float(base_h.iloc[-mid_len:-5].max()) if mid_len > 6 else base_peak
    mid_l = float(base_l.iloc[-mid_len:-5].min()) if mid_len > 6 else base_trough
    mid_drop_pct = ((mid_h - mid_l) / mid_h) * 100.0 if mid_h > 0 else first_drop_pct

    base_weeks = max(3, int(round(days_since_peak / 5.0)))

    is_vcp_contracting = False
    is_darvas_box = False
    t_label = "2T"

    if (first_drop_pct > mid_drop_pct * 1.15) and (mid_drop_pct > last_drop_curr5 * 1.15) and (last_drop_curr5 <= 8.0):
        is_vcp_contracting = True
        t_label = "4T" if base_weeks >= 12 and final_tight_pct <= 4.0 else "3T"
    elif (last_drop_curr5 <= first_drop_pct * 0.55) and (last_drop_curr5 <= 7.5):
        is_vcp_contracting = True
        t_label = "2T"
    elif (last_drop_prev5 <= first_drop_pct * 0.55) and (last_drop_prev5 <= 7.5):
        is_vcp_contracting = True
        t_label = "3T" if (first_drop_pct > mid_drop_pct * 1.15) else "2T"
    elif (4 <= base_weeks <= 8) and (8.0 <= first_drop_pct <= 15.5) and (last_drop_curr5 <= 6.5):
        is_darvas_box = True
        is_vcp_contracting = True
        t_label = "Darvas箱形"

    right_handle_high = float(high_s.iloc[-13:-1].max())
    if right_handle_high >= base_peak * 0.92:
        pivot_price = right_handle_high
        pivot_desc = f"柄部/右側樞紐 <b>{pivot_price:.2f}</b>" if right_handle_high < base_peak * 0.99 else f"前高樞紐 <b>{pivot_price:.2f}</b>"
    else:
        pivot_price = base_peak
        pivot_desc = f"基底高點樞紐 <b>{pivot_price:.2f}</b>"

    vol_50ma_now = float(vol_ma50_s.iloc[-2]) if vol_ma50_s.iloc[-2] > 0 else 1.0
    vol_right_5d = float(vol_s.iloc[-6:-1].mean())
    vol_min_5d = float(vol_s.iloc[-6:-1].min())
    vol_pct_of_50ma = (vol_right_5d / vol_50ma_now) * 100.0
    has_volume_dryup = (vol_pct_of_50ma <= 78.0) or ((vol_min_5d / vol_50ma_now) <= 0.55)

    disp_last = int(round(final_tight_pct))
    disp_first = int(round(first_drop_pct))
    footprint_code = f"{base_weeks}W {disp_first}/{disp_last} {t_label}"
    footprint_detail = f"<b>{footprint_code}</b> (打底{base_weeks}週｜首波-{first_drop_pct:.1f}% ➔ 樞紐-{final_tight_pct:.1f}%)"

    return {
        "valid": True,
        "is_vcp": is_vcp_contracting,
        "is_darvas": is_darvas_box,
        "weeks": base_weeks,
        "first_drop": first_drop_pct,
        "mid_drop": mid_drop_pct,
        "last_drop": last_drop_curr5,
        "pre_brk_drop": last_drop_prev5,
        "pivot_price": pivot_price,
        "pivot_desc": pivot_desc,
        "footprint_code": footprint_code,
        "footprint_detail": footprint_detail,
        "vol_pct_50ma": vol_pct_of_50ma,
        "has_dryup": has_volume_dryup
    }

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

def send_minervini_reports(cat1_brk, cat2_star, cat3_squat, cat4_pb, cat5_beat, total_universe, kline_scanned, template_passed):
    all_blocks = []
    if cat1_brk:
        all_blocks.append("🚀 <b>【一、今日突破樞紐買點 (Pivot Breakout — 立即行動)】</b>\n<i>(p.266/270：完成 VCP/箱形收斂 + 今日帶量穿越最小阻力樞紐線)</i>")
        all_blocks.extend(cat1_brk)
    if cat2_star:
        all_blocks.append("\n★◎ <b>【二、星號圓圈：樞紐點成型池 (Pivot Ready — 隨時突破)】</b>\n<i>(p.268/277：已完成 2T/3T/Darvas 右側窄幅收斂 + 樞紐量縮，設觸價單等突破)</i>")
        all_blocks.extend(cat2_star)
    if cat3_squat:
        all_blocks.append("\n🔄 <b>【三、Squat/RR 蹲下反轉 & 網球折返 (p.271 & p.280)】</b>\n<i>(突破或測試樞紐後短暫拉回 5~7 天、量縮穩守 20MA 之上，展開反轉回升)</i>")
        all_blocks.extend(cat3_squat)
    if cat4_pb:
        all_blocks.append("\n📌 <b>【四、pb 拉回整理觀察池 (Pullback Watchlist — p.278)】</b>\n<i>(符合 p.106 八大趨勢樣板 + p.99 價漲量增承接，拉回均線整理待右側收窄)</i>")
        all_blocks.extend(cat4_pb)
    if cat5_beat:
        all_blocks.append("\n💥 <b>【五、獨立專區：財報／營收超預期強勢股 (p.55 SEPA 盈餘驚喜)】</b>\n<i>(最新財報 EPS 超出法人預期或營收/獲利爆發 + 今日帶量跳空或創高之領頭羊)</i>")
        all_blocks.extend(cat5_beat)

    if not all_blocks:
        send_telegram_msg(f"📊 <b>【《超級績效》SEPA 選股雷達】</b>\n母體 {total_universe} 檔｜K線掃描 {kline_scanned} 檔｜符合八大趨勢樣板 {template_passed} 檔。")
        return

    chunk_size = 7
    total_pages = (len(all_blocks) + chunk_size - 1) // chunk_size
    for i in range(0, len(all_blocks), chunk_size):
        page = (i // chunk_size) + 1
        chunk = all_blocks[i:i + chunk_size]
        header = (
            f"📊 <b>【《超級績效》SEPA 筆記本選股日報】({page}/{total_pages})</b>\n"
            f"<i>母體 {total_universe} 檔｜掃描 {kline_scanned} 檔｜符合 p.106 趨勢樣板 {template_passed} 檔</i>\n\n"
        )
        send_telegram_msg(header + "\n\n".join(chunk))

# ==========================================
# 6. 主引擎
# ==========================================
def analyze_market_minervini(stock_dict):
    symbols = list(stock_dict.keys())
    raw_data = {}
    tw_rs_scores = {}
    us_rs_scores = {}

    start_date = (pd.Timestamp.now() - pd.DateOffset(months=16)).strftime('%Y-%m-%d')
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

    tw_brk, us_brk = [], []
    tw_star, us_star = [], []
    tw_squat, us_squat = [], []
    tw_pb, us_pb = [], []
    momentum_burst_candidates = [] # 候選進入「第五區：獨立財報超預期榜」之強勢跳空/爆量股
    template_passed_count = 0

    for symbol, df in raw_data.items():
        open_p = df['Open']
        close = df['Close']
        high = df['High']
        low = df['Low']
        vol = df['Volume']

        o_now, c_now, h_now, l_now = float(open_p.iloc[-1]), float(close.iloc[-1]), float(high.iloc[-1]), float(low.iloc[-1])
        c_prev = float(close.iloc[-2])
        rs_rating = rs_rating_map.get(symbol, 50)
        name = stock_dict.get(symbol, symbol)
        is_tw = symbol.endswith(".TW") or symbol.endswith(".TWO")

        ma20 = close.rolling(20).mean()
        ma50 = close.rolling(50).mean()
        ma150 = close.rolling(150).mean()
        ma200 = close.rolling(200).mean()
        vol_ma50 = vol.rolling(50).mean()

        if vol_ma50.iloc[-1] < 150000:
            continue

        high_52w = float(high.iloc[-252:].max())
        low_52w = float(low.iloc[-252:].min())
        ma200_up_1m = float(ma200.iloc[-1]) > float(ma200.iloc[-22])
        ma200_up_4m = float(ma200.iloc[-1]) > float(ma200.iloc[-44]) > float(ma200.iloc[-88])

        trend_template_8_rules = (
            (c_now > float(ma150.iloc[-1])) and (c_now > float(ma200.iloc[-1])) and
            (float(ma150.iloc[-1]) > float(ma200.iloc[-1])) and
            ma200_up_1m and
            (float(ma50.iloc[-1]) > float(ma150.iloc[-1])) and (float(ma50.iloc[-1]) > float(ma200.iloc[-1])) and
            (c_now > float(ma50.iloc[-1])) and
            (c_now >= low_52w * 1.30) and
            (c_now >= high_52w * 0.75) and
            (rs_rating >= 70)
        )
        if not trend_template_8_rules:
            continue

        template_passed_count += 1
        doubled_from_low = (c_now >= low_52w * 2.0)

        recent_40_close_diff = close.diff().iloc[-40:]
        recent_40_open_diff = (close - open_p).iloc[-40:]
        recent_40_vol = vol.iloc[-40:]
        recent_40_vma50 = vol_ma50.iloc[-40:]

        above_avg_vol = recent_40_vol > recent_40_vma50
        up_bars_40 = (recent_40_close_diff > 0) & (recent_40_open_diff >= 0)
        down_bars_40 = (recent_40_close_diff < 0) | (recent_40_open_diff < 0)

        up_vol_spikes = int((above_avg_vol & up_bars_40).sum())
        down_vol_spikes = int((above_avg_vol & down_bars_40).sum())

        vol_ratio_today = float(vol.iloc[-1] / vol_ma50.iloc[-2]) if vol_ma50.iloc[-2] > 0 else 1.0
        is_red_today = (c_now > o_now) and (c_now > c_prev) and ((c_now - l_now) >= (h_now - l_now) * 0.5 if h_now > l_now else True)

        tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
        atr20 = float(tr.rolling(20).mean().iloc[-1])
        stop_distance = min(2 * atr20, c_now * 0.08)
        stop_price = c_now - stop_distance
        stop_pct = (stop_distance / c_now) * 100.0

        flag = "🇹🇼" if is_tw else "🇺🇸"
        title = f"{flag} {symbol} {name}" if name != symbol else f"{flag} {symbol}"

        # 收集今日帶量跳空或大漲創高的領導股，稍後檢查是否為「財報/營收超預期」獨立專區標的
        if is_red_today and (vol_ratio_today >= 1.5) and (c_now >= high.iloc[-20:-1].max()):
            momentum_burst_candidates.append((rs_rating + int(vol_ratio_today * 10), symbol, title, c_now, rs_rating, vol_ratio_today, stop_price, stop_pct))

        max_vol_date = vol.iloc[-60:].idxmax()
        if (close.loc[max_vol_date] < open_p.loc[max_vol_date] * 0.99) or (close.loc[max_vol_date] < close.shift(1).loc[max_vol_date] * 0.99):
            continue
        if ((c_now < o_now * 0.99) or (c_now < c_prev * 0.99)) and (vol_ratio_today >= 1.3):
            continue
        if down_vol_spikes > up_vol_spikes + 1:
            continue

        fp = analyze_minervini_footprint(high, low, close, vol, vol_ma50)
        if not fp["valid"]:
            continue

        pivot_price = fp["pivot_price"]
        dist_to_pivot_pct = ((c_now - pivot_price) / pivot_price) * 100.0
        if dist_to_pivot_pct > 5.0 or dist_to_pivot_pct < -15.0:
            continue

        sepa_score = rs_rating
        if rs_rating >= 85:
            sepa_score += 15
        if ma200_up_4m:
            sepa_score += 10
        if doubled_from_low:
            sepa_score += 15
        if up_vol_spikes > down_vol_spikes:
            sepa_score += (up_vol_spikes - down_vol_spikes) * 4
        if fp["has_dryup"]:
            sepa_score += 20
        sepa_score += int(max(0, (10.0 - fp["last_drop"]) * 12))

        tags = [f"📊大量漲/跌支數 {up_vol_spikes}:{down_vol_spikes}"]
        if fp["has_dryup"]:
            tags.append(f"💧樞紐量縮({fp['vol_pct_50ma']:.0f}%均量)")
        if doubled_from_low:
            tags.append("👑低點翻倍")

        peak_60_idx = high.iloc[-61:-1].idxmax()
        sub_df = df.loc[peak_60_idx:]
        tp = (sub_df['High'] + sub_df['Low'] + sub_df['Close']) / 3
        vwap_series = (tp * sub_df['Volume']).cumsum() / sub_df['Volume'].cumsum()
        vwap_now = float(vwap_series.iloc[-1])
        if c_now >= vwap_now:
            tags.append(f"✅VWAP之上({vwap_now:.1f})")

        support_str = " | ".join(tags)
        above_20ma = c_now >= float(ma20.iloc[-1])

        if fp["is_vcp"] and above_20ma and is_red_today and (c_now > pivot_price) and (c_prev <= pivot_price) and (vol_ratio_today >= 1.35):
            status_str = f"今日帶量({vol_ratio_today:.1f}倍)突破 {fp['pivot_desc']}"
            item = (sepa_score + 30, symbol, title, c_now, rs_rating, status_str, fp["footprint_detail"], support_str, stop_price, stop_pct)
            (tw_brk if is_tw else us_brk).append(item)

        elif fp["is_vcp"] and above_20ma and (c_now <= pivot_price) and (dist_to_pivot_pct >= -6.5) and (fp["last_drop"] <= 7.0) and (fp["vol_pct_50ma"] <= 95.0):
            gap_pct = abs(dist_to_pivot_pct)
            star_mark = "★◎" if (fp["has_dryup"] and fp["last_drop"] <= 5.0) else "★"
            status_str = f"<b>{star_mark} 樞紐成型</b>｜差 +{gap_pct:.1f}% 突破 {fp['pivot_desc']}"
            item = (sepa_score + 20, symbol, title, c_now, rs_rating, status_str, fp["footprint_detail"], support_str, stop_price, stop_pct)
            (tw_star if is_tw else us_star).append(item)

        elif above_20ma and (float(high.iloc[-8:-1].max()) >= pivot_price * 0.99) and (c_now <= pivot_price * 1.02) and is_red_today and (fp["last_drop"] <= 8.5):
            status_str = f"<b>Squat/網球回升</b>｜守穩 20MA({ma20.iloc[-1]:.1f}) 轉強｜樞紐 <b>{pivot_price:.2f}</b>"
            item = (sepa_score + 10, symbol, title, c_now, rs_rating, status_str, fp["footprint_detail"], support_str, stop_price, stop_pct)
            (tw_squat if is_tw else us_squat).append(item)

        elif (c_now <= pivot_price) and (fp["last_drop"] <= fp["first_drop"] * 0.70) and (fp["last_drop"] <= 9.0) and (float(ma20.iloc[-1]) >= float(ma20.iloc[-10])):
            gap_pct = abs(dist_to_pivot_pct)
            status_str = f"<b>pb 拉回整理</b>｜距 {fp['pivot_desc']} +{gap_pct:.1f}%｜等最右端量縮突破"
            item = (sepa_score, symbol, title, c_now, rs_rating, status_str, fp["footprint_detail"], support_str, stop_price, stop_pct)
            (tw_pb if is_tw else us_pb).append(item)

    for lst in [tw_brk, us_brk, tw_star, us_star, tw_squat, us_squat, tw_pb, us_pb, momentum_burst_candidates]:
        lst.sort(key=lambda x: x[0], reverse=True)

    sel_brk = tw_brk[:4] + us_brk[:4]
    sel_star = tw_star[:6] + us_star[:5]
    sel_squat = tw_squat[:4] + us_squat[:3]
    sel_pb = tw_pb[:4] + us_pb[:4]

    already_listed_symbols = set()

    print("💰 正在查詢最新季報 EPS 與「財報/營收超預期」數據...")
    def format_block(items, prefix_icon):
        out = []
        for item in items:
            _, sym, title, c_now, rs, status_s, fp_s, sup_s, sp, sp_pct = item
            already_listed_symbols.add(sym)
            eps_str, is_beat, beat_tag = get_eps_and_surprise_info(sym)
            # 【規則】若上榜的 VCP 股票同時有財報/營收超預期，直接在標題旁醒目標註！
            title_display = f"{title} 🔥<b>[{beat_tag}]</b>" if is_beat else title
            out.append(
                f"<b>{title_display}</b> (收盤: {c_now:.2f} | <b>RS: {rs}</b>)\n"
                f" ├ {prefix_icon} <b>狀態</b>：{status_s}\n"
                f" ├ 📐 <b>技術足跡</b>：{fp_s}\n"
                f" ├ 💰 <b>獲利表現</b>：{eps_str}\n"
                f" ├ 💡 <b>承接配角</b>：{sup_s}\n"
                f" └ 🛡️ <b>建議停損</b>：<b>{sp:.2f}</b> (-{sp_pct:.1f}%)"
            )
        return out

    b1 = format_block(sel_brk, "🚀")
    b2 = format_block(sel_star, "🎯")
    b3 = format_block(sel_squat, "🔄")
    b4 = format_block(sel_pb, "📌")

    # 【規則】未在前面 VCP 清單上榜、但今日帶量創高且「財報/營收超預期」之標的，獨立列於第五區！
    b5_beat = []
    for item in momentum_burst_candidates[:25]:
        _, sym, title, c_now, rs, v_ratio, sp, sp_pct = item
        if sym in already_listed_symbols:
            continue
        eps_str, is_beat, beat_tag = get_eps_and_surprise_info(sym)
        if is_beat:
            b5_beat.append(
                f"<b>{title}</b> 🔥<b>[{beat_tag}]</b> (收盤: {c_now:.2f} | <b>RS: {rs}</b>)\n"
                f" ├ 💥 <b>財報催化</b>：今日帶量({v_ratio:.1f}倍)強勢突破創高\n"
                f" ├ 💰 <b>獲利表現</b>：{eps_str}\n"
                f" └ 🛡️ <b>建議停損</b>：<b>{sp:.2f}</b> (-{sp_pct:.1f}%)"
            )
        if len(b5_beat) >= 6:
            break

    return b1, b2, b3, b4, b5_beat, len(raw_data), template_passed_count

if __name__ == "__main__":
    stock_dict, total_universe = get_entire_market_universe()
    cat1, cat2, cat3, cat4, cat5, kline_scanned, template_passed = analyze_market_minervini(stock_dict)
    send_minervini_reports(cat1, cat2, cat3, cat4, cat5, total_universe, kline_scanned, template_passed)
