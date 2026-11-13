 """
《超級績效》SEPA / VCP 選股雷達 (Claude 原版 VCP 核心 + 進場日 2xATR 固定停損整合版)

對照書中頁碼：
  p.106      趨勢樣板八項準則
  p.99       第二階段：大量上漲日多於大量下跌日
  p.238-241  VCP：逐次收縮 (每次約為前一次的一半)、2~6 次收縮、箱形整理
  p.266-270  樞紐點 = 最後一次收縮的高點；樞紐點量縮；突破要帶大量
  p.270      盤中推估成交量
  p.271      沒有明確樞紐的平坦底部 (修正 10~15% 內) → 穿越底部最高價進場
  p.271-273  蹲下 (squat) / 反轉回升；突破後不該收破 20 日均線
  p.276      盤中回檔時堅持原來的停損
  p.278      觀察清單：星號 = 已出現樞紐點、pb = 等待拉回整理
  p.298      強勁表現型態 (8 週漲 100% → 3~6 週整理 ≤25% → 緊密 ≤10%)

本程式只是篩選工具，不構成投資建議。
"""
import os
import io
import html
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf
import warnings

warnings.filterwarnings("ignore")

# ==========================================
# 1. 設定
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "請換成你的_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "請換成你的_CHAT_ID")

# 📦 持股追蹤："代號": {"cost": 成本, "date": "進場日(美股用美東日期)", "stop": 可選，自訂停損價}
#    未填 "stop" 時，自動依進場日 2xATR (夾在 3%~8%) 固定於成本下方，不隨現價移動 (p.276)。
MY_HOLDINGS = {
    "FMX": {"cost": 133.48, "date": "2026-11-11"},
    "TEVA": {"cost": 44.21, "date": "2026-11-12"},
}

# --- 風險 ---
MAX_STOP_PCT = 8.0          # 單筆最大停損 %（書中第 12、13 章；可依自己的平均獲利調整）
MIN_STOP_PCT = 3.0
CHASE_LIMIT_PCT = 5.0       # 高於樞紐點超過此幅度就不追價 (p.267)

# --- 趨勢樣板 (p.106) ---
RS_MIN = 70
LOW52_MULT = 1.30
HIGH52_WITHIN = 0.25
MA200_UP_DAYS = 22          # 200MA 至少上升 1 個月
DAYS_ABOVE_LONG_MA = 15     # 額外條件(非書中)：近 N 日收盤都在 150/200MA 之上，擋掉春源這類糾纏股；設 1 = 只看今天
MA50_OVER_MA150_PCT = 0.0   # 額外條件(非書中)：50MA 高於 150MA 的最小 %；書中只要求 50 > 150 > 200

# --- 底部 / VCP (p.238-241) ---
BASE_LOOKBACKS = [300, 200, 130, 90, 60, 40]   # 底部最長 60 週
MIN_BASE_DAYS = 15          # 底部至少 3 週
MAX_BASE_DEPTH = 35.0
MIN_BASE_DEPTH = 6.0
FINAL_CONTRACTION_MAX = 10.0
CONTRACTION_RATIO = 0.85    # 每次收縮需 ≤ 前一次 × 此比例 (書中理想值約 0.5，容許誤差)
FINAL_VS_FIRST = 0.60       # 最後一次收縮需 ≤ 第一次 × 此比例
ZZ_ATR_MULT = 1.5           # 波段轉折門檻 = ATR% × 此倍數，夾在 3%~8%
PIVOT_MIN_AGE = 3           # 樞紐高點至少要是 3 根 K 棒以前
FLAT_BASE_MAX_DEPTH = 15.0  # 箱形 / 平坦底部
FLAT_BASE_DAYS = (15, 40)

# --- 量 ---
BREAKOUT_VOL_MULT = 1.5     # 突破量 ≥ 50 日均量倍數 (書中例子為 3~4 倍，可自行調高)
STAR_MAX_DIST = 6.0         # 距樞紐點 % 以內列入星號池
PB_MAX_DIST = 15.0
POST_BREAKOUT_DAYS = 10     # 突破後追蹤 squat / 反轉回升的天數 (p.271)
MIN_VOL_MA50 = 150_000


# ==========================================
# 2. p.270 盤中推估成交量
# ==========================================
def get_intraday_vol_multiplier(last_bar_date, is_tw):
    """開盤 30 分鐘內不推估（開盤量集中，線性外推會嚴重高估）；30~60 分鐘以 60 分鐘計。"""
    try:
        bar_date = pd.Timestamp(last_bar_date).date()
        if is_tw:
            now = datetime.now(ZoneInfo("Asia/Taipei"))
            open_min, session = 9 * 60, 270.0
        else:
            now = datetime.now(ZoneInfo("America/New_York"))
            open_min, session = 9 * 60 + 30, 390.0
        if bar_date == now.date():
            mins = (now.hour * 60 + now.minute) - open_min
            if 30 <= mins < session:
                return session / max(60, mins), True
    except Exception:
        pass
    return 1.0, False


