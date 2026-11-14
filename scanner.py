"""
《超級績效》SEPA / VCP 選股雷達  v3 —— 依全書第 3~13 章校正，台股／美股分流

書中規則對照（頁碼為中譯本）：
  第 3 章  p.54   評等程序：先過趨勢樣板 → 再用盈餘/營收/利潤率/相對強度過濾 → 人工檢視
  第 4 章  p.78   本益比極低是警訊；不因本益比高而排除
  第 5 章  p.106  趨勢樣板八項；p.99-100 大量上漲日 vs 大量下跌日、第三階段警訊；p.108-110 底部計數
  第 6 章  p.140  由下而上找領導產業群（同產業創新高/過樣板家數）
  第 7 章  p.161  最近一季 EPS 年增至少 20~25%；p.165 盈餘加速；p.152-157 盈餘意外與估計修正；p.172 減速警訊
  第 8 章  p.189  存貨/應收帳款成長快於營收為警訊；p.194 密碼三三
  第 9 章  p.201  新高家數 vs 新低家數；p.213 相對強度領先；p.226 修正超過 50% 太過份
  第 10 章 p.238-241 VCP；p.250-253 修正幅度與大盤比較、時間壓縮；p.254 洗盤；p.266-273 樞紐/量縮/突破/蹲下；
           p.279-283 網球行為；p.286-290 3C 杯狀完成騙局；p.295 失敗重設；p.298 強勁表現型態
  第 11 章 p.303-311 新上市股的初選底部
  第 12-13 章 停損不超過 10%、平均 6~7%；漲幅達停損 3 倍時上移到損益兩平；困難環境縮到 5~6%；部位 4~6 檔

標示「自訂」者不是書中數字，而是為了量化所加的近似條件，可在下方設定區調整。
本程式只是篩選工具，不構成投資建議。
"""
import os
import io
import json
import time
import html
from collections import defaultdict
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

# 📦 持股："代號": {"cost": 成本, "date": "進場日(美股用美東日期)", "stop": 可選，自訂停損價}
#    未填 "stop" 時，依進場日 2xATR (夾在 3%~8%) 固定於成本下方，不隨現價移動 (p.276)。
MY_HOLDINGS = {
    "FMX": {"cost": 133.48, "date": "2026-11-11"},
    "TEVA": {"cost": 44.21, "date": "2026-11-12"},
}

# --- 風險 (第 12、13 章) ---
MAX_STOP_PCT = 8.0            # 建議停損上限（書：平均 6~7%）
MIN_STOP_PCT = 3.0            # 自訂
ABS_MAX_STOP_PCT = 10.0       # p.323、p.351：任何情況不超過 10%
TOUGH_MARKET_STOP_PCT = 6.0   # p.364：困難環境停損縮到 5~6%
BREAKEVEN_R_MULT = 3.0        # p.360-361：漲幅達起始停損 3 倍 → 停損至少上移到損益兩平
RISK_PER_TRADE_PCT = 1.0      # 自訂：單筆願意虧掉的總資金 %（書中例子約 0.5~1.25%）
MAX_POSITION_PCT = 25.0       # p.365：一般 4~6 檔，四檔等量即每檔 25%
CHASE_LIMIT_PCT = 5.0         # p.267：不追價

# --- 市場別設定（台股／美股分開）---
MARKET_CFG = {
    "tw": {
        "name": "台股", "index": "^TWII",
        "min_turnover": 30_000_000,   # 自訂：50 日均成交值下限 (新台幣)
        "limit_pct": 10.0,            # 漲跌停
        "gap_risk_pct": 10.0,         # 自訂：停損可能因跌停鎖死出不掉 → 部位以「至少虧一根跌停」估算
        "cost_buy_pct": 0.1425,       # 券商手續費（未計折扣）
        "cost_sell_pct": 0.1425 + 0.3,  # 手續費 + 證交稅 0.3%（ETF 為 0.1%，本程式不掃 ETF）
    },
    "us": {
        "name": "美股", "index": "^GSPC",
        "min_turnover": 3_000_000,    # 自訂：50 日均成交值下限 (美元)
        "limit_pct": None,
        "gap_risk_pct": 0.0,
        "cost_buy_pct": 0.0,
        "cost_sell_pct": 0.0,
    },
}
MIN_VOL_MA50 = 150_000
US_MAX_SYMBOLS = 2200         # 美股依成交量取前 N 檔
US_CACHE_FILE = os.environ.get("US_UNIVERSE_CACHE", "us_universe_cache.json")
HISTORY_MONTHS = 36           # 底部計數需要較長歷史
HEAL_MIN_GAP_PCT = 1.5        # 自訂：除權息缺口大於此幅度才在型態分析中接合（小額配息不動）

# --- 趨勢樣板 (p.106) ---
RS_MIN = 70
LOW52_MULT = 1.30
HIGH52_WITHIN = 0.25
MA200_UP_DAYS = 22
DAYS_ABOVE_LONG_MA = 15       # 自訂加嚴：近 N 日收盤都在 150/200MA 之上（擋掉年線糾纏股）；設 1 = 只看今天
MA50_OVER_MA150_PCT = 0.0     # 自訂：書中只要求 50 > 150 > 200

# --- 底部 / VCP (p.238-253) ---
BASE_LOOKBACKS = [325, 200, 130, 90, 60, 40]   # 3~65 週
MIN_BASE_DAYS = 15
MAX_BASE_DEPTH = 35.0         # p.253：最好 10~35%
MAX_BASE_DEPTH_BEAR = 50.0    # p.226、p.253：大盤重挫時可到 50%，再深就太過份
MIN_BASE_DEPTH = 5.0          # p.277 手寫清單有 5W 5/2、6W 5/2
SHORT_BASE_DAYS = 25          # 5 週以內的短底部
SHORT_BASE_MAX_DEPTH = 25.0   # p.311：期間很短時修正不應超過 25%
REL_DEPTH_WARN = 3.0          # p.252：修正幅度達大盤 2~3 倍以上 → 偏弱
FINAL_CONTRACTION_MAX = 10.0
CONTRACTION_RATIO = 0.85      # 自訂：每次收縮 ≤ 前一次 × 此值（書中理想約 0.5）
FINAL_VS_FIRST = 0.60         # 自訂
ZZ_ATR_MULT = 1.5             # 自訂：波段轉折門檻 = ATR% × 此值，夾在 2.5%~8%
PIVOT_MIN_AGE = 3
FLAT_BASE_MAX_DEPTH = 15.0    # p.241、p.271
FLAT_BASE_DAYS = (15, 40)
BREAKOUT_VOL_MULT = 1.5       # 自訂下限（書中例子為 3~4 倍以上）
STAR_MAX_DIST = 6.0
PB_MAX_DIST = 15.0
POST_BREAKOUT_DAYS = 10       # p.271：蹲下後有的 10 天或更久才回升

# --- 3C 杯狀完成騙局 (p.286-290) ---
CHEAT_MIN_DEPTH = 15.0
CHEAT_MAX_DEPTH = 50.0
CHEAT_PAUSE_MAX = 10.0        # 停頓區高低幅度 5~10%
CHEAT_RECOV = (0.25, 0.70)    # 收復跌幅 1/3~1/2（上 1/3 屬一般柄部，交給 VCP）

# --- 新上市初選底部 (第 11 章) ---
PRIMARY_MIN_BARS = 45

# --- 基本面 (第 7、8 章) ---
EPS_MIN_GROWTH = 20.0         # p.161：最低 20~25%
REQUIRE_FUNDAMENTALS = False  # True = 未達 EPS 最低標者直接剔除（yfinance 台股財報常缺，預設只標示與降分）
FUND_OVERSAMPLE = 1.5         # 每類多抓一些候選，用基本面重排後再取前 N
USE_TW_OPEN_DATA = True       # 台股月營收年增與產業別（證交所／櫃買 OpenAPI）

TW_INDUSTRY = {
    "01": "水泥", "02": "食品", "03": "塑膠", "04": "紡織纖維", "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷",
    "09": "造紙", "10": "鋼鐵", "11": "橡膠", "12": "汽車", "14": "建材營造", "15": "航運", "16": "觀光餐旅",
    "17": "金融保險", "18": "貿易百貨", "19": "綜合", "20": "其他", "21": "化學", "22": "生技醫療", "23": "油電燃氣",
    "24": "半導體", "25": "電腦及週邊", "26": "光電", "27": "通信網路", "28": "電子零組件", "29": "電子通路",
    "30": "資訊服務", "31": "其他電子", "32": "文化創意", "33": "農業科技", "34": "電子商務", "35": "綠能環保",
    "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
}


def mkt_of(sym):
    return "tw" if sym.endswith((".TW", ".TWO")) else "us"


# ==========================================
# 2. p.270 盤中推估成交量
# ==========================================
def get_intraday_vol_multiplier(last_bar_date, is_tw):
    """開盤 30 分鐘內不推估；30~60 分鐘以 60 分鐘計。"""
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
# 3. 股票池（美股清單加上重試、分交易所、快取、S&P 1500 備援）
# ==========================================
NASDAQ_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Origin': 'https://www.nasdaq.com',
    'Referer': 'https://www.nasdaq.com/',
}


def _http_json(url, headers=None, timeout=20, tries=3, pause=4.0, label=""):
    err = ""
    for k in range(tries):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            err = f"HTTP {r.status_code}"
        except Exception as e:
            err = str(e)[:100]
        if k < tries - 1:
            time.sleep(pause * (k + 1))
    print(f"⚠️ {label or url} 抓取失敗：{err}")
    return None


def _num(x):
    try:
        v = float(str(x).replace(',', '').replace('%', '').replace('$', '').strip())
        return v if np.isfinite(v) else None
    except Exception:
        return None


