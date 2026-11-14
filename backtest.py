# -*- coding: utf-8 -*-
"""
回測（驗證用，手動執行，不是每天跑）
=====================================
目的：用過去幾年的「真實」K 線，檢查 scanner.py 的型態判斷準不準。

做法：把每一檔股票的歷史一天一天重放。站在第 t 天收盤時，只拿第 t 天（含）以前的資料，
      用 scanner.py 同一組函式判斷「今天有沒有突破、底部算不算成型」，把訊號記下來；
      然後才去看第 t+1 天以後實際怎麼走，計算損益。

兩種買法都測：
  甲「隔天開盤買」：日報收盤後才送出 → 看到「今日突破」後，隔天開盤買進。（第一次回測只測了這種）
  乙「預先掛單」  ：前一晚日報列為 ★（樞紐成型＋量縮、還沒突破）的股票，隔天盤中一超過樞紐價就買進
                    ——這才是書上的買點（p.266-270）。成交價＝樞紐價與開盤價取高者，再加 0.1% 滑價。
交易假設：
  ・買進價已經高過樞紐 5%（追價上限）就放棄；台股開盤即一字鎖漲停，視為買不到。
  ・停損用 scanner.plan_stop（危險點下方，3%~8%；大盤空頭時上限 6%）。
  ・沒碰到停損就抱滿 20 個交易日後收盤賣出（另外統計 60 個交易日）。
  ・已扣 scanner.MARKET_CFG 裡的交易成本。

比較的組別：
  S ★ 預先掛單、盤中碰到樞紐就買（買法乙）；S0 同 S 但突破前沒有量縮；S2 用「新讀法」認定的 ★
  A 有效突破（底部成型＋量縮＋帶量）＝日報第一類，隔天開盤買（買法甲）
  B 成型＋量縮，但突破量不足        C 成型，但突破前沒量縮
  D 底部「未成型」就站上底部高點（目前被程式淘汰的）
  E 「新讀法」才算成型的有效突破（見 v2_read；目前被程式淘汰）
  F 對照組：任何一天買進過趨勢樣板的股票
  G 型態同 A，但沒過趨勢樣板          H 3C 突破停頓區

第 3 題（只有美股）：線型之外再加上基本面——訊號出現「當時已經公布」的最近一季 EPS 年增率是否 ≥ 20%
  （書上的最低門檻 p.161；判斷方式與 scanner.get_fundamentals 的 meets_min 相同）。
  資料來自 Yahoo 的歷次財報公布日與當時公布的 EPS，所以不會用到訊號之後才公布的財報。
  營收與利潤率拿不到歷史資料，這一題只驗證 EPS。台股的歷史 EPS 不齊，不測。

第 4 題：買進之後照《超級績效 2》的規則處理（規則在看結果之前就定好，不事後調整）：
  (1) 起始停損同前（危險點下方，上限 8%）。
  (2) 漲到停損幅度的 3 倍：賣掉一半，剩下一半的停損移到成本價（②p.89-91、p.138-141、p.246 免費參賽）。
  (3) 50 日線升到成本價之上以後，收盤跌破 50 日線 → 隔天開盤賣出（②p.245-246）。
  (4) 最久抱 120 個交易日（書上沒有這個數字，是為了讓每筆交易都有結束）。
  另外單獨測一條：買進後 10 天內收盤跌破 20 日線就先走（②p.51-52 說這時成功率減半）。
  「順不順」：最近 10 筆已經結束的同類交易有一半以上賺錢＝順（②p.83、p.133-136 由小而大）。

限制（解讀時要記得）：
  ・股票池是「現在」還上市、而且現在成交量夠大的股票 → 已下市的不在裡面，整體報酬會偏樂觀；
    但各組用的是同一批股票，組與組之間的比較仍有意義。
  ・過去有效不代表未來有效。
本程式只是研究工具，不構成投資建議。
"""
import os
import io
import re
import time
import html
import contextlib
import traceback
import zlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests

import scanner as S

# ==========================================
# 設定
# ==========================================
try:
    BT_MONTHS = max(30, int(os.environ.get("BT_MONTHS") or 60))   # 下載幾個月的歷史（60 個月 ≈ 可重放 3.7 年）
except ValueError:
    BT_MONTHS = 60
MIN_HISTORY = 330            # 前面至少要有這麼多根 K 棒才開始重放（底部最長往回看 325 根）
HOLD_A, HOLD_B = 20, 60      # 持有天數（交易日）
TARGET_PCT = 20.0            # 「先漲到 +20% 還是先停損」
BASELINE_EVERY = 5           # 對照組：每檔股票每 5 個交易日取樣一次
DEDUP_BARS = 10              # 同一檔股票在同一組內，10 個交易日內的重複訊號只算第一次
MIN_N = 30                   # 筆數少於這個數字就標示「樣本太少」
BUY_STOP_SLIP_PCT = 0.1      # 預先掛單（觸價買進）的滑價假設
BOOK_BE_MULT = 3.0           # 漲到停損幅度的幾倍：賣一半、其餘停損移到成本（②p.89-91、p.246）
BOOK_MAX_HOLD = 120          # 自訂：最久抱幾個交易日
BOOK_EARLY_BARS = 10         # 另一組規則：買進後幾天內收盤跌破 20 日線就先走（②p.51-52）
FEEDBACK_N = 10              # 「順不順」看最近幾筆已結束的交易
QUARTER = 0.25               # 不順時只下 1/4（②p.133）
EPS_LIMIT = 60               # 每檔抓幾筆財報公布紀錄
EPS_WORKERS = 4              # 同時抓幾檔（太多會被 Yahoo 擋）
EPS_BUDGET_SEC = 1500        # 抓財報最多花多久；超過就用已經抓到的繼續
FUND_CHECK_N = 30            # 基本面自我檢查抽幾檔
EVENTS_FILE = "backtest_events.zip"       # 裡面是一個 CSV
DEBUG = os.environ.get("BT_DEBUG", "") == "1"            # 測試用：遇到例外直接丟出，不吞掉

NEUTRAL_FUND = {"line": "", "badges": [], "warn": [], "score": 0, "meets_min": None, "strong": False,
                "earn_days": None, "eps_hist": [], "last_split": None}


# ==========================================
# 1. 下載
# ==========================================
def _parse_batch(data, batch, raw, splits):
    n_ok = 0
    for sym in batch:
        try:
            df = S._extract(data, sym, len(batch) == 1)
            if 'Stock Splits' in df.columns:
                sp = df['Stock Splits'].dropna()
                sp = sp[sp > 0]
                if len(sp):
                    ix = pd.DatetimeIndex(sp.index)
                    if ix.tz is not None:
                        ix = ix.tz_localize(None)
                    splits[sym] = [(d.normalize(), float(r)) for d, r in zip(ix, sp.values)]
            cols = [x for x in ['Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume'] if x in df.columns]
            df = df[cols].dropna(subset=['Open', 'High', 'Low', 'Close', 'Volume'])
            df = df[df['Volume'] > 0]
            if getattr(df.index, "tz", None) is not None:
                df.index = df.index.tz_localize(None)
            if len(df) >= 252:
                raw[sym] = df
                n_ok += 1
        except Exception:
            continue
    return n_ok


def download_prices(symbols, start_date, batch_size=180):
    """與 scanner.analyze_market 相同的下載與清理方式；只留下至少 252 根 K 棒的股票。
    另外多要「股票分割」紀錄（第 3 題比較 EPS 年增率時要用）；要不到就退回原本的下載方式。回傳 (K 線, 分割紀錄)。"""
    raw, splits, use_actions = {}, {}, True
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        n_ok = 0
        for attempt in ((True, False) if use_actions else (False,)):
            try:
                kw = {"actions": True} if attempt else {}
                data = S.yf.download(batch, start=start_date, auto_adjust=False, group_by="ticker", threads=True, progress=False, **kw)
                n_ok = _parse_batch(data, batch, raw, splits)
            except Exception as e:
                print(f"批次下載略過: {e}")
                n_ok = 0
            if n_ok or not attempt:
                break
            use_actions = False                          # 帶 actions 抓不到東西 → 之後都用原本的方式
        print(f"  已下載 {min(i + batch_size, len(symbols))}/{len(symbols)}，有效 {len(raw)} 檔")
    return raw, splits


# ==========================================
# 1b. 歷次財報（第 3 題）：只留「公布日」與「當時公布的 EPS」
# ==========================================
def parse_earnings(ed):
    """yfinance 的財報公布日表格 → (公布日由舊到新, EPS, 驚喜%)；沒有資料回 None。"""
    if ed is None or not len(ed):
        return None
    ecol = next((x for x in ed.columns if "Reported" in str(x)), None)
    scol = next((x for x in ed.columns if "Surprise" in str(x)), None)
    if ecol is None:
        return None
    rep_ = ed[ed[ecol].notna()]
    if not len(rep_):
        return None
    ix = pd.DatetimeIndex(rep_.index)
    if ix.tz is not None:
        ix = ix.tz_localize(None)
    seen = {}
    sv = rep_[scol].values if scol is not None else [np.nan] * len(rep_)
    for d, e, sp in zip(ix.normalize(), rep_[ecol].values, sv):
        seen.setdefault(d, (float(e), float(sp) if pd.notna(sp) else np.nan))   # 同一天重複的列只留一筆
    items = sorted(seen.items())
    return (np.array([k for k, _ in items], dtype="datetime64[D]"), np.array([v[0] for _, v in items], dtype=float),
            np.array([v[1] for _, v in items], dtype=float))


def fetch_eps(symbols):
    """逐檔抓歷次財報。Yahoo 限流時全部一起暫停、之後放慢；超過時間預算就停，用已經抓到的繼續。回傳 (資料, 統計)。"""
    t0 = time.time()
    out, stat = {}, defaultdict(int)
    throttle = {"pause_until": 0.0, "delay": 0.0}

    def one(sym):
        err = ""
        for k in range(4):
            if time.time() - t0 > EPS_BUDGET_SEC:
                return sym, None, "budget"
            wait = throttle["pause_until"] - time.time()
            if wait > 0:
                time.sleep(wait)
            if throttle["delay"]:
                time.sleep(throttle["delay"])
            try:
                return sym, parse_earnings(S.yf.Ticker(sym).get_earnings_dates(limit=EPS_LIMIT)), ""
            except Exception as e:
                err = type(e).__name__
                if "Rate" in err or "429" in str(e) or "Too Many" in str(e):
                    throttle["pause_until"] = time.time() + 45.0          # 被限流：大家一起停 45 秒
                    throttle["delay"] = min(2.0, throttle["delay"] + 0.5)   # 之後每次請求多等一下
                    stat["throttled"] += 1
                else:
                    time.sleep(1.0 * (k + 1))
        return sym, None, err

    with ThreadPoolExecutor(max_workers=EPS_WORKERS) as ex:
        for i, (sym, hist, err) in enumerate(ex.map(one, symbols), 1):
            if hist is not None and len(hist[0]) >= 5:
                out[sym] = hist
                stat["ok"] += 1
            elif err == "budget":
                stat["budget"] += 1
            elif err:
                stat["error"] += 1
                stat["err_" + err] += 1
            else:
                stat["empty"] += 1
            if i % 300 == 0:
                print(f"  財報 {i}/{len(symbols)}，有資料 {stat['ok']} 檔，{time.time() - t0:.0f} 秒")
    stat["secs"] = int(time.time() - t0)
    return out, dict(stat)