# ==========================================
# 3. 股票池
# ==========================================
def get_entire_market_universe():
    tw_dict, us_dict = {}, {}
    total_raw_count = 0

    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=15)
        if res.status_code == 200:
            d = pd.DataFrame(res.json())
            d = d[d['Code'].str.match(r'^[1-9]\d{3}$')]
            total_raw_count += len(d)
            d['ClosingPrice'] = pd.to_numeric(d['ClosingPrice'].str.replace(',', ''), errors='coerce')
            d['TradeVolume'] = pd.to_numeric(d['TradeVolume'].str.replace(',', ''), errors='coerce')
            for _, r in d[(d['ClosingPrice'] > 0) & (d['TradeVolume'] >= 50000)].iterrows():
                tw_dict[f"{r['Code']}.TW"] = r['Name'].strip()
    except Exception as e:
        print(f"⚠️ 上市清單抓取異常: {e}")

    try:
        res = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", timeout=15)
        if res.status_code == 200:
            d = pd.DataFrame(res.json())
            d = d[d['SecuritiesCompanyCode'].str.match(r'^[1-9]\d{3}$')]
            total_raw_count += len(d)
            d['Close'] = pd.to_numeric(d['Close'].str.replace(',', ''), errors='coerce')
            d['TradingShares'] = pd.to_numeric(d['TradingShares'].str.replace(',', ''), errors='coerce')
            for _, r in d[(d['Close'] > 0) & (d['TradingShares'] >= 50000)].iterrows():
                tw_dict[f"{r['SecuritiesCompanyCode']}.TWO"] = r['CompanyName'].strip()
    except Exception as e:
        print(f"⚠️ 上櫃清單抓取異常: {e}")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Origin': 'https://www.nasdaq.com',
        'Referer': 'https://www.nasdaq.com/',
    }
    try:
        url = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&download=true"
        res = requests.get(url, headers=headers, timeout=20)
        if res.status_code == 200:
            rows = res.json().get('data', {}).get('rows', [])
            total_raw_count += len(rows)
            d = pd.DataFrame(rows)
            d = d[d['symbol'].str.match(r'^[A-Z]{1,5}$', na=False)]
            d['price'] = pd.to_numeric(d['lastsale'].astype(str).str.replace('$', '').str.replace(',', ''), errors='coerce')
            d['vol'] = pd.to_numeric(d['volume'].astype(str).str.replace(',', ''), errors='coerce')
            d['mcap'] = pd.to_numeric(d['marketCap'].astype(str).str.replace(',', ''), errors='coerce')
            d = d[(d['price'] >= 8.0) & (d['mcap'] >= 100_000_000) & (d['vol'] >= 120_000)]
            for _, r in d.sort_values('vol', ascending=False).head(2200).iterrows():
                s = str(r['symbol']).strip()
                us_dict[s] = s
    except Exception as e:
        print(f"⚠️ NASDAQ API 異常: {e}")

    if len(us_dict) < 100:
        try:
            res = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", headers=headers, timeout=15)
            if res.status_code == 200:
                for sym in pd.read_html(io.StringIO(res.text))[0]['Symbol'].tolist():
                    s = str(sym).replace('.', '-').strip()
                    us_dict[s] = s
                total_raw_count += len(us_dict)
        except Exception:
            pass

    for sym in MY_HOLDINGS:
        if sym not in tw_dict and sym not in us_dict:
            us_dict[sym] = sym

    return {**tw_dict, **us_dict}, total_raw_count


# ==========================================
# 4. 基本面 (p.52-55：盈餘、營收、盈餘驚喜)
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
                s = eh.sort_index()['surprisePercent'].dropna()
                if len(s) > 0:
                    v = float(s.iloc[-1])
                    surprise_pct = v * 100.0 if abs(v) < 5 else v
        except Exception:
            pass

        latest_q_eps = None
        try:
            q = tk.quarterly_income_stmt
            if q is not None and not q.empty:
                for row in ['Diluted EPS', 'Basic EPS']:
                    if row in q.index:
                        e = q.loc[row].dropna()
                        if len(e) >= 1:
                            latest_q_eps = float(e.iloc[0])
                        if q_growth is None and len(e) >= 5 and e.iloc[4] != 0:
                            q_growth = (e.iloc[0] - e.iloc[4]) / abs(e.iloc[4])
                        break
        except Exception:
            pass

        parts = []
        if latest_q_eps is not None:
            parts.append(f"季EPS <b>{latest_q_eps:.2f}</b>")
        elif ttm_eps is not None:
            parts.append(f"四季EPS <b>{ttm_eps:.2f}</b>")
        if q_growth is not None:
            parts.append(f"年增 <b>{q_growth * 100:+.0f}%</b>")

        tags = []
        is_strong = False
        if surprise_pct is not None and surprise_pct >= 5.0:
            is_strong = True
            tags.append(f"🔥EPS超預期+{surprise_pct:.0f}%")
        if q_growth is not None and q_growth >= 0.25 and (latest_q_eps is None or latest_q_eps > 0):
            is_strong = True
            tags.append(f"獲利年增+{q_growth * 100:.0f}%")
        if rev_growth is not None and rev_growth >= 0.25:
            is_strong = True
            tags.append(f"營收年增+{rev_growth * 100:.0f}%")

        return ("｜".join(parts) if parts else "財報無資料"), is_strong, " ".join(tags)
    except Exception:
        return "財報無資料", False, ""


