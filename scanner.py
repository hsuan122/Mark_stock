import os
import io
from datetime import datetime
from zoneinfo import ZoneInfo
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import warnings
warnings.filterwarnings("ignore")

# ==========================================
# 1. 讀取 Telegram 設定與【我的持股追蹤清單】
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "請換成你的_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "請換成你的_CHAT_ID")

# 📦 實戰持股追蹤 ("股票代號": 買進成本價)
MY_HOLDINGS = {
    "FMX": 133.48,
}

# ==========================================
# 2. 《超級績效》p.270 盤中推估成交量乘數計算器
# ==========================================
def get_intraday_vol_multiplier(last_bar_date, is_tw):
    try:
        bar_date = pd.Timestamp(last_bar_date).date()
        if is_tw:
            now_tw = datetime.now(ZoneInfo("Asia/Taipei"))
            if bar_date == now_tw.date():
                mins_from_open = (now_tw.hour * 60 + now_tw.minute) - (9 * 60)
                if 5 <= mins_from_open < 270:
                    return min(10.0, 270.0 / max(15, mins_from_open)), True
        else:
            now_ny = datetime.now(ZoneInfo("America/New_York"))
            if bar_date == now_ny.date():
                mins_from_open = (now_ny.hour * 60 + now_ny.minute) - (9 * 60 + 30)
                if 5 <= mins_from_open < 390:
                    return min(10.0, 390.0 / max(15, mins_from_open)), True
    except Exception:
        pass
    return 1.0, False

# ==========================================
# 3. 全市場股票池抓取 (台股全上市櫃含KY股 + 美股全市場)
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
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
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
# 4. 查詢 EPS 與「財報是否超預期」
# ==========================================
def get_eps_and_surprise_info(symbol):
    try:
        tk = yf.Ticker(symbol)
        info = tk.info or {}
        ttm_eps = info.get('trailingEps')
        q_growth = info.get('earningsQuarterlyGrowth')
        rev_growth = info.get('revenueGrowth')

        surprise_pct = None
        try:
            eh = tk.earnings_history
            if eh is not None and not eh.empty and 'surprisePercent' in eh.columns:
                valid_eh = eh['surprisePercent'].dropna()
                if len(valid_eh) > 0:
                    surprise_pct = float(valid_eh.iloc[-1]) * 100.0
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
            parts.append(f"季EPS <b>{latest_q_eps:.2f}</b>")
        elif ttm_eps is not None:
            parts.append(f"四季EPS <b>{ttm_eps:.2f}</b>")
        if q_growth is not None:
            sign = "+" if q_growth >= 0 else ""
            parts.append(f"年增 <b>{sign}{q_growth*100:.0f}%</b>")

        beat_tags = []
        is_beat = False
        if surprise_pct is not None and surprise_pct >= 5.0:
            is_beat = True
            beat_tags.append(f"🔥EPS超預期+{surprise_pct:.0f}%")
        if rev_growth is not None and rev_growth >= 0.25 and (q_growth is None or q_growth > 0):
            is_beat = True
            beat_tags.append(f"💥營收+{rev_growth*100:.0f}%")
        elif q_growth is not None and q_growth >= 0.40 and (latest_q_eps is not None and latest_q_eps > 0):
            is_beat = True
            beat_tags.append(f"💥獲利+{q_growth*100:.0f}%")

        beat_str = " ".join(beat_tags)
        eps_short = "｜".join(parts) if parts else "財報更新中"
        return eps_short, is_beat, beat_str
    except Exception:
        return "財報更新中", False, ""