def adjust_for_splits(hist, splits):
    """股價已還原分割，但 Yahoo 的歷史 EPS 有沒有還原並不確定 → 逐次分割用「分割前後兩季的 EPS 是否連續」來判斷：
      看起來沒還原 → 把分割前的 EPS 除以分割比例；看起來已還原 → 不動；分不出來 → 跨越那次分割的年增率不採用。
    回傳 (調整後的資料, 不採用的日期區間, 統計)。"""
    dates, eps, surp = hist
    eps = eps.copy()
    unknown, stat = [], {"raw": 0, "adj": 0, "amb": 0}
    for sd, ratio in sorted(splits or []):
        if not (ratio > 0) or abs(np.log(ratio)) < np.log(1.2):
            continue                                     # 比例很小（例如 5% 配股）對年增率影響不大
        i = int(np.searchsorted(dates, np.datetime64(sd, "D"), side="left"))   # 分割後第一次公布
        if i == 0 or i >= len(dates):
            continue                                     # 分割前或分割後沒有財報 → 不會拿兩邊互比
        eb, ea = eps[i - 1], eps[i]
        verdict = "amb"
        if eb > 0 and ea > 0:
            q, lr = np.log(eb / ea), np.log(ratio)
            if abs(q - lr) + 0.25 < abs(q):
                verdict = "raw"
            elif abs(q) + 0.25 < abs(q - lr):
                verdict = "adj"
        stat[verdict] += 1
        if verdict == "raw":
            eps[:i] = eps[:i] / ratio
        elif verdict == "amb":
            end = dates[i + 4] if i + 4 < len(dates) else np.datetime64("2262-01-01", "D")
            unknown.append((dates[i], end))              # 這段期間「最新一季」和「去年同期」分居分割兩側
    return (dates, eps, surp), unknown, stat


def fund_at(hist, unknown, day, px):
    """站在 day 這一天收盤後，用「當時已經公布」的財報判斷基本面。判斷方式與 scanner.get_fundamentals 的 meets_min 相同，
    另外要求資料夠新、季與季之間連續。cls：ok 達標／fail 未達標或虧損／na 無法比較（轉盈、基期太低）／none 沒有可用資料。"""
    res = {"cls": "none", "g0": np.nan, "accel": False, "beats": 0, "surp0": np.nan}
    if hist is None:
        return res
    dates, eps, surp = hist
    d = np.datetime64(pd.Timestamp(day).normalize(), "D")
    n = int(np.searchsorted(dates, d, side="right"))         # 公布日 ≤ 當天的筆數（盤後公布的，隔天開盤前也已經知道）
    if n < 5:
        return res
    if (d - dates[n - 1]).astype(int) > 135:
        return res                                           # 最新一季太舊：多半是資料中斷
    gaps = np.diff(dates[max(0, n - 7):n]).astype(int)
    if (gaps[-4:] < 45).any() or (gaps[-4:] > 135).any():
        return res                                           # 最近五季不連續
    for a, b in unknown:
        if a <= d < b:
            return res                                       # 分割前後的 EPS 無法確定是否同一基礎
    e = eps[n - 1::-1]                                       # 由新到舊
    eps_now, eps_prev = float(e[0]), float(e[4])
    g = []
    for k in range(min(3, len(e) - 4)):
        if k and not (45 <= gaps[-4 - k] <= 135 if len(gaps) >= 4 + k else False):
            break
        g.append(None if e[k + 4] <= 0 else float((e[k] - e[k + 4]) / abs(e[k + 4]) * 100.0))
    g0 = g[0]
    tiny = bool(px and px > 0 and ((0 <= eps_now / px < 0.001) or (0 < eps_prev / px < 0.0004)))
    meaningful = g0 is not None and eps_now > 0 and not tiny
    if eps_now <= 0:
        res["cls"] = "fail"
    elif meaningful:
        res["cls"] = "ok" if g0 >= S.EPS_MIN_GROWTH else "fail"
    else:
        res["cls"] = "na"
    if g0 is not None:
        res["g0"] = g0
    res["accel"] = bool(len(g) >= 3 and all(x is not None for x in g) and g[0] > g[1] > g[2] and meaningful and g[0] >= S.EPS_MIN_GROWTH)
    s_ = surp[n - 1::-1]
    res["surp0"] = float(s_[0]) if len(s_) else np.nan
    for x in s_:
        if np.isnan(x):
            continue                                         # 與 scanner 相同：沒有數字的季略過
        if x > 0:
            res["beats"] += 1
        else:
            break
    return res


def add_fundamentals(df, eps_data):
    """替每一筆訊號補上「當時」的基本面欄位（只有美股有）。eps_data：{代號: (資料, 不採用區間)}。"""
    if df is None or df.empty:
        return df
    cls, g0, accel, beats = [], [], [], []
    px_col = df["close"].values if "close" in df else np.full(len(df), np.nan)
    for sym, day, px in zip(df["sym"].values, df["date"].values, px_col):
        ent = eps_data.get(sym)
        r = fund_at(ent[0], ent[1], day, px) if ent else {"cls": "none", "g0": np.nan, "accel": False, "beats": 0}
        cls.append(r["cls"]); g0.append(r["g0"]); accel.append(r["accel"]); beats.append(r["beats"])
    return df.assign(f_cls=cls, f_g0=g0, f_accel=accel, f_beats=beats)


# ==========================================
# 2. 每一天「當時」的相對強度與大盤狀態
# ==========================================
def rs_tables(raw):
    """每個交易日、每檔股票當天的 RS 百分位（台股跟台股比、美股跟美股比），算法與 scanner 相同。"""
    out = {}
    for mkt in S.MARKET_CFG:
        cols = {}
        for sym, df in raw.items():
            if S.mkt_of(sym) != mkt:
                continue
            ca = df['Adj Close'] if ('Adj Close' in df.columns and df['Adj Close'].notna().all()) else df['Close']
            x = ca.values.astype(float)
            n = len(x)
            sc = np.full(n, np.nan)
            if n >= 252:
                t = np.arange(251, n)
                with np.errstate(divide='ignore', invalid='ignore'):
                    sc[251:] = (0.4 * (x[t] / x[t - 62] - 1) + 0.2 * (x[t] / x[t - 125] - 1)
                                + 0.2 * (x[t] / x[t - 188] - 1) + 0.2 * (x[t] / x[t - 249] - 1))
            sc[~np.isfinite(sc)] = np.nan
            ser = pd.Series(sc, index=df.index.normalize())
            cols[sym] = ser[~ser.index.duplicated(keep="last")]
        if not cols:
            continue
        tab = pd.DataFrame(cols).sort_index().ffill()          # 當天沒交易的股票沿用它最近一筆
        out[mkt] = (tab.rank(axis=1, pct=True) * 99).round(0)
    return out


def regime_series(idx_close):
    """每個交易日當時的大盤狀態，定義與 scanner.market_regime 相同。"""
    c = idx_close.astype(float)
    m50, m200 = c.rolling(50).mean(), c.rolling(200).mean()
    reg = pd.Series("mixed", index=c.index)
    reg[(c > m50) & (m50 > m200)] = "bull"
    reg[(c < m50) & (c < m200)] = "bear"
    reg[m200.isna()] = "unk"
    return reg


# ==========================================
# 3. 把一檔股票整理成「每一天當時看得到的數字」
# ==========================================
def prep(sym, df, rs_tab, indices, regimes):
    mkt = S.mkt_of(sym)
    is_tw = mkt == "tw"
    cfg = S.MARKET_CFG[mkt]
    n = len(df)
    rc = df['Close'].values.astype(float)                 # 未還原配息：均線、52 週高低用它
    v = df['Volume'].values.astype(float)
    v50 = df['Volume'].rolling(50).mean().values
    to50 = (df['Close'] * df['Volume']).rolling(50).mean().values
    o, h, l, c, events, warn = S.heal_series(df, is_tw)   # 連續化價格：型態、漲跌、損益用它
    close = df['Close']
    ma20 = close.rolling(20).mean().values
    ma50 = close.rolling(50).mean().values
    ma150 = close.rolling(150).mean().values
    ma200 = close.rolling(200).mean().values
    pc = np.r_[np.nan, c[:-1]]
    tr = np.fmax(h - l, np.fmax(np.abs(h - pc), np.abs(l - pc)))
    atr_pct = pd.Series(tr).rolling(20).mean().values / c
    hi52 = df['High'].rolling(252).max().values
    lo52 = df['Low'].rolling(252).min().values

    dates = df.index.normalize()
    rs = np.full(n, 50.0)
    if mkt in rs_tab and sym in rs_tab[mkt].columns:
        r = rs_tab[mkt][sym].reindex(dates).values.astype(float)
        rs = np.where(np.isfinite(r), r, 50.0)

    m, reg = None, np.array(["unk"] * n, dtype=object)
    if mkt in indices:
        mm = indices[mkt].reindex(dates, method="ffill").bfill().values.astype(float)
        if np.isfinite(mm).all():
            m = mm
        reg = regimes[mkt].reindex(dates, method="ffill").fillna("unk").values

    with np.errstate(divide='ignore', invalid='ignore'):
        liquid = (v50 >= S.MIN_VOL_MA50) & (to50 >= cfg["min_turnover"])
        hs, ls, cs = pd.Series(h), pd.Series(l), pd.Series(c)
        rng10 = (hs.rolling(10).max() - ls.rolling(10).min()).values / c
        jump60 = (cs / cs.shift(1)).rolling(60).max().values
        pinned = (rng10 < 0.025) & (jump60 >= 1.20)

        k = max(1, S.DAYS_ABOVE_LONG_MA)
        above = ((rc > ma150) & (rc > ma200)).astype(float)
        above_k = pd.Series(above).rolling(k).min().values == 1.0
        up_n = S.MA200_UP_DAYS
        ma200_prev = np.r_[np.full(up_n, np.nan), ma200[:-up_n]]
        ma200_up = ma200 > ma200_prev
        rs_ok = rs >= S.RS_MIN
        template = (above_k & (ma150 > ma200) & ma200_up & (ma50 > ma150 * (1 + S.MA50_OVER_MA150_PCT / 100))
                    & (rc > ma50) & (rc >= lo52 * S.LOW52_MULT) & (rc >= hi52 * (1 - S.HIGH52_WITHIN)) & rs_ok)
        long_ok = np.isfinite(ma200_prev) & (rc > ma200) & ma200_up & rs_ok & (rc >= lo52 * S.LOW52_MULT)

        v50_prev = np.r_[np.nan, v50[:-1]]
        vol_ratio = np.where(v50_prev > 0, v / v50_prev, 1.0)
        is_red = (c >= o) & (c > pc) & np.where(h > l, (c - l) >= (h - l) * 0.5, True)
        pct = (c / pc - 1) * 100.0
        lim = cfg["limit_pct"]
        locked = ((pct >= lim - 0.7) & (np.abs(h - c) < 1e-9)) if lim else np.zeros(n, dtype=bool)
        vol_ok = (vol_ratio >= S.BREAKOUT_VOL_MULT) | locked
        d = np.r_[np.nan, np.diff(c)]
        big = v > v50
        up40 = pd.Series((big & (d > 0)).astype(float)).rolling(40).sum().values
        dn40 = pd.Series((big & (d < 0)).astype(float)).rolling(40).sum().values
        heavy_down = (c < pc * 0.985) & (vol_ratio >= 1.35)
        more_down = dn40 > up40 + 1
    hma20 = pd.Series(c).rolling(20).mean().values       # 用連續化價格算的均線：出場規則拿它和連續化收盤價比
    hma50 = pd.Series(c).rolling(50).mean().values
    return dict(sym=sym, mkt=mkt, cfg=cfg, n=n, dates=df.index, rc=rc, v=v, v50=v50, o=o, h=h, l=l, c=c, warn=warn,
                ma20=ma20, ma50=ma50, hma20=hma20, hma50=hma50, atr_pct=atr_pct, rs=rs, m=m, reg=reg, liquid=liquid, pinned=pinned,
                template=template, long_ok=long_ok, vol_ratio=vol_ratio, is_red=is_red, locked=locked, vol_ok=vol_ok,
                heavy_down=heavy_down, more_down=more_down, lim=lim)