# ==========================================
# 5. 📦 持股守護雷達 (p.272 / p.276)
# ==========================================
def holding_stop_price(df, h):
    cost = float(h["cost"])
    if h.get("stop"):
        sp = float(h["stop"])
        return sp, (cost - sp) / cost * 100.0

    # 未手動指定 stop 時，取進場日之 2xATR (夾在 MIN_STOP_PCT ~ MAX_STOP_PCT) 固定於成本下方
    prev_c = df['Close'].shift(1)
    tr = pd.concat([df['High'] - df['Low'], (df['High'] - prev_c).abs(), (df['Low'] - prev_c).abs()], axis=1).max(axis=1)
    atr20 = tr.rolling(20).mean()
    atr_val = float(atr20.iloc[-1])
    if h.get("date"):
        sub = atr20[df.index <= pd.Timestamp(h["date"])].dropna()
        if len(sub) > 0:
            atr_val = float(sub.iloc[-1])

    stop_pct = float(np.clip((2.0 * atr_val / cost) * 100.0, MIN_STOP_PCT, MAX_STOP_PCT))
    return cost * (1.0 - stop_pct / 100.0), stop_pct


def check_my_holdings(raw_data):
    if not MY_HOLDINGS:
        return ""
    lines = ["📦 <b>【持股守護雷達】</b> <i>停損依進場成本與2xATR固定，不隨現價移動</i>"]
    for sym, h in MY_HOLDINGS.items():
        try:
            if not isinstance(h, dict):
                h = {"cost": float(h)}
            cost = float(h["cost"])
            df = raw_data.get(sym)
            if df is None or len(df) < 30:
                df = yf.Ticker(sym).history(period="6mo", auto_adjust=False)
                if df is not None and len(df) and getattr(df.index, "tz", None) is not None:
                    df.index = df.index.tz_localize(None)
            if df is None or len(df) < 25:
                lines.append(f"• <b>{sym}</b>｜⚠️ 抓不到報價，請手動檢查")
                continue

            c_now = float(df['Close'].iloc[-1])
            ma20 = float(df['Close'].rolling(20).mean().iloc[-1])
            stop, stop_pct = holding_stop_price(df, h)
            pnl = (c_now - cost) / cost * 100.0

            touched = False
            if h.get("date"):
                since = df[df.index >= pd.Timestamp(h["date"])]
                if len(since):
                    touched = float(since['Low'].min()) <= stop

            if c_now <= stop:
                advice = f"🚨 <b>收盤跌破停損 {stop:.2f} (-{stop_pct:.1f}%)，依計畫出場</b>"
            elif touched:
                advice = f"🚨 <b>進場後盤中曾觸及停損 {stop:.2f}</b>，若未出場請檢查"
            elif c_now < ma20:
                advice = f"⚠️ <b>收盤跌破 20MA({ma20:.2f})</b>，p.272：勝算明顯降低｜停損 <b>{stop:.2f}</b>"
            else:
                advice = f"✅ 守在 20MA(<b>{ma20:.2f}</b>) 之上｜🛡️停損 <b>{stop:.2f}</b> (距現價 {(c_now - stop) / c_now * 100:.1f}%)"

            icon = "🟢" if pnl >= 0 else "🔻"
            lines.append(f"• <b>{sym}</b>｜現價 <b>{c_now:.2f}</b> (成本 {cost:.2f}｜{icon} <b>{pnl:+.1f}%</b>)\n  └ {advice}")
        except Exception as e:
            lines.append(f"• <b>{sym}</b>｜⚠️ 檢查失敗: {html.escape(str(e))[:60]}")
    return "\n".join(lines) + "\n" + ("─" * 18) + "\n\n"