def _pick(rec, *subs):
    for s in subs:
        for k in rec:
            if s in str(k):
                return rec[k]
    return None


def fetch_tw_universe():
    tw, raw = {}, 0
    data = _http_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=20, label="上市清單")
    if isinstance(data, list) and data:
        try:
            d = pd.DataFrame(data)
            d = d[d['Code'].astype(str).str.match(r'^[1-9]\d{3}$')]
            raw += len(d)
            d['px'] = d['ClosingPrice'].map(_num)
            d['vol'] = d['TradeVolume'].map(_num)
            for _, r in d[(d['px'] > 0) & (d['vol'] >= 50000)].iterrows():
                tw[f"{r['Code']}.TW"] = str(r['Name']).strip()
        except Exception as e:
            print(f"⚠️ 上市清單解析異常: {e}")
    data = _http_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", timeout=20, label="上櫃清單")
    if isinstance(data, list) and data:
        try:
            d = pd.DataFrame(data)
            d = d[d['SecuritiesCompanyCode'].astype(str).str.match(r'^[1-9]\d{3}$')]
            raw += len(d)
            d['px'] = d['Close'].map(_num)
            d['vol'] = d['TradingShares'].map(_num)
            for _, r in d[(d['px'] > 0) & (d['vol'] >= 50000)].iterrows():
                tw[f"{r['SecuritiesCompanyCode']}.TWO"] = str(r['CompanyName']).strip()
        except Exception as e:
            print(f"⚠️ 上櫃清單解析異常: {e}")
    return tw, raw


def fetch_tw_open_data():
    """台股月營收年增率與產業別：上市、上櫃各一個請求。欄位以關鍵字比對，辨識不到就略過並印出實際欄位。"""
    out = {}
    if not USE_TW_OPEN_DATA:
        return out
    urls = (("https://openapi.twse.com.tw/v1/opendata/t187ap05_L", ".TW"),
            ("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O", ".TWO"))
    for url, suffix in urls:
        data = _http_json(url, timeout=25, tries=2, label=f"台股月營收{suffix}")
        if not isinstance(data, list) or not data:
            continue
        ok = 0
        for rec in data:
            try:
                code = str(_pick(rec, "公司代號", "CompanyCode") or "").strip()
                if not code:
                    continue
                ind = _pick(rec, "產業別", "Industry")
                ind = str(ind).strip() if ind not in (None, "") else None
                if ind and ind.isdigit():
                    ind = TW_INDUSTRY.get(ind.zfill(2), ind)
                if ind:
                    for suf in ("工業", "業"):
                        if ind.endswith(suf) and len(ind) > len(suf) + 1:
                            ind = ind[:-len(suf)]
                            break
                yoy = _num(_pick(rec, "去年同月增減"))
                out[code + suffix] = {
                    "yoy": yoy,
                    "cum": _num(_pick(rec, "前期比較增減")),
                    "industry": ind,
                    "ym": str(_pick(rec, "資料年月") or ""),
                }
                if yoy is not None or ind:
                    ok += 1
            except Exception:
                continue
        if ok == 0:
            print(f"⚠️ 台股月營收{suffix} 欄位無法辨識，實際欄位：{list(data[0].keys())[:20]}")
    return out


def _us_rows_to_universe(rows):
    d = pd.DataFrame(rows)
    d = d[d['symbol'].astype(str).str.match(r'^[A-Z]{1,5}$', na=False)]
    d['price'] = d['lastsale'].map(_num)
    d['vol'] = d['volume'].map(_num)
    d['mcap'] = d['marketCap'].map(_num)
    d = d[(d['price'] >= 8.0) & (d['mcap'] >= 100_000_000) & (d['vol'] >= 120_000)]
    us, meta = {}, {}
    for _, r in d.sort_values('vol', ascending=False).drop_duplicates('symbol').head(US_MAX_SYMBOLS).iterrows():
        s = str(r['symbol']).strip()
        us[s] = s
        ind = str(r.get('industry') or '').strip() or None
        meta[s] = {"industry": ind, "sector": str(r.get('sector') or '').strip() or None,
                   "ipo_year": _num(r.get('ipoyear'))}
    return us, meta


def fetch_us_universe():
    """回傳 (us_dict, meta, 原始筆數, 來源說明)。依序嘗試：NASDAQ API → 分交易所 → 本地快取 → S&P 1500。"""
    base = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&download=true"
    rows = []
    data = _http_json(base, headers=NASDAQ_HEADERS, timeout=30, tries=3, pause=5, label="NASDAQ 股票清單")
    if isinstance(data, dict):
        rows = (data.get('data') or {}).get('rows') or []
    if len(rows) < 1000:
        rows = []
        for ex in ("nasdaq", "nyse", "amex"):
            d = _http_json(f"{base}&exchange={ex}", headers=NASDAQ_HEADERS, timeout=30, tries=2, pause=5,
                           label=f"NASDAQ 股票清單({ex})")
            if isinstance(d, dict):
                rows += (d.get('data') or {}).get('rows') or []
    if len(rows) >= 1000:
        try:
            us, meta = _us_rows_to_universe(rows)
            if len(us) >= 300:
                try:
                    with open(US_CACHE_FILE, "w", encoding="utf-8") as f:
                        json.dump({"date": datetime.now().strftime("%Y-%m-%d"), "meta": meta}, f)
                except Exception:
                    pass
                return us, meta, len(rows), "NASDAQ 全市場"
        except Exception as e:
            print(f"⚠️ NASDAQ 清單解析異常: {e}")

    try:
        with open(US_CACHE_FILE, "r", encoding="utf-8") as f:
            cached = json.load(f)
        meta = cached.get("meta") or {}
        if len(meta) >= 300:
            return {s: s for s in meta}, meta, len(meta), f"⚠️快取清單({cached.get('date', '?')})"
    except Exception:
        pass

    us, meta = {}, {}
    pages = ("List_of_S%26P_500_companies", "List_of_S%26P_400_companies", "List_of_S%26P_600_companies")
    got = []
    for page in pages:
        try:
            res = requests.get(f"https://en.wikipedia.org/wiki/{page}", headers=NASDAQ_HEADERS, timeout=20)
            if res.status_code != 200:
                continue
            t = pd.read_html(io.StringIO(res.text))[0]
            cols = {str(c).lower(): c for c in t.columns}
            scol = cols.get("symbol") or cols.get("ticker symbol") or cols.get("ticker")
            if scol is None:
                continue
            icol = next((c for c in t.columns if "sub-industry" in str(c).lower()), None)
            gcol = next((c for c in t.columns if "sector" in str(c).lower()), None)
            for _, r in t.iterrows():
                s = str(r[scol]).replace('.', '-').strip()
                us[s] = s
                meta[s] = {"industry": (str(r[icol]).strip() if icol is not None else None),
                           "sector": (str(r[gcol]).strip() if gcol is not None else None), "ipo_year": None}
            got.append(page[-13:-10])
        except Exception:
            continue
    return us, meta, len(us), f"⚠️備援 S&P {'+'.join(got) or '無'}（NASDAQ 清單失敗）"


def get_entire_market_universe():
    tw, tw_raw = fetch_tw_universe()
    us, meta, us_raw, us_source = fetch_us_universe()
    for sym in MY_HOLDINGS:
        if sym not in tw and sym not in us:
            if mkt_of(sym) == "tw":
                tw[sym] = sym
            else:
                us[sym] = sym
    info = {"tw_n": len(tw), "us_n": len(us), "us_source": us_source, "raw": tw_raw + us_raw}
    return {**tw, **us}, meta, info


# ==========================================
# 4. 資料清理：除權息／公司行動造成的缺口（台美處理方式不同，是最容易出錯的地方）
# ==========================================
def heal_series(df, is_tw):
    """yfinance auto_adjust=False 的 Close 已還原分割、但「沒有」還原配息。
    台股配息集中且金額大（常見 3~8%），加上配股/減資 Yahoo 不一定有還原，
    會在未還原 K 線上留下假缺口，被誤判成收縮、洗盤或大量下跌日。
    做法：型態分析用「連續化」價格（以最新價為基準往回還原），均線與 52 週高低仍用未還原價，與看盤軟體一致。
    回傳 (o, h, l, c, events, warn)；events = [(K棒位置, 缺口%)]。"""
    o, h, l, c = (df[x].values.astype(float) for x in ['Open', 'High', 'Low', 'Close'])
    n = len(c)
    f = np.ones(n)
    events, warn = [], False
    if 'Adj Close' in df.columns:
        adj = df['Adj Close'].values.astype(float)
        with np.errstate(divide='ignore', invalid='ignore'):
            ratio = adj / c
        if np.isfinite(ratio).all() and (ratio > 0).all():
            step = ratio[1:] / ratio[:-1]
            if ((step < 0.97) | (step > 2.0)).any():
                warn = True                     # 還原係數異常 → 不採用
            else:
                # 只接合「大」缺口；小額配息（多數美股季配、台股季配）維持原價，樞紐價才會與線圖一致
                for i in np.where(step >= 1 + HEAL_MIN_GAP_PCT / 100.0)[0]:
                    f[:i + 1] /= step[i]
                    events.append((int(i + 1), float((1 - 1 / step[i]) * 100)))
    if is_tw:
        hc = c * f
        chg = hc[1:] / hc[:-1] - 1
        for i in np.where(np.abs(chg) > 0.105)[0]:      # 台股有 ±10% 限制，超過必為除權息/減資/資料錯誤
            t = int(i + 1)
            if t < 6:
                continue                                # 新掛牌前 5 日無漲跌幅限制
            g = (o[t] * f[t]) / (c[t - 1] * f[t - 1])
            if abs(g - 1) < 0.03:
                warn = True
                continue
            f[:t] *= g
            events.append((t, float((1 - g) * 100)))
    return o * f, h * f, l * f, c * f, events, warn