def distribution_at(x, t):
    """第 t 天當時的出貨濾網（與 scanner 的 d_rules 相同）。回傳 (是否出貨, 三個旗標)。"""
    s3 = S.stage3_flags(x["c"][:t + 1], x["v"][:t + 1], x["v50"][:t + 1], recent=15) is not None
    hd, md = bool(x["heavy_down"][t]), bool(x["more_down"][t])
    return (hd or md or s3), (hd, md, s3)


# ==========================================
# 4. 「新讀法」：把分段走完的下跌併成一次收縮
# ==========================================
def _max_dd(h, l, a, b):
    """[a, b) 內「先高後低」的最大回落 → (高點位置, 低點位置, 幅度%)；同一根 K 棒的高與低不算。"""
    if b - a < 2:
        return None
    run = np.maximum.accumulate(h[a:b])
    dd = 1.0 - l[a + 1:b] / run[:-1]
    j = int(np.argmax(dd))
    if not (dd[j] > 0):
        return None
    tr = a + 1 + j
    pk = a + int(np.argmax(h[a:tr]))                      # 第一次到達該高點的位置（與現行鋸齒規則一致）
    return pk, tr, float(dd[j] * 100.0)


def v2_read(h, l, c, fp, end, atr_last):
    """同一個底部，換一種數收縮的方法（書上 p.238-241 的講法：由左到右一次比一次小）：
      第 1 次＝整個底部從左側高點到最低點；
      第 2 次＝最低點之後最大的一次回檔；第 3 次＝第 2 次低點之後最大的一次回檔……
    現行規則（scanner.analyze_base）是把每一段鋸齒都當成一次收縮，所以左側「分三段跌完」會被數成
    10→12→14 而判定「沒有逐次變小」。新讀法會把它看成一次 23%。
    另外加一條：最後一次回檔不可以明顯大於它前面那一次小回檔（右側放大＝還沒收斂）。
    其餘條件（最後一次 ≤10% 且 ≤ 第一次的 60%、樞紐在底部上半部、停留至少 3 天、沒有時間壓縮）與現行相同。"""
    last = end - 1
    p0 = last - fp["base_days"]
    base_high = fp["base_high"]
    lo_i = p0 + int(np.argmin(l[p0:end]))
    base_low = float(l[lo_i])
    thr = float(np.clip(S.ZZ_ATR_MULT * atr_last, 0.025, 0.08)) * 100.0

    cons = [(p0, base_high, base_low, float(fp["base_depth"]))]      # (高點位置, 高點價, 低點價, 幅度%)
    s, last_dec = lo_i, None
    while len(cons) < 12:
        md = _max_dd(h, l, s, end)
        if md is None or md[2] < thr:
            break
        pk, tr, dd = md
        cons.append((pk, float(h[pk]), float(l[tr]), dd))
        last_dec = (s, pk, tr, dd)
        s = tr
    # 尾端：最後一個低點之後的反彈高點，要彈得夠高、而且停留夠久，才算新的緊密區（與現行規則一致）
    if s < last:
        th = s + 1 + int(np.argmax(h[s + 1:end]))
        if h[th] >= l[s] * (1 + thr / 100.0) and (last - th) >= S.PIVOT_MIN_AGE:
            lo_p = float(l[th:end].min())
            cons.append((th, float(h[th]), lo_p, (float(h[th]) - lo_p) / float(h[th]) * 100.0))

    depths = [x[3] for x in cons]
    n_t = len(depths)
    final = depths[-1]
    pivot = cons[-1][1]
    pivot_age = last - cons[-1][0]
    violations = sum(1 for k in range(1, n_t) if depths[k] > depths[k - 1] * S.CONTRACTION_RATIO)
    in_upper = pivot >= base_low + 0.5 * (base_high - base_low)
    expanding = False
    if last_dec:
        s0, pk, tr, dd = last_dec
        pre = _max_dd(h, l, s0, pk + 1)
        expanding = bool(pre and pre[2] >= thr and dd > pre[2] / S.CONTRACTION_RATIO)
    compressed = bool(fp["compressed"])

    is_vcp = (2 <= n_t <= 6 and violations <= (1 if n_t >= 4 else 0) and final <= min(depths) + 1e-9
              and final <= min(S.FINAL_CONTRACTION_MAX, depths[0] * S.FINAL_VS_FIRST)
              and in_upper and pivot_age >= S.PIVOT_MIN_AGE and not compressed and not expanding)
    hi5, lo5 = float(h[end - 5:end].max()), float(l[end - 5:end].min())
    tight5 = (hi5 - lo5) / hi5 * 100.0
    is_flat = ((not is_vcp) and fp["base_depth"] <= S.FLAT_BASE_MAX_DEPTH
               and S.FLAT_BASE_DAYS[0] <= fp["base_days"] <= S.FLAT_BASE_DAYS[1] and tight5 <= 8.0 and not compressed)
    why = ""
    if not (is_vcp or is_flat):
        if compressed:
            why = "compressed"
        elif n_t < 2:
            why = "one_contraction"
        elif n_t > 6:
            why = "too_many_swings"
        elif violations > (1 if n_t >= 4 else 0):
            why = "not_contracting"
        elif expanding:
            why = "expanding_right"
        elif final > S.FINAL_CONTRACTION_MAX:
            why = "final_too_wide"
        elif final > depths[0] * S.FINAL_VS_FIRST:
            why = "final_vs_first"
        elif not in_upper:
            why = "lower_half"
        elif pivot_age < S.PIVOT_MIN_AGE:
            why = "pivot_too_new"
        else:
            why = "other"
    return {"ready": bool(is_vcp or is_flat), "is_vcp": bool(is_vcp), "is_flat": bool(is_flat),
            "pivot": float(fp["base_top"] if is_flat else pivot),
            "danger_low": float(lo5 if is_flat else cons[-1][2]),
            "depths": depths, "n_t": n_t, "final": float(tight5 if is_flat else final), "why_not": why,
            "depth_str": "→".join(f"{d:.0f}" for d in depths)}


# ==========================================
# 5. 損益：訊號隔天開盤買進之後實際怎麼走
# ==========================================
def simulate(x, t, cap_pct, ref=None, danger_low=None, flat_stop=None, trigger=None):
    """第 t 根收盤後出現訊號 → 第 t+1 根進場。trigger=None：開盤買進；trigger=價格：盤中超過該價才買（預先掛單）。
    進場當天的最低價若低於停損價，一律算停損（盤中先後順序無從得知，取保守的一邊；amb=1 標記這種情況）。"""
    o, h, l, c, n = x["o"], x["h"], x["l"], x["c"], x["n"]
    out = {"skip": "", "stop_pct": np.nan, "ret20": np.nan, "raw20": np.nan, "stop20": np.nan, "r20": np.nan,
           "ret60": np.nan, "stop60": np.nan, "hit20": np.nan, "mfe60": np.nan, "idx20": np.nan, "gap_pct": np.nan,
           "amb": 0.0, "exit20": pd.NaT, "bk_ret": np.nan, "bk_r": np.nan, "bk_days": np.nan, "bk_half": np.nan,
           "bk_open": np.nan, "bk_why": "", "bk2_ret": np.nan, "bk2_days": np.nan, "bk2_why": ""}
    e = t + 1
    if e >= n:
        out["skip"] = "no_next_bar"
        return out
    P = float(o[e])
    if not np.isfinite(P) or P <= 0:
        out["skip"] = "bad_price"
        return out
    out["gap_pct"] = (P / float(c[t]) - 1) * 100.0
    if trigger is not None:
        if not (h[e] > trigger):
            out["skip"] = "no_trigger"                      # 當天沒有碰到掛單價 → 沒成交
            return out
        P = max(P, float(trigger)) * (1 + BUY_STOP_SLIP_PCT / 100.0)
    lim = x["cfg"].get("limit_pct")
    if lim and out["gap_pct"] >= lim - 0.7 and abs(h[e] - l[e]) < 1e-9:
        out["skip"] = "locked_limit_up"                     # 一字鎖漲停：買不到
        return out
    if ref is not None and P > ref * (1 + S.CHASE_LIMIT_PCT / 100.0):
        out["skip"] = "gap_over_limit"                      # 已超過追價上限 → 不追
        return out
    if flat_stop is not None:
        pct = float(flat_stop)
        sp = P * (1 - pct / 100.0)
    else:
        sp, pct, _ = S.plan_stop(P, danger_low, cap_pct)
    out["stop_pct"] = pct
    cost = x["cfg"]["cost_buy_pct"] + x["cfg"]["cost_sell_pct"]
    hit = np.flatnonzero(l[e:min(n, e + HOLD_B)] <= sp)
    k_stop = int(hit[0]) if len(hit) else None              # 進場後第幾根碰到停損（0 = 進場當天）
    if trigger is not None and k_stop == 0 and o[e] < trigger:
        out["amb"] = 1.0                                    # 盤中才觸價、同一天又碰到停損價：先後順序不明

    def stopped_ret(k):
        px = sp if k == 0 else min(float(o[e + k]), sp)     # 跳空跌破停損 → 以開盤價出場
        return (px / P - 1) * 100.0 - cost

    for hold, kr, ks in ((HOLD_A, "ret20", "stop20"), (HOLD_B, "ret60", "stop60")):
        if k_stop is not None and k_stop < hold:
            out[kr], out[ks] = stopped_ret(k_stop), 1.0
        elif e + hold - 1 < n:
            out[kr], out[ks] = (float(c[e + hold - 1]) / P - 1) * 100.0 - cost, 0.0
    if e + HOLD_A - 1 < n:
        out["raw20"] = (float(c[e + HOLD_A - 1]) / P - 1) * 100.0 - cost      # 不設停損、抱滿 20 天
        if x["m"] is not None:
            out["idx20"] = (x["m"][e + HOLD_A - 1] / x["m"][t] - 1) * 100.0   # 同期間大盤
    if np.isfinite(out["ret20"]):
        out["r20"] = out["ret20"] / pct
        if "dates" in x:                                     # 這筆「抱 20 天」的交易哪一天結束（判斷順不順時要用）
            out["exit20"] = x["dates"][e + (k_stop if (k_stop is not None and k_stop < HOLD_A) else HOLD_A - 1)]
        if "hma50" in x:
            for pre, early in (("bk", False), ("bk2", True)):
                b = book_exit(x, e, P, sp, cost, early)
                out[pre + "_ret"], out[pre + "_days"], out[pre + "_why"] = b["ret"], b["days"], b["why"]
                if not early:
                    out["bk_r"], out["bk_half"], out["bk_open"] = b["ret"] / pct, b["half"], b["open"]
    # 停損之前（停損當天不算）最高漲到哪裡
    kk = k_stop if k_stop is not None else min(HOLD_B, n - e)
    complete = k_stop is not None or (e + HOLD_B - 1 < n)
    if kk > 0:
        mx = float(h[e:e + kk].max())
        out["mfe60"] = (mx / P - 1) * 100.0
        if mx >= P * (1 + TARGET_PCT / 100.0):
            out["hit20"] = 1.0
        elif complete:
            out["hit20"] = 0.0
    elif complete:
        out["hit20"], out["mfe60"] = 0.0, 0.0
    return out


