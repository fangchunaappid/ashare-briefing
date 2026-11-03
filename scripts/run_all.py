#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主流程：交易日校验 → 采集数据 → LLM生成简报 → 推送飞书。

非交易日（周末/节假日/临时休市）时静默结束：不生成简报、不推送、退出码0。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fetch_data  # noqa: E402
import generate_brief  # noqa: E402
import push_feishu  # noqa: E402


def main():
    print("== 1/4 采集数据并校验交易日 ==")
    data = fetch_data.collect()
    print("数据源状态:", json.dumps(data.get("status", {}), ensure_ascii=False))

    if data.get("trading_day") is not True:
        # 无法确认当日开市（含节假日、休市、数据源故障）一律静默结束，不发送任何内容
        print("非交易日或无法确认开市（%s），按规则静默结束，不推送。" % data.get("trading_day"))
        return 0

    print("== 2/4 生成简报 ==")
    text = generate_brief.generate(data)
    print("---- 简报预览（前600字）----")
    print(text[:600])

    print("== 3/4 推送飞书 ==")
    res = push_feishu.push(text)

    print("== 4/4 推送结果 ==")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if not res.get("ok"):
        return 1
    print("推送成功：%d 条消息全部发送成功。" % res.get("total_chunks", 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