# ==========================================
# 5. 基本面（第 7、8 章）
# ==========================================
_FUND_CACHE = {}


def _yoy(vals):
    """vals 由新到舊的季資料 → 各季年增率 %（由新到舊）。去年同期 ≤ 0 時無法計算，回 None。"""
    out = []
    for i in range(len(vals) - 4):
        a, b = vals[i], vals[i + 4]
        if a is None or b is None or b <= 0:
            out.append(None)
        else:
            out.append((a - b) / abs(b) * 100.0)
    return out


def _row(q, *names):
    for nm in names:
        if nm in q.index:
            return [float(x) if pd.notna(x) else None for x in q.loc[nm].tolist()]
    return []


def get_fundamentals(symbol, price, tw_rev=None, deep=False):
    key = (symbol, deep)
    if key in _FUND_CACHE:
        return _FUND_CACHE[key]
    res = {"line": "財報無資料", "badges": [], "warn": [], "score": 0, "meets_min": None, "strong": False}
    try:
        tk = yf.Ticker(symbol)
        try:
            info = tk.info or {}
        except Exception:
            info = {}

        eps_q, surprises = [], []
        try:
            ed = tk.get_earnings_dates(limit=16)
            if ed is not None and len(ed):
                ed = ed.sort_index(ascending=False)
                ecol = next((x for x in ed.columns if "Reported" in str(x)), None)
                scol = next((x for x in ed.columns if "Surprise" in str(x)), None)
                if ecol is not None:
                    rep = ed[ed[ecol].notna()]
                    eps_q = [float(x) for x in rep[ecol].tolist()]
                    if scol is not None:
                        surprises = [float(x) for x in rep[scol].tolist() if pd.notna(x)]
        except Exception:
            pass

        rev_q, ni_q, q = [], [], None
        try:
            q = tk.quarterly_income_stmt
            if q is not None and not q.empty:
                q = q[sorted(q.columns, reverse=True)]
                e2 = _row(q, 'Diluted EPS', 'Basic EPS')
                while e2 and e2[-1] is None:
                    e2.pop()
                if len(e2) > len(eps_q):
                    eps_q = e2
                rev_q = _row(q, 'Total Revenue', 'Operating Revenue')
                ni_q = _row(q, 'Net Income', 'Net Income Common Stockholders')
        except Exception:
            pass

        if not surprises:
            try:
                eh = tk.earnings_history
                if eh is not None and not eh.empty and 'surprisePercent' in eh.columns:
                    s = eh.sort_index(ascending=False)['surprisePercent'].dropna().tolist()
                    surprises = [float(x) * 100.0 if abs(float(x)) < 5 else float(x) for x in s]
            except Exception:
                pass

        eps_now = eps_q[0] if eps_q else None
        eps_prev = eps_q[4] if len(eps_q) > 4 else None
        g = _yoy(eps_q)
        g0 = g[0] if g else None
        if g0 is None and eps_prev is None and info.get('earningsQuarterlyGrowth') is not None:
            g0 = float(info['earningsQuarterlyGrowth']) * 100.0
        px = price if price and price > 0 else None
        tiny = bool(px and ((eps_now is not None and 0 <= eps_now / px < 0.001)
                            or (eps_prev is not None and 0 < eps_prev / px < 0.0004)))
        turnaround = eps_now is not None and eps_now > 0 and eps_prev is not None and eps_prev <= 0
        meaningful = g0 is not None and eps_now is not None and eps_now > 0 and not tiny

        rg = _yoy(rev_q)
        r0 = rg[0] if rg else None
        if r0 is None and info.get('revenueGrowth') is not None:
            r0 = float(info['revenueGrowth']) * 100.0
        tw_yoy = tw_rev.get("yoy") if tw_rev else None
        nm = [(a / b * 100.0) for a, b in zip(ni_q, rev_q) if a is not None and b]

        accel = len(g) >= 3 and all(x is not None for x in g[:3]) and g[0] > g[1] > g[2] and meaningful
        decel = len(g) >= 2 and g[0] is not None and g[1] is not None and g[1] >= 40 and g[0] < g[1] * 0.6
        rev_accel = len(rg) >= 3 and all(x is not None for x in rg[:3]) and rg[0] > rg[1] > rg[2]
        margin_up = len(nm) >= 3 and nm[0] > nm[1] > nm[2]
        code33 = accel and rev_accel and margin_up
        beats = 0
        for s in surprises:
            if s > 0:
                beats += 1
            else:
                break

        parts = []
        if eps_now is not None:
            parts.append(f"季EPS <b>{eps_now:.2f}</b>")
        elif info.get('trailingEps') is not None:
            parts.append(f"四季EPS <b>{float(info['trailingEps']):.2f}</b>")
        if meaningful:
            if len(g) >= 3 and all(x is not None for x in g[:3]):
                parts.append(f"EPS年增 <b>{g[2]:+.0f}→{g[1]:+.0f}→{g[0]:+.0f}%</b>")
            else:
                parts.append(f"EPS年增 <b>{g0:+.0f}%</b>")
        elif tiny:
            parts.append("EPS基期過低，年增率不具意義")
        elif turnaround:
            parts.append("轉虧為盈")
        if tw_yoy is not None:
            cum = tw_rev.get("cum")
            parts.append(f"月營收年增 <b>{tw_yoy:+.0f}%</b>" + (f"(累計{cum:+.0f}%)" if cum is not None else ""))
        elif r0 is not None:
            parts.append(f"營收年增 <b>{r0:+.0f}%</b>")
        if nm:
            parts.append(f"淨利率 {nm[0]:.0f}%")

        score, badges, warn = 0, [], []
        if eps_now is not None and eps_now <= 0:
            res["meets_min"] = False
            warn.append("最近一季虧損")
            score -= 10
        elif meaningful:
            res["meets_min"] = g0 >= EPS_MIN_GROWTH
            if res["meets_min"]:
                score += 10
            else:
                warn.append(f"EPS年增未達{EPS_MIN_GROWTH:.0f}%")
                score -= 10
        if accel:
            badges.append("盈餘加速")
            score += 8
        if decel:
            warn.append("盈餘減速")
            score -= 8
        if code33:
            badges.append("密碼33")
            score += 10
        rev_ref = tw_yoy if tw_yoy is not None else r0
        if rev_ref is not None and rev_ref >= 20:
            score += 6
        if beats >= 2:
            badges.append(f"🔥連{beats}季超預期")
            score += 6
        elif beats == 1 and surprises and surprises[0] >= 5:
            badges.append(f"🔥EPS超預期+{surprises[0]:.0f}%")
            score += 3
        if turnaround:
            badges.append("轉機股(p.171 需連兩季強勁)")
        pe = _num(info.get('trailingPE'))
        if pe is not None and 0 < pe < 6:
            warn.append(f"本益比僅{pe:.0f}倍(p.78 極低是警訊)")

        if deep:
            try:
                et = tk.eps_trend
                if et is not None and not et.empty and '0y' in et.index:
                    cur, ago = _num(et.loc['0y', 'current']), _num(et.loc['0y', '30daysAgo'])
                    if cur is not None and ago is not None and ago > 0:
                        rv = (cur / ago - 1) * 100.0
                        if rv >= 5:
                            badges.append(f"估計上修+{rv:.0f}%")
                            score += 5
                        elif rv <= -5:
                            warn.append(f"估計下修{rv:.0f}%")
                            score -= 8
            except Exception:
                pass
            try:
                bs = tk.quarterly_balance_sheet
                if bs is not None and not bs.empty and r0 is not None:
                    bs = bs[sorted(bs.columns, reverse=True)]
                    hot = []
                    for label, names in (("存貨", ('Inventory',)), ("應收帳款", ('Accounts Receivable', 'Receivables'))):
                        x = _row(bs, *names)
                        if len(x) >= 5 and x[0] and x[4] and x[4] > 0:
                            gy = (x[0] - x[4]) / x[4] * 100.0
                            if gy > 25 and gy > r0 + 20:
                                hot.append(f"{label}+{gy:.0f}%")
                    if hot:
                        warn.append("、".join(hot) + f" 高於營收{r0:+.0f}%(p.189)")
                        score -= 6
            except Exception:
                pass

        res["line"] = "｜".join(parts) if parts else "財報無資料"
        res["badges"], res["warn"], res["score"] = badges, warn, score
        # 「基本面強」：EPS 達最低標；或 EPS 無法比較（缺資料／轉盈）但有獲利、且連續超預期或營收年增 ≥25%
        res["strong"] = bool(res["meets_min"] is True or (
            res["meets_min"] is None and not tiny and (eps_now or 0) > 0
            and (beats >= 2 or (rev_ref is not None and rev_ref >= 25))))
    except Exception:
        pass
    _FUND_CACHE[key] = res
    return res


def fund_text(f):
    s = f["line"]
    if f["badges"]:
        s += " <b>[" + " ".join(f["badges"]) + "]</b>"
    if f["warn"]:
        s += "\n  ⚠️ " + "；".join(f["warn"])
    return s


# ==========================================
# 6. 風險工具（第 12、13 章）
# ==========================================
def position_pct(stop_pct, mkt):
    """單筆風險固定為總資金的 RISK_PER_TRADE_PCT；台股因跌停可能出不掉，以至少一根跌停計算。"""
    eff = max(stop_pct, MARKET_CFG[mkt]["gap_risk_pct"])
    return min(MAX_POSITION_PCT, RISK_PER_TRADE_PCT / eff * 100.0)