def book_exit(x, e, P, sp, cost, early20=False):
    """買進之後照書上的規則處理（規則見檔案開頭第 4 題）。e＝進場那根、P＝成交價、sp＝起始停損價。
    同一根 K 棒裡同時碰到停損與目標價時，一律算停損先到；「收盤跌破均線」是收盤後才知道，所以隔天開盤才賣。
    回傳 ret（整筆報酬 %，已扣成本）、days（抱幾個交易日）、half（有沒有賣到一半）、open（資料結束時還沒出場）、why。"""
    o, h, l, c, n = x["o"], x["h"], x["l"], x["c"], x["n"]
    ma20, ma50 = x["hma20"], x["hma50"]
    tgt = P + BOOK_BE_MULT * (P - sp)
    stop, half, r_half, to_be = sp, False, 0.0, False
    px, why, exit_i, pending = None, "", -1, False
    last = min(n, e + BOOK_MAX_HOLD)
    for k in range(e, last):
        if to_be:
            stop, to_be = max(stop, P), False                # 前一根賣了一半 → 今天起停損在成本價
        if l[k] <= stop:
            px, exit_i = (stop if k == e else min(float(o[k]), stop)), k
            why = "stop" if stop < P else "breakeven"
            break
        if not half and h[k] >= tgt:
            r_half, half, to_be = (tgt if k == e else max(float(o[k]), tgt)) / P - 1.0, True, True
        reason = ""
        if np.isfinite(ma50[k]) and ma50[k] >= P and c[k] < ma50[k]:
            reason = "ma50"
        elif early20 and (k - e) < BOOK_EARLY_BARS and np.isfinite(ma20[k]) and c[k] < ma20[k]:
            reason = "early20"
        if reason:
            if k + 1 >= n:
                pending, exit_i = True, k                    # 賣出訊號出現在資料最後一根 → 還沒真的賣
            else:
                px, exit_i, why = float(o[k + 1]), k + 1, reason
            break
    is_open = 0.0
    if px is None:
        if not pending and last == e + BOOK_MAX_HOLD:
            exit_i, why = last - 1, "time"
        else:
            exit_i, why, is_open = (exit_i if pending else n - 1), "open", 1.0   # 資料到這裡為止還抱著 → 以最後收盤價計
        px = float(c[exit_i])
    r_rest = px / P - 1.0
    ret = (0.5 * r_half + 0.5 * r_rest if half else r_rest) * 100.0 - cost
    return {"ret": float(ret), "days": float(exit_i - e + 1), "half": 1.0 if half else 0.0, "open": is_open, "why": why}


# ==========================================
# 6. 重放
# ==========================================
def stop_cap(reg):
    return S.TOUGH_MARKET_STOP_PCT if reg == "bear" else S.MAX_STOP_PCT


def base_cache(x):
    """同一檔股票、同一個截止日的底部分析只算一次。"""
    memo = {}

    def base(end):
        r = memo.get(end, 0)
        if r == 0:
            r = S.analyze_base(x["h"], x["l"], x["c"], x["v"], x["v50"], x["atr_pct"], end, x["m"])
            memo[end] = r
        return r
    return base


def placed_recent(x, t, base, cap):
    """日報的第三層：近 10 日有過有效突破、而且還沒失敗 → 會被放進第二／四類（或因延伸而不列），不會再列為 ★。"""
    h, c, v, v50, lim = x["h"], x["c"], x["v"], x["v50"], x["lim"]
    for kk in range(S.POST_BREAKOUT_DAYS, 0, -1):
        b = t - kk
        fpb = base(b)
        if not fpb or not (fpb["ready"] and fpb["dryup"]):
            continue
        pv = fpb["pivot"]
        if not (c[b] > pv and c[b - 1] <= pv):
            continue
        b_locked = bool(lim and (c[b] / c[b - 1] - 1) * 100 >= lim - 0.7 and abs(h[b] - c[b]) < 1e-9)
        if not (v50[b - 1] > 0 and (v[b] >= S.BREAKOUT_VOL_MULT * v50[b - 1] or b_locked)):
            continue
        if c[t] >= pv:
            return True                                  # 仍在樞紐之上（買進區或已延伸）
        if c[t] > S.plan_stop(pv, fpb["danger_low"], cap)[0] and x["rc"][t] >= x["ma20"][t]:
            return True                                  # 蹲下 squat
    return False                                         # 沒有突破，或突破已失敗


def star_at(x, t, base, cap):
    """第 t 天收盤後，日報會不會把它列為 ★（樞紐成型＋量縮、收盤在樞紐下方 6% 內、守住 20 日線）。
    呼叫前要先確認：流動性、非釘住、過趨勢樣板、沒有出貨跡象。回傳底部分析或 None。"""
    fp = base(t)
    if not (fp and fp["ready"] and fp["dryup"]):
        return None
    pv = fp["pivot"]
    if not (x["c"][t] <= pv and (x["c"][t] / pv - 1) * 100.0 >= -S.STAR_MAX_DIST and x["rc"][t] >= x["ma20"][t] * 0.99):
        return None
    return None if placed_recent(x, t, base, cap) else fp


def replay_stock(x, events, baseline):
    """把一檔股票從頭重放一次。events：★ 名單與所有「收盤站上樞紐／底部高點」的事件；baseline：對照組的取樣。"""
    h, l, c, v, v50, atr_pct, m, n = x["h"], x["l"], x["c"], x["v"], x["v50"], x["atr_pct"], x["m"], x["n"]
    sym, mkt = x["sym"], x["mkt"]
    phase = zlib.crc32(sym.encode()) % BASELINE_EVERY
    base = base_cache(x)
    for t in range(MIN_HISTORY, n):
        if not x["liquid"][t] or x["pinned"][t]:
            continue
        tmpl = bool(x["template"][t])
        dist = distribution_at(x, t) if tmpl else None   # 沒過樣板的，用到才算

        def base_row(stream, stage, ref, danger_low, trigger=None):
            nonlocal dist
            if dist is None:
                dist = distribution_at(x, t)
            scale = x["rc"][t] / c[t]                    # 連續化價格 → 當時線圖上的價格
            row = {"date": x["dates"][t], "sym": sym, "mkt": mkt, "t": t, "stream": stream, "stage": stage,
                   "tmpl": tmpl, "dist": bool(dist[0]), "d_heavy": dist[1][0], "d_more": dist[1][1], "d_s3": dist[1][2],
                   "rs": float(x["rs"][t]), "reg": x["reg"][t], "vol_ratio": float(x["vol_ratio"][t]),
                   "is_red": bool(x["is_red"][t]), "locked": bool(x["locked"][t]),
                   "close": float(x["rc"][t]), "ref": float(ref * scale), "over_pct": float((c[t] / ref - 1) * 100.0)}
            row.update(simulate(x, t, stop_cap(x["reg"][t]), ref=ref, danger_low=danger_low, trigger=trigger))
            return row

        def common_of(fp, v2):
            return {"ready1": fp["ready"], "why1": fp["why_not"], "foot1": fp["footprint"], "dep1": fp["depth_str"],
                    "is_flat": fp["is_flat"], "poke": fp["poke"], "n_t1": fp["n_t"], "final1": round(fp["final"], 2),
                    "base_depth": round(fp["base_depth"], 2), "base_days": fp["base_days"], "dryup": fp["dryup"],
                    "dry_extreme": fp["dry_extreme"], "compressed": fp["compressed"], "shakeouts": fp["shakeouts"],
                    "rel_depth": fp["rel_depth"], "vol5": round(fp["vol5"], 3),
                    "ready2": bool(v2 and v2["ready"]), "why2": v2["why_not"] if v2 else "", "dep2": v2["depth_str"] if v2 else "",
                    "n_t2": v2["n_t"] if v2 else np.nan, "final2": round(v2["final"], 2) if v2 else np.nan}

        def order_row(stream, fp, v2, pv, danger_low):
            """前一晚列入名單 → 隔天掛單在樞紐價。另外記下成交那天收盤時的狀況（事後才知道，只用來分組觀察）。"""
            row = base_row(stream, 0, pv, danger_low, trigger=pv)
            row.update(common_of(fp, v2))
            e = t + 1
            if e < n:
                row["trig"] = bool(h[e] > pv)
                row["vol_e"] = float(v[e] / v50[t]) if v50[t] > 0 else np.nan
                row["e_over"] = bool(c[e] > pv)
            events.append(row)

        if tmpl and not dist[0]:
            if t % BASELINE_EVERY == phase:
                r = simulate(x, t, 0.0, flat_stop=stop_cap(x["reg"][t]))
                if not r["skip"]:
                    baseline.append({"mkt": mkt, "sym": sym, "t": t, "date": x["dates"][t], "reg": x["reg"][t],
                                     "close": float(x["rc"][t]), **r})
            # ---- 名單：樞紐已成型、收盤在樞紐下方 6% 內、守住 20 日線、近 10 日沒有還在進行中的突破 ----
            fp0 = base(t)
            if fp0 and x["rc"][t] >= x["ma20"][t] * 0.99:
                cap0 = stop_cap(x["reg"][t])
                if fp0["ready"]:
                    pv = fp0["pivot"]
                    if c[t] <= pv and (c[t] / pv - 1) * 100.0 >= -S.STAR_MAX_DIST and not placed_recent(x, t, base, cap0):
                        order_row("star" if fp0["dryup"] else "star0", fp0, None, pv, fp0["danger_low"])   # star＝日報的 ★
                else:
                    half = fp0["base_high"] - 0.5 * fp0["base_depth"] / 100.0 * fp0["base_high"]
                    if c[t] >= half * (1 - S.STAR_MAX_DIST / 100.0):
                        v2 = v2_read(h, l, c, fp0, t, atr_pct[t - 1])
                        if v2["ready"]:
                            pv = v2["pivot"]
                            if c[t] <= pv and (c[t] / pv - 1) * 100.0 >= -S.STAR_MAX_DIST and not placed_recent(x, t, base, cap0):
                                order_row("star2" if fp0["dryup"] else "star20", fp0, v2, pv, v2["danger_low"])

        if not (c[t] > c[t - 1]):
            continue                                     # 沒收漲就不可能「今天站上」任何價位

        fp = base(t)
        if fp:
            vol_b = bool(x["vol_ok"][t])
            ref = fp["pivot"] if fp["ready"] else fp["base_top"]
            cross1 = c[t] > ref and c[t - 1] <= ref
            v2 = None
            half = fp["base_high"] - 0.5 * fp["base_depth"] / 100.0 * fp["base_high"]
            if cross1 or c[t] >= half:
                v2 = v2_read(h, l, c, fp, t, atr_pct[t - 1])
            common = common_of(fp, v2)
            if cross1:
                stage = 1 if not fp["ready"] else (2 if not fp["dryup"] else (3 if not vol_b else 4))
                row = base_row("v1", stage, ref, fp["danger_low"])
                row.update(common)
                row["same_pivot"] = bool(v2 and v2["ready"] and abs(v2["pivot"] / ref - 1) < 0.002)
                events.append(row)
            if v2 and v2["ready"]:
                p2 = v2["pivot"]
                if c[t] > p2 and c[t - 1] <= p2:
                    stage = 2 if not fp["dryup"] else (3 if not vol_b else 4)
                    row = base_row("v2", stage, p2, v2["danger_low"])
                    row.update(common)
                    row["same_pivot"] = bool(fp["ready"] and abs(p2 / fp["pivot"] - 1) < 0.002)
                    events.append(row)
        # 3C：站上杯子裡的停頓區高點（只要求站在上升的 200 日線之上）
        if x["long_ok"][t]:
            ch = S.detect_cheat(h, l, c, v, v50, t, m)
            if ch and c[t] > ch["pivot"] and c[t - 1] <= ch["pivot"] and (c[t] / ch["pivot"] - 1) * 100.0 <= S.CHASE_LIMIT_PCT:
                lo = ch["lo_i"]
                dd, bigv = np.diff(c[lo:t + 1]), v[lo + 1:t + 1] > v50[lo + 1:t + 1]
                bad = int((bigv & (dd < 0)).sum()) > int((bigv & (dd > 0)).sum()) + 1 or bool(x["heavy_down"][t])
                if not bad:
                    row = base_row("3c", 4 if (x["vol_ok"][t] and x["is_red"][t]) else 3, ch["pivot"], ch["pause_low"])
                    row.update({"base_depth": round(ch["depth"], 2), "rel_depth": ch.get("rel_depth"), "dryup": ch["dry"],
                                "recov": round(ch["recov"], 1)})
                    events.append(row)


