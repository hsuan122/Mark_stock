"""
《超級績效》SEPA / VCP 選股雷達  v4.1 —— 依《超級績效》第 3~13 章與《超級績效 2》第 1~10 章校正，台股／美股分流

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

《超級績效 2》（頁碼前加 ②）：這本補的是「買進之後」——
  第 1 章 ②p.42-56  突破後的跟進計數與七項違例；20 日線；蹲下與反轉復甦
  第 2-3 章 ②p.66-91 停損放在危險點、不依波動放寬；依自己的平均獲利設停損；2~3 倍時上調停損
  第 4 章 ②p.99-115 交易統計：勝率、平均獲利／虧損、期望值（讀 trades.csv）
  第 5 章 ②p.143   沒有 10% 獲利緩衝不抱大部位過財報
  第 6-7 章 ②p.157  趨勢模板補充（RS 線方向）；②p.198-205 3C 與低位作弊
  第 8 章 ②p.217   部位規模：單筆風險 1.25~2.5%、單一部位 ≤25%
  第 9 章 ②p.229-252 底部計數；本益比擴張（②p.232-235）；趁強賣（高潮走勢）、趁弱賣；50 日線追蹤；免費參賽

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
#    未填 "stop" 時沿用原本的算法：進場日 2xATR（夾在 3%~8%）固定於成本下方，不隨現價移動 (p.276)。
#    註：第二本 p.79 不贊成用波動度決定停損幅度；想照書做，就在 "stop" 直接填你自己的停損價。
#    賣出後把這一行刪掉，並在 trades.csv 加一行（程式會用它算勝率、平均獲利、平均虧損）。
MY_HOLDINGS = {
    "FMX": {"cost": 133.48, "date": "2026-11-11"},
    "TEVA": {"cost": 44.21, "date": "2026-11-12"},
}

# --- 風險（第一本第 12、13 章；第二本第 2、3、8、9 章。標「②」的頁碼是第二本）---
MAX_STOP_PCT = 8.0            # 卡片停損上限（書：最大 10%，平均要壓在 6~7%，作者自己約 4~5%）
MIN_STOP_PCT = 3.0            # 自訂：留給正常波動的最小空間
STOP_BUFFER_PCT = 0.5         # 自訂：停損放在緊密區低點下方多少 %
ABS_MAX_STOP_PCT = 10.0       # p.323、p.351、②p.66：任何情況不超過 10%
TOUGH_MARKET_STOP_PCT = 6.0   # p.364、②p.83：困難環境停損縮到 5~6%
BREAKEVEN_R_MULT = 3.0        # p.360、②p.90：漲幅達起始停損 3 倍 → 停損至少上移到損益兩平
FREE_ROLL_R_MULT = 2.0        # ②p.246：漲幅達停損 2~3 倍 → 可賣一半、其餘停損移到成本（免費參賽）
RISK_PER_TRADE_PCT = 1.25     # ②p.217：單筆風險佔總資金 1.25%~2.5%（經驗少的靠低端）
MAX_POSITION_PCT = 25.0       # ②p.217：最看好的標的 20~25%；任何情況不超過 50%；持股 4~8 檔
CHASE_LIMIT_PCT = 5.0         # p.267：不追價
EARNINGS_WARN_DAYS = 14       # 自訂：財報在幾天內就提醒
EARNINGS_CUSHION_PCT = 10.0   # ②p.143：帳面獲利不到 10% 就不抱大部位過財報
FOLLOW_DAYS = 10              # ②p.46：突破後第 1~2 週觀察跟進買盤與違例
TRADES_FILE = os.environ.get("TRADES_FILE", "trades.csv")   # 已平倉交易紀錄；沒有這個檔就不顯示統計
TRADE_STATS_MIN_N = 20        # 自訂：累積到這個筆數，才用「平均獲利的一半」限制卡片停損上限（②p.86）
PE_EXPANSION_WARN = 2.0       # ②p.233：本益比比第一個底部當時擴張一倍以上 → 已到上漲後段
SEND_DIAGNOSTICS = True       # 調整期間多傳一則診斷訊息到 Telegram；穩定後改成 False

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

# --- 趨勢樣板 (p.106；②p.157) ---
RS_MIN = 70
LOW52_MULT = 1.30             # 第一本 30%、第二本 25%，取較嚴的；要照第二本就改成 1.25
HIGH52_WITHIN = 0.25
MA200_UP_DAYS = 22
DAYS_ABOVE_LONG_MA = 15       # 自訂加嚴：近 N 日收盤都在 150/200MA 之上（擋掉年線糾纏股）；設 1 = 只看今天
MA50_OVER_MA150_PCT = 0.0     # 自訂：書中只要求 50 > 150 > 200

# --- 底部 / VCP (p.238-253) ---
BASE_LOOKBACKS = [325, 200, 130, 90, 60, 40]   # 3~65 週
MIN_BASE_DAYS = 15
POKE_MAX_PCT = 3.0            # 自訂：近 3 週的新高若只比舊底部的高點多出這個幅度以內，視為在底部上緣試探，底部延續
                              # （舊底部本身要先滿 3 週；只有 3 週的來回震盪不算）
MAX_BASE_DEPTH = 35.0         # p.253、②p.178：最好 10~35%
MAX_BASE_DEPTH_BEAR = 50.0    # p.226、p.253：大盤重挫時可到 50%，再深就太過份
MIN_BASE_DEPTH = 5.0          # p.277 手寫清單有 5W 5/2、6W 5/2
SHORT_BASE_DAYS = 25          # 5 週以內的短底部
SHORT_BASE_MAX_DEPTH = 25.0   # p.311：期間很短時修正不應超過 25%
REL_DEPTH_WARN = 3.0          # p.252、②p.179：修正幅度達大盤 2.5~3 倍以上 → 偏弱
FINAL_CONTRACTION_MAX = 10.0
CONTRACTION_RATIO = 0.85      # 自訂：每次收縮 ≤ 前一次 × 此值（書中理想約 0.5）
FINAL_VS_FIRST = 0.60         # 自訂
ZZ_ATR_MULT = 1.5             # 自訂：波段轉折門檻 = ATR% × 此值，夾在 2.5%~8%
PIVOT_MIN_AGE = 3
FLAT_BASE_MAX_DEPTH = 15.0    # p.241、p.271
FLAT_BASE_DAYS = (20, 40)     # p.241：箱形 4~7 週（上限放寬到 8 週）
BREAKOUT_VOL_MULT = 1.5       # 自訂下限（書中例子為 3~4 倍以上）
STAR_MAX_DIST = 6.0
PB_MAX_DIST = 15.0
POST_BREAKOUT_DAYS = 10       # p.271：蹲下後有的 10 天或更久才回升

# --- 3C 杯狀完成騙局 (p.286-290) ---
CHEAT_MIN_DEPTH = 15.0
CHEAT_MAX_DEPTH = 40.0        # p.288、②p.198：通常 15~40%
CHEAT_MAX_DEPTH_BEAR = 50.0   # 大盤本身修正很深時才放寬到 50%
CHEAT_PAUSE_MAX = 10.0        # 停頓區高低幅度 5~10%
CHEAT_RECOV = (0.25, 0.70)    # 收復跌幅 1/3~1/2；下 1/3 是「低位作弊」(②p.202)，上 1/3 屬一般柄部，交給 VCP

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
                tw[f"{r['SecuritiesCompanyCode']}.TWO"] = str(r['CompanyName']).strip().rstrip('*')
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
    res = {"line": "財報無資料", "badges": [], "warn": [], "score": 0, "meets_min": None, "strong": False, "earn_days": None,
           "eps_hist": [], "last_split": None}
    try:
        tk = yf.Ticker(symbol)
        try:
            info = tk.info or {}
        except Exception:
            info = {}
        try:                                             # 最近一次股票分割／配股日（本益比擴張要避開）
            if info.get("lastSplitDate"):
                res["last_split"] = pd.Timestamp(int(info["lastSplitDate"]), unit="s").normalize()
        except Exception:
            pass

        eps_q, surprises = [], []
        try:
            ed = tk.get_earnings_dates(limit=28)
            if ed is not None and len(ed):
                ed = ed.sort_index(ascending=False)
                try:                                     # 下一次財報日（②p.143）
                    idx = pd.DatetimeIndex(ed.index)
                    if idx.tz is not None:
                        idx = idx.tz_localize(None)
                    today = pd.Timestamp.now().normalize()
                    fut = idx[idx.normalize() >= today]
                    if len(fut):
                        res["earn_days"] = int((fut.min().normalize() - today).days)
                except Exception:
                    pass
                ecol = next((x for x in ed.columns if "Reported" in str(x)), None)
                scol = next((x for x in ed.columns if "Surprise" in str(x)), None)
                if ecol is not None:
                    rep = ed[ed[ecol].notna()]
                    eps_q = [float(x) for x in rep[ecol].tolist()]
                    try:                                 # 帶公布日的歷史 EPS（由新到舊），算本益比擴張用
                        ridx = pd.DatetimeIndex(rep.index)
                        if ridx.tz is not None:
                            ridx = ridx.tz_localize(None)
                        seen = {}
                        for d, e in zip(ridx, eps_q):
                            seen.setdefault(d.normalize(), e)        # 同一天重複的列只留一筆
                        res["eps_hist"] = sorted(seen.items(), key=lambda x: x[0], reverse=True)
                    except Exception:
                        pass
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

        accel = len(g) >= 3 and all(x is not None for x in g[:3]) and g[0] > g[1] > g[2] and meaningful and g[0] >= EPS_MIN_GROWTH
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
                parts.append(f"近三季EPS年增 <b>{g[2]:+.0f}%→{g[1]:+.0f}%→{g[0]:+.0f}%</b>")
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
    """②p.215-217：部位大小由「這筆失敗時總資金會少多少」決定。單筆風險固定為 RISK_PER_TRADE_PCT；
    台股因跌停可能出不掉，以至少一根跌停計算。"""
    eff = max(stop_pct, MARKET_CFG[mkt]["gap_risk_pct"])
    return min(MAX_POSITION_PCT, RISK_PER_TRADE_PCT / eff * 100.0)


def plan_stop(entry, danger_low, cap_pct):
    """②p.70：停損放在「正常回檔即將變成危險走勢」的位置＝突破前緊密區的低點下方，並讓進場價貼近它；
    ②p.79：不因為股票波動大就放寬停損（所以不用 ATR 決定幅度）。回傳 (停損價, 距進場價 %, 是否被上限截斷)。"""
    cap_pct = max(cap_pct, MIN_STOP_PCT)
    if not danger_low or danger_low <= 0 or danger_low >= entry:
        raw = MIN_STOP_PCT
    else:
        raw = (entry - danger_low * (1 - STOP_BUFFER_PCT / 100.0)) / entry * 100.0
    pct = float(np.clip(raw, MIN_STOP_PCT, cap_pct))
    return entry * (1 - pct / 100.0), pct, bool(raw > cap_pct + 1e-9)


def follow_through(h, l, c, v, v50, rc, ma20, ma50, e, ref):
    """②p.42-56：突破（或進場）後的跟進狀況與違例。e = 突破／進場那根 K 棒的位置，ref = 樞紐價或成本，
    rc = 未還原收盤（跟均線比較用）。回傳 None 或 dict。"""
    n = len(c)
    if e is None or e < 1 or e >= n:
        return None
    days = n - e                                        # 含突破當天
    cc, pc = c[e:n], c[e - 1:n - 1]
    up, dn = int((cc > pc).sum()), int((cc < pc).sum())
    rng = h[e:n] - l[e:n]
    pos = np.where(rng > 0, (cc - l[e:n]) / np.where(rng > 0, rng, 1.0), 0.5)
    hi_half = int((pos >= 0.5).sum())
    viol = []
    if days <= FOLLOW_DAYS:
        vb = float(v50[e - 1]) if np.isfinite(v50[e - 1]) else 0.0
        if vb > 0 and days >= 2 and v[e] < vb:           # 1 小量突破、大量拉回
            heavy = v[e + 1:n][cc[1:] < pc[1:]]
            if len(heavy) and float(heavy.max()) > max(float(v[e]), vb):
                viol.append("小量突破卻大量拉回")
        ll, i = 0, n - 1                                 # 2 低點連續 3~4 天更低，且最後一天沒有買盤撐住
        while i > e and l[i] < l[i - 1]:
            ll += 1
            i -= 1
        if ll >= 3 and pos[-1] < 0.5 and c[-1] <= c[-2]:
            viol.append(f"連{ll}天低點更低")
        if days >= 4 and dn > up:                        # 3
            viol.append("收低多於收高")
        if days >= 4 and (days - hi_half) > hi_half:     # 4
            viol.append("多收在K棒下半部")
    if np.isfinite(ma20[-1]) and rc[-1] < ma20[-1]:      # 5
        viol.append("收盤跌破20MA")
    if np.isfinite(ma50[-1]) and rc[-1] < ma50[-1]:      # 6 帶量跌破 50MA
        for j in range(max(e, 1), n):
            if np.isfinite(ma50[j]) and rc[j] < ma50[j] and v50[j - 1] > 0 and v[j] >= 1.5 * v50[j - 1]:
                viol.append("帶量跌破50MA")
                break
    if ref and float(h[e:n].max()) >= ref * 1.08 and c[-1] <= ref * 1.005:   # 7（8% 為自訂的「明顯上漲」）
        viol.append("漲幅完全折返")
    upv, dnv = v[e:n][cc > pc], v[e:n][cc < pc]
    pull = float(dnv.mean() / upv.mean()) if (len(upv) and len(dnv) and upv.mean() > 0) else None
    return {"days": days, "up": up, "dn": dn, "hi_half": hi_half, "viol": viol, "pull": pull,
            "strong": bool(days >= 4 and up >= 0.75 * days)}


def strength_flags(h, l, c, v):
    """②p.235-241 高潮走勢的徵兆、②p.48 MVP。只描述現象；是初期的強勢還是後期的竭盡，由呼叫端依底部計數判斷。"""
    n = len(c)
    if n < 70:
        return None
    best, bk = -1e9, 5
    for k in range(5, 16):                               # 1~3 週內的最大漲幅
        g = (c[-1] / c[-1 - k] - 1) * 100.0
        if g > best:
            best, bk = g, k
    ups = {w: int((c[-w:] > c[-w - 1:-1]).sum()) for w in (8, 10, 15)}
    wr = next((w for w in (15, 10, 8) if ups[w] / w >= 0.7), None)      # 7~15 天內 70% 以上的天數收高
    lb = min(250, n - 2)
    chg = c[1:] / c[:-1] - 1
    i_up = n - lb + int(np.argmax(chg[-lb:]))            # 一年來漲幅最大的 K 棒
    spread = (h[1:] - l[1:]) / c[:-1]
    i_sp = n - lb + int(np.argmax(spread[-lb:]))         # 一年來最長的 K 棒
    gap = bool((l[-5:] > h[-6:-1]).any())                # 近 5 日向上跳空
    vprev = float(v[-65:-15].mean())
    g15 = (c[-1] / c[-16] - 1) * 100.0
    mvp = bool(ups[15] >= 12 and g15 >= 20.0 and vprev > 0 and float(v[-15:].mean()) >= 1.25 * vprev)
    return {"climax": bool(best >= 25.0 or (wr is not None and best >= 15.0)), "best": float(best), "bk": bk,
            "ups": ups, "wr": wr, "big_up": (n - 1 - i_up) < 5, "wide": (n - 1 - i_sp) < 5, "gap": gap, "mvp": mvp}


def load_trade_stats(path=None):
    """②第 4 章：用自己的交易結果算勝率、平均獲利、平均虧損、期望值。
    trades.csv 欄位：symbol,buy_date,buy_price,sell_date,sell_price,stop_price(可空),note(可空)。台股自動扣手續費與證交稅。"""
    path = path or TRADES_FILE
    if not os.path.exists(path):
        return None
    t = None
    for enc in ("utf-8-sig", "cp950"):                   # Excel 存的 CSV 可能帶 BOM 或是 Big5
        try:
            t = pd.read_csv(path, comment="#", skip_blank_lines=True, dtype=str, encoding=enc)
            break
        except Exception as e:
            err = e
    if t is None:
        print(f"⚠️ {path} 讀取失敗：{err}")
        return None
    cols = {str(x).strip().lower(): x for x in t.columns}
    need = ("symbol", "buy_date", "buy_price", "sell_date", "sell_price")
    if any(k not in cols for k in need):
        print(f"⚠️ {path} 缺少欄位，需要 {', '.join(need)}；實際是 {list(t.columns)}")
        return None
    rets, days, rmult, bad = [], [], [], 0
    for _, r in t.iterrows():
        try:
            if r.isna().all():
                continue                                  # 空白列
            sym = str(r[cols["symbol"]]).strip()
            bp, sp = _num(r[cols["buy_price"]]), _num(r[cols["sell_price"]])
            bd = pd.to_datetime(r[cols["buy_date"]], errors="coerce")
            sd = pd.to_datetime(r[cols["sell_date"]], errors="coerce")
            if not sym or sym.lower() == "nan" or not bp or not sp or bp <= 0 or sp <= 0 or pd.isna(bd) or pd.isna(sd):
                bad += 1
                continue
            cfg = MARKET_CFG[mkt_of(sym)]
            rets.append((sp / bp - 1) * 100.0 - cfg["cost_buy_pct"] - cfg["cost_sell_pct"])
            days.append(max(0, int((sd - bd).days)))
            st = _num(r[cols["stop_price"]]) if "stop_price" in cols else None
            rmult.append((sp - bp) / (bp - st) if (st and 0 < st < bp) else None)
        except Exception:
            bad += 1
    if bad:
        print(f"⚠️ {path} 有 {bad} 列無法解讀，已略過")
    if not rets:
        return None
    rets, days = np.array(rets), np.array(days)
    win = rets > 0
    nw, nl = int(win.sum()), int((~win).sum())
    avg_gain = float(rets[win].mean()) if nw else 0.0
    avg_loss = float(-rets[~win].mean()) if nl else 0.0
    wr = nw / len(rets)
    rm = [x for x in rmult if x is not None]
    return {
        "n": len(rets), "win_rate": wr, "avg_gain": avg_gain, "avg_loss": avg_loss,
        "ratio": (avg_gain / avg_loss) if avg_loss > 0 and nw else None,
        "expectancy": wr * avg_gain - (1 - wr) * avg_loss,
        "max_gain": float(rets.max()), "max_loss": float(rets.min()),
        "hold_win": float(days[win].mean()) if nw else None, "hold_loss": float(days[~win].mean()) if nl else None,
        "over10": int((rets < -ABS_MAX_STOP_PCT).sum()), "avg_r": (float(np.mean(rm)) if rm else None),
    }


def trade_stats_text(st, rba_cap=None):
    if not st:
        return ""
    n = st["n"]
    s = f"📒 <b>【交易統計】</b> {n} 筆已平倉" + ("（樣本少，僅供記錄）" if n < TRADE_STATS_MIN_N else "") + "\n"
    s += f"勝率 {st['win_rate'] * 100:.0f}%｜平均獲利 +{st['avg_gain']:.1f}%｜平均虧損 -{st['avg_loss']:.1f}%"
    if st["ratio"] is not None:
        s += f"｜盈虧比 {st['ratio']:.1f}"
    s += f"\n期望值 {st['expectancy']:+.2f}%/筆｜最大獲利 {st['max_gain']:+.1f}%｜最大虧損 {st['max_loss']:+.1f}%"
    if st["avg_r"] is not None:
        s += f"｜平均 {st['avg_r']:+.2f}R"
    if st["hold_win"] is not None and st["hold_loss"] is not None:
        s += f"\n平均持有：獲利 {st['hold_win']:.0f} 天／虧損 {st['hold_loss']:.0f} 天"
    flags = []
    if n >= 5:
        if st["over10"]:
            flags.append(f"有 {st['over10']} 筆虧損超過 {ABS_MAX_STOP_PCT:.0f}%（②p.107：不該越過的牆）")
        if st["max_gain"] > 0 > st["max_loss"] and st["max_gain"] < -st["max_loss"]:
            flags.append("最大獲利小於最大虧損（②p.105：虧損抱太久）")
        if st["hold_win"] is not None and st["hold_loss"] is not None and st["hold_win"] < st["hold_loss"]:
            flags.append("虧損部位抱得比獲利部位久（②p.105：太早賣贏家、太晚砍輸家）")
        if st["avg_gain"] > 0 and st["avg_loss"] > st["avg_gain"] / 2:
            flags.append(f"平均虧損高於平均獲利的一半；照你的平均獲利，停損應在 {st['avg_gain'] / 2:.1f}% 以內（②p.86）")
        if st["expectancy"] <= 0:
            flags.append("期望值為負")
    for fl in flags:
        s += f"\n⚠️ {fl}"
    if rba_cap is not None:
        s += f"\n→ 卡片停損上限已依「平均獲利的一半」改為 {rba_cap:.1f}%（②p.86）"
    return s + "\n" + ("─" * 18) + "\n\n"


def stage3_flags(c, v, v50, recent=10, lookback=250):
    """p.100、p.112-121、②p.242：出現「一年來最大單日（或單週）跌幅且帶大量」是第三階段／趁弱賣出的訊號。回傳文字或 None。"""
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
    # ②p.244：跌幅不一定最大，但成交量是一年來最大、而且收跌 4% 以上，同樣是賣出訊號
    iv = n - lb + int(np.argmax(v[-lb:]))
    if (n - 1 - iv) < recent and iv >= 1 and c[iv] <= c[iv - 1] * 0.96:
        return f"{n - 1 - iv}天前出現一年來最大量且收跌 {(1 - c[iv] / c[iv - 1]) * 100:.1f}%"
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
    for sym, hd in MY_HOLDINGS.items():
        try:
            if not isinstance(hd, dict):
                hd = {"cost": float(hd)}
            cost = float(hd["cost"])
            mkt = mkt_of(sym)
            cfg = MARKET_CFG[mkt]
            df = raw_data.get(sym)
            if df is None or len(df) < 30:
                df = yf.Ticker(sym).history(period="2y", auto_adjust=False)
                if df is not None and len(df) and getattr(df.index, "tz", None) is not None:
                    df.index = df.index.tz_localize(None)
            if df is None or len(df) < 25:
                lines.append(f"• <b>{sym}</b>｜⚠️ 抓不到報價，請手動檢查")
                continue

            n = len(df)
            _, h, l, c, _, _ = heal_series(df, mkt == "tw")
            rc = df['Close'].values.astype(float)
            v = df['Volume'].values.astype(float)
            v50 = df['Volume'].rolling(50).mean().values
            ma20s = df['Close'].rolling(20).mean().values
            ma50s = df['Close'].rolling(50).mean().values
            c_now, ma20, ma50 = float(rc[-1]), float(ma20s[-1]), float(ma50s[-1])
            stop, stop_pct = holding_stop_price(df, hd)
            pnl = (c_now - cost) / cost * 100.0
            fee = cfg["cost_buy_pct"] + cfg["cost_sell_pct"]
            breakeven = cost * (1 + fee / 100.0)

            e, touched, r_max = None, False, None
            if hd.get("date"):
                idx = np.where(df.index >= pd.Timestamp(hd["date"]))[0]
                if len(idx):
                    e = int(idx[0])
                    touched = float(df['Low'].values[e:].min()) <= stop
                    if cost > stop:
                        r_max = (float(df['High'].values[e:].max()) - cost) / (cost - stop)
            r_now = (c_now - cost) / (cost - stop) if cost > stop else None

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
                notes.append(f"⚠️ 停損幅度 {stop_pct:.1f}% 超過書中上限 {ABS_MAX_STOP_PCT:.0f}% (p.351)")
            if cfg["limit_pct"] and n > 1:
                chg = (c_now / rc[-2] - 1) * 100
                if chg <= -(cfg["limit_pct"] - 0.7) and abs(float(df['Low'].iloc[-1]) - c_now) < 1e-9:
                    notes.append("⚠️ 今日跌停鎖住，停損單可能無法成交")
            if n > 80:
                s3 = stage3_flags(c, v, v50)
                if s3:
                    notes.append(f"⚠️ {s3}（書中視為趁弱賣出訊號 p.100、②p.242）")

            # ②p.42-56：進場後第 1~2 週的跟進買盤與違例
            ft = follow_through(h, l, c, v, v50, rc, ma20s, ma50s, e, cost)
            if ft:
                if ft["days"] <= FOLLOW_DAYS:
                    q = "，跟進強" if ft["strong"] else ("，跟進弱" if (ft["days"] >= 4 and ft["dn"] > ft["up"]) else "")
                    notes.append(f"進場後 {ft['days']} 天：{ft['up']} 漲 {ft['dn']} 跌・收在K棒上半部 {ft['hi_half']}/{ft['days']}{q}"
                                 f"（書中希望 4 天有 3 天收高 ②p.47）")
                # 進場兩週內列出全部違例；之後只在出現「跌破 20MA」以外的項目時才提（20MA 已寫在上一行）
                vio = ft["viol"] if ft["days"] <= FOLLOW_DAYS else ([] if ft["viol"] == ["收盤跌破20MA"] else ft["viol"])
                if vio:
                    notes.append(f"⚠️ 違例 {len(vio)} 項：{'、'.join(vio)}（②p.56：違例越多越容易失敗，可在停損前先減碼）")

            # ②p.89-91、p.245-247：獲利後如何上調停損（只提示，不會改你的停損）
            if r_max is not None and stop < breakeven:
                if r_max >= BREAKEVEN_R_MULT:
                    notes.append(f"漲幅曾達停損的 {r_max:.1f} 倍 → 書中做法是把停損至少上移到損益兩平 {breakeven:.2f}（p.361、②p.90）")
                elif r_max >= FREE_ROLL_R_MULT:
                    notes.append(f"漲幅曾達停損的 {r_max:.1f} 倍 → 書中做法：賣一半、其餘停損移到成本 {breakeven:.2f}（免費參賽 ②p.246）")
            if np.isfinite(ma50) and pnl > 0 and ma50 >= breakeven and c_now > ma50 > stop:
                notes.append(f"50 日線 {ma50:.2f} 已高於成本 → 可改用「收盤跌破 50 日線」當出場點（②p.245）")

            # 底部計數（p.108、②p.229）：判斷處在這波漲勢的初期還是後期
            nb, nb_known, first_bar = (None, False, None)
            if n >= 260:
                nb, nb_known, first_bar = count_bases(c, rc, df['Close'].rolling(200).mean().values)

            # ②p.235-252：急漲是續抱還是該趁強賣，取決於處在初期還是後期
            if n > 80 and pnl > 0:
                sf = strength_flags(h, l, c, v)
                if sf and (sf["climax"] or sf["mvp"]):
                    held = (n - e) if e is not None else None
                    desc = f"近 {sf['bk']} 天漲 {sf['best']:.0f}%"
                    if sf["wr"]:
                        desc += f"・{sf['wr']} 天中 {sf['ups'][sf['wr']]} 天收高"
                    if sf["big_up"]:
                        desc += "・出現一年來最大漲幅K棒"
                    if sf["gap"]:
                        desc += "・向上跳空"
                    if nb is not None and nb >= 4:
                        desc += f" → 已是第 {nb} 個底部以後，書中做法是趁強勢至少賣出一部分（②p.239）"
                    elif (held is not None and held <= 20) or (nb_known and nb is not None and nb <= 2):
                        desc += " → 初期底部突破後的強勢，書中視為續抱訊號（②p.49、p.249）"
                    else:
                        desc += " → 若已到後期是趁強賣出訊號，初期底部則續抱（②p.239、p.249）"
                    notes.append("⚡ " + desc)

            # ②p.143：財報前沒有獲利緩衝就不抱大部位；②p.233：本益比擴張
            try:
                fnd = get_fundamentals(sym, c_now)
            except Exception:
                fnd = {}
            ed = fnd.get("earn_days")
            if first_bar is not None:
                try:
                    pe = pe_expansion(fnd.get("eps_hist"), df.index[first_bar], float(rc[first_bar]), float(rc[-1]),
                                      fnd.get("last_split"), df.index[-1])
                except Exception:                         # [pe-guard] 算不出來不影響其他檢查
                    pe = None
                if pe and "skip" not in pe and pe["ratio"] >= PE_EXPANSION_WARN:
                    notes.append(pe_text(pe, nb_known) + " → 要特別留意賣出訊號")
            if ed is not None and 0 <= ed <= EARNINGS_WARN_DAYS:
                when = "今天" if ed == 0 else f"約 {ed} 天後"
                if pnl < EARNINGS_CUSHION_PCT:
                    notes.append(f"📅 財報{when}，帳面獲利不到 {EARNINGS_CUSHION_PCT:.0f}% → 書中做法是減碼或不抱過財報（②p.143）")
                else:
                    notes.append(f"📅 財報{when}")

            icon = "🟢" if pnl >= 0 else "🔻"
            net = f"｜扣稅費 {pnl - fee:+.1f}%" if fee > 0 else ""
            r_txt = f"｜{r_now:+.1f}R" if r_now is not None else ""
            line = f"• <b>{sym}</b>｜現價 <b>{c_now:.2f}</b> (成本 {cost:.2f}｜{icon} <b>{pnl:+.1f}%</b>{net}{r_txt})\n  └ {advice}"
            for nt in notes[:6]:
                line += f"\n  └ {nt}"
            lines.append(line)
        except Exception as e:
            lines.append(f"• <b>{sym}</b>｜⚠️ 檢查失敗: {html.escape(str(e))[:60]}")
    return "\n".join(lines) + "\n" + ("─" * 18) + "\n\n"


NR_TXT = {"compressed": "右側時間壓縮", "one_contraction": "只有一次收縮", "too_many_swings": "波段太多",
          "not_contracting": "收縮沒有逐次變小", "final_not_smallest": "最後一次不是最小", "final_too_wide": "最後一次收縮超過10%",
          "final_vs_first": "最後一次相對第一次不夠小", "lower_half": "樞紐在底部下半部", "pivot_too_new": "樞紐高點太新", "other": "其他"}


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


_BASE_REJECT = ""


def analyze_base(h, l, c, v, v50, atr_pct, end, m=None):
    """分析 [0, end) 這段 K 棒形成的底部（end 那根不算，用來判斷「end 當天是否突破」）。
    m = 對齊後的大盤收盤價（可省略）。回傳 None = 沒有可辨識的底部（剛創高、太深、太短又太深…）。"""
    global _BASE_REJECT
    _BASE_REJECT = "short_history"
    if end < 80:
        return None
    last = end - 1

    p0, mkt_dd, poke = None, None, False
    for lb in BASE_LOOKBACKS:
        start = max(0, end - lb)
        seg = h[start:end]
        rel = len(seg) - 1 - int(np.argmax(seg[::-1]))
        cand = start + rel
        days = last - cand
        is_poke = False
        if days < MIN_BASE_DAYS:
            # 最高點落在最近 3 週內。若它只比更早的高點多出一點、而且兩者之間確實修正過，
            # 是「在舊底部上緣試探後縮回」，底部仍在延續；否則就是還在上漲途中，不是買點。
            cut = end - MIN_BASE_DAYS
            old = h[start:cut]
            ok = len(old) >= 10
            if ok:
                rel2 = len(old) - 1 - int(np.argmax(old[::-1]))
                cand2 = start + rel2
                ok = (h[cand] <= h[cand2] * (1 + POKE_MAX_PCT / 100.0)
                      and (cand - cand2) >= MIN_BASE_DAYS            # 試探之前，底部已經整理滿 3 週
                      and (h[cand2] - l[cand2:cand + 1].min()) / h[cand2] * 100.0 >= MIN_BASE_DEPTH)
            if not ok:
                _BASE_REJECT = "recent_high"
                return None
            cand, rel, days, is_poke = cand2, rel2, last - cand2, True
        if start > 0 and rel < 3:
            continue                                      # 視窗邊緣的假高點
        depth = (h[cand] - l[cand:end].min()) / h[cand] * 100.0
        dd = _market_drop(m, cand, end)
        cap = MAX_BASE_DEPTH if dd is None else min(MAX_BASE_DEPTH_BEAR, max(MAX_BASE_DEPTH, 2.5 * dd))
        if depth > cap:
            continue                                      # 太深，改看較近期的底部
        if days < SHORT_BASE_DAYS and depth > SHORT_BASE_MAX_DEPTH:
            _BASE_REJECT = "short_but_deep"
            return None                                   # p.311：短底部不該修正這麼深
        if depth < MIN_BASE_DEPTH:
            _BASE_REJECT = "too_shallow"
            return None
        p0, mkt_dd, poke = cand, dd, is_poke
        break
    if p0 is None:
        _BASE_REJECT = "too_deep"
        return None

    base_high = float(h[p0])                              # 左側高點：修正幅度由此起算
    base_top = float(h[p0:end].max())                     # 整個底部的最高價（試探過的話會略高於左側高點）
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
        _BASE_REJECT = "no_swing"
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
    lo5 = float(l[end - 5:end].min())
    tight5 = (hi5 - lo5) / hi5 * 100.0

    # p.241、p.271 箱形／平坦底部：沒有明顯收縮、修正 10~15% 內 → 以底部最高價為樞紐
    is_flat = (
        (not is_vcp)
        and base_depth <= FLAT_BASE_MAX_DEPTH
        and FLAT_BASE_DAYS[0] <= base_days <= FLAT_BASE_DAYS[1]
        and tight5 <= 8.0
        and not compressed
    )
    if is_flat:
        pivot_price = base_top
        t_label = "箱形"
    else:
        t_label = f"{n_t}T"

    # 樞紐未成型的主要原因（只用於診斷）
    why_not = ""
    if not (is_vcp or is_flat):
        if compressed:
            why_not = "compressed"
        elif n_t < 2:
            why_not = "one_contraction"
        elif n_t > 6:
            why_not = "too_many_swings"
        elif violations > (1 if n_t >= 4 else 0):
            why_not = "not_contracting"
        elif final > min(depths) + 1e-9:
            why_not = "final_not_smallest"
        elif final > FINAL_CONTRACTION_MAX:
            why_not = "final_too_wide"
        elif final > depths[0] * FINAL_VS_FIRST:
            why_not = "final_vs_first"
        elif not in_upper_half:
            why_not = "lower_half"
        elif pivot_age < PIVOT_MIN_AGE:
            why_not = "pivot_too_new"
        else:
            why_not = "other"

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
        "base_top": base_top,
        "poke": bool(poke),
        "danger_low": float(lo5 if is_flat else cons[-1][2]),   # ②p.70 危險點：突破前緊密區的低點
        "why_not": why_not,
        "vol5": v5 / vb,
        "volmin": vmin / vb,
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
        # 足跡：時間／整個底部最深的修正／最近一次／收縮次數（試探過舊高的，從試探高點起算）
        "footprint": f"{weeks}W {(base_top - base_low) / base_top * 100.0:.0f}/{shown_final:.0f} {t_label}",
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
    return {"pivot": hi, "low": float(l[end - k:end].min()), "gain": float((h[peak] / run_lo - 1) * 100.0),
            "corr": float(corr), "tight": tight, "days": int(days)}


def detect_cheat(h, l, c, v, v50, end, m=None):
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
        dd = _market_drop(m, cand, end)                 # 同期大盤從高點回落多少
        cap = CHEAT_MAX_DEPTH if dd is None else min(CHEAT_MAX_DEPTH_BEAR, max(CHEAT_MAX_DEPTH, 2.5 * dd))
        if depth > cap:
            return None                                  # 從 45 週內高點算起修正過深：不改用更小的視窗硬找一個淺杯
        if depth < CHEAT_MIN_DEPTH:
            return None
        p0, lo_i = cand, li
        break
    if p0 is None:
        return None
    cup_high, cup_low = float(h[p0]), float(l[lo_i])
    depth = (cup_high - cup_low) / cup_high * 100.0
    mdd = _market_drop(m, p0, end)
    rel_depth = (depth / mdd) if (mdd is not None and mdd >= 5.0) else None

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
    return {"pivot": phi, "pause_low": plo, "lo_i": int(lo_i), "depth": depth, "rel_depth": rel_depth, "recov": recov * 100.0, "pause_days": k,
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
    return {"pivot": pivot, "low": float(l[end - k:end].min()), "base_high": base_high, "depth": depth, "tight": tight, "dry": bool(dry),
            "weeks": max(1, int(round(days / 5.0))), "listed_weeks": int(round(n / 5.0))}


def count_bases(c, rc, ma200):
    """p.108-110 底部計數（估算）：自最後一次「持續跌破 200 日線」之後，數出幾個「整理 ≥4 週、回檔 ≥8% 後再創高」的底部。
    回傳 (目前是第幾個, 起點是否確定, 第一個底部完成並創新高的 K 棒位置或 None)。第二階段若在資料視窗之前就開始，只能給下限。"""
    n = len(c)
    valid = np.where(np.isfinite(ma200))[0]
    if len(valid) < 60:
        return None, False, None
    first = int(valid[0])
    below = (rc[first:] < ma200[first:]).astype(float)
    sustained = np.convolve(below, np.ones(20), mode="full")[:len(below)] >= 10
    idx = np.where(sustained)[0]
    known = len(idx) > 0
    s = first + int(idx[-1]) if known else first
    run_max, last_hi, trough, done = c[s], s, c[s], 0
    first_bar = None                                      # 第一個底部完成、再創新高的那根 K 棒
    for i in range(s + 1, n):
        if c[i] > run_max:
            if (i - last_hi) >= 20 and (run_max - trough) / run_max >= 0.08:
                done += 1
                if done == 1:
                    first_bar = i
            run_max, last_hi, trough = c[i], i, c[i]
        elif c[i] < trough:
            trough = c[i]
    in_base = (n - 1 - last_hi) >= 10 and (run_max - trough) / run_max >= 0.05
    return done + (1 if in_base else 0), known, first_bar


def pe_text(pe, known):
    """本益比擴張的提示文字。known=False 表示第二階段起點早於資料範圍，只能和資料內最早的底部比。"""
    ref = "第一個底部" if known else f"近 {HISTORY_MONTHS // 12} 年最早的底部"
    return (f"⚠️ 本益比約 {pe['then']:.0f}→{pe['now']:.0f} 倍，較{ref}擴張 {pe['ratio']:.1f} 倍"
            f"（②p.233：超過一倍通常已到上漲後段）")


def pe_expansion(eps_hist, d_then, p_then, p_now, last_split=None, d_now=None):
    """②p.232-235：比較「現在」和「這波漲勢第一個底部當時」的本益比。本益比擴張一倍以上，代表股價漲得比盈餘快很多，
    通常已到上漲後段。eps_hist = [(財報公布日, 當季 EPS), ...] 由新到舊（Yahoo 的 Reported EPS，多為調整後 EPS；
    前後用同一種口徑，所以倍數可以比，但絕對值和看盤軟體的本益比不一定相同）。
    本益比 = 股價 ÷ 該日之前最近四季 EPS 合計。
    回傳 {"then", "now", "ratio"}；資料不足或可疑時回 {"skip": 原因}（診斷用）；缺必要參數回 None。"""
    if d_then is None or not p_then or not p_now:
        return None
    if not eps_hist or len(eps_hist) < 5:
        return {"skip": "no_eps"}
    def day(x):
        x = pd.Timestamp(x)
        return (x.tz_localize(None) if x.tzinfo is not None else x).normalize()

    d_now = pd.Timestamp.now().normalize() if d_now is None else day(d_now)
    d_then = day(d_then)
    # 期間內有分割／配股時，股價已還原、但歷史 EPS 是否還原無法確認 → 寧可不算，避免假警報
    if last_split is not None and day(last_split) >= d_then - pd.Timedelta(days=460):
        return {"skip": "split"}

    def ttm(asof):
        xs = sorted(((day(d), float(e)) for d, e in eps_hist if day(d) <= asof), key=lambda x: x[0], reverse=True)[:4]
        if len(xs) < 4 or (asof - xs[0][0]).days > 135:                    # 最新一季不能太舊
            return None
        if any(not (45 <= (xs[i][0] - xs[i + 1][0]).days <= 135) for i in range(3)):   # 四季要連續（中間缺一季就不算）
            return None
        return sum(e for _, e in xs)

    e_now, e_then = ttm(d_now), ttm(d_then)
    if e_now is None or e_then is None:
        return {"skip": "no_eps"}
    if e_now <= 0 or e_then <= 0:
        return {"skip": "loss"}                                # 當時或現在虧損，本益比沒有意義
    pe_now, pe_then = p_now / e_now, p_then / e_then
    ratio = pe_now / pe_then
    if not (pe_then <= 1000 and pe_now <= 1000 and 0.1 <= ratio <= 10):   # 太離譜多半是資料問題
        return {"skip": "odd"}
    return {"then": float(pe_then), "now": float(pe_now), "ratio": float(ratio)}


# ==========================================
# 8. Telegram
# ==========================================
def _ents(t):
    """Telegram 每則最多 100 個格式標記，超過的粗體會失效"""
    return t.count("<b>") + t.count("<i>") + t.count("<code>")


def _split_message(message, max_len=3900, max_ents=95):
    """單則上限 4096 字、100 個標記。每個標記都在同一行內開關，所以按行切不會切壞 HTML。"""
    if len(message) <= max_len and _ents(message) <= max_ents:
        return [message]
    parts, cur = [], ""
    for line in message.split("\n"):
        add = line + "\n"
        if cur.strip() and (len(cur) + len(add) > max_len or _ents(cur) + _ents(add) > max_ents):
            parts.append(cur)
            cur = ""
        cur += add
    if cur.strip():
        parts.append(cur)
    return parts


def send_telegram_msg(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for part in _split_message(message):
        try:
            r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": part, "parse_mode": "HTML"}, timeout=15)
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
    ents = _ents
    pages, cur = [], ""
    for b in blocks:
        if cur and (len(cur) + len(b) + 2 > 3200 or ents(cur) + ents(b) > 85):
            pages.append(cur)
            cur = ""
        cur += b + "\n\n"
    if cur:
        pages.append(cur)
    first_prefix = holdings_msg + header + "\n"
    if len(first_prefix) + len(pages[0]) > 4000 or ents(first_prefix) + ents(pages[0]) > 95:
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
LAST_DIAG = []      # 最近一次分析的診斷文字（SEND_DIAGNOSTICS 開著時會多傳一則到 Telegram）


# ==========================================
# 10. 主引擎
# ==========================================
def _extract(data, sym, single):
    if single and not isinstance(data.columns, pd.MultiIndex):
        return data.copy()
    return data[sym].copy()


def analyze_market(stock_dict, meta, tw_open, uni_info, tstats=None):
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
    # ②p.86「結果為基礎」：停損不該超過自己平均獲利的一半。交易紀錄夠多才啟用。
    rba_cap = None
    if tstats and tstats["n"] >= TRADE_STATS_MIN_N and tstats["avg_gain"] > 0:
        half = max(MIN_STOP_PCT, tstats["avg_gain"] / 2.0)
        if half < MAX_STOP_PCT:
            rba_cap = half
            stop_cap = {mkt: min(x, half) for mkt, x in stop_cap.items()}

    keys = ("brk", "recent", "star", "squat", "cheat", "power", "primary", "pb")
    cats = {k: {"tw": [], "us": []} for k in keys}
    momentum = []
    stats = defaultdict(int, {"scanned": len(raw_data)})
    nh, nl = defaultdict(int), defaultdict(int)
    grp_total = {"tw": defaultdict(int), "us": defaultdict(int)}
    grp_pass = {"tw": defaultdict(list), "us": defaultdict(list)}
    grp_nh = {"tw": defaultdict(int), "us": defaultdict(int)}
    pending = []     # 需要產業群資訊的卡片，掃完後再補
    near_miss = []   # 診斷用
    pe_ctx = {}      # 代號 → (第一個底部完成日, 當時股價, 現在股價, 起點是否確定, 最後一根 K 棒日期)
    pe_diag = {"tw": defaultdict(int), "us": defaultdict(int)}   # 算得出來 ok／缺 EPS no_eps／期間有分割 split／虧損 loss／可疑 odd
    diag = []

    def dprint(x):
        print(x)
        diag.append(x)

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
            if (h[-10:].max() - l[-10:].min()) / c_now < 0.025 and float((c[-60:] / c[-61:-1]).max()) >= 1.20:
                stats["pinned"] += 1
                continue                                   # 自訂：大漲一根後釘在 2.5% 內，多半是已宣布被收購
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

            cap = stop_cap[mkt]

            def risk_line(entry, danger_low):
                sp, pct, capped = plan_stop(entry, danger_low, cap)
                pos = position_pct(pct, mkt)
                pos_note = f"部位≤{pos:.3g}%" + ("(跌停風險)" if is_tw and pos < MAX_POSITION_PCT else "")
                return f"🛡️停損 <b>{sp:.2f}</b> (-{pct:.1f}%{'上限' if capped else ''})｜{pos_note}"

            head = f"<b>{title}</b>｜現價 <b>{c_now:.2f}</b>｜RS <b>{rs}</b>"

            # 出貨跡象（p.99-100、②p.242）
            diff40 = np.diff(c[-41:])
            big40 = v[-40:] > v50[-40:]
            up_days = int((big40 & (diff40 > 0)).sum())
            dn_days = int((big40 & (diff40 < 0)).sum())
            imax = n - 60 + int(np.argmax(v[-60:]))
            s3 = stage3_flags(c, v, v50, recent=15)
            d_rules = {
                "d_today_heavy_down": c_now < c_prev * 0.985 and vol_ratio >= 1.35,  # 今天帶量下跌
                "d_more_down_days": dn_days > up_days + 1,                         # 大量下跌日明顯較多
                "d_stage3": s3 is not None,                                        # 一年來最大跌幅／最大量下跌
            }
            distribution = any(d_rules.values())
            maxvol_down = bool(c[imax] < c[imax - 1] * 0.98)   # 近 60 日最大量那天收跌：書上沒有這條，只標示與扣分，不剔除

            # ---------- p.298 強勁表現型態（書中明言不要求基本面，但要守供需法則）----------
            pp = detect_power_play(h, l, c, v, n)
            in_power = False
            if pp and c_now > ma50[-1] and c_now <= pp["pivot"] * (1 + CHASE_LIMIT_PCT / 100) and not distribution:
                state = "🚀突破" if (c_now > pp["pivot"] and vol_ok) else "等突破"
                cats["power"][mkt].append((rs + pp["gain"] / 10, symbol, (
                    f"{head}\n"
                    f"  ⚡ <b>{state}｜樞紐 {pp['pivot']:.2f}</b>{lock_note}\n"
                    f"  {risk_line(max(c_now, pp['pivot']), pp['low'])}\n"
                    f"  8週漲 {pp['gain']:.0f}%｜整理 {pp['days']}天 修正 {pp['corr']:.0f}%｜緊密 {pp['tight']:.0f}%"
                ), c_now))
                in_power = True

            long_ok = (np.isfinite(ma200[-1 - MA200_UP_DAYS]) and c_now > ma200[-1]
                       and ma200[-1] > ma200[-1 - MA200_UP_DAYS] and rs >= RS_MIN and c_now >= lo52 * LOW52_MULT)

            # 大盤對齊（修正幅度比較、RS 線）
            m = None
            if mkt in indices:
                m = indices[mkt].reindex(df.index.normalize(), method="ffill").bfill().values.astype(float)
                if not np.isfinite(m).all():
                    m = None

            # ---------- p.286-290 3C：只要求站在上升的 200 日線之上 ----------
            cheat_item = None
            if long_ok:
                ch = detect_cheat(h, l, c, v, v50, n - 1, m)
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
                        if ch.get("rel_depth") and ch["rel_depth"] > REL_DEPTH_WARN:
                            extra += f"｜⚠️修正為大盤{ch['rel_depth']:.1f}倍"
                        # ②p.202：停頓區在杯子下 1/3 是「低位作弊」，風險較高，書中只先建一部分；中間 1/3 才是 3C
                        kind = "低位作弊(位置低、風險較高，書中只先建部分)" if ch["recov"] < 100.0 / 3 else "3C"
                        cheat_item = (rs + (15 if ch["dry"] else 0) + (5 if ch["shake"] else 0) - (5 if ch["recov"] < 100.0 / 3 else 0)
                                      - (15 if (ch.get("rel_depth") or 0) > REL_DEPTH_WARN else 0), symbol, (
                            f"{head}\n"
                            f"  🥄 <b>{state}</b>{lock_note}\n"
                            f"  {risk_line(max(c_now, ch['pivot']), ch['pause_low'])}\n"
                            f"  {kind}｜杯 {ch['weeks']}週 深{ch['depth']:.0f}%・已收復{ch['recov']:.0f}%・停頓{ch['pause_days']}天 幅度{ch['pause_range']:.0f}%{extra}"
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
            for dk, dv in d_rules.items():
                stats[dk] += int(bool(dv))
            stats["w_maxvol_down"] += int(maxvol_down)
            grp_pass[mkt][ind].append((rs, symbol))

            if is_red and vol_ok and c_now >= h[-20:-1].max() and not distribution:
                momentum.append((rs + int(min(vol_ratio, 10) * 10), symbol, head + ("｜🔒漲停鎖住" if locked_up else ""), c_now, vol_ratio, vol_label))
            if distribution:
                continue

            ma200_up_4m = n > 290 and ma200[-1] > ma200[-44] > ma200[-88]
            score = rs + (15 if rs >= 85 else 0) + (10 if ma200_up_4m else 0) + (15 if c_now >= lo52 * 2 else 0)
            score += max(0, up_days - dn_days) * 4

            tags = [f"大量漲跌日 <b>{up_days}:{dn_days}</b>"]
            if maxvol_down:
                tags.append("⚠️近60日最大量是下跌日")
                score -= 10
            # 底部計數（p.108-110、②p.229-232）：第 1~2 個最好，第 3~4 個偏晚，第 5 個以後失敗率高
            nb, nb_known, first_bar = count_bases(c, rc, ma200)
            if first_bar is not None:
                pe_ctx[symbol] = (df.index[first_bar], float(rc[first_bar]), float(rc[-1]), nb_known, df.index[-1])
            if nb and (nb_known or nb >= 3):
                pre = '' if nb_known else '≥'
                if nb >= 5:
                    tags.append(f"⚠️第{pre}{nb}底(失敗率高)")
                    score -= 25
                elif nb == 4:
                    tags.append(f"⚠️第{pre}4底(後期)")
                    score -= 15
                elif nb == 3:
                    tags.append(f"第{pre}3底(偏晚)")
                else:
                    tags.append(f"第{nb}底")
                    score += 5
            # RS 線（個股÷大盤）：領先創高最好；②p.157 希望已上升 6~13 週、不該明顯走低
            rs_tag = ""
            if m is not None:
                rsl = c / m
                if rsl[-1] >= rsl[-252:].max() * 0.999:
                    rs_tag = "RS線領先創高" if c_now < hi52 * 0.98 else "RS線新高"
                    score += 8 if c_now < hi52 * 0.98 else 4
                elif rsl[-1] > rsl[-31] and rsl[-1] > rsl[-66]:
                    rs_tag = "RS線升13週"
                    score += 3
                elif rsl[-1] > rsl[-31]:
                    rs_tag = "RS線升6週"
                    score += 1
                elif rsl[-1] < rsl[-31] and rsl[-1] < rsl[-66]:
                    rs_tag = "⚠️RS線走低(②p.157)"
                    score -= 8
                    stats["w_rsline_down"] += 1

            def card(action, fp, pivot, extra_score=0, dry=False, show_risk=True, note="", entry=None):
                t = list(tags)
                if dry:
                    t.insert(0, "💧💧底部最低量" if fp.get("dry_extreme") else "💧量縮")
                if fp.get("shakeouts"):
                    t.append(f"洗盤{fp['shakeouts']}次")
                if fp.get("poke"):
                    t.append("曾試探舊高")
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
                    text += f"  {risk_line(entry or pivot, fp.get('danger_low'))}\n"
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

            def ft_note(ft):
                """②p.42-56：突破後的跟進買盤與違例 → (卡片上的一行, 加減分)"""
                if not ft:
                    return "", 0
                txt = f"突破後{ft['days']}天 {ft['up']}漲{ft['dn']}跌"
                if ft["pull"] is not None:
                    txt += "｜回檔量縮✅" if ft["pull"] < 0.8 else ("｜⚠️回檔量大於上漲量" if ft["pull"] > 1.1 else "")
                if ft["viol"]:
                    txt += f"｜⚠️違例：{'、'.join(ft['viol'])}"
                return txt, (5 if ft["strong"] else 0) - 6 * len(ft["viol"])

            # ---------- 第三層：近 10 日內有沒有過「有效突破」（樞紐固定在突破當時）----------
            # 先看這層：已經突破的股票，接下來幾天形成的小高點不算新樞紐，不能拿來當追價的理由。
            placed, fail_note = False, ""
            bo_stage = 0     # 診斷：近 10 日「收盤越過底部高點」最遠走到哪一關（1 底部未成型 2 未量縮 3 量不足 4 有效突破）
            for kk in range(POST_BREAKOUT_DAYS, 0, -1):
                b = n - 1 - kk
                fpb = analyze_base(h, l, c, v, v50, atr_pct, b, m)
                if not fpb:
                    continue
                ref_b = fpb["pivot"] if fpb["ready"] else fpb["base_top"]
                if not (c[b] > ref_b and c[b - 1] <= ref_b):
                    continue
                b_locked = bool(lim and (c[b] / c[b - 1] - 1) * 100 >= lim - 0.7 and abs(h[b] - c[b]) < 1e-9)
                vol_b = bool(v50[b - 1] > 0 and (v[b] >= BREAKOUT_VOL_MULT * v50[b - 1] or b_locked))
                stage = 1 if not fpb["ready"] else (2 if not fpb["dryup"] else (3 if not vol_b else 4))
                bo_stage = max(bo_stage, stage)
                if stage < 4:
                    continue
                pv = fpb["pivot"]
                dist = (c_now / pv - 1) * 100.0
                dipped = bool((c[b + 1:n - 1] < pv).any()) if kk > 1 else False
                stop_b = plan_stop(pv, fpb["danger_low"], cap)[0]
                note, ft_score = ft_note(follow_through(h, l, c, v, v50, rc, ma20, ma50, b, pv))
                if c_now > pv * (1 + CHASE_LIMIT_PCT / 100):
                    placed = True                                   # 已延伸，不追價
                    stats["x_extended_after_breakout"] += 1
                elif c_now >= pv:
                    if dipped and c_now > c_prev:
                        add("squat", card(f"🔄 <b>反轉回升：重新站上樞紐 {pv:.2f}</b> ({kk}天前突破)", fpb, pv, 15 + ft_score, True, note=note, entry=c_now))
                    else:
                        add("recent", card(f"✅ <b>{kk}天前突破 {pv:.2f}</b>，現高於樞紐 {dist:.1f}% (仍在買進區)", fpb, pv, 10 + ft_score, True, note=note, entry=c_now))
                    placed = True
                elif c_now > stop_b and c_now >= ma20[-1]:
                    add("squat", card(f"🔄 <b>蹲下 squat：跌回樞紐 {pv:.2f} 下方</b> ({kk}天前突破)｜守 20MA {ma20[-1]:.2f}", fpb, pv, 5 + ft_score, True, note=note))
                    placed = True
                else:
                    fail_note = f"⚠️ {kk}天前突破 {pv:.2f} 失敗，等重設樞紐 (p.295)"
                    continue                                        # 失敗後可能重設樞紐再突破一次 → 繼續往後看
                break

            stats[f"bo_{bo_stage}"] += 1

            # ---------- 第四層：截至昨天的底部 → 今天是否突破、樞紐是否已成型 ----------
            fp = analyze_base(h, l, c, v, v50, atr_pct, n - 1, m)
            stats["f1_no_distribution"] += 1
            stats["f1b_recent_breakout_handled"] += int(placed)
            if fp is None:
                stats["none_" + _BASE_REJECT] += 1
            else:
                stats["f2_has_base"] += 1
                stats["f2b_via_poke"] += int(fp["poke"])
                if fp["ready"]:
                    stats["f3_pivot_ready"] += 1
                    near = -STAR_MAX_DIST <= (c_now / fp["pivot"] - 1) * 100.0 <= CHASE_LIMIT_PCT
                    stats["f4_ready_near_pivot"] += int(near)
                    stats["f5_ready_near_dry"] += int(near and fp["dryup"])
                else:
                    stats["nr_" + fp["why_not"]] += 1
            if not placed and fp and fp["ready"]:
                pivot = fp["pivot"]
                dist = (c_now / pivot - 1) * 100.0
                if c_now > pivot and c_prev <= pivot:
                    if dist <= CHASE_LIMIT_PCT and is_red and vol_ok and fp["dryup"]:
                        add("brk", card(
                            f"🚀 <b>突破樞紐 {pivot:.2f}</b> ({vol_label}{vol_ratio:.1f}倍)｜追價上限 {pivot * (1 + CHASE_LIMIT_PCT / 100):.2f}",
                            fp, pivot, 30, True, note=fail_note, entry=c_now))
                    elif dist <= CHASE_LIMIT_PCT:
                        why = "量不足" if not vol_ok else ("突破前未量縮" if not fp["dryup"] else "收盤偏低")
                        add("star", card(f"⚠️ <b>越過樞紐 {pivot:.2f} 但{why}</b> ({vol_label}{vol_ratio:.1f}倍)", fp, pivot, 5, fp["dryup"],
                                         note=fail_note, entry=c_now))
                    placed = True
                elif c_now <= pivot and dist >= -STAR_MAX_DIST and fp["dryup"] and c_now >= ma20[-1] * 0.99:
                    mark = "★◎" if fp["final"] <= 5.0 else "★"
                    add("star", card(f"🎯 <b>{mark} 樞紐 {pivot:.2f}</b> (差 {abs(dist):.1f}%)｜突破需量 ≥{BREAKOUT_VOL_MULT}倍", fp, pivot, 20, True,
                                     note=fail_note))
                    placed = True

            # ---------- 3C：位置較低的早期進場點 ----------
            if not placed and cheat_item:
                add("cheat", cheat_item)
                placed = True

            # ---------- 第五層：pb 觀察池（還不是買點，所以不顯示停損與部位）----------
            if not placed and not in_power and fp and (fp["orderly"] or fail_note) and not fp["compressed"]:
                ref = fp["pivot"] if fp["ready"] else fp["base_top"]
                dist = (c_now / ref - 1) * 100.0
                if -PB_MAX_DIST <= dist <= 0:
                    need = []
                    if not fp["ready"]:
                        need.append("收縮未完成")
                    if not fp["dryup"]:
                        need.append("量未縮")
                    add("pb", card(f"📌 <b>pb｜參考高點 {ref:.2f}</b> (差 {abs(dist):.1f}%)｜{'、'.join(need) or '等靠近樞紐'}",
                                   fp, ref, -int(abs(dist)), fp["dryup"], show_risk=False, note=fail_note))

            # 診斷用：有底部、離參考高點不遠、卻沒進一~五類的，記下原因（只印在 log）
            if fp and not placed:
                ref = fp["pivot"] if fp["ready"] else fp["base_top"]
                dist = (c_now / ref - 1) * 100.0
                if -8.0 <= dist <= CHASE_LIMIT_PCT:
                    if not fp["ready"]:
                        why = NR_TXT.get(fp["why_not"], fp["why_not"])
                    elif dist > 0:
                        why = "已站上樞紐，但近10日沒有帶量的有效突破"
                    elif dist < -STAR_MAX_DIST:
                        why = f"離樞紐超過{STAR_MAX_DIST:.0f}%"
                    elif not fp["dryup"]:
                        why = "量未縮"
                    else:
                        why = "低於20MA"
                    ds = fp["depth_str"].split("→")
                    ds = "→".join(ds if len(ds) <= 8 else ds[:3] + ["…"] + ds[-4:])
                    near_miss.append((abs(dist), f"{symbol:<9} {fp['footprint']} ({ds}) 參考{ref:.2f} 差{dist:+.1f}%"
                                                 f" 量比5日{fp['vol5']:.2f}/最低{fp['volmin']:.2f} → {why}"))
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
            sp, stop_pct, _ = plan_stop(max(c_now, pb["pivot"]), pb["low"], stop_cap[mkt])
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
                f"  🛡️停損 <b>{sp:.2f}</b> (-{stop_pct:.1f}%)｜部位≤{pos:.3g}%\n"
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
        rows = [(len(lst) / grp_total[mkt][ind], len(lst), ind) for ind, lst in grp_pass[mkt].items()
                if ind and len(lst) >= 3 and grp_total[mkt][ind] >= 8]
        rows.sort(reverse=True)
        if rows:
            grp_line[mkt] = "、".join(f"{html.escape(str(ind)[:26])} {cnt}/{grp_total[mkt][ind]}" for _, cnt, ind in rows[:5])
        for ind, lst in grp_pass[mkt].items():
            for pos_i, (_, sym) in enumerate(sorted(lst, reverse=True), 1):
                grp_rank[sym] = (pos_i, len(lst))
    extra_line = {}
    this_year = datetime.now().year
    for key, mkt, sym, ind, rs, rs_tag in pending:
        bits = []
        if ind and sym in grp_rank:
            pos_i, cnt = grp_rank[sym]
            bits.append(f"🏭{html.escape(str(ind)[:26])}(過樣板{cnt}檔｜群內RS第{pos_i})")
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

    def pe_of(sym, f, count=False):
        """本益比相對第一個底部當時擴張了幾倍；只有已經完成過至少一個底部的股票才需要算。"""
        if sym not in pe_ctx:
            return None
        d_then, p_then, p_now, _, d_now = pe_ctx[sym]
        try:
            pe = pe_expansion(f.get("eps_hist"), d_then, p_then, p_now, f.get("last_split"), d_now)
        except Exception:                                 # [pe-guard] 算不出來不能讓整份日報失敗
            pe = {"skip": "odd"}
        if pe is None:
            return None
        if count:
            pe_diag[mkt_of(sym)][pe.get("skip", "ok")] += 1
        return None if "skip" in pe else pe

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
                pe = pe_of(sym, f)
                pe_pen = 10 if (pe and pe["ratio"] >= PE_EXPANSION_WARN) else 0
                ranked.append((s + f["score"] + grp_bonus - pe_pen, sym, text, px))
            ranked.sort(key=lambda x: x[0], reverse=True)
            for s, sym, text, px in ranked[:lim]:
                f = get_fundamentals(sym, px, tw_open.get(sym), deep=deep)
                listed.add(sym)
                earn = ""
                ed = f.get("earn_days")
                if key != "pb" and ed is not None and 0 <= ed <= EARNINGS_WARN_DAYS:
                    earn = f"\n  📅 財報{'今天' if ed == 0 else f'約 {ed} 天後'}（②p.143：沒有 {EARNINGS_CUSHION_PCT:.0f}% 獲利緩衝不抱大部位過財報）"
                pe = pe_of(sym, f, count=True)
                if pe and pe["ratio"] >= PE_EXPANSION_WARN:
                    earn += "\n  " + pe_text(pe, pe_ctx[sym][3])
                out.append(text + extra_line.get((key, sym), "") + "\n  💰 " + fund_text(f) + earn)
        return out

    dprint("🔎 漏斗：" + "｜".join(f"{k}={stats[k]}" for k in ("scanned", "liquid", "pinned", "template", "f1_no_distribution",
          "f1b_recent_breakout_handled", "x_extended_after_breakout", "f2_has_base", "f2b_via_poke", "f3_pivot_ready",
          "f4_ready_near_pivot", "f5_ready_near_dry")))
    dprint("🔎 無底部原因：" + "｜".join(f"{k[5:]}={v}" for k, v in sorted(stats.items()) if k.startswith("none_")))
    dprint("🔎 有底部但樞紐未成型：" + "｜".join(f"{k[3:]}={v}" for k, v in sorted(stats.items()) if k.startswith("nr_")))
    dprint("🔎 出貨濾網(可重複)：" + "｜".join(f"{k[2:]}={v}" for k, v in sorted(stats.items()) if k.startswith("d_"))
           + f"｜僅警示不剔除：maxvol_down={stats['w_maxvol_down']}｜rsline_down={stats['w_rsline_down']}")
    dprint(f"🔎 近10日收盤越過底部高點，最遠走到：沒有越過={stats['bo_0']}｜底部未成型={stats['bo_1']}｜成型但未量縮={stats['bo_2']}"
           f"｜量縮但突破量不足={stats['bo_3']}｜有效突破={stats['bo_4']}")
    if near_miss:
        near_miss.sort(key=lambda x: x[0])
        dprint(f"🔎 差一步名單（有底部、離參考高點 8% 內、沒進一~五類；共 {len(near_miss)} 檔，列最近的 25 檔）：")
        for _, txt in near_miss[:25]:
            dprint("· " + txt)
    print("💰 產生日報...")
    sections = [
        ("🚀 <b>【一、今日帶量突破樞紐】</b>", render("brk", True)),
        ("✅ <b>【二、近日已突破、仍在買進區】</b>", render("recent", True)),
        ("★ <b>【三、樞紐成型＋量縮（等突破，勿預先卡位 p.270）】</b>", render("star", True)),
        ("🔄 <b>【四、蹲下 Squat／反轉回升 (p.271)】</b>", render("squat", True)),
        ("🥄 <b>【五、3C／低位作弊 (p.286、②p.198)】</b>", render("cheat", True)),
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

    holdings_msg = check_my_holdings(raw_data) + trade_stats_text(tstats, rba_cap)
    dprint("🔎 本益比擴張（卡片中已完成過底部的）：" + "｜".join(
        f"{MARKET_CFG[k]['name']} 算得出={pe_diag[k]['ok']} 缺EPS={pe_diag[k]['no_eps']} 期間有分割={pe_diag[k]['split']}"
        f" 虧損={pe_diag[k]['loss']} 可疑={pe_diag[k]['odd']}" for k in ("us", "tw")))
    global LAST_DIAG
    LAST_DIAG = diag

    # ---------- 表頭 ----------
    mk = []
    for mkt, cfg in MARKET_CFG.items():
        mk.append(f"{cfg['name']}{REGIME_TXT[regime[mkt]]} 新高{nh[mkt]}/新低{nl[mkt]}")
    header = (
        f"📊 <b>【《超級績效》SEPA 選股日報】</b>\n"
        f"<i>台股 {uni_info['tw_n']}｜美股 {uni_info['us_n']}（{html.escape(uni_info['us_source'])}）</i>\n"
        f"<i>有 K 線 {stats['scanned']}｜過流動性 {stats['liquid']}｜過趨勢樣板 {stats['template']}</i>\n"
        f"大盤：{'｜'.join(mk)}\n"
    )
    for mkt, cfg in MARKET_CFG.items():
        if regime[mkt] == "bear":
            header += f"⚠️ {cfg['name']}空頭：書中做法是降低曝險、停損縮到 5~6% (p.364、②p.83)，卡片停損上限已改為 {min(TOUGH_MARKET_STOP_PCT, stop_cap[mkt]):.0f}%\n"
        if mkt in grp_line:
            header += f"🏭 {cfg['name']}領導產業(過樣板/總數)：{grp_line[mkt]}\n"
    return holdings_msg, header, sections


if __name__ == "__main__":
    stock_dict, meta, uni_info = get_entire_market_universe()
    tw_open = fetch_tw_open_data()
    holdings_msg, header, sections = analyze_market(stock_dict, meta, tw_open, uni_info, load_trade_stats())
    send_reports(holdings_msg, header, sections)
    if SEND_DIAGNOSTICS and LAST_DIAG:
        send_telegram_msg("🔧 診斷（調整期間用，整則轉貼或截圖給我即可）\n" + html.escape("\n".join(LAST_DIAG)))