# ==========================================
# 6. VCP 技術面足跡 (100% 採用 Claude 原版波段轉折演算法)
# ==========================================
def zigzag_pivots(h, l, thr):
    """從底部左側高點 (index 0) 開始，找出交替的波段高/低點。
    回傳 [(index, price, 'H'|'L'), ...]，最後一個是尚未確認的候選轉折。"""
    pivots = [(0, float(h[0]), 'H')]
    direction = 'down'
    ext_i, ext_p = 0, float(l[0])
    for i in range(1, len(h)):
        if direction == 'down':
            if l[i] < ext_p:
                ext_i, ext_p = i, float(l[i])
            elif h[i] >= ext_p * (1 + thr):
                pivots.append((ext_i, ext_p, 'L'))
                direction = 'up'
                ext_i, ext_p = i, float(h[i])
        else:
            if h[i] > ext_p:
                ext_i, ext_p = i, float(h[i])
            elif l[i] <= ext_p * (1 - thr):
                pivots.append((ext_i, ext_p, 'H'))
                direction = 'down'
                ext_i, ext_p = i, float(l[i])
    pivots.append((ext_i, ext_p, 'L' if direction == 'down' else 'H'))
    return pivots


def analyze_base(h, l, c, v, v50, atr_pct, end):
    """分析 [0, end) 這段 K 棒形成的底部 (end 那根不算，方便判斷「end 當天是否突破」)。
    回傳 None = 沒有可辨識的底部 (例如剛創高、還在延伸段)。"""
    if end < 80:
        return None
    last = end - 1

    p0 = None
    for lb in BASE_LOOKBACKS:
        start = max(0, end - lb)
        seg = h[start:end]
        rel = len(seg) - 1 - int(np.argmax(seg[::-1]))      # 最高點 (同價取最近一次)
        cand = start + rel
        days = last - cand
        if days < MIN_BASE_DAYS:
            return None                                     # 高點太近 → 沒有底部，不是買點
        if start > 0 and rel < 3:
            continue                                        # 視窗邊緣的假高點
        depth = (h[cand] - l[cand:end].min()) / h[cand] * 100.0
        if depth > MAX_BASE_DEPTH:
            continue                                        # 太深，改看較近期的底部
        if depth < MIN_BASE_DEPTH:
            return None
        p0 = cand
        break
    if p0 is None:
        return None

    base_high = float(h[p0])
    base_low = float(l[p0:end].min())
    base_depth = (base_high - base_low) / base_high * 100.0
    base_days = last - p0

    thr = float(np.clip(ZZ_ATR_MULT * atr_pct[last], 0.03, 0.08))
    sh, sl = h[p0:end], l[p0:end]
    piv = zigzag_pivots(sh, sl, thr)

    cons = []   # (高點位置, 高點價, 低點價, 幅度%)
    i = 0
    while i < len(piv):
        if piv[i][2] == 'H':
            hi_i, hi_p = piv[i][0], piv[i][1]
            if i + 1 < len(piv):
                lo_p = piv[i + 1][1]
                i += 2
            else:
                # 尾端尚未確認的高點：要停留夠久才算新的緊密區，否則只是上一次收縮的右側反彈
                if cons and (len(sh) - 1 - hi_i) < PIVOT_MIN_AGE:
                    break
                lo_p = float(sl[hi_i:].min())
                i += 1
            cons.append((hi_i, hi_p, lo_p, (hi_p - lo_p) / hi_p * 100.0))
        else:
            i += 1
    if not cons:
        return None

    depths = [x[3] for x in cons]
    n_t = len(depths)
    final = depths[-1]
    pivot_rel, pivot_price = cons[-1][0], cons[-1][1]
    pivot_age = (len(sh) - 1) - pivot_rel

    violations = sum(1 for k in range(1, n_t) if depths[k] > depths[k - 1] * CONTRACTION_RATIO)
    in_upper_half = pivot_price >= base_low + 0.5 * (base_high - base_low)

    is_vcp = (
        2 <= n_t <= 6
        and violations <= (1 if n_t >= 4 else 0)
        and final <= min(depths) + 1e-9
        and final <= min(FINAL_CONTRACTION_MAX, depths[0] * FINAL_VS_FIRST)
        and in_upper_half
        and pivot_age >= PIVOT_MIN_AGE
    )

    hi5 = float(h[end - 5:end].max())
    tight5 = (hi5 - float(l[end - 5:end].min())) / hi5 * 100.0

    # p.241 / p.271 箱形、平坦底部：沒有明顯收縮，修正 10~15% 內 → 以底部最高價為樞紐
    is_flat = (
        (not is_vcp)
        and base_depth <= FLAT_BASE_MAX_DEPTH
        and FLAT_BASE_DAYS[0] <= base_days <= FLAT_BASE_DAYS[1]
        and tight5 <= 8.0
    )
    if is_flat:
        pivot_price = base_high
        t_label = "箱形"
    else:
        t_label = f"{n_t}T"

    # p.269 樞紐點量縮
    vb = float(v50[last]) if v50[last] > 0 else 1.0
    v5 = float(v[end - 5:end].mean())
    v10 = float(v[end - 10:end].mean())
    vmin = float(v[end - 5:end].min())
    dryup = (v5 <= 0.8 * vb) or (v10 <= 0.7 * vb) or (vmin <= 0.5 * vb)

    weeks = max(1, int(round(base_days / 5.0)))
    shown_final = tight5 if is_flat else final
    return {
        "ready": bool(is_vcp or is_flat),
        "is_vcp": bool(is_vcp),
        "is_flat": bool(is_flat),
        "pivot": float(pivot_price),
        "base_high": base_high,
        "base_depth": base_depth,
        "depths": depths,
        "final": float(shown_final),
        "n_t": n_t,
        "weeks": weeks,
        "dryup": bool(dryup),
        "vol_pct": v5 / vb * 100.0,
        "footprint": f"{weeks}W {depths[0]:.0f}/{shown_final:.0f} {t_label}",
        "depth_str": "→".join(f"{d:.0f}" for d in depths),
    }