def replay_all(raw, indices):
    rs_tab = rs_tables(raw)
    regimes = {mkt: regime_series(s) for mkt, s in indices.items()}
    events, baseline, errors, used = [], [], 0, defaultdict(int)
    t0 = time.time()
    for i, (sym, df) in enumerate(raw.items(), 1):
        try:
            if len(df) <= MIN_HISTORY + 1:
                continue
            x = prep(sym, df, rs_tab, indices, regimes)
            if x["warn"]:
                used["warn_" + x["mkt"]] += 1            # 還原資料異常的股票不納入
                continue
            replay_stock(x, events, baseline)
            used[x["mkt"]] += 1
        except Exception:
            if DEBUG:
                raise
            errors += 1
        if i % 500 == 0:
            print(f"  重放 {i}/{len(raw)} 檔，累計事件 {len(events)}，{time.time() - t0:.0f} 秒")
    return pd.DataFrame(events), pd.DataFrame(baseline), {"errors": errors, **used}


# ==========================================
# 7. 自我檢查：回測的判斷和日報是不是同一套
# ==========================================
def last_day_counts(raw, indices):
    """用回測自己的判斷方式，算出「最後一天」的漏斗數字與 ★ 名單（應該要和日報完全一樣）。"""
    rs_tab = rs_tables(raw)
    regimes = {mkt: regime_series(s) for mkt, s in indices.items()}
    now_cap = {mkt: stop_cap(S.market_regime(indices.get(mkt))) for mkt in S.MARKET_CFG}   # 日報用「今天」的大盤狀態
    cnt = defaultdict(int)
    cnt["stars"] = set()
    for sym, df in raw.items():
        try:
            x = prep(sym, df, rs_tab, indices, regimes)
            t = x["n"] - 1
            mult, _ = S.get_intraday_vol_multiplier(df.index[-1], x["mkt"] == "tw")
            if mult != 1.0:                                # 盤中執行時，日報會推估全日量 → 這裡跟著做
                x["vol_ratio"][t] *= mult
                x["heavy_down"][t] = (x["c"][t] < x["c"][t - 1] * 0.985) and (x["vol_ratio"][t] >= 1.35)
            if not x["liquid"][t]:
                continue
            cnt["liquid"] += 1
            if x["pinned"][t]:
                cnt["pinned"] += 1
                continue
            if not x["template"][t]:
                continue
            cnt["template"] += 1
            if distribution_at(x, t)[0]:
                continue
            cnt["f1_no_distribution"] += 1
            h, l, c, v, v50 = x["h"], x["l"], x["c"], x["v"], x["v50"]
            lim, stage_max = x["lim"], 0
            for kk in range(S.POST_BREAKOUT_DAYS, 0, -1):
                b = t - kk
                fpb = S.analyze_base(h, l, c, v, v50, x["atr_pct"], b, x["m"])
                if not fpb:
                    continue
                ref_b = fpb["pivot"] if fpb["ready"] else fpb["base_top"]
                if not (c[b] > ref_b and c[b - 1] <= ref_b):
                    continue
                b_locked = bool(lim and (c[b] / c[b - 1] - 1) * 100 >= lim - 0.7 and abs(h[b] - c[b]) < 1e-9)
                vol_b = bool(v50[b - 1] > 0 and (v[b] >= S.BREAKOUT_VOL_MULT * v50[b - 1] or b_locked))
                stage_max = max(stage_max, 1 if not fpb["ready"] else (2 if not fpb["dryup"] else (3 if not vol_b else 4)))
            cnt[f"bo_{stage_max}"] += 1
            fp = S.analyze_base(h, l, c, v, v50, x["atr_pct"], t, x["m"])
            if fp:
                cnt["f2_has_base"] += 1
                cnt["f3_pivot_ready"] += int(fp["ready"])
            if star_at(x, t, base_cache(x), now_cap[x["mkt"]]):
                cnt["stars"].add(sym)
        except Exception:
            if DEBUG:
                raise
            cnt["errors"] += 1
    return cnt


CHECK_KEYS = ("liquid", "pinned", "template", "f1_no_distribution", "f2_has_base", "f3_pivot_ready",
              "bo_0", "bo_1", "bo_2", "bo_3", "bo_4")
BO_TXT = {"沒有越過": "bo_0", "底部未成型": "bo_1", "成型但未量縮": "bo_2", "量縮但突破量不足": "bo_3", "有效突破": "bo_4"}


def scanner_counts(raw, indices, stock_dict, meta, uni_info):
    """直接呼叫日報的 analyze_market（餵同一批已下載的資料、不抓財報、不發訊息），讀出它的診斷數字。"""
    start = pd.Timestamp.now() - pd.DateOffset(months=S.HISTORY_MONTHS)

    def fake_download(tickers, **kw):
        tickers = [tickers] if isinstance(tickers, str) else list(tickers)
        frames = {s: raw[s][raw[s].index >= start.normalize()] for s in tickers if s in raw}
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, axis=1)

    saved = (S.yf.download, S.fetch_indices, S.get_fundamentals, S.MY_HOLDINGS)
    try:
        S.yf.download = fake_download
        S.fetch_indices = lambda sd: {k: s[s.index >= pd.Timestamp(sd)] for k, s in indices.items()}
        S.get_fundamentals = lambda *a, **k: dict(NEUTRAL_FUND)
        S.MY_HOLDINGS = {}
        with contextlib.redirect_stdout(io.StringIO()):
            _, _, sections = S.analyze_market(stock_dict, meta, {}, uni_info, None)
    finally:
        S.yf.download, S.fetch_indices, S.get_fundamentals, S.MY_HOLDINGS = saved
    text = "\n".join(S.LAST_DIAG)
    out = {"stars": set(), "star_cards": defaultdict(int)}
    for title, items in sections:                        # 第三類：🎯 開頭的是 ★，其餘是「越過樞紐但量不足」
        if "【三、" not in title:
            continue
        for it in items:
            mm = re.search(r"<b>\S+ ([^\s<]+)", it)
            if mm:
                out["star_cards"][S.mkt_of(mm.group(1))] += 1
                if "🎯" in it:
                    out["stars"].add(mm.group(1))
    for k in CHECK_KEYS[:6]:
        mm = re.search(r"(?<![a-z0-9_])" + re.escape(k) + r"=(\d+)", text)
        if mm:
            out[k] = int(mm.group(1))
    for zh, k in BO_TXT.items():
        mm = re.search(re.escape(zh) + r"=(\d+)", text)
        if mm:
            out[k] = int(mm.group(1))
    return out


STAR_LIMIT = {"tw": 6, "us": 5}        # 日報第三類每個市場最多列幾檔（scanner.analyze_market 裡的 limits["star"]）


def star_diff(mine, theirs):
    """比對 ★ 名單。日報每個市場有顯示上限：列出來的一定要在回測名單裡；沒列滿時兩邊必須完全相同。"""
    out = []
    extra = theirs["stars"] - mine
    if extra:
        out.append(f"★ 日報有、回測沒有：{sorted(extra)}")
    for mkt in S.MARKET_CFG:
        if theirs["star_cards"].get(mkt, 0) < STAR_LIMIT[mkt]:
            miss = {x for x in mine if S.mkt_of(x) == mkt} - theirs["stars"]
            if miss:
                out.append(f"★ 回測有、日報沒有：{sorted(miss)}")
    return out


