#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推送简报到飞书自定义机器人。

- 按行边界拆分为 ≤3800 字符的多条消息，每条开头标注（第x/共y条）
- 校验响应 code/StatusCode == 0，失败自动重试一次
"""
import json
import os
import time

import requests

CHUNK_LIMIT = 3800


def split_text(text):
    chunks, cur = [], ""
    for line in text.split("\n"):
        if not cur:
            cur = line
        elif len(cur) + len(line) + 1 <= CHUNK_LIMIT:
            cur += "\n" + line
        else:
            chunks.append(cur)
            cur = line
    if cur:
        chunks.append(cur)
    return chunks


def send_one(webhook, text):
    r = requests.post(webhook, json={"msg_type": "text", "content": {"text": text}},
                      headers={"Content-Type": "application/json"}, timeout=20)
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        j = {"http_status": r.status_code, "raw": r.text[:200]}
    ok = (j.get("code") == 0) or (j.get("StatusCode") == 0)
    return ok, j


def push(text):
    webhook = os.environ.get("FEISHU_WEBHOOK", "")
    if not webhook:
        return {"ok": False, "error": "未配置 FEISHU_WEBHOOK 环境变量"}
    chunks = split_text(text)
    total = len(chunks)
    results, all_ok = [], True
    for i, chunk in enumerate(chunks, 1):
        body = chunk if total == 1 else "（第%d/共%d条）\n%s" % (i, total, chunk)
        ok, resp = False, None
        for attempt in (1, 2):  # 失败重试一次
            try:
                ok, resp = send_one(webhook, body)
            except Exception as e:  # noqa: BLE001
                resp = {"error": str(e)}
            if ok:
                break
            print("[push] 第%d条第%d次发送失败: %s" % (i, attempt, json.dumps(resp, ensure_ascii=False)))
            time.sleep(3)
        results.append({"idx": i, "ok": ok, "resp": resp})
        if not ok:
            all_ok = False
    return {"ok": all_ok, "total_chunks": total, "results": results}


if __name__ == "__main__":
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "brief.md"), encoding="utf-8") as f:
        text = f.read()
    res = push(text)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    raise SystemExit(0 if res.get("ok") else 1)