def detect_power_play(h, l, c, v, n):
    """p.298 強勁表現型態：8 週內帶量漲 ≥100% → 整理 12~30 天、修正 ≤25% → 緊密 ≤10%。"""
    if n < 120:
        return None
    end = n - 1                                    # 不含今天
    w = h[end - 32:end]
    rel = len(w) - 1 - int(np.argmax(w[::-1]))
    peak = end - 32 + rel
    days = (end - 1) - peak
    if days < 10 or days > 30:
        return None
    run_lo_i = peak - 40 + int(np.argmin(l[peak - 40:peak + 1]))
    run_lo = float(l[run_lo_i])
    if run_lo <= 0 or h[peak] / run_lo < 2.0:
        return None
    prior = v[max(0, run_lo_i - 50):run_lo_i]
    if len(prior) < 20 or v[run_lo_i:peak + 1].mean() < 1.5 * prior.mean():
        return None
    corr = (h[peak] - l[peak:end].min()) / h[peak] * 100.0
    if corr > 25.0:
        return None
    k = min(10, days)
    hi = float(h[end - k:end].max())
    tight = (hi - float(l[end - k:end].min())) / hi * 100.0
    if tight > 10.0:
        return None
    return {"pivot": hi, "gain": (h[peak] / run_lo - 1) * 100.0, "corr": corr, "tight": tight, "days": days}


