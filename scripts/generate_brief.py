#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调用 LLM API（OpenAI 兼容接口）生成 ≤30 条的A股收盘简报。

环境变量：
- LLM_API_KEY  必填
- LLM_BASE_URL 可选，默认 https://open.bigmodel.cn/api/paas/v4 （智谱GLM）
- LLM_MODEL    可选，默认 glm-5.3-flash

LLM 调用失败时退化为纯数据模板简报，保证推送链路不中断。
"""
import json
import os

import requests

MAX_ITEMS = 30

SYSTEM_PROMPT = "你是资深A股财经简报编辑。你严格遵守给定的数据与写作规则，绝不编造数字、新闻或链接。"

RULES = """写作规则（必须全部遵守）：
1. 全文编号条目总数不超过30条，开头一行标注收盘日期，格式如【A股收盘简报 2026-11-02】。
2. 按以下六个板块分组，用"一、二、三、四、五、六"标题：市场概览 / 板块热点 / 个股与资金 / 国家队动向（重点） / 工业金属·电解铝（重点） / 消息面。
3. 每条为一句话概括，句末附来源链接；链接只能从输入数据 news 部分给定的 link/url 字段中选取原文，严禁编造、拼接或改写链接；确实无对应链接时写"（查不到来源）"。
4. 指数点位、涨跌幅、成交额、库存、金属价格等所有数字必须直接引用输入数据中的数值，禁止四舍五入以外的任何加工，禁止使用你记忆中的数字；数据中缺失的指标直接写"未查到"。
4a. 金额表述必须使用数据中 *_亿 / *_万亿 字段（如"两市成交额2.04万亿""成交额953.3亿"），严禁输出以元为单位的长数字（如 953265155867.0）。指数点位小数可保留原样。
5. 国家队动向：只有当宽基ETF出现明显放量（volume_spike_ratio 明显大于1）或新闻中有汇金/证金/国家队相关报道时才能写；仅凭ETF放量异动推断的必须标注"（推断）"；完全查不到时明确写"今日未查到可确认的国家队动向"。
6. 工业金属·电解铝部分分三层：①库存 ②成交与价格 ③热点新闻；库存数据若未提供具体数值，如实写"未查到当日库存数据"。
7. 简体中文，条目按编号 1. 2. 3. 连续编号（跨板块连续编号，总条数≤30）。
8. 北向资金当日实时数据自2024年8月起已停止披露：输入数据中没有北向字段时，严禁编造当日北向净流入/流出数字，可引用新闻中的定性表述，或写"当日北向实时数据未披露"。"""


def _fmt_yi(v):
    if v is None:
        return "未查到"
    return "%.0f亿" % (v / 1e8) if abs(v) >= 1e8 else str(v)


def build_user_prompt(data):
    brief_data = {k: v for k, v in data.items() if k != "index_kline_tail"}
    return (
        "以下是通过行情接口与新闻检索得到的今日原始数据（JSON）。\n"
        + RULES
        + "\n\n原始数据：\n"
        + json.dumps(brief_data, ensure_ascii=False, indent=1)
    )


def llm_chat(user_text, retries=2):
    key = os.environ.get("LLM_API_KEY")
    if not key:
        raise RuntimeError("未配置 LLM_API_KEY")
    base = (os.environ.get("LLM_BASE_URL") or "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
    model = os.environ.get("LLM_MODEL") or "glm-5.3-flash"
    url = base + ("/chat/completions" if "/chat/completions" not in base else "")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.2,
        # glm-5.3-flash 强制深度思考（thinking 不可关闭）；reasoning_effort=low 为轻度推理，避免长 prompt 超时。
        "reasoning_effort": "low",
    }
    last_err = None
    for _ in range(retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=300,
                              headers={"Authorization": "Bearer " + key,
                                       "Content-Type": "application/json"})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError("LLM调用失败: %s" % last_err)


def fallback_brief(data):
    """LLM不可用时的兜底模板简报（纯数据，无编造）。"""
    lines = ["【A股收盘简报 %s】（LLM生成失败，以下为脚本模板简报，数字来自行情接口快照）" % data["date"]]
    idx = data.get("indices") or {}
    n = 0

    def add(s):
        nonlocal n
        n += 1
        lines.append("%d. %s（数据来自腾讯/东方财富行情接口，无网页来源链接）" % (n, s))

    for name in ("上证指数", "深证成指", "创业板指"):
        d = idx.get(name) or {}
        if d.get("price") is not None:
            add("%s收盘 %.2f 点，涨跌幅 %s%%" % (name, d["price"], d.get("change_pct")))
    if data.get("两市成交额_元"):
        add("两市成交额约 %s" % _fmt_yi(data["两市成交额_元"]))
    for label, key in [("领涨行业", "行业板块领涨"), ("领跌行业", "行业板块领跌"), ("领涨概念", "概念板块领涨")]:
        bl = data.get(key) or []
        if bl:
            names = "、".join("%s(%s%%)" % (b["name"], b["change_pct"]) for b in bl[:3] if b.get("name"))
            add("%s：%s" % (label, names))
    for d in (data.get("涨幅榜") or [])[:3]:
        add("%s(%s) 涨 %s%%" % (d.get("name"), d.get("code"), d.get("change_pct")))
    for d in (data.get("国家队宽基ETF") or []):
        if d.get("volume_spike_ratio") and d["volume_spike_ratio"] >= 2:
            add("%s今日放量约%s倍（推断，可能有国家队申购动向）" % (d.get("name"), d["volume_spike_ratio"]))
    add_lines = data.get("大盘资金与涨跌家数") or []
    if add_lines:
        d = add_lines[0]
        add("两市主力资金净流入 %s，上涨家数 %s，下跌家数 %s"
            % (_fmt_yi(d.get("main_inflow_yuan")), d.get("advancers"), d.get("decliners")))
    fut = data.get("沪铝主力")
    if fut:
        add("%s收盘 %s，涨跌幅 %s%%" % (fut.get("name"), fut.get("price"), fut.get("change_pct")))
    lme = data.get("LME铝")
    if lme:
        add("LME铝 %s，涨跌幅 %s%%" % (lme.get("price"), lme.get("change_pct")))
    for d in (data.get("铝板块个股") or []):
        if d.get("price") is not None:
            add("%s收盘 %s，涨跌幅 %s%%" % (d.get("name"), d.get("price"), d.get("change_pct")))
    add("库存、新闻、公告类条目需LLM生成，本次暂缺。")
    return "\n".join(lines)


def generate(data):
    try:
        text = llm_chat(build_user_prompt(data))
        print("[generate] LLM生成成功，长度", len(text))
    except Exception as e:  # noqa: BLE001
        print("[generate] LLM失败，使用兜底模板：", e)
        text = fallback_brief(data)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "brief.md"), "w", encoding="utf-8") as f:
        f.write(text)
    return text


if __name__ == "__main__":
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "data.json"), encoding="utf-8") as f:
        print(generate(json.load(f)))