# ==========================================
# 5. 📦 我的持股守護雷達 (使用未還原真實價格對齊看盤軟體)
# ==========================================
def check_my_holdings(raw_data):
    if not MY_HOLDINGS:
        return ""
    lines = ["📦 <b>【我的持股守護雷達 (p.272 20MA 鐵律)】</b>"]
    for sym, cost in MY_HOLDINGS.items():
        try:
            df = raw_data.get(sym)
            if df is None or len(df) < 30:
                df = yf.Ticker(sym).history(period="3mo", auto_adjust=False)
            if df is None or len(df) < 25:
                continue
            c_now = float(df['Close'].iloc[-1])
            ma20_now = float(df['Close'].rolling(20).mean().iloc[-1])
            tr = pd.concat([df['High'] - df['Low'], (df['High'] - df['Close'].shift(1)).abs(), (df['Low'] - df['Close'].shift(1)).abs()], axis=1).max(axis=1)
            atr20 = float(tr.rolling(20).mean().iloc[-1])
            stop_price = c_now - min(2 * atr20, c_now * 0.08)
            pnl_pct = ((c_now - cost) / cost) * 100.0
            pnl_icon = "🟢" if pnl_pct >= 0 else "🔻"

            if c_now < stop_price:
                advice = f"🚨 <b>觸發停損：現價已跌破停損線({stop_price:.2f})，應執行停損！</b>"
            elif c_now < ma20_now:
                advice = f"⚠️ <b>警戒：收盤跌破 20MA({ma20_now:.2f})，依 p.272 應考慮減碼或嚴守停損 {stop_price:.2f}</b>"
            else:
                advice = f"✅ <b>續抱安全</b>：穩守 20MA(<b>{ma20_now:.2f}</b>) 之上｜🛡️停損線：<b>{stop_price:.2f}</b>"

            lines.append(
                f"• <b>{sym}</b>｜現價 <b>{c_now:.2f}</b> (成本 {cost:.2f}｜{pnl_icon} <b>{pnl_pct:+.1f}%</b>)\n  └ {advice}"
            )
        except Exception:
            continue
    return "\n".join(lines) + "\n" + ("─" * 18) + "\n\n"

# ==========================================
# 6. 《超級績效》技術面足跡 (XW A/B NT) 演算法
# ==========================================
def analyze_minervini_footprint(high_s, low_s, close_s, vol_s, vol_ma50_s):
    lookback = min(90, len(high_s) - 2)
    window_h = high_s.iloc[-(lookback + 1):-1]
    peak_pos = int(window_h.values.argmax())
    days_since_peak = len(window_h) - peak_pos

    is_short_pullback = False
    if days_since_peak < 14:
        is_short_pullback = True
        days_since_peak = max(15, days_since_peak + 15)

    base_h = high_s.iloc[-(days_since_peak + 1):-1]
    base_l = low_s.iloc[-(days_since_peak + 1):-1]
    base_peak = float(base_h.max())
    base_trough = float(base_l.min())

    first_drop_pct = ((base_peak - base_trough) / base_peak) * 100.0 if base_peak > 0 else 0.0
    if first_drop_pct > 35.0 or first_drop_pct < 5.0:
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
    t_label = "2T"

    if (first_drop_pct > mid_drop_pct * 1.12) and (mid_drop_pct > last_drop_curr5 * 1.12) and (last_drop_curr5 <= 8.5):
        is_vcp_contracting = True
        t_label = "4T" if base_weeks >= 12 and final_tight_pct <= 4.0 else "3T"
    elif (last_drop_curr5 <= first_drop_pct * 0.60) and (last_drop_curr5 <= 8.0):
        is_vcp_contracting = True
        t_label = "2T"
    elif (last_drop_prev5 <= first_drop_pct * 0.60) and (last_drop_prev5 <= 8.0):
        is_vcp_contracting = True
        t_label = "3T" if (first_drop_pct > mid_drop_pct * 1.12) else "2T"
    elif (3 <= base_weeks <= 8) and (6.0 <= first_drop_pct <= 16.0) and (last_drop_curr5 <= 7.0):
        is_vcp_contracting = True
        t_label = "箱形"

    right_handle_high = float(high_s.iloc[-13:-1].max())
    if right_handle_high >= base_peak * 0.92:
        pivot_price = right_handle_high
    else:
        pivot_price = base_peak

    vol_50ma_now = float(vol_ma50_s.iloc[-2]) if vol_ma50_s.iloc[-2] > 0 else 1.0
    vol_right_5d = float(vol_s.iloc[-6:-1].mean())
    vol_min_5d = float(vol_s.iloc[-6:-1].min())
    vol_pct_of_50ma = (vol_right_5d / vol_50ma_now) * 100.0
    has_volume_dryup = (vol_pct_of_50ma <= 80.0) or ((vol_min_5d / vol_50ma_now) <= 0.55)

    disp_last = int(round(final_tight_pct))
    disp_first = int(round(first_drop_pct))
    footprint_code = f"{base_weeks}W {disp_first}/{disp_last} {t_label}"

    return {
        "valid": True,
        "is_vcp": is_vcp_contracting,
        "is_short_pb": is_short_pullback,
        "weeks": base_weeks,
        "first_drop": first_drop_pct,
        "last_drop": last_drop_curr5,
        "pivot_price": pivot_price,
        "footprint_code": footprint_code,
        "vol_pct_50ma": vol_pct_of_50ma,
        "has_dryup": has_volume_dryup
    }

# ==========================================
# 7. Telegram 極簡清爽版推播函式
# ==========================================
def send_telegram_msg(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"❌ 網路連線錯誤：{e}")