def self_check(raw, indices, stock_dict, meta, uni_info):
    """回傳一行文字。兩邊數字完全相同才算通過。"""
    try:
        start = (pd.Timestamp.now() - pd.DateOffset(months=S.HISTORY_MONTHS)).normalize()
        cut = {s: df[df.index >= start] for s, df in raw.items()}
        cut = {s: df for s, df in cut.items() if len(df) >= 252}            # 日報只看 36 個月 → 用同樣長度比
        idx = {k: s[s.index >= start] for k, s in indices.items()}
        mine = last_day_counts(cut, idx)
        theirs = scanner_counts(raw, indices, stock_dict, meta, uni_info)
        if any(k not in theirs for k in CHECK_KEYS):
            return "🔧 自我檢查：讀不到日報的診斷數字，無法比對（不影響下面的統計）"
        diff = [f"{k} 回測{mine.get(k, 0)}／日報{theirs[k]}" for k in CHECK_KEYS if mine.get(k, 0) != theirs[k]]
        diff += star_diff(mine["stars"], theirs)
        if not diff:
            return (f"🔧 自我檢查 ✅ 回測與日報對最後一天的判斷完全一致（過樣板 {theirs['template']}、無出貨 {theirs['f1_no_distribution']}、"
                    f"有底部 {theirs['f2_has_base']}、成型 {theirs['f3_pivot_ready']}、近10日有效突破 {theirs['bo_4']}、★ {len(mine['stars'])} 檔）")
        return "🔧 自我檢查 ⚠️ 回測與日報的判斷不一致，請把這行回報：" + "；".join(diff)
    except Exception as e:
        if DEBUG:
            raise
        return f"🔧 自我檢查：執行失敗（{type(e).__name__}: {str(e)[:80]}），不影響下面的統計"


# ==========================================
# 8. 統計與報告
# ==========================================
def dedup(df):
    """同一檔股票在同一組內，DEDUP_BARS 個交易日內重複出現的訊號只留第一次（避免同一段行情被算好幾次）。"""
    if df.empty:
        return df
    df = df.sort_values(["sym", "t"])
    keep, last_sym, last_t = [], None, -10 ** 9
    for sym, t in zip(df["sym"].values, df["t"].values):
        ok = sym != last_sym or (t - last_t) > DEDUP_BARS
        keep.append(ok)
        if ok:
            last_sym, last_t = sym, t
    return df[np.array(keep)]


def stat_line(df, col_ret="ret20", col_stop="stop20", col_r="r20"):
    """筆數｜平均報酬 ±誤差｜賺錢比例｜停損比例｜期望值 R"""
    d = df[np.isfinite(df[col_ret])] if len(df) else df
    n = len(d)
    if n == 0:
        return "N=0"
    r = d[col_ret].values
    se = 1.96 * r.std(ddof=1) / np.sqrt(n) if n > 1 else float("nan")
    txt = f"N={n}｜{r.mean():+.1f}% ±{se:.1f}｜賺 {np.mean(r > 0) * 100:.0f}%｜停損 {d[col_stop].mean() * 100:.0f}%"
    if col_r in d:
        txt += f"｜{d[col_r].mean():+.2f}R"
    return txt + ("　(樣本太少)" if n < MIN_N else "")


def stat_line60(df):
    d = df[np.isfinite(df["hit20"]) & np.isfinite(df["ret60"])] if len(df) else df
    n = len(d)
    if n == 0:
        return "N=0"
    return (f"N={n}｜先漲{TARGET_PCT:.0f}% {d['hit20'].mean() * 100:.0f}%｜碰停損 {d['stop60'].mean() * 100:.0f}%｜平均 {d['ret60'].mean():+.1f}%"
            + ("　(樣本太少)" if n < MIN_N else ""))


def by_market(df, fn=stat_line, **kw):
    out = []
    for mkt in ("us", "tw"):
        sub = df[df["mkt"] == mkt] if len(df) else df
        out.append(f" {S.MARKET_CFG[mkt]['name'][0]} {fn(sub, **kw)}")
    return "\n".join(out)


def groups(ev):
    """把事件分成要比較的幾組（都先去掉買不到的：開盤超過追價上限、鎖漲停、掛單沒成交）。"""
    keys = ("S", "Sv", "Sn", "Sc", "S0", "S2", "A", "B", "C", "D", "Dv", "Dn", "E", "E_all", "G", "H")
    if ev.empty:
        return {**{k: ev for k in keys}, "skipped": 0, "A_all": 0, "star_days": 0, "star_trig": 0, "star_gap": 0, "star_amb": 0}
    st = ev["stream"]
    v1, v2, c3 = st == "v1", st == "v2", st == "3c"
    tmpl, dist = ev["tmpl"].astype(bool), ev["dist"].astype(bool)
    gate = tmpl & ~dist
    ok = ev["skip"] == ""
    ready1 = ev["ready1"].fillna(False).astype(bool) if "ready1" in ev else pd.Series(False, index=ev.index)
    big = ev["vol_ratio"] >= S.BREAKOUT_VOL_MULT
    g = {}
    # ---- 買法乙：★ 預先掛單 ----
    star = st == "star"
    for col in ("vol_e", "e_over", "trig", "amb"):
        if col not in ev:
            ev = ev.assign(**{col: np.nan})
    trig = ev["trig"].fillna(False).astype(bool)
    g["star_days"] = int(star.sum())                                   # ★ 出現的「檔・天」數
    g["star_trig"] = int((star & trig).sum())                          # 其中隔天有碰到樞紐價
    g["star_gap"] = int((star & ev["skip"].isin(["gap_over_limit", "locked_limit_up"])).sum())
    g["S"] = dedup(ev[star & ok])
    g["star_amb"] = int(g["S"]["amb"].sum()) if len(g["S"]) and "amb" in g["S"] else 0
    sv = (g["S"]["vol_e"] >= S.BREAKOUT_VOL_MULT) if len(g["S"]) else []
    g["Sv"] = g["S"][sv] if len(g["S"]) else g["S"]                      # 成交那天收盤確認帶量（事後才知道）
    g["Sn"] = g["S"][~sv] if len(g["S"]) else g["S"]
    g["Sc"] = g["S"][g["S"]["e_over"].fillna(False).astype(bool)] if len(g["S"]) else g["S"]   # 成交那天收盤仍站在樞紐之上
    g["S0"] = dedup(ev[(st == "star0") & ok])                           # 同 ★ 但突破前沒有量縮
    g["S2"] = dedup(ev[(st == "star2") & ok])                           # 「新讀法」才算成型的 ★
    # ---- 買法甲：收盤站上之後，隔天開盤買 ----
    a_all = ev[v1 & (ev["stage"] == 4) & gate]
    g["A_all"] = len(dedup(a_all))
    g["skipped"] = len(dedup(a_all[a_all["skip"] != ""]))
    g["A"] = dedup(ev[v1 & (ev["stage"] == 4) & gate & ok])
    g["B"] = dedup(ev[v1 & (ev["stage"] == 3) & gate & ok])
    g["C"] = dedup(ev[v1 & (ev["stage"] == 2) & gate & ok])
    g["D"] = dedup(ev[v1 & (ev["stage"] == 1) & gate & ok])
    g["Dv"] = dedup(ev[v1 & (ev["stage"] == 1) & gate & ok & big])
    g["Dn"] = dedup(ev[v1 & (ev["stage"] == 1) & gate & ok & ~big])
    g["E"] = dedup(ev[v2 & (ev["stage"] == 4) & gate & ok & ~ready1])
    g["E_all"] = dedup(ev[v2 & (ev["stage"] == 4) & gate & ok])
    g["G"] = dedup(ev[v1 & (ev["stage"] == 4) & ~tmpl & ok])
    g["H"] = dedup(ev[c3 & (ev["stage"] == 4) & ~dist & ok])
    return g


WHY_TXT = {"not_contracting": "收縮沒有逐次變小", "too_many_swings": "波段太多", "final_vs_first": "最後一次相對第一次不夠小",
           "final_not_smallest": "最後一次不是最小", "final_too_wide": "最後一次太寬", "compressed": "右側時間壓縮",
           "one_contraction": "只有一次收縮", "pivot_too_new": "樞紐太新", "lower_half": "樞紐在下半部", "other": "其他"}


def verdict(a, b, col="ret20"):
    """兩組平均報酬的差距，是不是大到不能用運氣解釋（差距超過 2 倍標準誤才算）。純機械判讀。"""
    xa = a[col].values[np.isfinite(a[col].values)] if len(a) else np.array([])
    xb = b[col].values[np.isfinite(b[col].values)] if len(b) else np.array([])
    if len(xa) < MIN_N or len(xb) < MIN_N:
        return "樣本太少，無法判斷"
    diff = xa.mean() - xb.mean()
    se = float(np.sqrt(xa.var(ddof=1) / len(xa) + xb.var(ddof=1) / len(xb)))
    word = "明顯較好" if diff > 2 * se else ("明顯較差" if diff < -2 * se else "差距在誤差範圍內，分不出高下")
    return f"每筆 {diff:+.1f} 個百分點 → {word}"


def build_report(ev, base, info):
    g = groups(ev)
    mk = lambda df, m: df[df["mkt"] == m] if len(df) else df
    L = []
    L.append("🧪 <b>【回測 2：預先掛單，盤中碰到樞紐就買】</b>")
    L.append(f"期間 {info['d0']} ~ {info['d1']}｜台股 {info.get('tw', 0)} 檔、美股 {info.get('us', 0)} 檔（{html.escape(str(info.get('us_source', '')))}）")
    L.append(f"買法：前一晚日報列為 ★ 的股票，隔天盤中一超過樞紐價就買（已高過樞紐 {S.CHASE_LIMIT_PCT:.0f}% 或鎖漲停就放棄）。"
             f"停損放危險點（上限 {S.MAX_STOP_PCT:.0f}%）、抱 {HOLD_A} 個交易日賣出，已扣交易成本與滑價。")
    L.append("")
    L.append("<b>先看結論（機械判讀，只比平均報酬）</b>")
    for m in ("us", "tw"):
        nm = S.MARKET_CFG[m]["name"]
        L.append(f"{nm}｜★ 掛單 對比 隨便買過樣板的股票：{verdict(mk(g['S'], m), mk(base, m))}")
        L.append(f"{nm}｜★ 掛單 對比 隔天開盤才買：{verdict(mk(g['S'], m), mk(g['A'], m))}")
    L.append("")
    L.append("<i>每行：筆數｜平均報酬 ±誤差｜賺錢的比例｜碰到停損的比例｜平均每筆賺幾個 R</i>")
    L.append("<b>① ★ 預先掛單、盤中碰到樞紐就買</b>")
    L.append(by_market(g["S"]))
    rate = f"{g['star_trig'] / g['star_days'] * 100:.0f}%" if g["star_days"] else "—"
    L.append(f" <i>★ 共出現 {g['star_days']} 檔・天，隔天碰到樞紐價的有 {g['star_trig']}（{rate}），其中 {g['star_gap']} 次因跳空過高或鎖漲停放棄；"
             f"同一檔 {DEDUP_BARS} 天內只算第一次。有 {g['star_amb']} 筆買進當天就碰到停損價，一律算停損（偏保守）。</i>")
    L.append(" 其中成交當天收盤確認帶量的（盤中無法事先知道，僅供參考）：")
    L.append(by_market(g["Sv"]))
    L.append(" 其中成交當天量不足的：")
    L.append(by_market(g["Sn"]))
    L.append(" 其中成交當天收盤仍站在樞紐之上的（同樣是事後才知道）：")
    L.append(by_market(g["Sc"]))
    L.append("<b>② 同 ①，但突破前沒有量縮</b>（日報不會列為 ★）")
    L.append(by_market(g["S0"]))
    L.append("<b>③ 用「新讀法」才算成型的 ★</b>（目前日報不會列）")
    L.append(by_market(g["S2"]))
    L.append("<b>④ 對照：有效突破，隔天開盤才買</b>（＝第一次回測的 A）")
    L.append(by_market(g["A"]))
    L.append(f"<b>⑤ 對照：任何一天買進過樣板的股票</b>（固定停損 {S.MAX_STOP_PCT:.0f}%）")
    L.append(by_market(base))
    L.append("")
    L.append(f"<b>抱 {HOLD_B} 天：先漲到 +{TARGET_PCT:.0f}% 的比例、碰到停損的比例</b>")
    for key, name in (("S", "① ★ 掛單"), ("A", "④ 隔天開盤買")):
        L.append(name)
        L.append(by_market(g[key], stat_line60))
    L.append("⑤ 對照組")
    L.append(by_market(base, stat_line60))
    if len(base) and "idx20" in base:
        ix = []
        for m in ("us", "tw"):
            sub = base[(base["mkt"] == m) & np.isfinite(base["idx20"])]
            if len(sub):
                ix.append(f"{S.MARKET_CFG[m]['name']} {sub['idx20'].mean():+.1f}%")
        if ix:
            L.append("")
            L.append(f"同期間大盤指數每 {HOLD_A} 天平均：" + "、".join(ix))
    L.append("")
    L.append(info.get("check", ""))
    note = f"處理 {info.get('tw', 0) + info.get('us', 0)} 檔"
    if info.get("errors"):
        note += f"、失敗 {info['errors']} 檔"
    wn = info.get("warn_tw", 0) + info.get("warn_us", 0)
    if wn:
        note += f"、還原資料異常不納入 {wn} 檔"
    L.append(f"<i>{note}；事件共 {len(ev)} 筆、對照組取樣 {len(base)} 筆；耗時 {info.get('secs', 0):.0f} 秒。</i>")
    L.append("<i>限制：只含現在還上市、現在成交量夠大的股票，整體報酬偏樂觀，但各組之間可以互相比；沒有驗證基本面；過去不代表未來。"
             "其餘各組（未成型、3C…）的數字在明細檔裡。</i>")
    return "\n".join(L)


