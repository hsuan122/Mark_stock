import os
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import warnings
warnings.filterwarnings("ignore")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "請換成你的_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "請換成你的_CHAT_ID")

def send_telegram_msg(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"❌ 網路連線錯誤：{e}")

# ==========================================
# 1. 自動抓取證交所 (TWSE) 與櫃買中心 (TPEx) 官方「處置股」名單
# ==========================================
def get_official_disposition_stocks():
    disp_dict = {} # 格式: {"6213.TW": "處置原因與期間"}
    # A. 上市處置股官方 API
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/announcement/punish", timeout=15)
        if res.status_code == 200:
            for row in res.json():
                code = str(row.get('Code', '')).strip()
                if code and len(code) == 4:
                    period = str(row.get('DispositionPeriod', '')).strip()
                    measures = str(row.get('DispositionMeasures', '')).strip()
                    disp_dict[f"{code}.TW"] = f"上市處置({period})"
    except Exception as e:
        print(f"⚠️ 上市處置股 API 異常: {e}")

    # B. 上櫃處置股官方 API
    try:
        res_otc = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_disposal_information", timeout=15)
        if res_otc.status_code == 200:
            for row in res_otc.json():
                code = str(row.get('SecuritiesCompanyCode', '')).strip()
                if code and len(code) == 4:
                    period = str(row.get('DisposalPeriod', '')).strip()
                    disp_dict[f"{code}.TWO"] = f"上櫃處置({period})"
    except Exception as e:
        print(f"⚠️ 上櫃處置股 API 異常: {e}")

    print(f"✅ 成功抓取官方處置股共 {len(disp_dict)} 檔！")
    return disp_dict

# ==========================================
# 2. 抓取台股全市場清單
# ==========================================
def get_tw_all_stocks():
    tw_dict = {}
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=15)
        if res.status_code == 200:
            df = pd.DataFrame(res.json())
            df = df[df['Code'].str.match(r'^[1-9]\d{3}$')]
            df['TradeVolume'] = pd.to_numeric(df['TradeVolume'].str.replace(',', ''), errors='coerce')
            for _, r in df[df['TradeVolume'] >= 100000].iterrows():
                tw_dict[f"{r['Code']}.TW"] = r['Name'].strip()
    except Exception:
        pass

    try:
        res_otc = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", timeout=15)
        if res_otc.status_code == 200:
            df = pd.DataFrame(res_otc.json())
            df = df[df['SecuritiesCompanyCode'].str.match(r'^[1-9]\d{3}$')]
            df['TradingShares'] = pd.to_numeric(df['TradingShares'].str.replace(',', ''), errors='coerce')
            for _, r in df[df['TradingShares'] >= 100000].iterrows():
                tw_dict[f"{r['SecuritiesCompanyCode']}.TWO"] = r['CompanyName'].strip()
    except Exception:
        pass

    return tw_dict

