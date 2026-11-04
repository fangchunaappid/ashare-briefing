#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集A股收盘简报所需数据，输出 data.json。

数据源：
- 腾讯行情 qt.gtimg.cn / web.ifzq.gtimg.cn（指数、ETF、铝板块个股、K线）
- 东方财富 push2.eastmoney.com / np-anotice-stock.eastmoney.com（板块、个股榜、大盘资金、ETF份额、公告、期货）、
  np-listapi 滚动要闻（新闻池补充源）
- 新浪财经 hq.sinajs.cn / zhibo.sina.com.cn（LME铝、沪铝备用源、7x24快讯、完整行情与板块/个股榜备用源）
- 华尔街见闻 api-one.wallstcn.com（A股7x24快讯备用源）
- Google News RSS（新闻检索，含原文链接）

任一数据源失败不影响整体，缺失项记为 None 并在 status 中说明，由简报生成环节如实处理。
"""
import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import requests

TZ = timezone(timedelta(hours=8))  # Asia/Shanghai
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
DATA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT_PATH = os.path.join(DATA_DIR, "output", "etf_shares.json")

def _load_share_snapshot():
    """读取最近一次 ETF 份额快照（由 workflow 的 Actions 缓存跨日恢复）。"""
    try:
        with open(SNAPSHOT_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}

def _save_share_snapshot(etfs):
    """把今日份额快照写入 output/，由 workflow 缓存步骤持久化到下次运行。"""
    try:
        os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
        snap = {}
        for row in etfs or []:
            if row.get("_secid") and row.get("fund_total_shares"):
                snap[row["_secid"]] = {"fund_total_shares": row["fund_total_shares"],
                                       "name": row.get("name")}
        with open(SNAPSHOT_PATH, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, ensure_ascii=False, indent=1)
        return True
    except Exception as e:
        print("[快照] ETF份额快照写入失败: %s" % e)
        return False

# 沪深300ETF、中证500ETF、上证50ETF、中证1000ETF（国家队常用宽基ETF）
NATIONAL_ETFS = [("sh510300", "1.510300"), ("sh510500", "1.510500"),
                 ("sh510050", "1.510050"), ("sh512100", "0.512100")]
ALUMINUM_STOCKS = "sh601600,sz000807,sz000933,sz002532"  # 中国铝业/云铝股份/神火股份/天山铝业


def http_get(url, referer=None, timeout=25, is_gbk=False):
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    if is_gbk:
        r.encoding = "gbk"
        return r.text
    return r.text


def to_num(v):
    try:
        if v in (None, "", "-"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 腾讯行情
def tencent_quotes(codes):
    raw = http_get("https://qt.gtimg.cn/q=" + codes, is_gbk=True)
    out = {}
    for m in re.finditer(r'v_(\w+)="([^"]*)"', raw):
        key, f = m.group(1), m.group(2).split("~")
        if len(f) < 40:
            continue
        amount = None
        try:
            seg = f[35].split("/")
            amount = float(seg[2]) if len(seg) >= 3 else None
        except Exception:
            pass
        out[key] = {
            "name": f[1],
            "price": to_num(f[3]),
            "prev_close": to_num(f[4]),
            "open": to_num(f[5]),
            "change": to_num(f[31]),
            "change_pct": to_num(f[32]),
            "high": to_num(f[33]),
            "low": to_num(f[34]),
            "amount_yuan": amount,
            "amount_yi": round(amount / 1e8, 1) if amount else None,
            "volume_ratio": to_num(f[49]) if len(f) > 49 else None,
            "quote_time": f[30] if len(f) > 30 else "",
        }
    return out


def tencent_kline_dates_and_volume(code, n=6):
    """返回 [(date, volume), ...] 最近n个交易日，用于交易日判断与ETF放量判断。"""
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           "param=%s,day,,,%d,qfq" % (code, n))
    j = json.loads(http_get(url))
    d = j["data"][code]
    bars = d.get("day") or d.get("qfqday") or []
    return [(b[0], to_num(b[5])) for b in bars]


def sina_daily_kline(code, n=6):
    """新浪日K兜底（腾讯ifzq不可用时）。"""
    url = ("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
           "?symbol=%s&scale=240&ma=no&datalen=%d" % (code, n))
    j = json.loads(http_get(url, referer="https://finance.sina.com.cn"))
    return [(row["day"][:10], to_num(row.get("volume"))) for row in j]


def daily_kline(code, n=6):
    try:
        return tencent_kline_dates_and_volume(code, n)
    except Exception:
        return sina_daily_kline(code, n)


def sina_full_quotes(codes):
    """新浪完整行情（腾讯行情失败时的备用源，指数/ETF/个股通用）。

    字段: 0名称 1今开 2昨收 3最新 4最高 5最低 ... 8成交量 9成交额(元) 30日期 31时间
    """
    raw = http_get("https://hq.sinajs.cn/list=" + ",".join(codes),
                   referer="https://finance.sina.com.cn", is_gbk=True)
    out = {}
    for m in re.finditer(r'hq_str_(\w+)="([^"]*)"', raw):
        key, f = m.group(1), m.group(2).split(",")
        if len(f) < 32 or to_num(f[3]) is None:
            continue
        price, prev = to_num(f[3]), to_num(f[2])
        amt = to_num(f[9])
        out[key] = {
            "name": f[0],
            "price": price,
            "prev_close": prev,
            "open": to_num(f[1]),
            "change": round(price - prev, 3) if price is not None and prev is not None else None,
            "change_pct": round((price / prev - 1) * 100, 2) if price and prev else None,
            "high": to_num(f[4]),
            "low": to_num(f[5]),
            "amount_yuan": amt,
            "amount_yi": round(amt / 1e8, 1) if amt else None,
            "quote_time": (f[30] + f[31]).replace("-", "").replace(":", "") if len(f) > 31 else "",
        }
    return out


def stock_quotes(codes):
    """行情统一入口：腾讯为主，新浪完整行情兜底。返回 (quotes, 来源名)。"""
    try:
        q = tencent_quotes(",".join(codes))
        if q:
            return q, "腾讯"
    except Exception:
        pass
    return sina_full_quotes(list(codes)), "新浪"


# ---------------------------------------------------------------- 东方财富
def _em_json(path):
    """push2/push2delay 双域名互备 × 3 轮重试（东财偶发整站 502）。"""
    import time as _t
    last = None
    for round_ in range(4):
        for host in ("push2.eastmoney.com", "push2delay.eastmoney.com"):
            try:
                return json.loads(http_get("https://%s%s" % (host, path)))
            except Exception as e:  # noqa: BLE001
                last = e
        if round_ < 3:
            _t.sleep((2, 5, 8)[round_])
    raise last


def _em_diff(j):
    diff = (j.get("data") or {}).get("diff")
    if isinstance(diff, dict):
        diff = list(diff.values())
    return diff or []


def em_boards(board_type, po, pz=8):
    fs = "m:90+t:2+f:!50" if board_type == "industry" else "m:90+t:3+f:!50"
    j = _em_json("/api/qt/clist/get?pn=1&pz=%d&po=%d&np=1"
                 "&fltt=2&invt=2&fid=f3&fs=%s&fields=f3,f12,f14,f62,f128,f136" % (pz, po, fs))
    return [{
        "name": d.get("f14"),
        "change_pct": to_num(d.get("f3")),
        "main_inflow_yuan": to_num(d.get("f62")),
        "main_inflow_yi": round(to_num(d.get("f62")) / 1e8, 1) if to_num(d.get("f62")) else None,
        "leader_stock": d.get("f128"),
        "leader_change_pct": to_num(d.get("f136")),
    } for d in _em_diff(j)]


def em_stock_rank(sort_field, pz=10):
    j = _em_json("/api/qt/clist/get?pn=1&pz=%d&po=1&np=1"
                 "&fltt=2&invt=2&fid=%s&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
                 "&fields=f2,f3,f5,f6,f12,f14" % (pz, sort_field))
    return [{
        "code": d.get("f12"),
        "name": d.get("f14"),
        "price": to_num(d.get("f2")),
        "change_pct": to_num(d.get("f3")),
        "volume": to_num(d.get("f5")),
        "amount_yuan": to_num(d.get("f6")),
        "amount_yi": round(to_num(d.get("f6")) / 1e8, 1) if to_num(d.get("f6")) else None,
    } for d in _em_diff(j)]


def em_market_overview():
    j = _em_json("/api/qt/ulist.np/get?fltt=2&invt=2"
                 "&fields=f12,f14,f62,f104,f105,f106&secids=1.000001,0.399001")
    return [{
        "name": d.get("f14"),
        "main_inflow_yuan": to_num(d.get("f62")),
        "main_inflow_yi": round(to_num(d.get("f62")) / 1e8, 1) if to_num(d.get("f62")) else None,
        "advancers": to_num(d.get("f104")),
        "decliners": to_num(d.get("f105")),
        "flat": to_num(d.get("f106")),
    } for d in _em_diff(j)]


def em_stock_extra(secid, fields):
    j = _em_json("/api/qt/stock/get?invt=2&fltt=2&secid=%s&fields=%s" % (secid, fields))
    return j.get("data") or {}


def em_futures_main(secid):
    d = em_stock_extra(secid, "f43,f58,f60,f170,f47,f48")
    if not d or d.get("f43") in (None, "-", ""):
        return None
    return {
        "name": d.get("f58"),
        "price": to_num(d.get("f43")),
        "prev_close": to_num(d.get("f60")),
        "change_pct": to_num(d.get("f170")),
        "volume": to_num(d.get("f47")),
        "amount": to_num(d.get("f48")),
    }


def em_notices(n=15):
    url = ("https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1&page_size=%d"
           "&page_index=1&ann_type=A&client_source=web&f_node=0&s_node=0" % n)
    j = json.loads(http_get(url))
    out = []
    for it in (j.get("data") or {}).get("list") or []:
        codes = it.get("codes") or []
        code = codes[0].get("stock_code") if codes else ""
        art = it.get("art_code")
        out.append({
            "title": it.get("title"),
            "stock_code": code,
            "date": (it.get("notice_date") or "")[:10],
            "link": ("https://data.eastmoney.com/notices/detail/%s/%s.html" % (code or "0", art))
                    if art else "",
        })
    return out


def em_roll_news(n=30):
    """东财滚动要闻（新闻池补充源，带原文链接）。"""
    url = ("https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
           "?client=web&biz=web_news_col&column=350&order=1&needInteractData=0"
           "&page_index=1&page_size=%d&req_trace=%d" % (n, int(time.time() * 1000)))
    j = json.loads(http_get(url))
    rows = ((j.get("data") or {}).get("list")) or []
    out = []
    for it in rows:
        link = it.get("uniqueUrl") or it.get("url") or ""
        out.append({
            "title": it.get("title") or "",
            "link": link,
            "time": (it.get("showTime") or "")[:16],
            "source": it.get("mediaName") or "",
        })
    return out


# ---------------------------------------------------------------- 新浪
def sina_lme_aluminum():
    raw = http_get("https://hq.sinajs.cn/list=hf_ALI",
                   referer="https://finance.sina.com.cn", is_gbk=True)
    m = re.search(r'"([^"]*)"', raw)
    if not m:
        return None
    p = m.group(1).split(",")
    if len(p) < 8 or to_num(p[0]) is None:
        return None
    price, prev = to_num(p[0]), to_num(p[7])
    pct = round((price / prev - 1) * 100, 2) if price and prev else None
    return {"name": "LME铝(3个月)", "price": price, "prev_close": prev, "change_pct": pct}


def em_lme_aluminum():
    """东财外盘期货：LME 综合铝03（109.LALT）。新浪 hf_ 外盘接口失效后的备用源。"""
    j = _em_json("/api/qt/stock/get?secid=109.LALT&fltt=2&invt=2"
                 "&fields=f43,f57,f58,f60,f169,f170")
    d = j.get("data") or {}
    price, prev = to_num(d.get("f43")), to_num(d.get("f60"))
    if not price:
        return None
    return {"name": d.get("f58") or "LME铝(3个月)", "price": price,
            "prev_close": prev, "change": to_num(d.get("f169")),
            "change_pct": to_num(d.get("f170")), "source": "eastmoney"}

def sina_shfe_aluminum_fallback():
    raw = http_get("https://hq.sinajs.cn/list=nf_AL0",
                   referer="https://finance.sina.com.cn", is_gbk=True)
    m = re.search(r'"([^"]*)"', raw)
    if not m:
        return None
    p = m.group(1).split(",")
    if len(p) < 19:
        return None
    # 字段: 0名称 1时间 2开 3高 4低 5昨收 8最新价 10昨结算 13持仓 17日期
    price, prev_settle = to_num(p[8]), to_num(p[10])
    if not price or not prev_settle:
        return None
    return {"name": "沪铝主力/连续(%s)" % p[0], "price": price,
            "prev_settlement": prev_settle,
            "change_pct": round((price / prev_settle - 1) * 100, 2),
            "open": to_num(p[2]), "high": to_num(p[3]), "low": to_num(p[4]),
            "open_interest": to_num(p[13]), "quote_date": p[17]}


def sina_7x24(n=30):
    url = ("https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=%d"
           "&zhibo_id=152&tag_id=0" % n)
    j = json.loads(http_get(url))
    lst = (((j.get("result") or {}).get("data") or {}).get("feed") or {}).get("list") or []
    out = []
    for it in lst:
        out.append({
            "time": it.get("create_time"),
            "text": re.sub(r"<[^>]+>", "", it.get("rich_text") or "")[:220],
            "link": it.get("docurl") or "",
        })
    return out


def sina_roll_news(n=50):
    """新浪财经滚动新闻池（含标题+原文链接），作为主题检索的补充。"""
    url = "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&num=%d&page=1" % n
    j = json.loads(http_get(url))
    rows = ((j.get("result") or {}).get("data")) or []
    out = []
    for it in rows:
        ctime = it.get("ctime")
        out.append({
            "title": re.sub(r"<[^>]+>", "", it.get("title") or ""),
            "link": it.get("url") or "",
            "time": datetime.fromtimestamp(int(ctime), TZ).strftime("%m-%d %H:%M")
                    if ctime and str(ctime).isdigit() else "",
            "source": it.get("media_name") or "",
        })
    return out


def sina_boards(kind):
    """新浪板块列表（东财板块接口失败时的备用源）。kind: industry=行业 / concept=概念。

    返回按涨跌幅降序的完整列表（无主力资金字段，记为 None）。
    字段: 0代码 1名称 2家数 3平均价 4涨跌额 5涨跌幅 6成交量 7成交额 8领涨股代码
          9领涨股涨跌幅 10领涨股价 11领涨股涨跌额 12领涨股名称
    """
    url = ("https://money.finance.sina.com.cn/q/view/newSinaHy.php" if kind == "industry"
           else "https://money.finance.sina.com.cn/q/view/newFLJK.php?param=class")
    raw = http_get(url, is_gbk=True)
    rows = []
    for m in re.finditer(r'"(\w+)":"([^"]+)"', raw):
        f = m.group(2).split(",")
        if len(f) < 13 or to_num(f[5]) is None:
            continue
        rows.append({
            "name": f[1],
            "change_pct": to_num(f[5]),
            "main_inflow_yuan": None,
            "main_inflow_yi": None,
            "leader_stock": f[12],
            "leader_change_pct": to_num(f[9]),
        })
    rows.sort(key=lambda r: r["change_pct"] or 0, reverse=True)
    return rows


def sina_stock_rank(sort_field, pz=10):
    """新浪个股排行（东财个股榜失败时的备用源）。sort_field: f3=涨幅榜 f6=成交额榜。"""
    sort = "changepercent" if sort_field == "f3" else "amount"
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "Market_Center.getHQNodeData?page=1&num=%d&sort=%s&asc=0&node=hs_a" % (pz, sort))
    rows = json.loads(http_get(url)) or []
    out = []
    for r in rows:
        amt = to_num(r.get("amount"))
        out.append({
            "code": r.get("code"),
            "name": r.get("name"),
            "price": to_num(r.get("trade")),
            "change_pct": to_num(r.get("changepercent")),
            "volume": to_num(r.get("volume")),
            "amount_yuan": amt,
            "amount_yi": round(amt / 1e8, 1) if amt else None,
        })
    return out


# ---------------------------------------------------------------- 华尔街见闻
def wallstreetcn_lives(n=30):
    """华尔街见闻A股频道7x24快讯（新浪7x24失败时的备用源）。"""
    j = json.loads(http_get("https://api-one.wallstcn.com/apiv1/content/lives"
                            "?channel=a-stock-channel&limit=%d" % n))
    items = ((j.get("data") or {}).get("items")) or []
    out = []
    for it in items:
        ts = it.get("display_time")
        out.append({
            "time": datetime.fromtimestamp(int(ts), TZ).strftime("%m-%d %H:%M")
                    if ts and str(ts).isdigit() else "",
            "text": re.sub(r"<[^>]+>", "", it.get("content") or "")[:220],
            "link": "",
        })
    return out


# ---------------------------------------------------------------- 新闻RSS
def google_news(query, n=6):
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(query)
           + "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans")
    root = ET.fromstring(http_get(url, timeout=20))
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        src_el = item.find("source")
        items.append({
            "title": re.sub(r"\s*-\s*[^-]{1,30}$", "", title),
            "link": (item.findtext("link") or "").strip(),
            "pubDate": (item.findtext("pubDate") or "").strip(),
            "source": (src_el.text or "").strip() if src_el is not None else "",
        })
        if len(items) >= n:
            break
    return items


# ---------------------------------------------------------------- 主采集
def collect():
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    data = {"date": today, "fetch_time": datetime.now(TZ).isoformat(), "status": {}}
    ETF_SHARES_PREV = _load_share_snapshot()
    print("[快照] 已加载 ETF 份额历史快照（%d 只）" % len(ETF_SHARES_PREV))

    def status(name, ok, note=""):
        data["status"][name] = ("ok" if ok else "失败") + (("(%s)" % note) if note else "")

    # 1) 交易日校验：上证指数当日是否有K线（腾讯K线失败时用新浪K线、行情快照时间兜底）
    try:
        bars = daily_kline("sh000001", 6)
        data["index_kline_tail"] = bars
        if bars:
            data["trading_day"] = bars[-1][0] == today
            status("交易日校验", True)
        else:
            raise RuntimeError("K线为空")
    except Exception as e:
        try:
            q = tencent_quotes("sh000001").get("sh000001") or {}
            qday = (q.get("quote_time") or "")[:8]
            data["trading_day"] = qday == today.replace("-", "") if qday else None
            status("交易日校验", data["trading_day"] is not None, "K线失败改用快照时间")
        except Exception as e2:
            data["trading_day"] = None
            data["index_kline_tail"] = []
            status("交易日校验", False, (str(e)[:60] + "|" + str(e2)[:60]))
    if data["trading_day"] is not True:
        return data

    # 1.5) 收盘校验：指数快照时间必须为今日且 >= 15:00，否则视为盘中，静默结束
    #      （腾讯行情不可用时改用新浪完整行情的日期时间）
    try:
        q, _src = stock_quotes(["sh000001"])
        qt = (q.get("sh000001") or {}).get("quote_time") or ""
        if qt and not (qt[:8] == today.replace("-", "") and qt[8:14] >= "150000"):
            data["trading_day"] = "盘中/快照未收盘(%s)" % qt
            return data
    except Exception:
        pass  # 校验失败不拦截，以K线交易日判断为准

    # 2) 指数与两市成交额（腾讯：上证指数含沪市总额，深证成指含深市总额；新浪完整行情兜底）
    try:
        q, src = stock_quotes(["sh000001", "sz399001", "sz399006", "sz399106"])
        sh, sz, cyb, szc = q.get("sh000001"), q.get("sz399001"), q.get("sz399006"), q.get("sz399106")
        data["indices"] = {"上证指数": sh, "深证成指": sz, "创业板指": cyb}
        if sh and sz and sh.get("amount_yuan") and sz.get("amount_yuan"):
            _total = sh["amount_yuan"] + sz["amount_yuan"]
            data["两市成交额_元"] = _total
            data["两市成交额_亿"] = round(_total / 1e8)
            data["两市成交额_万亿"] = round(_total / 1e12, 2)
        status("指数行情", True, "新浪备用源" if src == "新浪" else "")
    except Exception as e:
        status("指数行情", False, str(e)[:80])

    # 3) 板块热点（东财行业+概念，涨跌幅排序；失败时改用新浪板块，无主力资金字段）
    try:
        data["行业板块领涨"] = em_boards("industry", po=1)
        data["行业板块领跌"] = em_boards("industry", po=0)
        data["概念板块领涨"] = em_boards("concept", po=1)
        status("板块数据", True)
    except Exception as e:
        try:
            ind, con = sina_boards("industry"), sina_boards("concept")
            data["行业板块领涨"] = ind[:8]
            data["行业板块领跌"] = list(reversed(ind[-8:]))
            data["概念板块领涨"] = con[:8]
            status("板块数据", True, "新浪备用源(无主力资金)")
        except Exception as e2:
            status("板块数据", False, (str(e)[:40] + "|新浪:" + str(e2)[:40]))

    # 4) 个股榜与大盘资金（东财；个股榜失败时改用新浪排行，大盘资金仅东财有公开接口）
    try:
        data["涨幅榜"] = em_stock_rank("f3")
        data["成交额榜"] = em_stock_rank("f6")
        status("个股榜", True)
    except Exception as e:
        try:
            data["涨幅榜"] = sina_stock_rank("f3")
            data["成交额榜"] = sina_stock_rank("f6")
            status("个股榜", True, "新浪备用源")
        except Exception as e2:
            status("个股榜", False, (str(e)[:40] + "|新浪:" + str(e2)[:40]))
    try:
        data["大盘资金与涨跌家数"] = em_market_overview()
        status("大盘资金", True)
    except Exception as e:
        status("大盘资金", False, str(e)[:80])

    # 5) 国家队宽基ETF：行情+近5日均量对比+份额（东财f84总股本即基金份额）
    try:
        etf_quotes, _src = stock_quotes([c for c, _ in NATIONAL_ETFS])
        etfs = []
        for tcode, secid in NATIONAL_ETFS:
            row = dict(etf_quotes.get(tcode) or {"name": tcode})
            row["_secid"] = secid
            try:
                bars = daily_kline(tcode, 6)
                if len(bars) >= 2:
                    vols = [v for _, v in bars[:-1] if v]
                    row["prev5_avg_volume"] = round(sum(vols) / len(vols), 2) if vols else None
                    row["today_volume"] = bars[-1][1]
                    if vols and sum(vols):
                        row["volume_spike_ratio"] = round(bars[-1][1] / (sum(vols) / len(vols)), 2)
            except Exception:
                pass
            try:
                extra = em_stock_extra(secid, "f58,f84")
                row["fund_total_shares"] = to_num(extra.get("f84"))  # ETF总份额(份)
            except Exception:
                pass
            # 份额变化：今日份额与最近一次快照（Actions 缓存跨日持久化）的差额
            try:
                prev_shares = (ETF_SHARES_PREV or {}).get(secid, {}).get("fund_total_shares")
                if row.get("fund_total_shares") and prev_shares:
                    row["fund_total_shares_prev"] = prev_shares
                    row["fund_share_change"] = round(row["fund_total_shares"] - prev_shares, 0)
            except Exception:
                pass
            etfs.append(row)
        data["国家队宽基ETF"] = etfs
        status("宽基ETF", True)
    except Exception as e:
        status("宽基ETF", False, str(e)[:80])

    # 5.5) 东财关键板块失败时的补采（EM 偶发整站 502，等 60 秒再垫一轮）
    _retry_targets = [k for k in ("板块数据", "个股榜", "大盘资金") if str(data["status"].get(k, "")).startswith("失败")]
    _shares_missing = all(not (r or {}).get("fund_total_shares") for r in data.get("国家队宽基ETF", []) or [])
    if _retry_targets or _shares_missing:
        print("[补采] 东财关键数据缺失(%s%s)，等待60秒后重试一轮"
              % (",".join(_retry_targets), "，ETF份额" if _shares_missing else ""))
        time.sleep(60)
        for k in _retry_targets:
            try:
                if k == "板块数据":
                    data["行业板块领涨"] = em_boards("industry", po=1)
                    data["行业板块领跌"] = em_boards("industry", po=0)
                    data["概念板块领涨"] = em_boards("concept", po=1)
                elif k == "个股榜":
                    data["涨幅榜"] = em_stock_rank("f3")
                    data["成交额榜"] = em_stock_rank("f6")
                else:
                    data["大盘资金与涨跌家数"] = em_market_overview()
                data["status"][k] = "ok(60s补采成功)"
            except Exception as e:
                data["status"][k] = "失败(60s补采后仍失败: %s)" % str(e)[:60]
        if _shares_missing:
            for row in data.get("国家队宽基ETF", []):
                if row.get("fund_total_shares"):
                    continue
                try:
                    extra = em_stock_extra(row.get("_secid") or "", "f84")
                    row["fund_total_shares"] = to_num(extra.get("f84"))
                except Exception:
                    pass
            data["status"]["宽基ETF"] = "ok(60s补采后%s)" % ("部分恢复" if any(
                r.get("fund_total_shares") for r in data.get("国家队宽基ETF", [])) else "仍无份额")

    # 6) 电解铝：期货价格、LME、板块个股
    fut = None
    try:
        fut = em_futures_main("113.alm")  # 沪铝主力连续
    except Exception:
        fut = None
    if not fut:
        try:
            fut = sina_shfe_aluminum_fallback()
        except Exception:
            fut = None
    data["沪铝主力"] = fut
    status("沪铝期价", fut is not None)
    try:
        _lme = None
        for _attempt in range(2):
            try:
                _lme = sina_lme_aluminum()
            except Exception:
                _lme = None
            if _lme:
                break
            time.sleep(2)
        if not _lme:  # 新浪外盘接口已失效/被限：改走东财外盘期货
            try:
                _lme = em_lme_aluminum()
            except Exception:
                _lme = None
        data["LME铝"] = _lme
        status("LME铝", _lme is not None)
    except Exception as e:
        status("LME铝", False, str(e)[:80])
    try:
        data["铝板块个股"] = list(stock_quotes(ALUMINUM_STOCKS.split(","))[0].values())
        status("铝个股", True)
    except Exception as e:
        status("铝个股", False, str(e)[:80])
    # 库存/仓单：公开接口不稳定，直接交由新闻检索补齐；未命中则生成环节写"未查到"
    data["库存数据接口"] = "无稳定的公开日度库存接口，库存信息通过下方新闻条目提供"

    # 7) 新闻与公告（带原文链接）
    news = {}
    for key, query in [
        ("A股收盘", "A股 收盘 沪指"),
        ("国家队", "汇金 OR 国家队 ETF 增持"),
        ("电解铝库存", "电解铝 社会库存 OR LME铝库存"),
        ("氧化铝", "氧化铝 价格 利润"),
        ("电解铝产能", "电解铝 产能 政策 电价"),
        ("铝板块", "电解铝 板块 中国铝业"),
    ]:
        try:
            news[key] = google_news(query)
        except Exception as e:
            news[key] = []
            status("新闻-" + key, False, str(e)[:60])
        else:
            status("新闻-" + key, True)
    try:
        news["7x24快讯"] = sina_7x24()
        status("新浪7x24", True)
    except Exception as e:
        try:  # 新浪7x24不可用时改用华尔街见闻A股频道
            news["7x24快讯"] = wallstreetcn_lives()
            status("新浪7x24", True, "华尔街见闻备用源")
        except Exception as e2:
            news["7x24快讯"] = []
            status("新浪7x24", False, (str(e)[:40] + "|见闻:" + str(e2)[:40]))
    try:
        news["新浪财经滚动"] = sina_roll_news()
        status("新浪滚动", True)
    except Exception as e:
        news["新浪财经滚动"] = []
        status("新浪滚动", False, str(e)[:60])
    try:
        news["东方财富公告"] = em_notices()
        status("公告", True)
    except Exception as e:
        news["东方财富公告"] = []
        status("公告", False, str(e)[:60])
    try:
        news["东财滚动要闻"] = em_roll_news()
        status("东财滚动", True)
    except Exception as e:
        news["东财滚动要闻"] = []
        status("东财滚动", False, str(e)[:60])
    data["news"] = news

    if data.get("国家队宽基ETF"):
        if _save_share_snapshot(data["国家队宽基ETF"]):
            data["status"]["ETF份额快照"] = "ok"

    return data


if __name__ == "__main__":
    d = collect()
    out = os.path.join(DATA_DIR, "data.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    print("交易日:", d.get("trading_day"), "| 已写入", out)
    print("各数据源状态:", json.dumps(d.get("status", {}), ensure_ascii=False))