def stage3_flags(c, v, v50, recent=10, lookback=250):
    """p.100、p.112-121：出現「一年來最大單日（或單週）跌幅且帶大量」是第三階段警訊。回傳警訊文字或 None。"""
    n = len(c)
    lb = min(lookback, n - 7)
    if lb < 60:
        return None
    d1 = c[1:] / c[:-1] - 1
    i1 = n - lb + int(np.argmin(d1[-lb:]))          # K 棒位置
    if (n - 1 - i1) < recent and d1[i1 - 1] <= -0.04 and v50[i1 - 1] > 0 and v[i1] >= 1.5 * v50[i1 - 1]:
        return f"{n - 1 - i1}天前出現一年來最大單日跌幅 {d1[i1 - 1] * 100:.1f}% 且爆量"
    d5 = c[5:] / c[:-5] - 1
    i5 = n - lb + int(np.argmin(d5[-lb:]))
    if (n - 1 - i5) < recent and d5[i5 - 5] <= -0.08 and v50[i5 - 5] > 0 and v[i5 - 4:i5 + 1].mean() >= 1.3 * v50[i5 - 5]:
        return f"近期出現一年來最大單週跌幅 {d5[i5 - 5] * 100:.1f}% 且帶量"
    return None


def holding_stop_price(df, h):
    cost = float(h["cost"])
    if h.get("stop"):
        sp = float(h["stop"])
        return sp, (cost - sp) / cost * 100.0
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
    lines = ["📦 <b>【持股守護雷達】</b> <i>停損固定於成本下方，不隨現價移動</i>"]
    for sym, h in MY_HOLDINGS.items():
        try:
            if not isinstance(h, dict):
                h = {"cost": float(h)}
            cost = float(h["cost"])
            mkt = mkt_of(sym)
            cfg = MARKET_CFG[mkt]
            df = raw_data.get(sym)
            if df is None or len(df) < 30:
                df = yf.Ticker(sym).history(period="1y", auto_adjust=False)
                if df is not None and len(df) and getattr(df.index, "tz", None) is not None:
                    df.index = df.index.tz_localize(None)
            if df is None or len(df) < 25:
                lines.append(f"• <b>{sym}</b>｜⚠️ 抓不到報價，請手動檢查")
                continue

            c = df['Close'].values.astype(float)
            v = df['Volume'].values.astype(float)
            c_now = float(c[-1])
            ma20 = float(df['Close'].rolling(20).mean().iloc[-1])
            stop, stop_pct = holding_stop_price(df, h)
            pnl = (c_now - cost) / cost * 100.0
            fee = cfg["cost_buy_pct"] + cfg["cost_sell_pct"]
            breakeven = cost * (1 + fee / 100.0)

            touched, r_max = False, None
            if h.get("date"):
                since = df[df.index >= pd.Timestamp(h["date"])]
                if len(since):
                    touched = float(since['Low'].min()) <= stop
                    if cost > stop:
                        r_max = (float(since['High'].max()) - cost) / (cost - stop)

            if c_now <= stop:
                advice = f"🚨 <b>收盤跌破停損 {stop:.2f} (-{stop_pct:.1f}%)，依計畫出場</b>"
            elif touched:
                advice = f"🚨 <b>進場後盤中曾觸及停損 {stop:.2f}</b>，若未出場請檢查"
            elif c_now < ma20:
                advice = f"⚠️ <b>收盤跌破 20MA({ma20:.2f})</b>，p.272：勝算明顯降低｜停損 <b>{stop:.2f}</b>"
            else:
                advice = f"✅ 守在 20MA(<b>{ma20:.2f}</b>) 之上｜🛡️停損 <b>{stop:.2f}</b> (距現價 {(c_now - stop) / c_now * 100:.1f}%)"

            notes = []
            if stop_pct > ABS_MAX_STOP_PCT:
                notes.append(f"停損幅度 {stop_pct:.1f}% 超過書中上限 {ABS_MAX_STOP_PCT:.0f}% (p.351)")
            if r_max is not None and r_max >= BREAKEVEN_R_MULT and stop < breakeven:
                notes.append(f"漲幅已達停損的 {r_max:.1f} 倍 → 書中做法是把停損上移到損益兩平 {breakeven:.2f} (p.361)")
            if len(c) > 80:
                v50 = df['Volume'].rolling(50).mean().values
                s3 = stage3_flags(c, v, v50)
                if s3:
                    notes.append(f"{s3} (p.100 第三階段警訊)")
            if cfg["limit_pct"] and len(c) > 1:
                chg = (c_now / c[-2] - 1) * 100
                if chg <= -(cfg["limit_pct"] - 0.7) and abs(float(df['Low'].iloc[-1]) - c_now) < 1e-9:
                    notes.append("今日跌停鎖住，停損單可能無法成交")

            icon = "🟢" if pnl >= 0 else "🔻"
            net = f"｜扣稅費 {pnl - fee:+.1f}%" if fee > 0 else ""
            line = f"• <b>{sym}</b>｜現價 <b>{c_now:.2f}</b> (成本 {cost:.2f}｜{icon} <b>{pnl:+.1f}%</b>{net})\n  └ {advice}"
            for nt in notes:
                line += f"\n  └ ⚠️ {nt}"
            lines.append(line)
        except Exception as e:
            lines.append(f"• <b>{sym}</b>｜⚠️ 檢查失敗: {html.escape(str(e))[:60]}")
    return "\n".join(lines) + "\n" + ("─" * 18) + "\n\n"


# ==========================================
# 7. 型態引擎
# ==========================================
def zigzag_pivots(h, l, thr):
    """從底部左側高點 (index 0) 開始，找出交替的波段高/低點。最後一個是尚未確認的候選轉折。"""
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


def _market_drop(m, a, b):
    """大盤在 [a, b) 期間由高點回落的最大幅度 %。"""
    if m is None:
        return None
    seg = m[a:b]
    if len(seg) < 5 or not np.isfinite(seg).all():
        return None
    k = int(np.argmax(seg))
    return float((seg[k] - seg[k:].min()) / seg[k] * 100.0)


def analyze_base(h, l, c, v, v50, atr_pct, end, m=None):
    """分析 [0, end) 這段 K 棒形成的底部（end 那根不算，用來判斷「end 當天是否突破」）。
    m = 對齊後的大盤收盤價（可省略）。回傳 None = 沒有可辨識的底部（剛創高、太深、太短又太深…）。"""
    if end < 80:
        return None
    last = end - 1

    p0, mkt_dd = None, None
    for lb in BASE_LOOKBACKS:
        start = max(0, end - lb)
        seg = h[start:end]
        rel = len(seg) - 1 - int(np.argmax(seg[::-1]))
        cand = start + rel
        days = last - cand
        if days < MIN_BASE_DAYS:
            return None                                   # 高點太近 → 還在延伸段，不是買點
        if start > 0 and rel < 3:
            continue                                      # 視窗邊緣的假高點
        depth = (h[cand] - l[cand:end].min()) / h[cand] * 100.0
        dd = _market_drop(m, cand, end)
        cap = MAX_BASE_DEPTH if dd is None else min(MAX_BASE_DEPTH_BEAR, max(MAX_BASE_DEPTH, 2.5 * dd))
        if depth > cap:
            continue                                      # 太深，改看較近期的底部
        if days < SHORT_BASE_DAYS and depth > SHORT_BASE_MAX_DEPTH:
            return None                                   # p.311：短底部不該修正這麼深
        if depth < MIN_BASE_DEPTH:
            return None
        p0, mkt_dd = cand, dd
        break
    if p0 is None:
        return None

    base_high = float(h[p0])
    lo_i = p0 + int(np.argmin(l[p0:end]))
    base_low = float(l[lo_i])
    base_depth = (base_high - base_low) / base_high * 100.0
    base_days = last - p0

    thr = float(np.clip(ZZ_ATR_MULT * atr_pct[last], 0.025, 0.08))
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

    # p.253 時間壓縮：低點才剛出現就 V 形拉回高檔，右側沒有時間醞釀
    low_age = last - lo_i
    recov = (c[last] - base_low) / (base_high - base_low) if base_high > base_low else 0.0
    compressed = low_age < max(5, int(0.2 * base_days)) and recov > 0.6

    is_vcp = (
        2 <= n_t <= 6
        and violations <= (1 if n_t >= 4 else 0)
        and final <= min(depths) + 1e-9
        and final <= min(FINAL_CONTRACTION_MAX, depths[0] * FINAL_VS_FIRST)
        and in_upper_half
        and pivot_age >= PIVOT_MIN_AGE
        and not compressed
    )

    hi5 = float(h[end - 5:end].max())
    tight5 = (hi5 - float(l[end - 5:end].min())) / hi5 * 100.0

    # p.241、p.271 箱形／平坦底部：沒有明顯收縮、修正 10~15% 內 → 以底部最高價為樞紐
    is_flat = (
        (not is_vcp)
        and base_depth <= FLAT_BASE_MAX_DEPTH
        and FLAT_BASE_DAYS[0] <= base_days <= FLAT_BASE_DAYS[1]
        and tight5 <= 8.0
        and not compressed
    )
    if is_flat:
        pivot_price = base_high
        t_label = "箱形"
    else:
        t_label = f"{n_t}T"

    # p.269 樞紐點量縮；常出現整個底部的最低量
    vb = float(v50[last]) if v50[last] > 0 else 1.0
    v5 = float(v[end - 5:end].mean())
    v10 = float(v[end - 10:end].mean())
    vmin = float(v[end - 5:end].min())
    dryup = (v5 <= 0.8 * vb) or (v10 <= 0.7 * vb) or (vmin <= 0.5 * vb)
    dry_extreme = vmin <= float(v[p0:end].min()) * 1.05

    # p.254-257 洗盤：跌破底部內先前的波段低點後又站回
    lows = [(p[0], p[1]) for p in piv if p[2] == 'L']
    shakeouts = 0
    for k in range(1, len(lows)):
        if lows[k][1] < lows[k - 1][1] and float(c[p0 + lows[k][0]:end].max()) > lows[k - 1][1]:
            shakeouts += 1

    # pb 觀察池用：收縮方向是否正確（越來越寬的不算）
    orderly = violations <= 1 and (n_t == 1 or depths[-1] <= depths[0] * 0.9) and not (n_t >= 3 and depths[-1] > depths[-2])

    rel_depth = (base_depth / mkt_dd) if (mkt_dd is not None and mkt_dd >= 5.0) else None
    weeks = max(1, int(round(base_days / 5.0)))
    shown_final = tight5 if is_flat else final
    return {
        "ready": bool(is_vcp or is_flat),
        "is_vcp": bool(is_vcp),
        "is_flat": bool(is_flat),
        "pivot": float(pivot_price),
        "base_high": base_high,
        "base_depth": base_depth,
        "base_days": int(base_days),
        "depths": depths,
        "final": float(shown_final),
        "n_t": n_t,
        "weeks": weeks,
        "dryup": bool(dryup),
        "dry_extreme": bool(dry_extreme),
        "shakeouts": int(shakeouts),
        "compressed": bool(compressed),
        "orderly": bool(orderly),
        "rel_depth": rel_depth,
        "footprint": f"{weeks}W {depths[0]:.0f}/{shown_final:.0f} {t_label}",
        "depth_str": "→".join(f"{d:.0f}" for d in depths),
    }