# ==========================================
# 7. Telegram
# ==========================================
def send_telegram_msg(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=15)
        if r.status_code != 200:
            print(f"❌ Telegram 回應 {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"❌ 網路連線錯誤：{e}")


def send_reports(holdings_msg, market_msg, sections, stats):
    blocks = []
    for title, items in sections:
        if items:
            blocks.append(title)
            blocks.extend(items)

    head = (
        f"📊 <b>【《超級績效》SEPA 選股日報】</b>\n"
        f"<i>母體 {stats['universe']}｜掃描 {stats['scanned']}｜過趨勢樣板 {stats['template']}</i>\n"
        f"{market_msg}\n"
    )
    if not blocks:
        send_telegram_msg(holdings_msg + head + "\n今天沒有符合條件的標的。")
        return

    pages, cur = [], ""
    for b in blocks:
        if len(cur) + len(b) + 2 > 3300:
            pages.append(cur)
            cur = ""
        cur += b + "\n\n"
    if cur:
        pages.append(cur)
    for i, p in enumerate(pages, 1):
        prefix = (holdings_msg + head + "\n") if i == 1 else f"📊 <b>續 ({i}/{len(pages)})</b>\n\n"
        send_telegram_msg(prefix + p)


# ==========================================
# 8. 大盤狀態 (p.202、p.299：基本面、技術面、整體市況要同時配合)
# ==========================================
def get_market_status():
    out = []
    for sym, name in [("^TWII", "台股"), ("^GSPC", "S&P500"), ("^IXIC", "NASDAQ")]:
        try:
            d = yf.Ticker(sym).history(period="1y", auto_adjust=False)
            c = d['Close']
            c_now, m50, m200 = float(c.iloc[-1]), float(c.rolling(50).mean().iloc[-1]), float(c.rolling(200).mean().iloc[-1])
            if c_now > m50 > m200:
                s = "🟢多頭"
            elif c_now < m50 and c_now < m200:
                s = "🔴空頭"
            else:
                s = "🟡整理"
            out.append(f"{name}{s}")
        except Exception:
            continue
    return ("大盤：" + "｜".join(out)) if out else ""


# ==========================================
# 9. 主引擎
# ==========================================
def _extract(data, sym, single):
    if single and not isinstance(data.columns, pd.MultiIndex):
        return data.copy()
    return data[sym].copy()


def analyze_market_minervini(stock_dict):
    symbols = list(stock_dict.keys())
    raw_data, tw_rs, us_rs = {}, {}, {}
    start_date = (pd.Timestamp.now() - pd.DateOffset(months=27)).strftime('%Y-%m-%d')
    batch_size = 180

    print(f"📥 批次下載 {len(symbols)} 檔未還原 K 線...")
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        try:
            data = yf.download(batch, start=start_date, auto_adjust=False, group_by="ticker", threads=True, progress=False)
            for sym in batch:
                try:
                    df = _extract(data, sym, len(batch) == 1)
                    df = df[['Open', 'High', 'Low', 'Close', 'Volume']].dropna()
                    df = df[df['Volume'] > 0]
                    if len(df) < 252:
                        continue
                    raw_data[sym] = df
                    c = df['Close']
                    score = (0.4 * (c.iloc[-1] / c.iloc[-63] - 1) + 0.2 * (c.iloc[-1] / c.iloc[-126] - 1)
                             + 0.2 * (c.iloc[-1] / c.iloc[-189] - 1) + 0.2 * (c.iloc[-1] / c.iloc[-250] - 1))
                    (tw_rs if sym.endswith((".TW", ".TWO")) else us_rs)[sym] = score
                except Exception:
                    continue
        except Exception as e:
            print(f"批次下載略過: {e}")

    rs_map = {}
    for scores in (tw_rs, us_rs):
        if scores:
            rs_map.update((pd.Series(scores).rank(pct=True) * 99).round(0).astype(int).to_dict())

    cats = {k: {"tw": [], "us": []} for k in ("brk", "recent", "star", "squat", "pb", "power")}
    momentum = []
    template_passed = 0

    for symbol, df in raw_data.items():
        try:
            n = len(df)
            o, h, l, c, v = (df[x].values.astype(float) for x in ['Open', 'High', 'Low', 'Close', 'Volume'])
            close = df['Close']
            ma20 = close.rolling(20).mean().values
            ma50 = close.rolling(50).mean().values
            ma150 = close.rolling(150).mean().values
            ma200 = close.rolling(200).mean().values
            v50 = df['Volume'].rolling(50).mean().values
            if not v50[-1] >= MIN_VOL_MA50:
                continue

            prev_c = close.shift(1)
            tr = pd.concat([df['High'] - df['Low'], (df['High'] - prev_c).abs(), (df['Low'] - prev_c).abs()], axis=1).max(axis=1)
            atr_pct = (tr.rolling(20).mean() / close).values

            c_now, c_prev, o_now, h_now, l_now = c[-1], c[-2], o[-1], h[-1], l[-1]
            rs = rs_map.get(symbol, 50)
            is_tw = symbol.endswith((".TW", ".TWO"))
            mkt = "tw" if is_tw else "us"
            name = stock_dict.get(symbol, symbol)
            flag = "🇹🇼" if is_tw else "🇺🇸"
            title = f"{flag} {symbol} {html.escape(name)}" if name != symbol else f"{flag} {symbol}"

            mult, is_intraday = get_intraday_vol_multiplier(df.index[-1], is_tw)
            vol_ratio = (v[-1] / v50[-2] if v50[-2] > 0 else 1.0) * mult
            vol_label = "預估量" if is_intraday else "量"
            is_red = (c_now >= o_now) and (c_now > c_prev) and ((c_now - l_now) >= (h_now - l_now) * 0.5 if h_now > l_now else True)

            stop_pct = float(np.clip(2 * atr_pct[-1] * 100.0, MIN_STOP_PCT, MAX_STOP_PCT))

            # ---------- p.298 強勁表現型態 ----------
            pp = detect_power_play(h, l, c, v, n)
            if pp and c_now > ma50[-1] and c_now <= pp["pivot"] * (1 + CHASE_LIMIT_PCT / 100):
                state = "🚀突破" if (c_now > pp["pivot"] and vol_ratio >= BREAKOUT_VOL_MULT) else "等突破"
                cats["power"][mkt].append((rs + pp["gain"] / 10, symbol, (
                    f"<b>{title}</b>｜現價 <b>{c_now:.2f}</b>｜RS <b>{rs}</b>\n"
                    f"  ⚡ <b>{state}｜樞紐 {pp['pivot']:.2f}</b>｜🛡️停損 <b>{pp['pivot'] * (1 - stop_pct / 100):.2f}</b> (-{stop_pct:.1f}%)\n"
                    f"  8週漲 {pp['gain']:.0f}%｜整理 {pp['days']}天 修正 {pp['corr']:.0f}%｜緊密 {pp['tight']:.0f}%"
                )))

            # ---------- 第一層：p.106 趨勢樣板八項 ----------
            hi52, lo52 = float(h[-252:].max()), float(l[-252:].min())
            k = max(1, DAYS_ABOVE_LONG_MA)
            template = (
                bool((c[-k:] > ma150[-k:]).all() and (c[-k:] > ma200[-k:]).all())   # 1. 股價 > 150MA、200MA
                and ma150[-1] > ma200[-1]                                            # 2. 150MA > 200MA
                and ma200[-1] > ma200[-1 - MA200_UP_DAYS]                            # 3. 200MA 上升至少 1 個月
                and ma50[-1] > ma150[-1] * (1 + MA50_OVER_MA150_PCT / 100)           # 4. 50MA > 150MA (> 200MA)
                and c_now > ma50[-1]                                                 # 5. 股價 > 50MA
                and c_now >= lo52 * LOW52_MULT                                       # 6. 高於 52 週低點 30%
                and c_now >= hi52 * (1 - HIGH52_WITHIN)                              # 7. 距 52 週高點 25% 內
                and rs >= RS_MIN                                                     # 8. RS ≥ 70
            )
            if not template:
                continue
            template_passed += 1

            ma200_up_4m = n > 290 and ma200[-1] > ma200[-44] > ma200[-88]

            # ---------- 第二層：p.99 大量上漲日 vs 大量下跌日 ----------
            diff40 = np.diff(c[-41:])
            big40 = v[-40:] > v50[-40:]
            up_days = int((big40 & (diff40 > 0)).sum())
            dn_days = int((big40 & (diff40 < 0)).sum())

            if is_red and vol_ratio >= BREAKOUT_VOL_MULT and c_now >= h[-20:-1].max():
                momentum.append((rs + int(vol_ratio * 10), symbol, title, c_now, rs, vol_ratio, vol_label, stop_pct))

            # 出貨跡象：近 60 日最大量那天是大跌 / 今天帶量下跌 / 大量下跌日明顯較多
            imax = n - 60 + int(np.argmax(v[-60:]))
            if c[imax] < c[imax - 1] * 0.98:
                continue
            if c_now < c_prev * 0.985 and vol_ratio >= 1.35:
                continue
            if dn_days > up_days + 1:
                continue

            score = rs + (15 if rs >= 85 else 0) + (10 if ma200_up_4m else 0) + (15 if c_now >= lo52 * 2 else 0)
            score += max(0, up_days - dn_days) * 4
            chip = f"大量漲跌日 <b>{up_days}:{dn_days}</b>"

            def card(action, fp, pivot, extra_score=0, dry=False):
                stop = pivot * (1 - stop_pct / 100)
                dry_tag = "💧量縮｜" if dry else ""
                text = (
                    f"<b>{title}</b>｜現價 <b>{c_now:.2f}</b>｜RS <b>{rs}</b>\n"
                    f"  {action}｜🛡️停損 <b>{stop:.2f}</b> (-{stop_pct:.1f}%)\n"
                    f"  📐<code>{fp['footprint']}</code> ({fp['depth_str']})｜{dry_tag}{chip}"
                )
                s = score + extra_score + (20 if dry else 0) + int(max(0, 10 - fp["final"]) * 6)
                return (s, symbol, text)

            # ---------- 第三層：截至昨天的底部，今天是否突破 ----------
            fp = analyze_base(h, l, c, v, v50, atr_pct, n - 1)
            placed = False
            if fp and fp["ready"]:
                pivot = fp["pivot"]
                dist = (c_now / pivot - 1) * 100.0
                if c_now > pivot and c_prev <= pivot:
                    if dist <= CHASE_LIMIT_PCT and is_red and vol_ratio >= BREAKOUT_VOL_MULT and fp["dryup"]:
                        cats["brk"][mkt].append(card(
                            f"🚀 <b>突破樞紐 {pivot:.2f}</b> ({vol_label}{vol_ratio:.1f}倍)｜追價上限 {pivot * (1 + CHASE_LIMIT_PCT / 100):.2f}",
                            fp, pivot, 30, True))
                    elif dist <= CHASE_LIMIT_PCT:
                        why = "量不足" if vol_ratio < BREAKOUT_VOL_MULT else ("突破前未量縮" if not fp["dryup"] else "收盤偏低")
                        cats["star"][mkt].append(card(
                            f"⚠️ <b>越過樞紐 {pivot:.2f} 但{why}</b> ({vol_label}{vol_ratio:.1f}倍)", fp, pivot, 5, fp["dryup"]))
                    placed = True
                elif c_now <= pivot and dist >= -STAR_MAX_DIST and fp["dryup"] and c_now >= ma20[-1] * 0.99:
                    mark = "★◎" if fp["final"] <= 5.0 else "★"
                    cats["star"][mkt].append(card(
                        f"🎯 <b>{mark} 樞紐 {pivot:.2f}</b> (差 {abs(dist):.1f}%)｜突破需量 ≥{BREAKOUT_VOL_MULT}倍", fp, pivot, 20, True))
                    placed = True

            # ---------- 第四層：近 10 日是否突破過 → squat / 反轉回升 (樞紐點固定在突破當時) ----------
            if not placed:
                for kk in range(POST_BREAKOUT_DAYS, 0, -1):
                    b = n - 1 - kk
                    fpb = analyze_base(h, l, c, v, v50, atr_pct, b)
                    if not (fpb and fpb["ready"] and fpb["dryup"]):
                        continue
                    pv = fpb["pivot"]
                    if not (c[b] > pv and c[b - 1] <= pv and v50[b - 1] > 0 and v[b] >= BREAKOUT_VOL_MULT * v50[b - 1]):
                        continue
                    dist = (c_now / pv - 1) * 100.0
                    dipped = bool((c[b + 1:n - 1] < pv).any()) if kk > 1 else False
                    stop_line = pv * (1 - stop_pct / 100)
                    if c_now > pv * (1 + CHASE_LIMIT_PCT / 100):
                        placed = True                                   # 已延伸，不追價
                    elif c_now >= pv:
                        if dipped and c_now > c_prev:
                            cats["squat"][mkt].append(card(
                                f"🔄 <b>反轉回升：重新站上樞紐 {pv:.2f}</b> ({kk}天前突破)", fpb, pv, 15, True))
                        else:
                            cats["recent"][mkt].append(card(
                                f"✅ <b>{kk}天前突破 {pv:.2f}</b>，現高於樞紐 {dist:.1f}% (仍在買進區)", fpb, pv, 10, True))
                        placed = True
                    elif c_now > stop_line and c_now >= ma20[-1]:
                        cats["squat"][mkt].append(card(
                            f"🔄 <b>蹲下 squat：跌回樞紐 {pv:.2f} 下方</b> ({kk}天前突破)｜守 20MA {ma20[-1]:.2f}", fpb, pv, 5, True))
                        placed = True
                    else:
                        placed = True                                   # 突破失敗 (破 20MA 或停損)
                    break

            # ---------- 第五層：pb 觀察池 (有底部但樞紐/量縮尚未到位) ----------
            if not placed and fp:
                ref = fp["pivot"] if fp["ready"] else fp["base_high"]
                dist = (c_now / ref - 1) * 100.0
                if -PB_MAX_DIST <= dist <= 0:
                    need = []
                    if not fp["ready"]:
                        need.append("收縮未完成")
                    if not fp["dryup"]:
                        need.append("量未縮")
                    cats["pb"][mkt].append(card(
                        f"📌 <b>pb｜參考高點 {ref:.2f}</b> (差 {abs(dist):.1f}%)｜{'、'.join(need) or '等靠近樞紐'}", fp, ref, 0, fp["dryup"]))
        except Exception:
            continue

    for d in cats.values():
        for lst in d.values():
            lst.sort(key=lambda x: x[0], reverse=True)
    momentum.sort(key=lambda x: x[0], reverse=True)

    limits = {"brk": (5, 5), "recent": (4, 4), "star": (6, 5), "squat": (4, 4), "pb": (5, 4), "power": (3, 3)}
    listed = set()

    def render(key):
        picked = cats[key]["tw"][:limits[key][0]] + cats[key]["us"][:limits[key][1]]
        out = []
        for _, sym, text in picked:
            listed.add(sym)
            eps, strong, tag = get_eps_and_surprise_info(sym)
            badge = f" <b>[{tag}]</b>" if strong else ""
            out.append(f"{text}\n  💰 {eps}{badge}")
        return out

    print("💰 產生日報...")
    sections = [
        ("🚀 <b>【一、今日帶量突破樞紐】</b>", render("brk")),
        ("✅ <b>【二、近日已突破、仍在買進區】</b>", render("recent")),
        ("★ <b>【三、樞紐成型＋量縮（設觸價等突破）】</b>", render("star")),
        ("🔄 <b>【四、Squat 蹲下／反轉回升】</b>", render("squat")),
        ("⚡ <b>【五、強勁表現型態 (p.298)】</b>", render("power")),
        ("📌 <b>【六、pb 觀察池】</b>", render("pb")),
    ]

    beat = []
    for _, sym, title, c_now, rs, vr, vl, sp in momentum[:25]:
        if sym in listed:
            continue
        eps, strong, tag = get_eps_and_surprise_info(sym)
        if strong:
            beat.append(
                f"<b>{title}</b>｜現價 <b>{c_now:.2f}</b>｜RS <b>{rs}</b> <b>[{tag}]</b>\n"
                f"  💥 帶量創 20 日高 ({vl}{vr:.1f}倍)｜尚無樞紐，僅列觀察\n  💰 {eps}"
            )
        if len(beat) >= 5:
            break
    sections.append(("💥 <b>【七、基本面強＋帶量創高（觀察，非買點）】</b>", beat))

    holdings_msg = check_my_holdings(raw_data)
    stats = {"scanned": len(raw_data), "template": template_passed}
    return holdings_msg, sections, stats


if __name__ == "__main__":
    stock_dict, total_universe = get_entire_market_universe()
    holdings_msg, sections, stats = analyze_market_minervini(stock_dict)
    stats["universe"] = total_universe
    send_reports(holdings_msg, get_market_status(), sections, stats)