def fund_self_check(raw, eps_data, n=FUND_CHECK_N):
    """抽幾檔美股：回測對「今天」的基本面判斷，應該和日報的 get_fundamentals（meets_min）一樣。"""
    try:
        cand = [x for x in sorted(eps_data) if x in raw and not eps_data[x][2] and len(eps_data[x][0][0]) >= 12]
        if not cand:
            return "🔧 基本面自我檢查：沒有可比對的股票"
        pick = cand[::max(1, len(cand) // n)][:n]
        same, diff, skip = 0, [], 0
        want_of = {"ok": True, "fail": False, "na": None}
        for sym in pick:
            px = float(raw[sym]['Close'].iloc[-1])
            mine = fund_at(eps_data[sym][0], eps_data[sym][1], pd.Timestamp.now().normalize(), px)
            if mine["cls"] == "none":
                skip += 1
                continue
            S._FUND_CACHE.pop((sym, False), None)
            f = S.get_fundamentals(sym, px)
            if f.get("line") in ("財報無資料", "", None):
                skip += 1                                    # 日報這邊這次沒抓到資料，無從比較
                continue
            if f.get("meets_min") is want_of[mine["cls"]]:
                same += 1
            else:
                diff.append(f"{sym} 回測={mine['cls']}／日報={f.get('meets_min')}")
        if not same and not diff:
            return f"🔧 基本面自我檢查：抽 {len(pick)} 檔都無法比對（資料不足）"
        if not diff:
            return f"🔧 基本面自我檢查 ✅ 抽 {same + skip} 檔，可比對的 {same} 檔與日報的判斷全部相同"
        return f"🔧 基本面自我檢查 ⚠️ {same} 檔相同、{len(diff)} 檔不同，請把這行回報：" + "；".join(diff[:8])
    except Exception as e:
        if DEBUG:
            raise
        return f"🔧 基本面自我檢查：執行失敗（{type(e).__name__}: {str(e)[:80]}），不影響統計"


F_TXT = (("ok", "達標"), ("fail", "未達標（含虧損）"), ("na", "轉盈或基期太低"), ("none", "沒有資料"))


def build_report3(ev, base, info):
    """第 3 題：線型＋盈餘成長（只有美股）。"""
    us = lambda df: df[df["mkt"] == "us"] if len(df) else df
    cls = lambda df, k: df[df["f_cls"] == k] if len(df) and "f_cls" in df else df.iloc[0:0]
    g = groups(ev)
    F, Sg, A = us(base), us(g["S"]), us(g["A"])
    L = []
    L.append("🧪 <b>【回測 3：線型＋盈餘成長（只測美股）】</b>")
    es = info.get("eps", {})
    L.append(f"期間 {info['d0']} ~ {info['d1']}｜美股 {info.get('us', 0)} 檔，其中 {es.get('n', 0)} 檔拿得到歷次財報")
    L.append(f"「盈餘達標」＝訊號出現當時已公布的最近一季 EPS 年增 ≥ {S.EPS_MIN_GROWTH:.0f}%（書上的最低門檻，與日報的判斷相同）。"
             "買法、停損、持有天數都和前兩題一樣。")
    L.append("")
    L.append("<b>先看結論（機械判讀，只比平均報酬）</b>")
    L.append(f"・過樣板的股票｜盈餘達標 對比 未達標：{verdict(cls(F, 'ok'), cls(F, 'fail'))}")
    L.append(f"・★ 掛單｜盈餘達標 對比 未達標：{verdict(cls(Sg, 'ok'), cls(Sg, 'fail'))}")
    L.append(f"・有效突破隔天買｜盈餘達標 對比 未達標：{verdict(cls(A, 'ok'), cls(A, 'fail'))}")
    L.append(f"・★ 掛單＋盈餘達標 對比 隨便買過樣板的股票：{verdict(cls(Sg, 'ok'), F)}")
    L.append(f"・有效突破＋盈餘達標 對比 隨便買過樣板的股票：{verdict(cls(A, 'ok'), F)}")
    L.append("")
    L.append("<i>每行：筆數｜平均報酬 ±誤差｜賺錢的比例｜碰到停損的比例｜平均每筆賺幾個 R</i>")
    L.append("<b>① 任何一天買進過樣板的股票</b>")
    ok = cls(F, "ok")
    L.append(f" 達標　{stat_line(ok)}")
    if len(ok):
        L.append(f" 　└ 其中盈餘連三季加速　{stat_line(ok[ok['f_accel'].astype(bool)])}")
        L.append(f" 　└ 其中年增 ≥50%　{stat_line(ok[ok['f_g0'] >= 50])}")
        L.append(f" 　└ 其中連兩季以上超預期　{stat_line(ok[ok['f_beats'] >= 2])}")
    for k, name in F_TXT[1:]:
        L.append(f" {name}　{stat_line(cls(F, k))}")
    for title, df in (("<b>② ★ 預先掛單、盤中碰到樞紐就買</b>", Sg), ("<b>③ 有效突破，隔天開盤才買</b>", A)):
        L.append(title)
        for k, name in F_TXT:
            L.append(f" {name}　{stat_line(cls(df, k))}")
    L.append("")
    L.append(f"<b>抱 {HOLD_B} 天：先漲到 +{TARGET_PCT:.0f}% 的比例、碰到停損的比例</b>")
    for name, df in (("① 過樣板的股票", F), ("② ★ 掛單", Sg), ("③ 有效突破隔天買", A)):
        L.append(name)
        L.append(f" 達標　{stat_line60(cls(df, 'ok'))}")
        L.append(f" 未達標　{stat_line60(cls(df, 'fail'))}")
    if len(F) and "idx20" in F:
        sub = F[np.isfinite(F["idx20"])]
        if len(sub):
            L.append("")
            L.append(f"同期間美股大盤指數每 {HOLD_A} 天平均：{sub['idx20'].mean():+.1f}%")
    L.append("")
    L.append(info.get("check", ""))
    L.append(info.get("fcheck", ""))
    sp = info.get("split", {})
    L.append(f"<i>財報：{es.get('ok', 0)} 檔有資料、{es.get('empty', 0)} 檔沒有、{es.get('error', 0)} 檔抓取失敗、{es.get('budget', 0)} 檔來不及抓"
             f"（{es.get('secs', 0)} 秒，被限流 {es.get('throttled', 0)} 次）。期間內的股票分割：EPS 看起來未還原而由程式換算 {sp.get('raw', 0)} 次、已還原 {sp.get('adj', 0)} 次、"
             f"分不出來而不採用 {sp.get('amb', 0)} 次。</i>")
    L.append(f"<i>事件共 {len(ev)} 筆、對照組取樣 {len(base)} 筆；總耗時 {info.get('secs', 0):.0f} 秒。"
             "限制：只驗證 EPS（營收、利潤率沒有歷史資料）；只含現在還上市、成交量夠大的股票；過去不代表未來。</i>")
    return "\n".join(L)


def stat_line_wl(df, ret="bk_ret", days="bk_days"):
    """筆數｜平均每筆 ±誤差｜勝率｜賺的平均賺多少｜賠的平均賠多少｜平均抱幾天"""
    d = df[np.isfinite(df[ret])] if len(df) and ret in df else df.iloc[0:0]
    n = len(d)
    if n == 0:
        return "N=0"
    r = d[ret].values
    se = 1.96 * r.std(ddof=1) / np.sqrt(n) if n > 1 else float("nan")
    win, loss = r[r > 0], r[r <= 0]
    txt = (f"N={n}｜{r.mean():+.1f}% ±{se:.1f}｜勝率 {len(win) / n * 100:.0f}%｜賺的 " + (f"{win.mean():+.1f}%" if len(win) else "—")
           + "｜賠的 " + (f"{loss.mean():+.1f}%" if len(loss) else "—"))
    if days and days in d:
        txt += f"｜抱 {np.nanmean(d[days].values):.0f} 天"
    return txt + ("　(樣本太少)" if n < MIN_N else "")


def verdict_paired(df, a="bk_ret", b="ret20"):
    """同一批交易、兩種出場方式的差距（逐筆相減再平均），是不是大到不能用運氣解釋。"""
    if df.empty or a not in df or b not in df:
        return "樣本太少，無法判斷"
    d = (df[a] - df[b]).values
    d = d[np.isfinite(d)]
    if len(d) < MIN_N:
        return "樣本太少，無法判斷"
    diff, se = d.mean(), d.std(ddof=1) / np.sqrt(len(d))
    word = "明顯較好" if diff > 2 * se else ("明顯較差" if diff < -2 * se else "差距在誤差範圍內，分不出高下")
    return f"每筆 {diff:+.1f} 個百分點 → {word}"


def feedback_states(df):
    """每一筆交易出現訊號的那一天，「最近 FEEDBACK_N 筆已經結束的同類交易」是不是有一半以上賺錢。
    只用當時已經知道結果的交易（以抱 20 天的結果與結束日為準）。good＝順、bad＝不順、start＝筆數還不夠。"""
    if df.empty:
        return pd.Series([], dtype=object)
    d = df.sort_values("date")
    sig = pd.to_datetime(d["date"]).values.astype("datetime64[ns]")
    ex = pd.to_datetime(d["exit20"]).values.astype("datetime64[ns]")
    ok = ~np.isnat(ex)
    order = np.argsort(ex[ok], kind="stable")
    ex_sorted, win_sorted = ex[ok][order], (d["ret20"].values[ok] > 0)[order]
    out = []
    for day in sig:
        k = int(np.searchsorted(ex_sorted, day, side="left"))          # 結束日早於訊號日的筆數
        if k < FEEDBACK_N:
            out.append("start")
        else:
            out.append("good" if int(win_sorted[k - FEEDBACK_N:k].sum()) * 2 >= FEEDBACK_N else "bad")
    return pd.Series(out, index=d.index)


BK_WHY = (("stop", "碰到起始停損"), ("breakeven", "賣半後剩下的打平出場"), ("ma50", "跌破 50 日線"), ("time", f"抱滿 {BOOK_MAX_HOLD} 天"),
          ("open", "到今天還抱著"))


def build_report4(ev, base, info):
    """第 4 題：買進之後照書上的規則處理。"""
    mk = lambda df, m: df[df["mkt"] == m] if len(df) else df
    g = groups(ev)
    Sg, A, F = g["S"], g["A"], base
    done = lambda df: df[np.isfinite(df["bk_ret"])] if len(df) and "bk_ret" in df else df.iloc[0:0]
    Sg, A, F = done(Sg), done(A), done(F)
    L = []
    L.append("🧪 <b>【回測 4：買進之後照書上的規則處理】</b>")
    L.append(f"期間 {info['d0']} ~ {info['d1']}｜台股 {info.get('tw', 0)} 檔、美股 {info.get('us', 0)} 檔")
    L.append(f"出場規則（跑之前就定好，不看結果調整）：漲到停損幅度的 {BOOK_BE_MULT:.0f} 倍 → 賣一半、其餘停損移到成本價；"
             f"50 日線升過成本價之後，收盤跌破 50 日線就賣；最久抱 {BOOK_MAX_HOLD} 個交易日。")
    L.append("")
    L.append("<b>先看結論（機械判讀，只比平均每筆報酬）</b>")
    for m in ("us", "tw"):
        nm = S.MARKET_CFG[m]["name"]
        L.append(f"・{nm}｜★ 掛單：書上的出場 對比 抱 20 天就賣：{verdict_paired(mk(Sg, m))}")
        L.append(f"・{nm}｜都用書上的出場：★ 掛單 對比 隨便買過樣板的股票：{verdict(mk(Sg, m), mk(F, m), 'bk_ret')}")
    L.append("")
    L.append("<i>每行：筆數｜平均每筆 ±誤差｜勝率｜賺的平均賺多少｜賠的平均賠多少｜平均抱幾天</i>")
    L.append("<i>書上他自己的數字：勝率約 46~50%、賺的平均 +13~15%、賠的平均 -5~6%</i>")
    for title, df in (("<b>① ★ 預先掛單、盤中碰到樞紐就買</b>", Sg), ("<b>② 有效突破，隔天開盤才買</b>", A),
                      ("<b>③ 任何一天買進過樣板的股票</b>", F)):
        L.append(title)
        for m in ("us", "tw"):
            nm = S.MARKET_CFG[m]["name"][0]
            L.append(f" {nm}｜書上的出場　{stat_line_wl(mk(df, m))}")
            L.append(f" {nm}｜抱 20 天　　{stat_line_wl(mk(df, m), 'ret20', None)}")
    if len(Sg) and "f_cls" in Sg:
        L.append("<b>④ ★ 掛單＋盈餘達標</b>（只有美股，書上的出場）")
        L.append(f" 美｜{stat_line_wl(Sg[(Sg['mkt'] == 'us') & (Sg['f_cls'] == 'ok')])}")
    L.append(f"<b>⑤ ★ 掛單，再加一條：買進後 {BOOK_EARLY_BARS} 天內收盤跌破 20 日線就先走</b>")
    for m in ("us", "tw"):
        L.append(f" {S.MARKET_CFG[m]['name'][0]}｜{stat_line_wl(mk(Sg, m), 'bk2_ret', 'bk2_days')}")
    L.append(f"<b>⑥ 順不順</b>（★ 掛單、書上的出場；最近 {FEEDBACK_N} 筆已結束的 ★ 交易有一半以上賺錢＝順）")
    for m in ("us", "tw"):
        nm = S.MARKET_CFG[m]["name"][0]
        sub = mk(Sg, m)
        if sub.empty or "exit20" not in sub:
            L.append(f" {nm}｜N=0")
            continue
        st = feedback_states(sub)
        good, bad = sub.loc[st.index[st == "good"]], sub.loc[st.index[st == "bad"]]
        L.append(f" {nm}｜順的時候出現的訊號　{stat_line_wl(good)}")
        L.append(f" {nm}｜不順的時候出現的訊號　{stat_line_wl(bad)}")
        w = st.map({"good": 1.0, "bad": QUARTER, "start": QUARTER}).reindex(sub.index).values
        r = sub["bk_ret"].values
        L.append(f" {nm}｜順 對比 不順：{verdict(good, bad, 'bk_ret')}")
        L.append(f" {nm}｜順的時候下全額、不順只下 1/4 → 每一塊錢平均賺 {np.sum(w * r) / np.sum(w):+.1f}%（一視同仁是 {r.mean():+.1f}%）")
    if len(Sg):
        tot = len(Sg)
        vc = Sg["bk_why"].value_counts()
        L.append("★ 掛單最後怎麼出場（台美合計）：" + "｜".join(f"{name} {vc.get(k, 0) / tot * 100:.0f}%" for k, name in BK_WHY)
                 + f"；有賣到一半的 {Sg['bk_half'].mean() * 100:.0f}%")
    if len(F) and "idx20" in F:
        ix = []
        for m in ("us", "tw"):
            sub = F[(F["mkt"] == m) & np.isfinite(F["idx20"])]
            if len(sub):
                ix.append(f"{S.MARKET_CFG[m]['name']} {sub['idx20'].mean():+.1f}%")
        if ix:
            L.append("")
            L.append(f"同期間大盤指數每 {HOLD_A} 天平均：" + "、".join(ix))
    L.append("")
    L.append(info.get("check", ""))
    L.append(info.get("fcheck", ""))
    es = info.get("eps", {})
    L.append(f"<i>事件共 {len(ev)} 筆、對照組取樣 {len(base)} 筆；財報 {es.get('ok', 0)} 檔有資料；總耗時 {info.get('secs', 0):.0f} 秒。"
             "限制：只含現在還上市、成交量夠大的股票；同一根 K 棒同時碰到停損與目標時算停損；過去不代表未來。</i>")
    return "\n".join(L)


def send_file(path, caption):
    url = f"https://api.telegram.org/bot{S.TELEGRAM_TOKEN}/sendDocument"
    try:
        with open(path, "rb") as f:
            r = requests.post(url, data={"chat_id": S.TELEGRAM_CHAT_ID, "caption": caption},
                              files={"document": (os.path.basename(path), f)}, timeout=180)
        if r.status_code != 200:
            print(f"❌ Telegram 傳檔回應 {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"❌ 傳檔失敗：{e}")


def save_events(ev, base, path=EVENTS_FILE):
    """明細檔：每一筆訊號一列；對照組的取樣也附在後面（stream = base），方便之後依年份、多空再細分。"""
    if ev.empty:
        return False
    out = ev
    if len(base):
        b = base.copy()
        b["stream"], b["stage"] = "base", 0
        out = pd.concat([ev, b], ignore_index=True)
    out = out.drop(columns=["t"], errors="ignore").copy()
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    for col in out.columns:
        if out[col].dtype == float:
            out[col] = out[col].round(3)
    out.to_csv(path, index=False, compression={"method": "zip", "archive_name": "backtest_events.csv"})
    return True


# ==========================================
# 9. 主程式
# ==========================================
def main():
    t0 = time.time()
    print("🧪 回測開始")
    stock_dict, meta, uni_info = S.get_entire_market_universe()
    symbols = list(stock_dict.keys())
    start_date = (pd.Timestamp.now() - pd.DateOffset(months=BT_MONTHS)).strftime('%Y-%m-%d')
    print(f"📥 下載 {len(symbols)} 檔、{BT_MONTHS} 個月的 K 線…")
    S.send_telegram_msg(f"🧪 回測開始：下載 {len(symbols)} 檔股票 {BT_MONTHS} 個月的 K 線，這次還要逐檔抓美股的歷次財報，"
                        f"會比前兩次久（估 15~40 分鐘），跑完會再傳結果。")
    raw, splits = download_prices(symbols, start_date)
    if len(raw) < 50:
        raise RuntimeError(f"只下載到 {len(raw)} 檔股票的資料，無法回測（可能是 Yahoo 暫時擋住，請稍後再執行一次）")
    indices = S.fetch_indices(start_date)
    print(f"🔁 重放 {len(raw)} 檔…")
    ev, base, used = replay_all(raw, indices)
    info = dict(used)
    info["us_source"] = uni_info.get("us_source", "")
    dts = pd.to_datetime(pd.concat([ev["date"] if len(ev) else pd.Series(dtype="datetime64[ns]"),
                                    base["date"] if len(base) else pd.Series(dtype="datetime64[ns]")]))
    info["d0"] = dts.min().strftime("%Y-%m-%d") if len(dts) else "?"
    info["d1"] = dts.max().strftime("%Y-%m-%d") if len(dts) else "?"
    us_syms = sorted(x for x in raw if S.mkt_of(x) == "us")
    print(f"📑 抓 {len(us_syms)} 檔美股的歷次財報…")
    hists, eps_stat = fetch_eps(us_syms)
    eps_data, split_stat = {}, defaultdict(int)
    for sym, h in hists.items():
        h2, unk, st = adjust_for_splits(h, splits.get(sym))
        eps_data[sym] = (h2, unk, bool(st["raw"] or st["amb"]))
        for k, v in st.items():
            split_stat[k] += v
    ev, base = add_fundamentals(ev, eps_data), add_fundamentals(base, eps_data)
    info["eps"] = {**eps_stat, "n": len(eps_data)}
    info["split"] = dict(split_stat)
    print("🔧 自我檢查…")
    info["check"] = self_check(raw, indices, stock_dict, meta, uni_info)
    info["fcheck"] = fund_self_check(raw, eps_data)
    info["secs"] = time.time() - t0
    report = build_report4(ev, base, info)
    print(re.sub(r"<[^>]+>", "", report))
    S.send_telegram_msg(report)
    if save_events(ev, base):
        send_file(EVENTS_FILE, "回測明細（每一筆訊號與之後的損益）。需要進一步分析時，把這個檔案傳給 Claude。")
    print(f"✅ 回測完成，{time.time() - t0:.0f} 秒")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        msg = f"🧪 回測沒有跑完：{type(e).__name__}: {html.escape(str(e)[:300])}\n<code>{html.escape(traceback.format_exc()[-900:])}</code>"
        print(msg)
        try:
            S.send_telegram_msg(msg)
        except Exception:
            pass
        raise