def detect_power_play(h, l, c, v, n):
    """p.298 強勁表現型態：8 週內帶量漲 ≥100% → 整理 12~30 天、修正 ≤25% → 緊密 ≤10%。"""
    if n < 120:
        return None
    end = n - 1
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
    return {"pivot": hi, "gain": float((h[peak] / run_lo - 1) * 100.0), "corr": float(corr), "tight": tight, "days": int(days)}


def detect_cheat(h, l, c, v, v50, end):
    """p.286-290 3C（杯狀完成騙局）：下跌 → 回升收復跌幅 1/3~1/2 → 停頓區（幅度 5~10%、量縮）→ 突破停頓區高點。
    這是書中「最早的進場點」，位置在杯子的中段或下段，所以不要求站上 50 日線。"""
    if end < 150:
        return None
    last = end - 1
    p0 = lo_i = None
    for lb in (225, 150, 100, 60, 35):                   # 型態 3~45 週
        start = max(0, end - lb)
        seg = h[start:end]
        rel = len(seg) - 1 - int(np.argmax(seg[::-1]))
        cand = start + rel
        if last - cand < 15:
            return None
        if start > 0 and rel < 3:
            continue
        li = cand + int(np.argmin(l[cand:end]))
        depth = (h[cand] - l[li]) / h[cand] * 100.0
        if depth > CHEAT_MAX_DEPTH:
            continue
        if depth < CHEAT_MIN_DEPTH:
            return None
        p0, lo_i = cand, li
        break
    if p0 is None:
        return None
    cup_high, cup_low = float(h[p0]), float(l[lo_i])
    depth = (cup_high - cup_low) / cup_high * 100.0

    best = None
    for k in range(5, 21):                               # 停頓區：數天到數週
        a = end - k
        if a <= lo_i + 2:
            break
        hi, lo = float(h[a:end].max()), float(l[a:end].min())
        if (hi - lo) / hi * 100.0 <= CHEAT_PAUSE_MAX:
            best = (k, hi, lo)
        else:
            break
    if not best:
        return None
    k, phi, plo = best
    recov = (phi - cup_low) / (cup_high - cup_low)
    if not (CHEAT_RECOV[0] <= recov <= CHEAT_RECOV[1]):
        return None
    pre = l[max(0, p0 - 250):p0 + 1]                     # 先前 3~36 個月已漲至少 25%
    if len(pre) < 40 or cup_high / float(pre.min()) < 1.25:
        return None
    vb = float(v50[last]) if v50[last] > 0 else 1.0
    dry = float(v[end - 5:end].mean()) <= 0.8 * vb or float(v[end - 5:end].min()) <= 0.5 * vb
    mid = l[lo_i + 1:end - k]
    shake = bool(len(mid) > 0 and plo < float(mid[-min(len(mid), 15):].min()))
    return {"pivot": phi, "pause_low": plo, "lo_i": int(lo_i), "depth": depth, "recov": recov * 100.0, "pause_days": k,
            "pause_range": (phi - plo) / phi * 100.0, "dry": bool(dry), "shake": shake,
            "weeks": max(1, int(round((last - p0) / 5.0)))}


def detect_primary_base(h, l, c, v, n):
    """第 11 章：新上市股掛牌後第一個可買的底部。至少 3 週；修正 ≤25~35%（期間很短時 ≤25%）。"""
    end = n - 1
    if end < PRIMARY_MIN_BARS - 1:
        return None
    skip = 5                                             # 掛牌頭幾天波動失真（台股前 5 日無漲跌幅限制）
    hi_i = skip + int(np.argmax(h[skip:end]))
    days = (end - 1) - hi_i
    if days < 15:
        return None
    base_high = float(h[hi_i])
    depth = (base_high - float(l[hi_i:end].min())) / base_high * 100.0
    cap = SHORT_BASE_MAX_DEPTH if days < SHORT_BASE_DAYS else (MAX_BASE_DEPTH_BEAR if days >= 200 else MAX_BASE_DEPTH)
    if depth > cap or depth < MIN_BASE_DEPTH:
        return None
    k = min(10, days)
    hi = float(h[end - k:end].max())
    tight = (hi - float(l[end - k:end].min())) / hi * 100.0
    if tight > 10.0:
        return None
    va = float(v[max(0, end - 50):end].mean()) or 1.0
    dry = float(v[end - 5:end].mean()) <= 0.8 * va
    pivot = hi if hi >= base_high * 0.92 else base_high
    return {"pivot": pivot, "base_high": base_high, "depth": depth, "tight": tight, "dry": bool(dry),
            "weeks": max(1, int(round(days / 5.0))), "listed_weeks": int(round(n / 5.0))}


def count_bases(c, rc, ma200):
    """p.108-110 底部計數（估算）：自最後一次「持續跌破 200 日線」之後，數出幾個「整理 ≥4 週、回檔 ≥8% 後再創高」的底部。
    回傳 (目前是第幾個, 起點是否確定)。第二階段若在資料視窗之前就開始，只能給下限。"""
    n = len(c)
    valid = np.where(np.isfinite(ma200))[0]
    if len(valid) < 60:
        return None, False
    first = int(valid[0])
    below = (rc[first:] < ma200[first:]).astype(float)
    sustained = np.convolve(below, np.ones(20), mode="full")[:len(below)] >= 10
    idx = np.where(sustained)[0]
    known = len(idx) > 0
    s = first + int(idx[-1]) if known else first
    run_max, last_hi, trough, done = c[s], s, c[s], 0
    for i in range(s + 1, n):
        if c[i] > run_max:
            if (i - last_hi) >= 20 and (run_max - trough) / run_max >= 0.08:
                done += 1
            run_max, last_hi, trough = c[i], i, c[i]
        elif c[i] < trough:
            trough = c[i]
    in_base = (n - 1 - last_hi) >= 10 and (run_max - trough) / run_max >= 0.05
    return done + (1 if in_base else 0), known