# ==========================================
# 3. 執行小哥 7 大短線與處置戰法掃描
# ==========================================
def run_xiaoge_strategies():
    disp_stocks = get_official_disposition_stocks()
    tw_dict = get_tw_all_stocks()
    # 確保所有處置股都在掃描池中
    for k in disp_stocks.keys():
        if k not in tw_dict:
            tw_dict[k] = k

    symbols = list(tw_dict.keys())
    start_date = (pd.Timestamp.now() - pd.DateOffset(months=14)).strftime('%Y-%m-%d')

    # 先檢查大盤 (0050.TW) 與台指狀況
    market_status = "⚪ 大盤盤整中"
    try:
        m_df = yf.Ticker("0050.TW").history(period="3mo")
        if len(m_df) >= 20:
            m_c = m_df['Close'].iloc[-1]
            m_prev = m_df['Close'].iloc[-2]
            m_ma20 = m_df['Close'].rolling(20).mean().iloc[-1]
            m_chg = ((m_c - m_prev) / m_prev) * 100
            if m_c > m_ma20 or m_chg > 0.3:
                market_status = f"🟢 大盤強勢 (0050 漲跌 {m_chg:+.2f}%｜適合作「進處置弱、大盤強」抄底)"
            elif m_chg <= -1.0:
                market_status = f"🔴 大盤急殺 ({m_chg:+.2f}%｜留意國安基金防線或地板價爆量反彈)"
    except Exception:
        pass

    disposition_list = []   # 1. 處置股回檔 10~20% 抄底戰法
    prodigal_son_list = []  # 2. 浪子回頭 (飆股回測月線 20MA，位階<=5)
    floor_rebound_list = [] # 3. 地板價殺低出量易彈 & 跌停敲開
    ceiling_turn_list = []  # 4. 天花板價隔日沖急拉爆量易轉折
    bollinger_monster = []  # 5. 開布林沿上軌妖股 & 跳空一字線

    batch_size = 180
    print(f"📥 正在掃描台股 {len(symbols)} 檔短線與處置訊號...")
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        try:
            data = yf.download(batch, start=start_date, group_by="ticker", threads=True, progress=False)
            for sym in batch:
                try:
                    df = data[sym].copy() if len(batch) > 1 else data.copy()
                    df = df[['Open', 'High', 'Low', 'Close', 'Volume']].dropna()
                    df = df[df['Volume'] > 0]
                    if len(df) < 120:
                        continue

                    open_p, high, low, close, vol = df['Open'], df['High'], df['Low'], df['Close'], df['Volume']
                    o_now, h_now, l_now, c_now = float(open_p.iloc[-1]), float(high.iloc[-1]), float(low.iloc[-1]), float(close.iloc[-1])
                    c_prev = float(close.iloc[-2])
                    name = tw_dict.get(sym, sym)
                    title = f"🇹🇼 {sym} {name}"

                    ma10 = close.rolling(10).mean()
                    ma20 = close.rolling(20).mean()
                    ma60 = close.rolling(60).mean()
                    vol_ma20 = vol.rolling(20).mean()

                    ma20_now = float(ma20.iloc[-1])
                    vma20_now = float(vol_ma20.iloc[-2]) if vol_ma20.iloc[-2] > 0 else 1.0
                    vol_ratio = float(vol.iloc[-1]) / vma20_now

                    # ----------------------------------------------------
                    # 戰法 A：【處置股戰法】進處置弱 (自高點回檔 8%~25%) 或 處置期間靠近月線
                    # ----------------------------------------------------
                    if sym in disp_stocks:
                        peak_20d = float(high.iloc[-25:].max())
                        drop_from_peak = ((peak_20d - c_now) / peak_20d) * 100.0
                        dist_20ma = ((c_now - ma20_now) / ma20_now) * 100.0
                        if drop_from_peak >= 8.0 or abs(dist_20ma) <= 5.0:
                            disposition_list.append((
                                drop_from_peak,
                                f"<b>{title}</b> (收盤: {c_now:.2f} | {disp_stocks[sym]})\n"
                                f" ├ 🎯 <b>處置回檔抄底</b>：自高點({peak_20d:.1f})已回檔 <b>-{drop_from_peak:.1f}%</b>\n"
                                f" └ 📍 月線(20MA)支撐：<b>{ma20_now:.2f}</b> (乖離 {dist_20ma:+.1f}%)"
                            ))

                    if vma20_now < 500000:
                        continue

                    # ----------------------------------------------------
                    # 戰法 B：【浪子回頭】過去 60 天曾大漲逾 45% 的飆股，拉回靠近月線(20MA ±3.5%內)，且近10日位階 <= 3
                    # ----------------------------------------------------
                    low_60d = float(low.iloc[-65:-10].min())
                    high_30d = float(high.iloc[-35:-3].max())
                    surge_pct = ((high_30d - low_60d) / low_60d) * 100.0 if low_60d > 0 else 0.0
                    pullback_from_high = ((high_30d - c_now) / high_30d) * 100.0
                    dist_to_20ma = ((c_now - ma20_now) / ma20_now) * 100.0

                    # 計算近 15 日位階 (0 ~ 10 級，<= 3 代表落入低位階支撐區)
                    h15, l15 = float(high.iloc[-15:].max()), float(low.iloc[-15:].min())
                    pos_rank = ((c_now - l15) / (h15 - l15) * 10.0) if h15 > l15 else 5.0

                    if (surge_pct >= 45.0) and (pullback_from_high >= 8.0) and (-2.5 <= dist_to_20ma <= 3.5) and (pos_rank <= 3.8) and (ma20_now > float(ma60.iloc[-1])):
                        disp_tag = f" 🔥[{disp_stocks[sym]}]" if sym in disp_stocks else ""
                        prodigal_son_list.append((
                            surge_pct,
                            f"<b>{title}</b>{disp_tag} (收盤: {c_now:.2f})\n"
                            f" ├ 🌊 <b>【浪子回頭】飆股近月線</b>：前波狂飆 +{surge_pct:.0f}% 後回檔 -{pullback_from_high:.1f}%\n"
                            f" └ 📍 位階：<b>{pos_rank:.1f}</b> (≤4)｜月線支撐：<b>{ma20_now:.2f}</b>"
                        ))

                    # ----------------------------------------------------
                    # 戰法 C：【地板／天花板反轉價位監控表】(100% 還原第9張截圖公式！)
                    # 公式：取過去每日 (股價 - 20MA) / 20MA，排序第 95% 作為正負偏離門檻，且當日成交量 >= 20日均量 2 倍
                    # ----------------------------------------------------
                    bias_series = ((close - ma20) / ma20).dropna()
                    pos_biases = bias_series[bias_series > 0]
                    neg_biases = bias_series[bias_series < 0].abs()

                    ceil_thresh = float(np.percentile(pos_biases, 95)) if len(pos_biases) > 20 else 0.15
                    floor_thresh = float(np.percentile(neg_biases, 95)) if len(neg_biases) > 20 else 0.10

                    ceiling_price = ma20_now * (1.0 + ceil_thresh)
                    floor_price = ma20_now * (1.0 - floor_thresh)

                    # 檢查「跌停敲開」：今日最低價跌破昨收 -9.2% (觸及或逼近跌停)，但收盤拉升超過 3%
                    limit_down_price = c_prev * 0.905
                    limit_down_opened = (l_now <= limit_down_price) and (c_now >= l_now * 1.03) and (vol_ratio >= 1.5)

                    # 檢查「殺低出量觸及地板價易彈」：今日最低價 <= 地板價 且 成交量 >= 20日均量 2 倍 且留長下影線或收紅
                    hit_floor_with_vol = (l_now <= floor_price * 1.01) and (vol_ratio >= 2.0) and ((c_now - l_now) >= (h_now - l_now) * 0.35)

                    if limit_down_opened or hit_floor_with_vol:
                        tag = "🔨跌停敲開爆量長下影線" if limit_down_opened else f"📉殺低出量觸地板價({vol_ratio:.1f}倍量)"
                        floor_rebound_list.append((
                            vol_ratio,
                            f"<b>{title}</b> (收盤: {c_now:.2f} | 最低: {l_now:.2f})\n"
                            f" ├ ⚡ <b>{tag}</b>\n"
                            f" └ 📏 小哥95%地板價：<b>{floor_price:.2f}</b> (負乖離門檻 -{floor_thresh*100:.1f}%)｜月線: {ma20_now:.2f}"
                        ))

                    # 檢查「隔日沖急拉爆量觸天花板價易轉折」：今日最高價 >= 天花板價 且 成交量 >= 20日均量 2 倍 且開高走低/留上影線
                    hit_ceiling_with_vol = (h_now >= ceiling_price) and (vol_ratio >= 2.0) and ((c_now < o_now) or ((h_now - c_now) / c_now >= 0.03))
                    if hit_ceiling_with_vol:
                        ceiling_turn_list.append((
                            vol_ratio,
                            f"<b>{title}</b> (收盤: {c_now:.2f} | 最高: {h_now:.2f})\n"
                            f" ├ ⚠️ <b>急拉爆量觸天花板({vol_ratio:.1f}倍量)</b>：留上影線/黑K易短線轉折\n"
                            f" └ 📏 小哥95%天花板價：<b>{ceiling_price:.2f}</b> (正乖離門檻 +{ceil_thresh*100:.1f}%)"
                        ))

                    # ----------------------------------------------------
                    # 戰法 D：【開布林沿上軌妖股 & 跳空一字線軋空股】
                    # ----------------------------------------------------
                    std20 = close.rolling(20).std()
                    upper_bb = ma20 + 2.0 * std20
                    lower_bb = ma20 - 2.0 * std20
                    bb_width = (upper_bb - lower_bb) / ma20

                    is_locked_gap_up = (o_now == h_now == l_now == c_now) and (c_now >= c_prev * 1.09)
                    is_opening_bb = (
                        (c_now > float(upper_bb.iloc[-1])) and
                        (float(bb_width.iloc[-1]) > float(bb_width.iloc[-2]) * 1.15) and
                        (float(bb_width.iloc[-2]) <= 0.15) and
                        (vol_ratio >= 2.2) and
                        (c_now > o_now)
                    )
                    if is_locked_gap_up or is_opening_bb:
                        m_tag = "🚫跳空一字漲停鎖死(極強軋空勿空)" if is_locked_gap_up else f"🚀開布林首根爆量({vol_ratio:.1f}倍)站上軌妖股"
                        bollinger_monster.append((
                            vol_ratio,
                            f"<b>{title}</b> (收盤: {c_now:.2f})\n"
                            f" ├ 🔥 <b>{m_tag}</b>\n"
                            f" └ 📏 布林上軌：<b>{float(upper_bb.iloc[-1]):.2f}</b>｜明日參考天花板價：<b>{ceiling_price:.2f}</b>"
                        ))
                except Exception:
                    continue
        except Exception:
            continue

    for lst in [disposition_list, prodigal_son_list, floor_rebound_list, ceiling_turn_list, bollinger_monster]:
        lst.sort(key=lambda x: x[0], reverse=True)

    blocks = [f"⚡ <b>【小哥 7 大短線＆處置戰法雷達】</b>\n{market_status}\n"]
    if disposition_list:
        blocks.append("🔒 <b>【一、處置股回檔抄底池 (進處置弱、大盤強好買點)】</b>")
        blocks.extend([x[1] for x in disposition_list[:6]])
    if prodigal_son_list:
        blocks.append("\n🌊 <b>【二、浪子回頭：強勢飆股回測月線 (位階 ≤ 4)】</b>")
        blocks.extend([x[1] for x in prodigal_son_list[:6]])
    if floor_rebound_list:
        blocks.append("\n🔨 <b>【三、地板價殺低出量易彈 ＆ 跌停敲開】</b>")
        blocks.extend([x[1] for x in floor_rebound_list[:6]])
    if bollinger_monster:
        blocks.append("\n🚀 <b>【四、開布林沿上軌妖股 ＆ 跳空一字線(勿空)】</b>")
        blocks.extend([x[1] for x in bollinger_monster[:6]])
    if ceiling_turn_list:
        blocks.append("\n⚠️ <b>【五、天花板價急拉爆量警戒 (隔日沖易轉折)】</b>")
        blocks.extend([x[1] for x in ceiling_turn_list[:5]])

    chunk_size = 8
    for i in range(0, len(blocks), chunk_size):
        send_telegram_msg("\n\n".join(blocks[i:i + chunk_size]))

if __name__ == "__main__":
    run_xiaoge_strategies()
