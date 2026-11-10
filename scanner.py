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
# 2. 全市場股票池抓取 (不限股價高低！台股全上市櫃 + 美股 S&P500 / Nasdaq100)
# ==========================================
def get_full_market_stock_list():
    stock_dict = {}
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

    # A. 台股【全上市】普通股 (不限制股價！僅排除日量 < 100 張的極冷門殭屍股)
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

    # B. 台股【全上櫃】普通股 (不限制股價！僅排除日量 < 100 張的殭屍股)
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
# 3. Telegram 推播函式
# ==========================================
def send_telegram_msg(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"❌ 網路連線錯誤：{e}")

def send_minervini_reports(breakouts, vcp_watchlist, pullbacks, total_scanned, template_passed):
    all_blocks = []
    if breakouts:
        all_blocks.append("🚀 <b>【一、今日突破樞紐買點 (Pivot Breakout)】</b>\n<i>(符合 Mark 模板 + VCP 收斂後今日帶量突破前波小高點)</i>")
        all_blocks.extend(breakouts)
    if vcp_watchlist:
        all_blocks.append("\n🎯 <b>【二、每週 VCP 波動收縮觀察池 (Weekly Watchlist)】</b>\n<i>(符合 Mark 模板 + 波動由左至右遞減、量縮整理等突破)</i>")
        all_blocks.extend(vcp_watchlist)
    if pullbacks:
        all_blocks.append("\n🎾 <b>【三、強勢股淺回檔／網球反彈 (Tennis Ball Action)】</b>\n<i>(符合 Mark 模板 + 回測均線量縮守穩或紅K彈升)</i>")
        all_blocks.extend(pullbacks)

    if not all_blocks:
        send_telegram_msg(f"📊 <b>【Mark Minervini 主角選股雷達 V5】</b>\n已掃描 {total_scanned} 檔，通過趨勢模板共 {template_passed} 檔，但今日無符合 VCP 收斂條件之標的。")
        return

    chunk_size = 8
    total_pages = (len(all_blocks) + chunk_size - 1) // chunk_size
    for i in range(0, len(all_blocks), chunk_size):
        page = (i // chunk_size) + 1
        chunk = all_blocks[i:i + chunk_size]
        header = (
            f"📊 <b>【Mark Minervini 選股日報 V5】({page}/{total_pages})</b>\n"
            f"<i>掃描 {total_scanned} 檔｜通過 Mark 趨勢模板 {template_passed} 檔</i>\n\n"
        )
        send_telegram_msg(header + "\n\n".join(chunk))

# ==========================================
# 4. 主角：Mark Minervini 完整理論引擎 + 配角：其他 6 大指標
# ==========================================
def analyze_market_minervini(stock_dict):
    symbols = list(stock_dict.keys())
    raw_data = {}
    tw_rs_scores = {}
    us_rs_scores = {}

    # 抓取 24 個月資料，確保剔除假日時仍有充足的 265+ 交易日計算 200MA 上升 3 個月
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
                    # 【關鍵修復】徹底清除含 NaN 或成交量為 0 的無效列，防止均線計算變 NaN
                    df = df[['Open', 'High', 'Low', 'Close', 'Volume']].dropna()
                    df = df[df['Volume'] > 0]
                    if len(df) < 265:
                        continue
                    
                    c = df['Close']
                    raw_data[sym] = df
                    
                    # 歐奈爾 RS 加權計算
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

    breakout_list = []
    vcp_watchlist = []
    pullback_list = []
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

        # 均線與均量
        ma10 = close.rolling(10).mean()
        ma20 = close.rolling(20).mean()
        ma50 = close.rolling(50).mean()
        ma150 = close.rolling(150).mean()
        ma200 = close.rolling(200).mean()
        vol_ma20 = vol.rolling(20).mean()
        vol_ma50 = vol.rolling(50).mean()

        # 基本流動性門檻 (20日均量 >= 250張)
        if vol_ma20.iloc[-1] < 250000:
            continue

        # ====================================================
        # 👑 【主角第一關】Mark Minervini 趨勢模板 (Trend Template)
        # ====================================================
        high_52w = high.iloc[-252:].max()
        low_52w = low.iloc[-252:].min()

        # 1. 200MA 至少上升 3 個月 (63個交易日)
        ma200_rising_3m = (ma200.iloc[-1] > ma200.iloc[-21]) and (ma200.iloc[-21] > ma200.iloc[-42]) and (ma200.iloc[-42] > ma200.iloc[-63])
        # 2. 過去 4~6 個月 (126天) 內曾創下 52 週新高
        rolling_52w_high = high.rolling(252).max()
        made_high_in_6m = (high.iloc[-126:] >= rolling_52w_high.iloc[-126:] * 0.99).any()

        trend_template_pass = (
            (c_now > ma50.iloc[-1]) and
            (c_now > ma150.iloc[-1]) and
            (c_now > ma200.iloc[-1]) and
            (ma50.iloc[-1] > ma150.iloc[-1] > ma200.iloc[-1]) and
            ma200_rising_3m and
            (c_now >= low_52w * 1.30) and       # 高於 52 週低點至少 30%
            (c_now >= high_52w * 0.75) and      # 距離 52 週高點 25% 以內
            made_high_in_6m and                 # 近 4~6 個月內曾創高
            (rs_rating >= 80)                   # 強勢領頭羊 RS >= 80
        )

        if not trend_template_pass:
            continue

        template_passed_count += 1
        doubled_from_low = (c_now >= low_52w * 2.0) # 貼文強調：距 52 週低點翻倍 (+100%)

        # ====================================================
        # 👑 【主角第二關】Mark 的 VCP 波動收縮與價量結構
        # ====================================================
        # 排除基底崩潰股：從近半年最高點回檔不得超過 33% (排除大甲那種跌42%的結構破壞股)
        peak_6m = high.iloc[-126:].max()
        peak_6m_pos = high.iloc[-126:].values.argmax()
        low_after_peak = low.iloc[-126 + peak_6m_pos:].min()
        base_depth = (peak_6m - low_after_peak) / peak_6m
        if base_depth > 0.33:
            continue

        # 排除「今日爆量下跌 / 開高走低大黑K」(排除藥華藥 11/10 那種出貨長黑)
        vol_ratio_today = vol.iloc[-1] / vol_ma20.iloc[-2] if vol_ma20.iloc[-2] > 0 else 1.0
        is_distribution_today = ((c_now < o_now * 0.985) or (c_now < c_prev * 0.985)) and (vol_ratio_today >= 1.25)
        # 排除近 5 日內有超巨量長黑出貨 (如佳大 11/05 爆 2.5 倍量長上影線收黑)
        recent_5 = df.iloc[-5:]
        recent_climax_dump = (
            ((recent_5['Close'] < recent_5['Open']) | ((recent_5['High'] - recent_5['Close']) / recent_5['High'] > 0.05)) &
            (recent_5['Volume'] > vol_ma20.iloc[-5:] * 2.0)
        ).any()
        if is_distribution_today or recent_climax_dump:
            continue

        # 照貼文公式計算 3 段波動收縮率：(波段低點 / 波段高點) - 1
        # 左段 C1 (前 30~45 天)、中段 C2 (前 15~30 天)、右段 C3 (近 15 天，不含今日)
        c1_drop = abs((low.iloc[-46:-31].min() / high.iloc[-46:-31].max()) - 1)
        c2_drop = abs((low.iloc[-31:-16].min() / high.iloc[-31:-16].max()) - 1)
        c3_drop = abs((low.iloc[-16:-1].min() / high.iloc[-16:-1].max()) - 1)
        recent_5d_drop = abs((low.iloc[-6:-1].min() / high.iloc[-6:-1].max()) - 1)

        # 定義「前波小高點 (Previous Swing High = 樞紐買點 Pivot Point)」
        pivot_high = high.iloc[-21:-1].max()
        dist_from_pivot = (c_now - pivot_high) / pivot_high

        # Mark 的不追高原則：若目前股價已經突破樞紐點超過 +8% 以上，代表已錯過買點 (自然淘汰騰雲)
        if dist_from_pivot > 0.08:
            continue

        # 檢查 VCP 收斂條件：
        # 條件 A (3段遞減或右段明顯小於左/中段，且右段收斂至 12% 內，或近5日收斂至 6% 內)
        max_prev_drop = max(c1_drop, c2_drop)
        is_vcp_contracting = (
            ((c3_drop <= max_prev_drop * 0.70) and (c3_drop <= 0.12)) or
            ((recent_5d_drop <= max_prev_drop * 0.50) and (recent_5d_drop <= 0.065))
        )

        # 檢查價量配合 (Up Volume vs Down Volume & 回檔量縮 Consolidating with lower volume)
        price_diff_20 = close.diff().iloc[-20:]
        vol_20 = vol.iloc[-20:]
        up_vol_avg = vol_20[price_diff_20 > 0].mean() if (price_diff_20 > 0).any() else 0
        down_vol_avg = vol_20[price_diff_20 <= 0].mean() if (price_diff_20 <= 0).any() else 1
        good_up_down_vol = up_vol_avg >= (down_vol_avg * 0.95)

        vol_5d_avg = vol.iloc[-6:-1].mean()
        vol_quiet = vol_5d_avg <= (vol_ma50.iloc[-2] * 0.90) # 整理期成交量低於 50 日均量 90%

        # ====================================================
        # 👑 【主角第三關】計算 Mark 停損價 (2x 20日ATR，最大 10%)
        # ====================================================
        tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
        atr20 = tr.rolling(20).mean().iloc[-1]
        stop_distance = min(2 * atr20, c_now * 0.10)
        stop_price = c_now - stop_distance
        stop_pct = (stop_distance / c_now) * 100

        # ====================================================
        # 🧩 【配角指標計算】只當加分輔助標籤，絕不用來淘汰股票！
        # ====================================================
        supporting_tags = []
        if doubled_from_low:
            supporting_tags.append("👑低點翻倍(+100%)")
        if good_up_down_vol:
            supporting_tags.append("📊上漲量>下跌量")

        # 配角 1：Auto Anchored Swing VWAP (Martin Luk)
        peak_60_idx = high.iloc[-61:-1].idxmax()
        sub_df = df.loc[peak_60_idx:]
        tp = (sub_df['High'] + sub_df['Low'] + sub_df['Close']) / 3
        vwap_series = (tp * sub_df['Volume']).cumsum() / sub_df['Volume'].cumsum()
        vwap_now = vwap_series.iloc[-1]
        if (c_now > vwap_now) and (c_prev <= vwap_series.iloc[-2] if len(vwap_series) > 1 else False):
            supporting_tags.append(f"📈剛站上VWAP({vwap_now:.1f})")
        elif c_now > vwap_now:
            supporting_tags.append(f"✅高點VWAP之上({vwap_now:.1f})")

        # 配角 2：Oliver Kell Wedge Pop (WP)
        ema10 = close.ewm(span=10, adjust=False).mean()
        ema20 = close.ewm(span=20, adjust=False).mean()
        if (low.iloc[-6:-1] < ema20.iloc[-6:-1]).any() and (c_now > ema10.iloc[-1]) and (c_now > ema20.iloc[-1]) and (c_prev <= ema10.iloc[-2] or c_prev <= ema20.iloc[-2]):
            supporting_tags.append("⚡Wedge Pop")

        # 配角 3：Dan Zanger Volume Ratio
        if (c_now > o_now) and (vol_ratio_today >= 1.8):
            supporting_tags.append(f"🔥Zanger爆量({vol_ratio_today:.1f}倍)")

        # 配角 4：Larry Williams Vix Fix (恐慌見底綠柱)
        highest_c22 = close.rolling(22).max()
        wvf = ((highest_c22 - low) / highest_c22) * 100
        wvf_upper = wvf.rolling(20).mean() + 2.0 * wvf.rolling(20).std()
        if wvf.iloc[-1] >= wvf_upper.iloc[-1]:
            supporting_tags.append("🟢Vix_Fix洗盤見底")

        support_str = " | ".join(supporting_tags) if supporting_tags else "標準多頭排列"
        flag = "🇹🇼" if is_tw else "🇺🇸"
        title = f"{flag} {symbol} {name}" if name != symbol else f"{flag} {symbol}"
        vcp_str = f"左-{c1_drop*100:.0f}% ➔ 中-{c2_drop*100:.0f}% ➔ 右-{c3_drop*100:.0f}% (近5日-{recent_5d_drop*100:.1f}%)"

        # ====================================================
        # 👑 依 Mark 三大實戰情境分類輸出
        # ====================================================
        is_red_k = (c_now > o_now) and (c_now > c_prev) and ((c_now - l_now) >= (h_now - l_now) * 0.5 if h_now > l_now else True)

        # 情境 1：🚀 今日突破前波小高點 (Buy when previous swing high is taken out)
        if is_vcp_contracting and is_red_k and (c_now > pivot_high) and (c_prev <= pivot_high) and (vol_ratio_today >= 1.3):
            score = rs_rating + (20 if doubled_from_low else 0) + (15 if good_up_down_vol else 0)
            msg = (
                f"<b>{title}</b> (收盤: {c_now:.2f} | <b>RS: {rs_rating}</b>)\n"
                f" ├ 👑 <b>Mark突破買點</b>：今日帶量({vol_ratio_today:.1f}倍)突破前高樞紐 <b>{pivot_high:.2f}</b>\n"
                f" ├ 📐 VCP收縮：{vcp_str}\n"
                f" ├ 💡 配角輔助：{support_str}\n"
                f" └ 🛡️ 2xATR停損：<b>{stop_price:.2f}</b> (-{stop_pct:.1f}%)"
            )
            breakout_list.append((score, msg))

        # 情境 2：🎯 每週 VCP 波動收縮觀察清單 (Weekly VCP Watchlist - 尚未突破前高，正在量縮收斂)
        elif is_vcp_contracting and vol_quiet and (c_now <= pivot_high) and (c_now >= pivot_high * 0.90):
            score = rs_rating + (15 if doubled_from_low else 0) + (15 if good_up_down_vol else 0) + int((1 - recent_5d_drop) * 10)
            msg = (
                f"<b>{title}</b> (收盤: {c_now:.2f} | <b>RS: {rs_rating}</b>)\n"
                f" ├ 🎯 <b>VCP量縮築底</b>：近5日量縮至均量 {vol_5d_avg/vol_ma50.iloc[-2]*100:.0f}%｜等待突破前高 <b>{pivot_high:.2f}</b>\n"
                f" ├ 📐 VCP收縮：{vcp_str}\n"
                f" ├ 💡 配角輔助：{support_str}\n"
                f" └ 🛡️ 預估停損：<b>{stop_price:.2f}</b> (-{stop_pct:.1f}%)"
            )
            vcp_watchlist.append((score, msg))

        # 情境 3：🎾 淺回檔與網球反彈 (Shallow Pullback / Tennis Ball Action)
        elif good_up_down_vol and (c3_drop <= 0.14) and (low.iloc[-3:].min() <= ma20.iloc[-1] * 1.02) and (c_now >= ma20.iloc[-1]) and is_red_k:
            score = rs_rating + (15 if doubled_from_low else 0)
            msg = (
                f"<b>{title}</b> (收盤: {c_now:.2f} | <b>RS: {rs_rating}</b>)\n"
                f" ├ 🎾 <b>網球反彈/淺回檔</b>：回測 20MA({ma20.iloc[-1]:.2f}) 守穩收紅｜前高樞紐 <b>{pivot_high:.2f}</b>\n"
                f" ├ 📐 波動收縮：{vcp_str}\n"
                f" ├ 💡 配角輔助：{support_str}\n"
                f" └ 🛡️ 2xATR停損：<b>{stop_price:.2f}</b> (-{stop_pct:.1f}%)"
            )
            pullback_list.append((score, msg))

    breakout_list.sort(key=lambda x: x[0], reverse=True)
    vcp_watchlist.sort(key=lambda x: x[0], reverse=True)
    pullback_list.sort(key=lambda x: x[0], reverse=True)

    return (
        [x[1] for x in breakout_list[:10]],
        [x[1] for x in vcp_watchlist[:12]],
        [x[1] for x in pullback_list[:8]],
        len(raw_data),
        template_passed_count
    )

if __name__ == "__main__":
    stock_dict = get_full_market_stock_list()
    breakouts, vcp_watch, pullbacks, total_scanned, template_passed = analyze_market_minervini(stock_dict)
    send_minervini_reports(breakouts, vcp_watch, pullbacks, total_scanned, template_passed)