# ==========================================
# 8. Telegram
# ==========================================
def send_telegram_msg(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=15)
        if r.status_code != 200:
            print(f"❌ Telegram 回應 {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"❌ 網路連線錯誤：{e}")


def send_reports(holdings_msg, header, sections):
    blocks = []
    for title, items in sections:
        if items:
            blocks.append(title)
            blocks.extend(items)
    if not blocks:
        send_telegram_msg(holdings_msg + header + "\n今天沒有符合條件的標的。")
        return
    pages, cur = [], ""
    for b in blocks:
        if len(cur) + len(b) + 2 > 3200:
            pages.append(cur)
            cur = ""
        cur += b + "\n\n"
    if cur:
        pages.append(cur)
    first_prefix = holdings_msg + header + "\n"
    if len(first_prefix) + len(pages[0]) > 4000:        # Telegram 單則上限 4096
        send_telegram_msg(first_prefix)
        first_prefix = ""
    for i, p in enumerate(pages, 1):
        prefix = first_prefix if i == 1 else f"📊 <b>續 ({i}/{len(pages)})</b>\n\n"
        send_telegram_msg(prefix + p)


# ==========================================
# 9. 大盤（第 9 章；p.202、p.299：整體市況要配合）
# ==========================================
def fetch_indices(start_date):
    out = {}
    for mkt, cfg in MARKET_CFG.items():
        try:
            d = yf.Ticker(cfg["index"]).history(start=start_date, auto_adjust=False)
            if d is None or len(d) < 210:
                continue
            s = d['Close'].dropna()
            if getattr(s.index, "tz", None) is not None:
                s.index = s.index.tz_localize(None)
            s.index = s.index.normalize()
            out[mkt] = s[~s.index.duplicated(keep="last")]
        except Exception as e:
            print(f"⚠️ 指數 {cfg['index']} 抓取失敗: {e}")
    return out


def market_regime(s):
    if s is None or len(s) < 200:
        return "unk"
    c = s.values.astype(float)
    c_now, m50, m200 = c[-1], c[-50:].mean(), c[-200:].mean()
    if c_now > m50 > m200:
        return "bull"
    if c_now < m50 and c_now < m200:
        return "bear"
    return "mixed"


REGIME_TXT = {"bull": "🟢多頭", "mixed": "🟡整理", "bear": "🔴空頭", "unk": "⚪未知"}


# ==========================================
# 10. 主引擎
# ==========================================
def _extract(data, sym, single):
    if single and not isinstance(data.columns, pd.MultiIndex):
        return data.copy()
    return data[sym].copy()


def analyze_market(stock_dict, meta, tw_open, uni_info):
    symbols = list(stock_dict.keys())
    raw_data, young_data, rs_scores = {}, {}, {"tw": {}, "us": {}}
    start_date = (pd.Timestamp.now() - pd.DateOffset(months=HISTORY_MONTHS)).strftime('%Y-%m-%d')
    batch_size = 180

    print(f"📥 批次下載 {len(symbols)} 檔 K 線（還原分割、未還原配息）...")
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        try:
            data = yf.download(batch, start=start_date, auto_adjust=False, group_by="ticker", threads=True, progress=False)
            for sym in batch:
                try:
                    df = _extract(data, sym, len(batch) == 1)
                    cols = [x for x in ['Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume'] if x in df.columns]
                    df = df[cols].dropna(subset=['Open', 'High', 'Low', 'Close', 'Volume'])
                    df = df[df['Volume'] > 0]
                    if getattr(df.index, "tz", None) is not None:
                        df.index = df.index.tz_localize(None)
                    if len(df) < 252:
                        if len(df) >= PRIMARY_MIN_BARS:
                            young_data[sym] = df
                        continue
                    raw_data[sym] = df
                    # 相對強度用含息報酬：台股除息後不會平白少掉一段漲幅
                    ca = df['Adj Close'] if ('Adj Close' in df.columns and df['Adj Close'].notna().all()) else df['Close']
                    score = (0.4 * (ca.iloc[-1] / ca.iloc[-63] - 1) + 0.2 * (ca.iloc[-1] / ca.iloc[-126] - 1)
                             + 0.2 * (ca.iloc[-1] / ca.iloc[-189] - 1) + 0.2 * (ca.iloc[-1] / ca.iloc[-250] - 1))
                    if np.isfinite(score):
                        rs_scores[mkt_of(sym)][sym] = float(score)
                except Exception:
                    continue
        except Exception as e:
            print(f"批次下載略過: {e}")

    # 相對強度：台股跟台股比、美股跟美股比
    rs_map = {}
    for scores in rs_scores.values():
        if scores:
            rs_map.update((pd.Series(scores).rank(pct=True) * 99).round(0).astype(int).to_dict())

    indices = fetch_indices(start_date)
    regime = {mkt: market_regime(indices.get(mkt)) for mkt in MARKET_CFG}
    stop_cap = {mkt: (TOUGH_MARKET_STOP_PCT if regime[mkt] == "bear" else MAX_STOP_PCT) for mkt in MARKET_CFG}

    keys = ("brk", "recent", "star", "squat", "cheat", "power", "primary", "pb")
    cats = {k: {"tw": [], "us": []} for k in keys}
    momentum = []
    stats = {"scanned": len(raw_data), "liquid": 0, "template": 0}
    nh, nl = defaultdict(int), defaultdict(int)
    grp_total = {"tw": defaultdict(int), "us": defaultdict(int)}
    grp_pass = {"tw": defaultdict(list), "us": defaultdict(list)}
    grp_nh = {"tw": defaultdict(int), "us": defaultdict(int)}
    pending = []     # 需要產業群資訊的卡片，掃完後再補

    def industry_of(sym):
        ind = (meta.get(sym) or {}).get("industry")
        if not ind and sym in tw_open:
            ind = tw_open[sym].get("industry")
        return ind

    for symbol, df in raw_data.items():
        try:
            mkt = mkt_of(symbol)
            is_tw = mkt == "tw"
            cfg = MARKET_CFG[mkt]
            n = len(df)
            rc = df['Close'].values.astype(float)            # 未還原配息（與看盤軟體一致）
            v = df['Volume'].values.astype(float)
            v50 = df['Volume'].rolling(50).mean().values
            turnover = float(np.mean(rc[-50:] * v[-50:]))
            if not (v50[-1] >= MIN_VOL_MA50) or turnover < cfg["min_turnover"]:
                continue
            stats["liquid"] += 1

            o, h, l, c, events, data_warn = heal_series(df, is_tw)    # 連續化價格：型態分析用
            close = df['Close']
            ma20 = close.rolling(20).mean().values
            ma50 = close.rolling(50).mean().values
            ma150 = close.rolling(150).mean().values
            ma200 = close.rolling(200).mean().values
            pc = np.r_[np.nan, c[:-1]]
            tr = np.fmax(h - l, np.fmax(np.abs(h - pc), np.abs(l - pc)))
            atr_pct = pd.Series(tr).rolling(20).mean().values / c

            c_now, c_prev, o_now, h_now, l_now = c[-1], c[-2], o[-1], h[-1], l[-1]
            rs = rs_map.get(symbol, 50)
            name = stock_dict.get(symbol, symbol)
            flag = "🇹🇼" if is_tw else "🇺🇸"
            title = f"{flag} {symbol} {html.escape(name)}" if name != symbol else f"{flag} {symbol}"
            ind = industry_of(symbol)
            grp_total[mkt][ind] += 1

            hi52 = float(df['High'].values[-252:].max())        # 52 週高低用未還原價
            lo52 = float(df['Low'].values[-252:].min())
            is_nh = h[-1] >= h[-252:-1].max()
            if is_nh:
                nh[mkt] += 1
                grp_nh[mkt][ind] += 1
            if l[-1] <= l[-252:-1].min():
                nl[mkt] += 1

            mult, is_intraday = get_intraday_vol_multiplier(df.index[-1], is_tw)
            vol_ratio = (v[-1] / v50[-2] if v50[-2] > 0 else 1.0) * mult
            vol_label = "預估量" if is_intraday else "量"
            is_red = (c_now >= o_now) and (c_now > c_prev) and ((c_now - l_now) >= (h_now - l_now) * 0.5 if h_now > l_now else True)
            pct_today = (c_now / c_prev - 1) * 100.0
            lim = cfg["limit_pct"]
            locked_up = bool(lim and pct_today >= lim - 0.7 and abs(h_now - c_now) < 1e-9)   # 漲停鎖住：量小是因為買不到
            vol_ok = vol_ratio >= BREAKOUT_VOL_MULT or locked_up
            lock_note = "｜🔒漲停鎖住，買不到；明日開高超過追價上限就放棄" if locked_up else ""

            stop_pct = float(np.clip(2 * atr_pct[-1] * 100.0, MIN_STOP_PCT, stop_cap[mkt]))
            pos = position_pct(stop_pct, mkt)
            pos_note = f"部位≤{pos:.0f}%" + ("(跌停風險)" if is_tw and pos < MAX_POSITION_PCT else "")

            def risk_line(pivot):
                return f"🛡️停損 <b>{pivot * (1 - stop_pct / 100):.2f}</b> (-{stop_pct:.1f}%)｜{pos_note}"

            head = f"<b>{title}</b>｜現價 <b>{c_now:.2f}</b>｜RS <b>{rs}</b>"

            # 出貨跡象（p.99-100）
            diff40 = np.diff(c[-41:])
            big40 = v[-40:] > v50[-40:]
            up_days = int((big40 & (diff40 > 0)).sum())
            dn_days = int((big40 & (diff40 < 0)).sum())
            imax = n - 60 + int(np.argmax(v[-60:]))
            s3 = stage3_flags(c, v, v50, recent=15)
            distribution = (
                c[imax] < c[imax - 1] * 0.98                       # 近 60 日最大量那天是大跌
                or (c_now < c_prev * 0.985 and vol_ratio >= 1.35)  # 今天帶量下跌
                or dn_days > up_days + 1                           # 大量下跌日明顯較多
                or s3 is not None                                  # 一年來最大跌幅且爆量
            )

            # ---------- p.298 強勁表現型態（書中明言不要求基本面，但要守供需法則）----------
            pp = detect_power_play(h, l, c, v, n)
            in_power = False
            if pp and c_now > ma50[-1] and c_now <= pp["pivot"] * (1 + CHASE_LIMIT_PCT / 100) and not distribution:
                state = "🚀突破" if (c_now > pp["pivot"] and vol_ok) else "等突破"
                cats["power"][mkt].append((rs + pp["gain"] / 10, symbol, (
                    f"{head}\n"
                    f"  ⚡ <b>{state}｜樞紐 {pp['pivot']:.2f}</b>{lock_note}\n"
                    f"  {risk_line(pp['pivot'])}\n"
                    f"  8週漲 {pp['gain']:.0f}%｜整理 {pp['days']}天 修正 {pp['corr']:.0f}%｜緊密 {pp['tight']:.0f}%"
                ), c_now))
                in_power = True

            long_ok = (np.isfinite(ma200[-1 - MA200_UP_DAYS]) and c_now > ma200[-1]
                       and ma200[-1] > ma200[-1 - MA200_UP_DAYS] and rs >= RS_MIN and c_now >= lo52 * LOW52_MULT)

            # ---------- p.286-290 3C：只要求站在上升的 200 日線之上 ----------
            cheat_item = None
            if long_ok:
                ch = detect_cheat(h, l, c, v, v50, n - 1)
                if ch:                                  # 杯子左側本來就帶量下跌，出貨檢查只看杯底之後
                    lo = ch["lo_i"]
                    dd, bigv = np.diff(c[lo:]), v[lo + 1:] > v50[lo + 1:]
                    if int((bigv & (dd < 0)).sum()) > int((bigv & (dd > 0)).sum()) + 1 or (c_now < c_prev * 0.985 and vol_ratio >= 1.35):
                        ch = None
                if ch:
                    d = (c_now / ch["pivot"] - 1) * 100.0
                    if c_now > ch["pivot"] and c_prev <= ch["pivot"] and d <= CHASE_LIMIT_PCT:
                        state = f"🚀突破停頓區 {ch['pivot']:.2f} ({vol_label}{vol_ratio:.1f}倍)" if (vol_ok and is_red) else f"越過停頓區 {ch['pivot']:.2f} 但量不足"
                    elif -4.0 <= d <= 0:
                        state = f"等突破停頓區高點 {ch['pivot']:.2f} (差 {abs(d):.1f}%)"
                    else:
                        state = None
                    if state:
                        extra = ("｜💧量縮" if ch["dry"] else "") + ("｜洗盤✓" if ch["shake"] else "")
                        cheat_item = (rs + (15 if ch["dry"] else 0) + (5 if ch["shake"] else 0), symbol, (
                            f"{head}\n"
                            f"  🥄 <b>{state}</b>{lock_note}\n"
                            f"  {risk_line(ch['pivot'])}\n"
                            f"  杯 {ch['weeks']}週 深{ch['depth']:.0f}%・已收復{ch['recov']:.0f}%・停頓{ch['pause_days']}天 幅度{ch['pause_range']:.0f}%{extra}"
                        ), c_now)

            # ---------- 第一層：p.106 趨勢樣板八項 ----------
            k = max(1, DAYS_ABOVE_LONG_MA)
            template = (
                bool((rc[-k:] > ma150[-k:]).all() and (rc[-k:] > ma200[-k:]).all())  # 1 股價 > 150MA、200MA
                and ma150[-1] > ma200[-1]                                            # 2 150MA > 200MA
                and ma200[-1] > ma200[-1 - MA200_UP_DAYS]                            # 3 200MA 上升至少 1 個月
                and ma50[-1] > ma150[-1] * (1 + MA50_OVER_MA150_PCT / 100)           # 4 50MA > 150MA (> 200MA)
                and c_now > ma50[-1]                                                 # 5 股價 > 50MA
                and c_now >= lo52 * LOW52_MULT                                       # 6 高於 52 週低點 30%
                and c_now >= hi52 * (1 - HIGH52_WITHIN)                              # 7 距 52 週高點 25% 內
                and rs >= RS_MIN                                                     # 8 RS ≥ 70
            )
            if not template:
                if cheat_item:
                    cats["cheat"][mkt].append(cheat_item)
                continue
            stats["template"] += 1
            grp_pass[mkt][ind].append((rs, symbol))

            if is_red and vol_ok and c_now >= h[-20:-1].max() and not distribution:
                momentum.append((rs + int(min(vol_ratio, 10) * 10), symbol, head + ("｜🔒漲停鎖住" if locked_up else ""), c_now, vol_ratio, vol_label))
            if distribution:
                continue

            # 大盤對齊（修正幅度比較、RS 線）
            m = None
            if mkt in indices:
                m = indices[mkt].reindex(df.index.normalize(), method="ffill").bfill().values.astype(float)
                if not np.isfinite(m).all():
                    m = None

            ma200_up_4m = n > 290 and ma200[-1] > ma200[-44] > ma200[-88]
            score = rs + (15 if rs >= 85 else 0) + (10 if ma200_up_4m else 0) + (15 if c_now >= lo52 * 2 else 0)
            score += max(0, up_days - dn_days) * 4

            tags = [f"大量漲跌日 <b>{up_days}:{dn_days}</b>"]
            nb, nb_known = count_bases(c, rc, ma200)
            if nb and (nb_known or nb >= 3):
                if nb >= 4:
                    tags.append(f"⚠️第{'' if nb_known else '≥'}{nb}底(後期 p.110)")
                    score -= 15
                else:
                    tags.append(f"第{'' if nb_known else '≥'}{nb}底")
                    score += 5 if (nb <= 2 and nb_known) else 0
            rs_tag = ""
            if m is not None:
                rsl = c / m
                if rsl[-1] >= rsl[-252:].max() * 0.999:
                    rs_tag = "RS線領先創高" if c_now < hi52 * 0.98 else "RS線新高"
                    score += 8 if c_now < hi52 * 0.98 else 4

            def card(action, fp, pivot, extra_score=0, dry=False, show_risk=True, note=""):
                t = list(tags)
                if dry:
                    t.insert(0, "💧💧底部最低量" if fp.get("dry_extreme") else "💧量縮")
                if fp.get("shakeouts"):
                    t.append(f"洗盤{fp['shakeouts']}次")
                if fp.get("rel_depth") and fp["rel_depth"] > REL_DEPTH_WARN:
                    t.append(f"⚠️修正為大盤{fp['rel_depth']:.1f}倍")
                if fp.get("compressed"):
                    t.append("⚠️右側時間壓縮(p.253)")
                recent_ev = [e for e in events if e[0] >= n - fp.get("base_days", 0) - 2]
                if recent_ev:
                    t.append(f"除權息缺口{sum(e[1] for e in recent_ev):.1f}%已還原")
                if data_warn:
                    t.append("⚠️還原資料異常，請對照線圖")
                text = f"{head}\n  {action}{lock_note}\n"
                if show_risk:
                    text += f"  {risk_line(pivot)}\n"
                text += f"  📐<code>{fp['footprint']}</code> ({fp['depth_str']})｜" + "｜".join(t)
                if note:
                    text += f"\n  {note}"
                s = score + extra_score + (20 if dry else 0) + (5 if fp.get("dry_extreme") and dry else 0)
                s += int(max(0, 10 - fp["final"]) * 6) + min(2, fp.get("shakeouts", 0)) * 4
                if fp.get("rel_depth") and fp["rel_depth"] > REL_DEPTH_WARN:
                    s -= 15
                return (s, symbol, text, c_now)

            def add(key, item):
                cats[key][mkt].append(item)
                pending.append((key, mkt, symbol, ind, rs, rs_tag))

            # ---------- 第三層：截至昨天的底部，今天是否突破 ----------
            fp = analyze_base(h, l, c, v, v50, atr_pct, n - 1, m)
            placed, fail_note = False, ""
            if fp and fp["ready"]:
                pivot = fp["pivot"]
                dist = (c_now / pivot - 1) * 100.0
                if c_now > pivot and c_prev <= pivot:
                    if dist <= CHASE_LIMIT_PCT and is_red and vol_ok and fp["dryup"]:
                        add("brk", card(
                            f"🚀 <b>突破樞紐 {pivot:.2f}</b> ({vol_label}{vol_ratio:.1f}倍)｜追價上限 {pivot * (1 + CHASE_LIMIT_PCT / 100):.2f}",
                            fp, pivot, 30, True))
                    elif dist <= CHASE_LIMIT_PCT:
                        why = "量不足" if not vol_ok else ("突破前未量縮" if not fp["dryup"] else "收盤偏低")
                        add("star", card(f"⚠️ <b>越過樞紐 {pivot:.2f} 但{why}</b> ({vol_label}{vol_ratio:.1f}倍)", fp, pivot, 5, fp["dryup"]))
                    placed = True
                elif c_now <= pivot and dist >= -STAR_MAX_DIST and fp["dryup"] and c_now >= ma20[-1] * 0.99:
                    mark = "★◎" if (fp["final"] <= 5.0 or fp["dry_extreme"]) else "★"
                    add("star", card(f"🎯 <b>{mark} 樞紐 {pivot:.2f}</b> (差 {abs(dist):.1f}%)｜突破需量 ≥{BREAKOUT_VOL_MULT}倍", fp, pivot, 20, True))
                    placed = True

            # ---------- 第四層：近 10 日是否突破過 → 蹲下／反轉回升／網球行為（樞紐固定在突破當時）----------
            if not placed:
                for kk in range(POST_BREAKOUT_DAYS, 0, -1):
                    b = n - 1 - kk
                    fpb = analyze_base(h, l, c, v, v50, atr_pct, b, m)
                    if not (fpb and fpb["ready"] and fpb["dryup"]):
                        continue
                    pv = fpb["pivot"]
                    b_locked = bool(lim and (c[b] / c[b - 1] - 1) * 100 >= lim - 0.7 and abs(h[b] - c[b]) < 1e-9)
                    if not (c[b] > pv and c[b - 1] <= pv and v50[b - 1] > 0 and (v[b] >= BREAKOUT_VOL_MULT * v50[b - 1] or b_locked)):
                        continue
                    dist = (c_now / pv - 1) * 100.0
                    dipped = bool((c[b + 1:n - 1] < pv).any()) if kk > 1 else False
                    # p.282 網球行為：拉回時的量應小於上漲時
                    seg_c, seg_p, seg_v = c[b:n], c[b - 1:n - 1], v[b:n]
                    upv, dnv = seg_v[seg_c > seg_p], seg_v[seg_c < seg_p]
                    tb = ""
                    if len(upv) and len(dnv):
                        ratio = dnv.mean() / upv.mean()
                        tb = "回檔量縮✅" if ratio < 0.8 else ("⚠️回檔量大於上漲量" if ratio > 1.1 else "")
                    if c_now > pv * (1 + CHASE_LIMIT_PCT / 100):
                        placed = True                                   # 已延伸，不追價
                    elif c_now >= pv:
                        if dipped and c_now > c_prev:
                            add("squat", card(f"🔄 <b>反轉回升：重新站上樞紐 {pv:.2f}</b> ({kk}天前突破)", fpb, pv, 15, True, note=tb))
                        else:
                            add("recent", card(f"✅ <b>{kk}天前突破 {pv:.2f}</b>，現高於樞紐 {dist:.1f}% (仍在買進區)", fpb, pv, 10, True, note=tb))
                        placed = True
                    elif c_now > pv * (1 - stop_pct / 100) and c_now >= ma20[-1]:
                        add("squat", card(f"🔄 <b>蹲下 squat：跌回樞紐 {pv:.2f} 下方</b> ({kk}天前突破)｜守 20MA {ma20[-1]:.2f}", fpb, pv, 5, True, note=tb))
                        placed = True
                    else:
                        fail_note = f"⚠️ {kk}天前突破 {pv:.2f} 失敗，等重設樞紐 (p.295)"
                    break

            # ---------- 3C：位置較低的早期進場點 ----------
            if not placed and cheat_item:
                add("cheat", cheat_item)
                placed = True

            # ---------- 第五層：pb 觀察池（還不是買點，所以不顯示停損與部位）----------
            if not placed and not in_power and fp and (fp["orderly"] or fail_note) and not fp["compressed"]:
                ref = fp["pivot"] if fp["ready"] else fp["base_high"]
                dist = (c_now / ref - 1) * 100.0
                if -PB_MAX_DIST <= dist <= 0:
                    need = []
                    if not fp["ready"]:
                        need.append("收縮未完成")
                    if not fp["dryup"]:
                        need.append("量未縮")
                    add("pb", card(f"📌 <b>pb｜參考高點 {ref:.2f}</b> (差 {abs(dist):.1f}%)｜{'、'.join(need) or '等靠近樞紐'}",
                                   fp, ref, -int(abs(dist)), fp["dryup"], show_risk=False, note=fail_note))
        except Exception:
            continue

    # ---------- 第 11 章：新上市股初選底部 ----------
    for symbol, df in young_data.items():
        try:
            mkt = mkt_of(symbol)
            cfg = MARKET_CFG[mkt]
            n = len(df)
            v = df['Volume'].values.astype(float)
            rc = df['Close'].values.astype(float)
            w = min(50, n)
            if float(v[-w:].mean()) < MIN_VOL_MA50 or float(np.mean(rc[-w:] * v[-w:])) < cfg["min_turnover"]:
                continue
            o, h, l, c, events, _ = heal_series(df, mkt == "tw")
            pb = detect_primary_base(h, l, c, v, n)
            if not pb:
                continue
            c_now = c[-1]
            d = (c_now / pb["pivot"] - 1) * 100.0
            if not (-5.0 <= d <= CHASE_LIMIT_PCT) or not pb["dry"]:    # 自訂：要貼近樞紐且量縮才列出
                continue
            if n >= 50 and c_now < float(np.mean(rc[-50:])):
                continue
            pc = np.r_[np.nan, c[:-1]]
            tr = np.fmax(h - l, np.fmax(np.abs(h - pc), np.abs(l - pc)))
            atr = float(np.nanmean(tr[-20:]) / c_now * 100.0)
            stop_pct = float(np.clip(2 * atr, MIN_STOP_PCT, stop_cap[mkt]))
            name = stock_dict.get(symbol, symbol)
            flag = "🇹🇼" if mkt == "tw" else "🇺🇸"
            title = f"{flag} {symbol} {html.escape(name)}" if name != symbol else f"{flag} {symbol}"
            vr = v[-1] / (float(v[-w - 1:-1].mean()) or 1.0)
            state = (f"🚀突破 {pb['pivot']:.2f} (量{vr:.1f}倍)" if (c_now > pb["pivot"] and vr >= BREAKOUT_VOL_MULT)
                     else (f"越過 {pb['pivot']:.2f} 但量不足" if c_now > pb["pivot"] else f"等突破 {pb['pivot']:.2f} (差 {abs(d):.1f}%)"))
            pos = position_pct(stop_pct, mkt)
            cats["primary"][mkt].append((50 - pb["depth"] + (15 if pb["dry"] else 0) - pb["tight"], symbol, (
                f"<b>{title}</b>｜現價 <b>{c_now:.2f}</b>｜掛牌約 {pb['listed_weeks']} 週\n"
                f"  🌱 <b>{state}</b>\n"
                f"  🛡️停損 <b>{pb['pivot'] * (1 - stop_pct / 100):.2f}</b> (-{stop_pct:.1f}%)｜部位≤{pos:.0f}%\n"
                f"  初選底部 {pb['weeks']}週 修正{pb['depth']:.0f}%・緊密{pb['tight']:.0f}%" + ("｜💧量縮" if pb["dry"] else "")
            ), c_now))
        except Exception:
            continue

    for d in cats.values():
        for lst in d.values():
            lst.sort(key=lambda x: x[0], reverse=True)
    momentum.sort(key=lambda x: x[0], reverse=True)

    # ---------- 第 6、9 章：領導產業群（由下而上：哪個產業過樣板的家數最多）----------
    grp_rank, grp_line = {}, {}
    for mkt in MARKET_CFG:
        rows = [(len(lst), ind) for ind, lst in grp_pass[mkt].items() if ind and len(lst) >= 2 and grp_total[mkt][ind] >= 4]
        rows.sort(reverse=True)
        if rows:
            grp_line[mkt] = "、".join(f"{html.escape(str(ind))} {cnt}/{grp_total[mkt][ind]}" + (f"(新高{grp_nh[mkt][ind]})" if grp_nh[mkt][ind] else "")
                                     for cnt, ind in rows[:5])
        for ind, lst in grp_pass[mkt].items():
            for pos_i, (_, sym) in enumerate(sorted(lst, reverse=True), 1):
                grp_rank[sym] = (pos_i, len(lst))
    extra_line = {}
    this_year = datetime.now().year
    for key, mkt, sym, ind, rs, rs_tag in pending:
        bits = []
        if ind and sym in grp_rank:
            pos_i, cnt = grp_rank[sym]
            bits.append(f"🏭{html.escape(str(ind))}(過樣板{cnt}檔｜群內RS第{pos_i})")
        if rs_tag:
            bits.append(rs_tag)
        ipo = (meta.get(sym) or {}).get("ipo_year")
        if ipo and 0 <= this_year - ipo <= 10:
            bits.append(f"上市{int(this_year - ipo)}年(p.57 年輕公司)")
        if bits:
            extra_line[(key, sym)] = "\n  " + "｜".join(bits)

    limits = {"brk": (5, 5), "recent": (4, 4), "star": (6, 5), "squat": (4, 4), "cheat": (3, 3), "power": (3, 3),
              "primary": (2, 2), "pb": (5, 4)}
    listed = set()

    def render(key, deep):
        out = []
        for mi, mkt in enumerate(("tw", "us")):
            lim = limits[key][mi]
            pool = cats[key][mkt][:int(np.ceil(lim * FUND_OVERSAMPLE))]
            ranked = []
            for s, sym, text, px in pool:
                f = get_fundamentals(sym, px, tw_open.get(sym))
                if REQUIRE_FUNDAMENTALS and f["meets_min"] is False:
                    continue
                grp_bonus = 5 if (sym in grp_rank and grp_rank[sym][0] <= 3 and grp_rank[sym][1] >= 3) else 0
                ranked.append((s + f["score"] + grp_bonus, sym, text, px))
            ranked.sort(key=lambda x: x[0], reverse=True)
            for s, sym, text, px in ranked[:lim]:
                f = get_fundamentals(sym, px, tw_open.get(sym), deep=deep)
                listed.add(sym)
                out.append(text + extra_line.get((key, sym), "") + "\n  💰 " + fund_text(f))
        return out

    print("💰 產生日報...")
    sections = [
        ("🚀 <b>【一、今日帶量突破樞紐】</b>", render("brk", True)),
        ("✅ <b>【二、近日已突破、仍在買進區】</b>", render("recent", True)),
        ("★ <b>【三、樞紐成型＋量縮（等突破，勿預先卡位 p.270）】</b>", render("star", True)),
        ("🔄 <b>【四、蹲下 Squat／反轉回升 (p.271)】</b>", render("squat", True)),
        ("🥄 <b>【五、3C 杯狀完成騙局 (p.286)】</b>", render("cheat", True)),
        ("⚡ <b>【六、強勁表現型態 (p.298)】</b>", render("power", False)),
        ("🌱 <b>【七、新上市初選底部 (第11章)】</b>", render("primary", False)),
        ("📌 <b>【八、pb 觀察池（還不是買點）】</b>", render("pb", False)),
    ]
    beat = []
    for _, sym, head, px, vr, vl in momentum[:20]:
        if sym in listed:
            continue
        f = get_fundamentals(sym, px, tw_open.get(sym))
        if f["strong"]:
            beat.append(f"{head}\n  💥 帶量創 20 日高 ({vl}{vr:.1f}倍)｜尚無樞紐，僅列觀察\n  💰 {fund_text(f)}")
        if len(beat) >= 5:
            break
    sections.append(("💥 <b>【九、基本面強＋帶量創高（觀察，非買點）】</b>", beat))

    holdings_msg = check_my_holdings(raw_data)

    # ---------- 表頭 ----------
    mk = []
    for mkt, cfg in MARKET_CFG.items():
        mk.append(f"{cfg['name']}{REGIME_TXT[regime[mkt]]} 新高{nh[mkt]}/新低{nl[mkt]}")
    header = (
        f"📊 <b>【《超級績效》SEPA 選股日報】</b>\n"
        f"<i>台股 {uni_info['tw_n']}｜美股 {uni_info['us_n']}（{html.escape(uni_info['us_source'])}）\n"
        f"有 K 線 {stats['scanned']}｜過流動性 {stats['liquid']}｜過趨勢樣板 {stats['template']}</i>\n"
        f"大盤：{'｜'.join(mk)}\n"
    )
    for mkt, cfg in MARKET_CFG.items():
        if regime[mkt] == "bear":
            header += f"⚠️ {cfg['name']}空頭：書中做法是降低曝險、停損縮到 5~6% (p.364)，卡片停損上限已改為 {TOUGH_MARKET_STOP_PCT:.0f}%\n"
        if mkt in grp_line:
            header += f"🏭 {cfg['name']}領導產業(過樣板/總數)：{grp_line[mkt]}\n"
    return holdings_msg, header, sections


if __name__ == "__main__":
    stock_dict, meta, uni_info = get_entire_market_universe()
    tw_open = fetch_tw_open_data()
    holdings_msg, header, sections = analyze_market(stock_dict, meta, tw_open, uni_info)
    send_reports(holdings_msg, header, sections)
