# -*- coding: utf-8 -*-
"""
回測（驗證用，手動執行，不是每天跑）
=====================================
目的：用過去幾年的「真實」K 線，檢查 scanner.py 的型態判斷準不準。

做法：把每一檔股票的歷史一天一天重放。站在第 t 天收盤時，只拿第 t 天（含）以前的資料，
      用 scanner.py 同一組函式判斷「今天有沒有突破、底部算不算成型」，把訊號記下來；
      然後才去看第 t+1 天以後實際怎麼走，計算損益。

交易假設（盡量貼近實際使用方式）：
  ・日報在收盤後才送出，所以一律「隔天開盤」買進。
  ・隔天開盤價已經高過樞紐 5%（追價上限）就放棄。
  ・停損用 scanner.plan_stop（危險點下方，3%~8%；大盤空頭時上限 6%）。
  ・沒碰到停損就抱滿 20 個交易日後收盤賣出（另外統計 60 個交易日）。
  ・已扣 scanner.MARKET_CFG 裡的交易成本。

比較的組別：
  A 有效突破（底部成型＋量縮＋帶量）＝日報第一類
  B 成型＋量縮，但突破量不足        C 成型，但突破前沒量縮
  D 底部「未成型」就站上底部高點（目前被程式淘汰的）
  E 「新讀法」才算成型的有效突破（見 v2_read；目前被程式淘汰）
  F 對照組：任何一天買進過趨勢樣板的股票
  G 型態同 A，但沒過趨勢樣板          H 3C 突破停頓區

限制（解讀時要記得）：
  ・股票池是「現在」還上市、而且現在成交量夠大的股票 → 已下市的不在裡面，整體報酬會偏樂觀；
    但各組用的是同一批股票，組與組之間的比較仍有意義。
  ・拿不到「過去某一天當時」的財報，所以只驗證價量規則，不含基本面。
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
EVENTS_FILE = "backtest_events.zip"       # 裡面是一個 CSV
DEBUG = os.environ.get("BT_DEBUG", "") == "1"            # 測試用：遇到例外直接丟出，不吞掉

NEUTRAL_FUND = {"line": "", "badges": [], "warn": [], "score": 0, "meets_min": None, "strong": False,
                "earn_days": None, "eps_hist": [], "last_split": None}


# ==========================================
# 1. 下載
# ==========================================
def download_prices(symbols, start_date, batch_size=180):
    """與 scanner.analyze_market 相同的下載與清理方式；只留下至少 252 根 K 棒的股票。"""
    raw = {}
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        try:
            data = S.yf.download(batch, start=start_date, auto_adjust=False, group_by="ticker", threads=True, progress=False)
        except Exception as e:
            print(f"批次下載略過: {e}")
            continue
        for sym in batch:
            try:
                df = S._extract(data, sym, len(batch) == 1)
                cols = [x for x in ['Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume'] if x in df.columns]
                df = df[cols].dropna(subset=['Open', 'High', 'Low', 'Close', 'Volume'])
                df = df[df['Volume'] > 0]
                if getattr(df.index, "tz", None) is not None:
                    df.index = df.index.tz_localize(None)
                if len(df) >= 252:
                    raw[sym] = df
            except Exception:
                continue
        print(f"  已下載 {min(i + batch_size, len(symbols))}/{len(symbols)}，有效 {len(raw)} 檔")
    return raw


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
    return dict(sym=sym, mkt=mkt, cfg=cfg, n=n, dates=df.index, rc=rc, v=v, v50=v50, o=o, h=h, l=l, c=c, warn=warn,
                ma20=ma20, ma50=ma50, atr_pct=atr_pct, rs=rs, m=m, reg=reg, liquid=liquid, pinned=pinned,
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
def simulate(x, t, cap_pct, ref=None, danger_low=None, flat_stop=None):
    o, h, l, c, n = x["o"], x["h"], x["l"], x["c"], x["n"]
    out = {"skip": "", "stop_pct": np.nan, "ret20": np.nan, "raw20": np.nan, "stop20": np.nan, "r20": np.nan,
           "ret60": np.nan, "stop60": np.nan, "hit20": np.nan, "mfe60": np.nan, "idx20": np.nan, "gap_pct": np.nan}
    e = t + 1
    if e >= n:
        out["skip"] = "no_next_bar"
        return out
    P = float(o[e])
    if not np.isfinite(P) or P <= 0:
        out["skip"] = "bad_price"
        return out
    out["gap_pct"] = (P / float(c[t]) - 1) * 100.0
    if ref is not None and P > ref * (1 + S.CHASE_LIMIT_PCT / 100.0):
        out["skip"] = "gap_over_limit"                      # 開盤已超過追價上限 → 不追
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


# ==========================================
# 6. 重放
# ==========================================
def stop_cap(reg):
    return S.TOUGH_MARKET_STOP_PCT if reg == "bear" else S.MAX_STOP_PCT


def replay_stock(x, events, baseline):
    """把一檔股票從頭重放一次。events：所有「收盤站上樞紐／底部高點」的事件；baseline：對照組的取樣。"""
    h, l, c, v, v50, atr_pct, m, n = x["h"], x["l"], x["c"], x["v"], x["v50"], x["atr_pct"], x["m"], x["n"]
    sym, mkt = x["sym"], x["mkt"]
    phase = zlib.crc32(sym.encode()) % BASELINE_EVERY
    for t in range(MIN_HISTORY, n):
        if not x["liquid"][t] or x["pinned"][t]:
            continue
        tmpl = bool(x["template"][t])
        dist = None                                      # 用到才算
        if tmpl and t % BASELINE_EVERY == phase:
            dist = distribution_at(x, t)
            if not dist[0]:
                r = simulate(x, t, 0.0, flat_stop=stop_cap(x["reg"][t]))
                if not r["skip"]:
                    baseline.append({"mkt": mkt, "sym": sym, "t": t, "date": x["dates"][t], "reg": x["reg"][t], **r})
        if not (c[t] > c[t - 1]):
            continue                                     # 沒收漲就不可能「今天站上」任何價位

        def base_row(stream, stage, ref, danger_low):
            nonlocal dist
            if dist is None:
                dist = distribution_at(x, t)
            scale = x["rc"][t] / c[t]                    # 連續化價格 → 當時線圖上的價格
            row = {"date": x["dates"][t], "sym": sym, "mkt": mkt, "t": t, "stream": stream, "stage": stage,
                   "tmpl": tmpl, "dist": bool(dist[0]), "d_heavy": dist[1][0], "d_more": dist[1][1], "d_s3": dist[1][2],
                   "rs": float(x["rs"][t]), "reg": x["reg"][t], "vol_ratio": float(x["vol_ratio"][t]),
                   "is_red": bool(x["is_red"][t]), "locked": bool(x["locked"][t]),
                   "close": float(x["rc"][t]), "ref": float(ref * scale), "over_pct": float((c[t] / ref - 1) * 100.0)}
            row.update(simulate(x, t, stop_cap(x["reg"][t]), ref=ref, danger_low=danger_low))
            return row

        fp = S.analyze_base(h, l, c, v, v50, atr_pct, t, m)
        if fp:
            vol_b = bool(x["vol_ok"][t])
            ref = fp["pivot"] if fp["ready"] else fp["base_top"]
            cross1 = c[t] > ref and c[t - 1] <= ref
            v2 = None
            half = fp["base_high"] - 0.5 * fp["base_depth"] / 100.0 * fp["base_high"]
            if cross1 or c[t] >= half:
                v2 = v2_read(h, l, c, fp, t, atr_pct[t - 1])
            common = {"ready1": fp["ready"], "why1": fp["why_not"], "foot1": fp["footprint"], "dep1": fp["depth_str"],
                      "is_flat": fp["is_flat"], "poke": fp["poke"], "n_t1": fp["n_t"], "final1": round(fp["final"], 2),
                      "base_depth": round(fp["base_depth"], 2), "base_days": fp["base_days"], "dryup": fp["dryup"],
                      "dry_extreme": fp["dry_extreme"], "compressed": fp["compressed"], "shakeouts": fp["shakeouts"],
                      "rel_depth": fp["rel_depth"], "vol5": round(fp["vol5"], 3),
                      "ready2": bool(v2 and v2["ready"]), "why2": v2["why_not"] if v2 else "", "dep2": v2["depth_str"] if v2 else "",
                      "n_t2": v2["n_t"] if v2 else np.nan, "final2": round(v2["final"], 2) if v2 else np.nan}
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
    """用回測自己的判斷方式，算出「最後一天」的漏斗數字（應該要和日報診斷完全一樣）。"""
    rs_tab = rs_tables(raw)
    regimes = {mkt: regime_series(s) for mkt, s in indices.items()}
    cnt = defaultdict(int)
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
            S.analyze_market(stock_dict, meta, {}, uni_info, None)
    finally:
        S.yf.download, S.fetch_indices, S.get_fundamentals, S.MY_HOLDINGS = saved
    text = "\n".join(S.LAST_DIAG)
    out = {}
    for k in CHECK_KEYS[:6]:
        mm = re.search(r"(?<![a-z0-9_])" + re.escape(k) + r"=(\d+)", text)
        if mm:
            out[k] = int(mm.group(1))
    for zh, k in BO_TXT.items():
        mm = re.search(re.escape(zh) + r"=(\d+)", text)
        if mm:
            out[k] = int(mm.group(1))
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
        if len(theirs) < len(CHECK_KEYS):
            return "🔧 自我檢查：讀不到日報的診斷數字，無法比對（不影響下面的統計）"
        diff = [f"{k} 回測{mine.get(k, 0)}／日報{theirs[k]}" for k in CHECK_KEYS if mine.get(k, 0) != theirs[k]]
        if not diff:
            return (f"🔧 自我檢查 ✅ 回測與日報對最後一天的判斷完全一致（過樣板 {theirs['template']}、無出貨 {theirs['f1_no_distribution']}、"
                    f"有底部 {theirs['f2_has_base']}、成型 {theirs['f3_pivot_ready']}、近10日有效突破 {theirs['bo_4']}）")
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
    """把事件分成要比較的幾組（都先去掉「隔天開盤已超過追價上限」的）。"""
    if ev.empty:
        return {**{k: ev for k in ("A", "B", "C", "D", "Dv", "Dn", "E", "E_all", "G", "H")}, "skipped": 0, "A_all": 0}
    v1, v2, c3 = ev["stream"] == "v1", ev["stream"] == "v2", ev["stream"] == "3c"
    tmpl, dist = ev["tmpl"].astype(bool), ev["dist"].astype(bool)
    gate = tmpl & ~dist
    ok = ev["skip"] == ""
    ready1 = ev["ready1"].fillna(False).astype(bool) if "ready1" in ev else pd.Series(False, index=ev.index)
    big = ev["vol_ratio"] >= S.BREAKOUT_VOL_MULT
    g = {}
    a_all = ev[v1 & (ev["stage"] == 4) & gate]
    g["A_all"] = len(dedup(a_all))
    g["skipped"] = len(dedup(a_all[a_all["skip"] == "gap_over_limit"]))
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


def build_report(ev, base, info):
    g = groups(ev)
    L = []
    L.append("🧪 <b>【回測結果】</b>")
    L.append(f"期間 {info['d0']} ~ {info['d1']}｜台股 {info.get('tw', 0)} 檔、美股 {info.get('us', 0)} 檔（{html.escape(str(info.get('us_source', '')))}）")
    L.append(f"做法：訊號隔天開盤買進（開盤已高過樞紐 {S.CHASE_LIMIT_PCT:.0f}% 就放棄）、停損放危險點（上限 {S.MAX_STOP_PCT:.0f}%）、"
             f"抱 {HOLD_A} 個交易日賣出，已扣交易成本。")
    L.append("<i>每行：筆數｜平均報酬 ±誤差｜賺錢的比例｜碰到停損的比例｜平均每筆賺幾個 R（1R＝停損一次賠的錢）</i>")
    L.append("")
    L.append("<b>A 有效突破</b>（成型＋量縮＋帶量＝日報第一類）")
    L.append(by_market(g["A"]))
    if g["A_all"]:
        L.append(f" <i>另有 {g['skipped']}/{g['A_all']} 筆因隔天開盤超過追價上限而放棄</i>")
    L.append("<b>B 成型＋量縮，但突破量不足</b>")
    L.append(by_market(g["B"]))
    L.append("<b>C 成型，但突破前沒量縮</b>")
    L.append(by_market(g["C"]))
    L.append("<b>D 底部「未成型」就站上高點</b>（目前被淘汰）")
    L.append(" 帶量：")
    L.append(by_market(g["Dv"]))
    L.append(" 不帶量：")
    L.append(by_market(g["Dn"]))
    if len(g["D"]):
        top = g["D"]["why1"].value_counts().head(3)
        for why in top.index:
            sub = g["D"][(g["D"]["why1"] == why) & (g["D"]["vol_ratio"] >= S.BREAKOUT_VOL_MULT)]
            L.append(f" ・{WHY_TXT.get(why, why)}（帶量，台美合計）{stat_line(sub)}")
    L.append("<b>E 用「新讀法」才算成型的有效突破</b>（分段下跌併成一次收縮；目前被淘汰）")
    L.append(by_market(g["E"]))
    L.append(" 新讀法全部（含與 A 重疊的）：")
    L.append(by_market(g["E_all"]))
    L.append(f"<b>F 對照組</b>：任何一天買進過樣板的股票（固定停損 {S.MAX_STOP_PCT:.0f}%）")
    L.append(by_market(base))
    L.append("<b>G 型態同 A，但沒過趨勢樣板</b>")
    L.append(by_market(g["G"]))
    L.append("<b>H 3C：帶量站上停頓區</b>")
    L.append(by_market(g["H"]))
    L.append("")
    L.append(f"<b>抱 {HOLD_B} 天：先漲到 +{TARGET_PCT:.0f}% 的比例、碰到停損的比例</b>")
    for key, name in (("A", "A 有效突破"), ("Dv", "D 未成型帶量"), ("E", "E 新讀法"), ("H", "H 3C")):
        L.append(f"{name}")
        L.append(by_market(g[key], stat_line60))
    L.append("F 對照組")
    L.append(by_market(base, stat_line60))
    if len(base) and "idx20" in base:
        mk = []
        for mkt in ("us", "tw"):
            s = base[(base["mkt"] == mkt) & np.isfinite(base["idx20"])]
            if len(s):
                mk.append(f"{S.MARKET_CFG[mkt]['name']} {s['idx20'].mean():+.1f}%")
        if mk:
            L.append("")
            L.append(f"同期間大盤指數每 {HOLD_A} 天平均：" + "、".join(mk))
    L.append("")
    L.append(info.get("check", ""))
    note = f"處理 {info.get('tw', 0) + info.get('us', 0)} 檔"
    if info.get("errors"):
        note += f"、失敗 {info['errors']} 檔"
    wn = info.get("warn_tw", 0) + info.get("warn_us", 0)
    if wn:
        note += f"、還原資料異常不納入 {wn} 檔"
    L.append(f"<i>{note}；事件共 {len(ev)} 筆、對照組取樣 {len(base)} 筆；耗時 {info.get('secs', 0):.0f} 秒。</i>")
    L.append("<i>限制：只含現在還上市、現在成交量夠大的股票，整體報酬偏樂觀，但各組之間可以互相比；沒有驗證基本面；過去不代表未來。</i>")
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
    S.send_telegram_msg(f"🧪 回測開始：下載 {len(symbols)} 檔股票 {BT_MONTHS} 個月的 K 線，跑完會再傳結果（通常要幾十分鐘）。")
    raw = download_prices(symbols, start_date)
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
    print("🔧 自我檢查…")
    info["check"] = self_check(raw, indices, stock_dict, meta, uni_info)
    info["secs"] = time.time() - t0
    report = build_report(ev, base, info)
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