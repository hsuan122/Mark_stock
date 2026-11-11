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
    disp_dict = {}
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/announcement/punish", timeout=15)
        if res.status_code == 200:
            for row in res.json():
                code = str(row.get('Code', '')).strip()
                if code and len(code) == 4:
                    period = str(row.get('DispositionPeriod', '')).strip()
                    disp_dict[f"{code}.TW"] = f"上市處置({period})"
    except Exception as e:
        print(f"⚠️ 上市處置股 API 異常: {e}")

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
# 3. 執行小哥 2026/11/11 直播完整版戰法掃描
# ==========================================
def run_xiaoge_strategies():
    disp_stocks = get_official_disposition_stocks()
    tw_dict = get_tw_all_stocks()
    for k in disp_stocks.keys():
        if k not in tw_dict:
            tw_dict[k] = k

    symbols = list(tw_dict.keys())
    start_date = (pd.Timestamp.now() - pd.DateOffset(months=14)).strftime('%Y-%m-%d')

    # 1. 檢查大盤 (0050.TW) 單日與近 5 日漲跌幅 (作為處置股「大盤強、處置弱」的比較基準)
    market_status = "⚪ 大盤盤整中"
    m_5d_chg = 0.0
    try:
        m_df = yf.Ticker("0050.TW").history(period="3mo")
        if len(m_df) >= 20:
            m_c = float(m_df['Close'].iloc[-1])
            m_prev = float(m_df['Close'].iloc[-2])
            m_5d_ago = float(m_df['Close'].iloc[-6])
            m_ma20 = float(m_df['Close'].rolling(20).mean().iloc[-1])
            m_chg = ((m_c - m_prev) / m_prev) * 100.0
            m_5d_chg = ((m_c - m_5d_ago) / m_5d_ago) * 100.0
            if m_c > m_ma20 or m_chg > 0.3:
                market_status = f"🟢 大盤強勢 (0050 今日 {m_chg:+.2f}%｜近5日 {m_5d_chg:+.2f}%)\n💡 <i>直播重點：大盤漲時找「處置股累幅最低/跌10~20%」或「前日收黑K之同族群補漲股」</i>"
            elif m_chg <= -1.0:
                market_status = f"🔴 大盤急殺 ({m_chg:+.2f}%)\n💡 <i>直播重點：夜盤若殺逾1%可做多(賺錢隔日收盤出、賠錢留倉至結算)；留意地板股10%/20%分批搶反彈</i>"
    except Exception:
        pass

    disposition_list = []   # 1. 處置股戰法 (依累幅由低到高排序 + 處置跌10%/20%通知 + 低檔平盤吸籌)
    prodigal_son_list = []  # 2. 浪子回頭 (飆股回測月線 20MA，位階<=4)
    floor_rebound_list = [] # 3. 地板價殺低出量易彈 & 跌停敲開 (含 10%/20% 資金 SOP)
    bollinger_monster = []  # 4. 開布林沿上軌妖股 (位階>=15 + 上軌斜率>3) & 跳空一字線 (絕對勿空！)
    autumn_short_list = []  # 5. 隔日衝急拉爆量觸天花板 & 四季選股法「秋天」反彈月線轉弱

    total_below_floor_count = 0 # 統計全市場跌破地板價總家數 (直播重點：家數暴增代表大盤見底即將強彈！)

    batch_size = 180
    print(f"📥 正在掃描台股 {len(symbols)} 檔小哥直播戰法訊號...")
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
                    c_5d_ago = float(close.iloc[-6])
                    name = tw_dict.get(sym, sym)
                    title = f"🇹🇼 {sym} {name}"

                    ma10 = close.rolling(10).mean()
                    ma20 = close.rolling(20).mean()
                    ma60 = close.rolling(60).mean()
                    vol_ma20 = vol.rolling(20).mean()

                    ma20_now = float(ma20.iloc[-1])
                    ma60_now = float(ma60.iloc[-1])
                    vma20_now = float(vol_ma20.iloc[-2]) if vol_ma20.iloc[-2] > 0 else 1.0
                    vol_ratio = float(vol.iloc[-1]) / vma20_now

                    # 計算近 15 日位階 (0 ~ 20 級，直播提到位階 >= 15 為極強勢高檔，位階 <= 4 為低位階)
                    h15, l15 = float(high.iloc[-15:].max()), float(low.iloc[-15:].min())
                    pos_rank_10 = ((c_now - l15) / (h15 - l15) * 10.0) if h15 > l15 else 5.0
                    pos_rank_20 = pos_rank_10 * 2.0

                    # ----------------------------------------------------
                    # 戰法 A：【處置股戰法】(直播重點：找累幅最低補漲股 + 處置跌10%/20% + 低檔狹幅橫盤有人狂接)
                    # ----------------------------------------------------
                    if sym in disp_stocks:
                        peak_20d = float(high.iloc[-25:].max())
                        drop_from_peak = ((peak_20d - c_now) / peak_20d) * 100.0
                        stock_5d_chg = ((c_now - c_5d_ago) / c_5d_ago) * 100.0
                        lag_vs_market = stock_5d_chg - m_5d_chg # 負越多代表大盤在漲它越沒漲 (累幅最低)
                        dist_20ma = ((c_now - ma20_now) / ma20_now) * 100.0

                        tags = []
                        if drop_from_peak >= 20.0:
                            tags.append(f"🚨處置後重挫-{drop_from_peak:.1f}%(滿20%級別)")
                        elif drop_from_peak >= 10.0:
                            tags.append(f"🎯處置後回檔-{drop_from_peak:.1f}%(滿10%級別)")
                        if lag_vs_market <= -4.0:
                            tags.append(f"🐢累幅落後大盤{lag_vs_market:.1f}%(補漲潛力)")
                        # 檢查致茂型態：回檔後今日在平盤附近狹幅震盪 (高低振幅 <= 3.5%) 且收守平盤附近消化最後賣壓
                        if (drop_from_peak >= 8.0) and (((h_now - l_now) / l_now) <= 0.035) and (abs(c_now - c_prev) / c_prev <= 0.015):
                            tags.append("🧲低檔平盤狹幅承接(消化最後賣壓)")

                        if tags or drop_from_peak >= 6.0:
                            tag_str = " | ".join(tags) if tags else f"自高點回檔-{drop_from_peak:.1f}%"
                            # 依「落後大盤幅度」與「回檔深度」排序，把最委屈的處置股排第一
                            sort_key = drop_from_peak - lag_vs_market
                            disposition_list.append((
                                sort_key,
                                f"<b>{title}</b> (收盤: {c_now:.2f} | 近5日: {stock_5d_chg:+.1f}%)\n"
                                f" ├ 🔒 <b>{disp_stocks[sym]}</b>\n"
                                f" ├ 💡 訊號：{tag_str}\n"
                                f" └ 📍 月線(20MA)：<b>{ma20_now:.2f}</b> (乖離 {dist_20ma:+.1f}%)"
                            ))

                    if vma20_now < 400000:
                        continue

                    # ----------------------------------------------------
                    # 戰法 B：【浪子回頭】前波狂飆股回測月線 20MA (位階 <= 4)
                    # ----------------------------------------------------
                    low_60d = float(low.iloc[-65:-10].min())
                    high_30d = float(high.iloc[-35:-3].max())
                    surge_pct = ((high_30d - low_60d) / low_60d) * 100.0 if low_60d > 0 else 0.0
                    pullback_from_high = ((high_30d - c_now) / high_30d) * 100.0
                    dist_to_20ma = ((c_now - ma20_now) / ma20_now) * 100.0

                    if (surge_pct >= 45.0) and (pullback_from_high >= 8.0) and (-2.5 <= dist_to_20ma <= 3.5) and (pos_rank_10 <= 4.0) and (ma20_now > ma60_now):
                        disp_tag = f" 🔥[{disp_stocks[sym]}]" if sym in disp_stocks else ""
                        prodigal_son_list.append((
                            surge_pct,
                            f"<b>{title}</b>{disp_tag} (收盤: {c_now:.2f})\n"
                            f" ├ 🌊 <b>【浪子回頭】飆股近月線</b>：前波狂飆 +{surge_pct:.0f}% 後回檔 -{pullback_from_high:.1f}%\n"
                            f" └ 📍 位階：<b>{pos_rank_10:.1f}</b> (≤4)｜月線支撐：<b>{ma20_now:.2f}</b>"
                        ))

                    # ----------------------------------------------------
                    # 戰法 C：【地板／天花板反轉監控表】+ 跌停敲開 (含全市場地板股家數統計)
                    # ----------------------------------------------------
                    bias_series = ((close - ma20) / ma20).dropna()
                    pos_biases = bias_series[bias_series > 0]
                    neg_biases = bias_series[bias_series < 0].abs()

                    ceil_thresh = float(np.percentile(pos_biases, 95)) if len(pos_biases) > 20 else 0.15
                    floor_thresh = float(np.percentile(neg_biases, 95)) if len(neg_biases) > 20 else 0.10

                    ceiling_price = ma20_now * (1.0 + ceil_thresh)
                    floor_price = ma20_now * (1.0 - floor_thresh)

                    if l_now <= floor_price:
                        total_below_floor_count += 1

                    limit_down_price = c_prev * 0.905
                    limit_down_opened = (l_now <= limit_down_price) and (c_now >= l_now * 1.03) and (vol_ratio >= 1.5)
                    hit_floor_with_vol = (l_now <= floor_price * 1.01) and (vol_ratio >= 2.0) and ((c_now - l_now) >= (h_now - l_now) * 0.35)

                    if limit_down_opened or hit_floor_with_vol:
                        tag = "🔨跌停敲開爆量長下影線(好買點)" if limit_down_opened else f"📉殺低出量觸地板價({vol_ratio:.1f}倍量)"
                        floor_rebound_list.append((
                            vol_ratio,
                            f"<b>{title}</b> (收盤: {c_now:.2f} | 最低: {l_now:.2f})\n"
                            f" ├ ⚡ <b>{tag}</b>\n"
                            f" ├ 💰 直播資金SOP：首日殺低搶 <b>10%</b> 資金，隔日若再殺低搶 <b>20%</b> 資金\n"
                            f" └ 📏 小哥95%地板價：<b>{floor_price:.2f}</b> (負乖離門檻 -{floor_thresh*100:.1f}%)"
                        ))

                    # ----------------------------------------------------
                    # 戰法 D：【開布林沿上軌妖股 (位階>=15 + 上通道斜率>3)】&【跳空一字線】(直播警告：這兩類絕對不可放空！)
                    # ----------------------------------------------------
                    std20 = close.rolling(20).std()
                    upper_bb = ma20 + 2.0 * std20
                    lower_bb = ma20 - 2.0 * std20
                    bb_width = (upper_bb - lower_bb) / ma20

                    # 計算布林上通道斜率 (近 3 日上軌每日平均上升百分比 * 100)
                    ub_now = float(upper_bb.iloc[-1])
                    ub_3d_ago = float(upper_bb.iloc[-4]) if len(upper_bb) >= 4 else ub_now
                    upper_bb_slope = ((ub_now - ub_3d_ago) / ub_3d_ago) * 100.0 if ub_3d_ago > 0 else 0.0

                    is_locked_gap_up = (o_now == h_now == l_now == c_now) and (c_now >= c_prev * 1.09)
                    is_super_bollinger_monster = (
                        (c_now >= ub_now * 0.995) and
                        (pos_rank_20 >= 15.0) and
                        (upper_bb_slope >= 3.0) and
                        (vol_ratio >= 2.0) and
                        (c_now > o_now)
                    )
                    if is_locked_gap_up or is_super_bollinger_monster:
                        if is_locked_gap_up:
                            m_tag = "🚫跳空一字漲停鎖死 (少漲盤中段，隔日極強軋空絕對勿空！)"
                        else:
                            m_tag = f"👹開布林沿上軌妖股 (位階 {pos_rank_20:.0f}≥15｜上軌斜率 {upper_bb_slope:.1f}>3｜絕對勿空！)"
                        bollinger_monster.append((
                            upper_bb_slope + vol_ratio,
                            f"<b>{title}</b> (收盤: {c_now:.2f} | 量倍數: {vol_ratio:.1f}x)\n"
                            f" ├ 🔥 <b>{m_tag}</b>\n"
                            f" └ 📏 布林上軌：<b>{ub_now:.2f}</b>｜明日參考天花板價：<b>{ceiling_price:.2f}</b>"
                        ))

                    # ----------------------------------------------------
                    # 戰法 E：【隔日沖急拉爆量觸天花板】&【四季選股法「秋天」反彈月線轉弱】(直播短空觀察池)
                    # ----------------------------------------------------
                    hit_ceiling_turn = (h_now >= ceiling_price) and (vol_ratio >= 2.0) and ((c_now < o_now) or ((h_now - c_now) / c_now >= 0.03))
                    # 四季選股法「秋天」：股價在季線(60MA)之下、60MA下彎，反彈觸及月線(20MA)附近且爆量留上影線或收黑
                    is_autumn_pattern = (
                        (c_now < ma60_now) and
                        (ma60_now < float(ma60.iloc[-10])) and
                        (h_now >= ma20_now * 0.99) and
                        (c_now <= ma20_now * 1.02) and
                        ((c_now < o_now) or ((h_now - c_now) / c_now >= 0.025)) and
                        (vol_ratio >= 1.5)
                    )
                    if (hit_ceiling_turn or is_autumn_pattern) and (not is_locked_gap_up) and (upper_bb_slope < 3.0):
                        s_tag = f"⚠️急拉爆量觸天花板({vol_ratio:.1f}倍量)" if hit_ceiling_turn else f"🍂四季選股【秋天】反彈月線遇壓({vol_ratio:.1f}倍量)"
                        autumn_short_list.append((
                            vol_ratio,
                            f"<b>{title}</b> (收盤: {c_now:.2f} | 最高: {h_now:.2f})\n"
                            f" ├ {s_tag}\n"
                            f" ├ 💡 直播短空紀律：明日若開太高(>5%)不做，挑開盤弱勢、急拉買盤竭盡空(3%停利/1.5%停損)\n"
                            f" └ 📏 天花板價：<b>{ceiling_price:.2f}</b>｜月線(20MA)：<b>{ma20_now:.2f}</b>"
                        ))

                except Exception:
                    continue
        except Exception:
            continue

    for lst in [disposition_list, prodigal_son_list, floor_rebound_list, bollinger_monster, autumn_short_list]:
        lst.sort(key=lambda x: x[0], reverse=True)

    floor_market_alert = (
        f"🚨 <b>全市場跌破地板價高達 {total_below_floor_count} 檔 (大盤見底強彈訊號！)</b>"
        if total_below_floor_count >= 60 else
        f"📊 全市場今日觸及地板價家數：<b>{total_below_floor_count}</b> 檔 (常態水位)"
    )

    blocks = [f"⚡ <b>【小哥 11/11 直播完整版：短線＆處置戰法雷達】</b>\n{market_status}\n{floor_market_alert}\n"]
    if disposition_list:
        blocks.append("🔒 <b>【一、處置股抄底池 (依落後大盤累幅 & 跌10~20%排序)】</b>")
        blocks.extend([x[1] for x in disposition_list[:6]])
    if prodigal_son_list:
        blocks.append("\n🌊 <b>【二、浪子回頭：強勢飆股回測月線 (位階 ≤ 4)】</b>")
        blocks.extend([x[1] for x in prodigal_son_list[:6]])
    if floor_rebound_list:
        blocks.append("\n🔨 <b>【三、地板價殺低出量易彈 ＆ 跌停敲開 (10%/20%分批法)】</b>")
        blocks.extend([x[1] for x in floor_rebound_list[:6]])
    if bollinger_monster:
        blocks.append("\n👹 <b>【四、開布林沿上軌妖股(位階≥15、斜率>3) ＆ 跳空一字線(禁空令)】</b>")
        blocks.extend([x[1] for x in bollinger_monster[:6]])
    if autumn_short_list:
        blocks.append("\n🍂 <b>【五、天花板爆量轉折 ＆ 四季「秋天」反彈月線弱勢股】</b>")
        blocks.extend([x[1] for x in autumn_short_list[:5]])

    chunk_size = 8
    for i in range(0, len(blocks), chunk_size):
        send_telegram_msg("\n\n".join(blocks[i:i + chunk_size]))

if __name__ == "__main__":
    run_xiaoge_strategies()