def send_minervini_reports(holdings_msg, cat1_brk, cat2_star, cat3_squat, cat4_pb, cat5_beat, total_universe, kline_scanned, template_passed):
    all_blocks = []
    if cat1_brk:
        all_blocks.append("🚀 <b>【一、今日突破樞紐買點 (立即行動)】</b>")
        all_blocks.extend(cat1_brk)
    if cat2_star:
        all_blocks.append("\n★◎ <b>【二、星號樞紐成型池 (設觸價單等突破)】</b>")
        all_blocks.extend(cat2_star)
    if cat3_squat:
        all_blocks.append("\n🔄 <b>【三、Squat 蹲下反轉 & 網球折返 (守20MA轉強)】</b>")
        all_blocks.extend(cat3_squat)
    if cat4_pb:
        all_blocks.append("\n📌 <b>【四、pb 拉回整理觀察池 (等右端量縮突破)】</b>")
        all_blocks.extend(cat4_pb)
    if cat5_beat:
        all_blocks.append("\n💥 <b>【五、財報／營收超預期強勢股 (SEPA 盈餘驚喜)】</b>")
        all_blocks.extend(cat5_beat)

    if not all_blocks:
        send_telegram_msg(f"{holdings_msg}📊 <b>【《超級績效》SEPA 選股雷達】</b>\n母體 {total_universe} 檔｜掃描 {kline_scanned} 檔｜過模板 {template_passed} 檔。")
        return

    chunk_size = 8
    total_pages = (len(all_blocks) + chunk_size - 1) // chunk_size
    for i in range(0, len(all_blocks), chunk_size):
        page = (i // chunk_size) + 1
        chunk = all_blocks[i:i + chunk_size]
        top_prefix = holdings_msg if page == 1 else ""
        header = (
            f"{top_prefix}📊 <b>【《超級績效》極簡選股日報】({page}/{total_pages})</b>\n"
            f"<i>母體 {total_universe} 檔｜掃描 {kline_scanned} 檔｜未還原真實均線過模板 {template_passed} 檔</i>\n\n"
        )
        send_telegram_msg(header + "\n\n".join(chunk))

# ==========================================
# 8. 主引擎 (強制 auto_adjust=False 對齊看盤軟體 + 封殺年線糾纏股)
# ==========================================
def analyze_market_minervini(stock_dict):
    symbols = list(stock_dict.keys())
    raw_data = {}
    tw_rs_scores = {}
    us_rs_scores = {}

    start_date = (pd.Timestamp.now() - pd.DateOffset(months=16)).strftime('%Y-%m-%d')
    batch_size = 180

    print(f"📥 正在高速批次下載 {len(symbols)} 檔台美股未還原真實 K 線...")
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        try:
            # 【關鍵修正 1】強制設定 auto_adjust=False！
            # 確保 50MA/150MA/200MA 與 52 週高低點跟台灣看盤軟體 100% 一致，不被除權息誤導！
            data = yf.download(batch, start=start_date, auto_adjust=False, group_by="ticker", threads=True, progress=False)
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
    momentum_burst_candidates = []
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

        # ====================================================
        # 👑 第一層：原書 p.106 八大趨勢樣板 + 【關鍵修正 2】封殺春源這種年線糾纏股！
        # ====================================================
        high_52w = float(high.iloc[-252:].max())
        low_52w = float(low.iloc[-252:].min())

        # 200MA 必須明確處於上升趨勢 (今日 > 1個月前 > 2個月前)
        ma200_up_strict = (float(ma200.iloc[-1]) > float(ma200.iloc[-22])) and (float(ma200.iloc[-22]) > float(ma200.iloc[-44]))
        ma200_up_4m = ma200_up_strict and (float(ma200.iloc[-44]) > float(ma200.iloc[-88]))

        # 【新增鐵律】過去 15 個交易日收盤價必須「全部」站在 150MA 與 200MA 之上！
        # 只要最近 3 週內有跌破過 150MA 或 200MA (如春源前幾天都在 22.97 底下)，代表處於第一階段糾纏，直接剔除！
        stayed_above_long_ma = (close.iloc[-15:] > ma150.iloc[-15:]).all() and (close.iloc[-15:] > ma200.iloc[-15:]).all()

        # 【新增鐵律】50MA 必須明確高於 150MA 至少 1.5% 以上，拒絕 50MA/150MA/200MA 黏成麻花的股票！
        clear_ma_separation = (float(ma50.iloc[-1]) >= float(ma150.iloc[-1]) * 1.015) and (float(ma150.iloc[-1]) > float(ma200.iloc[-1]))

        trend_template_8_rules = (
            stayed_above_long_ma and
            clear_ma_separation and
            ma200_up_strict and
            (c_now > float(ma50.iloc[-1])) and
            (c_now >= low_52w * 1.30) and
            (c_now >= high_52w * 0.75) and
            (rs_rating >= 70)
        )
        if not trend_template_8_rules:
            continue

        template_passed_count += 1
        doubled_from_low = (c_now >= low_52w * 2.0)

        # 👑 第二層：原書 p.99 大量上漲支數 vs 大量下跌支數
        recent_40_close_diff = close.diff().iloc[-40:]
        recent_40_vol = vol.iloc[-40:]
        recent_40_vma50 = vol_ma50.iloc[-40:]

        above_avg_vol = recent_40_vol > recent_40_vma50
        up_bars_40 = recent_40_close_diff > 0
        down_bars_40 = recent_40_close_diff < 0

        up_vol_spikes = int((above_avg_vol & up_bars_40).sum())
        down_vol_spikes = int((above_avg_vol & down_bars_40).sum())

        intraday_mult, is_intraday = get_intraday_vol_multiplier(df.index[-1], is_tw)
        raw_vol_ratio = float(vol.iloc[-1] / vol_ma50.iloc[-2]) if vol_ma50.iloc[-2] > 0 else 1.0
        vol_ratio_today = raw_vol_ratio * intraday_mult
        vol_type_label = "預估量" if is_intraday else "量"

        is_red_today = (c_now >= o_now) and (c_now > c_prev) and ((c_now - l_now) >= (h_now - l_now) * 0.45 if h_now > l_now else True)

        tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
        atr20 = float(tr.rolling(20).mean().iloc[-1])
        stop_distance = min(2 * atr20, c_now * 0.08)
        stop_price = c_now - stop_distance
        stop_pct = (stop_distance / c_now) * 100.0

        flag = "🇹🇼" if is_tw else "🇺🇸"
        title = f"{flag} {symbol} {name}" if name != symbol else f"{flag} {symbol}"

        if is_red_today and (vol_ratio_today >= 1.5) and (c_now >= high.iloc[-20:-1].max()):
            momentum_burst_candidates.append((rs_rating + int(vol_ratio_today * 10), symbol, title, c_now, rs_rating, vol_ratio_today, vol_type_label, stop_price, stop_pct))

        max_vol_date = vol.iloc[-60:].idxmax()
        if close.loc[max_vol_date] < close.shift(1).loc[max_vol_date] * 0.98:
            continue
        if (c_now < c_prev * 0.985) and (vol_ratio_today >= 1.35):
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

        peak_60_idx = high.iloc[-61:-1].idxmax()
        sub_df = df.loc[peak_60_idx:]
        tp = (sub_df['High'] + sub_df['Low'] + sub_df['Close']) / 3
        vwap_series = (tp * sub_df['Volume']).cumsum() / sub_df['Volume'].cumsum()
        vwap_now = float(vwap_series.iloc[-1])
        vwap_tag = f"✅VWAP <b>{vwap_now:.1f}</b>" if c_now >= vwap_now else f"VWAP {vwap_now:.1f}"
        dry_tag = "💧量縮｜" if fp["has_dryup"] else ""
        chip_summary = f"{dry_tag}大戶買賣 <b>{up_vol_spikes}:{down_vol_spikes}</b>｜{vwap_tag}"

        above_20ma = c_now >= float(ma20.iloc[-1]) * 0.995

        # 1. 🚀 【今日突破樞紐買點】
        if fp["is_vcp"] and above_20ma and is_red_today and (c_now > pivot_price) and (c_prev <= pivot_price) and (vol_ratio_today >= 1.3):
            action_line = f"🚀 <b>突破買點：{pivot_price:.2f}</b> ({vol_type_label}{vol_ratio_today:.1f}倍)"
            item = (sepa_score + 30, symbol, title, c_now, rs_rating, action_line, fp["footprint_code"], chip_summary, stop_price, stop_pct)
            (tw_brk if is_tw else us_brk).append(item)

        # 2. ★◎ 【星號圓圈：樞紐點成型池】
        elif fp["is_vcp"] and above_20ma and (c_now <= pivot_price) and (dist_to_pivot_pct >= -7.0) and (fp["last_drop"] <= 7.5):
            gap_pct = abs(dist_to_pivot_pct)
            star_mark = "★◎" if (fp["has_dryup"] and fp["last_drop"] <= 5.0) else "★"
            action_line = f"🎯 <b>{star_mark} 樞紐買點：{pivot_price:.2f}</b> (差 +{gap_pct:.1f}%)"
            item = (sepa_score + 20, symbol, title, c_now, rs_rating, action_line, fp["footprint_code"], chip_summary, stop_price, stop_pct)
            (tw_star if is_tw else us_star).append(item)

        # 3. 🔄 【Squat/RR 蹲下反轉 & 網球折返】
        elif above_20ma and ((float(high.iloc[-10:-1].max()) >= pivot_price * 0.99) or fp["is_short_pb"]) and (c_now <= pivot_price * 1.02) and (fp["last_drop"] <= 9.0):
            gap_pct = max(0.0, ((pivot_price - c_now) / c_now) * 100.0)
            action_line = f"🔄 <b>守20MA({ma20.iloc[-1]:.1f})轉強｜樞紐：{pivot_price:.2f}</b> (差 +{gap_pct:.1f}%)"
            item = (sepa_score + 10, symbol, title, c_now, rs_rating, action_line, fp["footprint_code"], chip_summary, stop_price, stop_pct)
            (tw_squat if is_tw else us_squat).append(item)

        # 4. 📌 【pb 拉回整理觀察池】
        elif (c_now <= pivot_price) and (fp["last_drop"] <= fp["first_drop"] * 0.80) and (fp["last_drop"] <= 10.0):
            gap_pct = abs(dist_to_pivot_pct)
            action_line = f"📌 <b>pb拉回｜樞紐：{pivot_price:.2f}</b> (差 +{gap_pct:.1f}%)"
            item = (sepa_score, symbol, title, c_now, rs_rating, action_line, fp["footprint_code"], chip_summary, stop_price, stop_pct)
            (tw_pb if is_tw else us_pb).append(item)

    for lst in [tw_brk, us_brk, tw_star, us_star, tw_squat, us_squat, tw_pb, us_pb, momentum_burst_candidates]:
        lst.sort(key=lambda x: x[0], reverse=True)

    sel_brk = tw_brk[:4] + us_brk[:4]
    sel_star = tw_star[:5] + us_star[:4]
    sel_squat = tw_squat[:4] + us_squat[:3]
    sel_pb = tw_pb[:5] + us_pb[:3]

    already_listed_symbols = set()

    print("💰 正在產生 3 行極簡版日報與持股診斷...")
    holdings_msg = check_my_holdings(raw_data)

    def format_clean_card(items):
        out = []
        for item in items:
            _, sym, title, c_now, rs, action_line, fp_code, chip_s, sp, sp_pct = item
            already_listed_symbols.add(sym)
            eps_short, is_beat, beat_tag = get_eps_and_surprise_info(sym)
            beat_badge = f" <b>[{beat_tag}]</b>" if is_beat else ""
            out.append(
                f"<b>{title}</b>｜現價 <b>{c_now:.2f}</b>｜RS <b>{rs}</b>{beat_badge}\n"
                f"  {action_line}｜🛡️停損 <b>{sp:.2f}</b> (-{sp_pct:.1f}%)\n"
                f"  📐<code>{fp_code}</code>｜{chip_s}｜{eps_short}"
            )
        return out

    b1 = format_clean_card(sel_brk)
    b2 = format_clean_card(sel_star)
    b3 = format_clean_card(sel_squat)
    b4 = format_clean_card(sel_pb)

    b5_beat = []
    for item in momentum_burst_candidates[:25]:
        _, sym, title, c_now, rs, v_ratio, v_label, sp, sp_pct = item
        if sym in already_listed_symbols:
            continue
        eps_short, is_beat, beat_tag = get_eps_and_surprise_info(sym)
        if is_beat:
            b5_beat.append(
                f"<b>{title}</b>｜現價 <b>{c_now:.2f}</b>｜RS <b>{rs}</b> <b>[{beat_tag}]</b>\n"
                f"  💥 <b>財報催化突破</b> ({v_label}{v_ratio:.1f}倍)｜🛡️停損 <b>{sp:.2f}</b> (-{sp_pct:.1f}%)\n"
                f"  💰 {eps_short}"
            )
        if len(b5_beat) >= 5:
            break

    return holdings_msg, b1, b2, b3, b4, b5_beat, len(raw_data), template_passed_count

if __name__ == "__main__":
    stock_dict, total_universe = get_entire_market_universe()
    holdings_msg, cat1, cat2, cat3, cat4, cat5, kline_scanned, template_passed = analyze_market_minervini(stock_dict)
    send_minervini_reports(holdings_msg, cat1, cat2, cat3, cat4, cat5, total_universe, kline_scanned, template_passed)
