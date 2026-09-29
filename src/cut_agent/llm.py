"""本地 LLM 客户端（OpenAI 兼容 /v1/chat/completions）。

只依赖 `openai` SDK 或原生 requests。这里用原生 requests，避免额外依赖与版本漂移，
并统一处理"要求 JSON"的调用（剥离 ```json 代码块、容错解析）。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable

import requests

from .config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from .runctl import retry_checkpoint


class LLMError(RuntimeError):
    pass


def _post_chat(messages: list[dict], temperature: float, max_tokens: int,
               timeout: float = 600.0, seed: int | None = None) -> str:
    url = f"{LLM_BASE_URL.rstrip('/')}/chat/completions"
    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if seed is not None:
        payload["seed"] = seed
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    last_err = "no attempt"
    for attempt in range(3):
        retry_checkpoint()
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if r.status_code == 400:
                # 常见：max_tokens 超过 ctx 等参数问题，降级重试
                payload["max_tokens"] = max(512, max_tokens // 2)
                last_err = f"HTTP 400: {r.text[:300]}"
                continue
            r.raise_for_status()
            data = r.json()
            content = (data["choices"][0]["message"]["content"] or "").strip()
            if not content:
                # 单 LLM 实例被并发争用时可能返回空；退避重试
                last_err = "model returned empty content"
                if attempt < 2:
                    retry_checkpoint(5 * (attempt + 1))
                continue
            return content
        except (requests.RequestException, KeyError, IndexError) as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < 2:
                retry_checkpoint(2 * (attempt + 1))
    raise LLMError(f"chat completion failed: {last_err}")


def chat(system: str, user: str, *, temperature: float = 0.4,
         max_tokens: int = 2048, seed: int | None = None) -> str:
    """单轮对话，返回助手文本。"""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    return _post_chat(messages, temperature, max_tokens, seed=seed)


def chat_vision(system: str, parts: list[dict], *, temperature: float = 0.2,
                max_tokens: int = 2048, timeout: float = 600.0) -> str:
    """多模态单轮：parts 为 user content 数组（{"type":"text"} / {"type":"image_url"}）。

    本地 llama.cpp 需以 --mmproj 加载视觉模型，端点 capabilities 含 multimodal。
    服务未启/不支持图像时抛 LLMError，由调用方降级。
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": parts},
    ]
    return _post_chat(messages, temperature, max_tokens, timeout)


def chat_json(system: str, user: str, *, temperature: float = 0.2,
              max_tokens: int = 3000, seed: int | None = None) -> Any:
    """要求模型输出 JSON 并解析。容错：剥离 markdown 代码块、截取首个 {…} / […]。"""
    raw = chat(
        system + "\n\n你必须只输出合法 JSON，不要输出任何解释、注释或代码块标记。",
        user,
        temperature=temperature,
        max_tokens=max_tokens,
        seed=seed,
    )
    return parse_json_lenient(raw)


def parse_json_lenient(raw: str) -> Any:
    raw = raw.strip()
    # 剥离 ```json ... ``` 或 ``` ... ```
    m = re.search(r"```(?:json)?\s*(.+?)```", raw, re.DOTALL)
    if m:
        raw = m.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # 截取首个平衡的 {…} 或 […]
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = raw.find(open_ch)
        if start == -1:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(raw)):
            c = raw[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    chunk = raw[start:i + 1]
                    try:
                        return json.loads(chunk)
                    except json.JSONDecodeError:
                        break
    # 截断容错：输出被 max_tokens 截断时，补闭合未结束的字符串与括号
    repaired = _repair_truncated(raw)
    if repaired is not None:
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass
    raise LLMError(f"无法从模型输出解析 JSON: {raw[:400]}")


def _repair_truncated(raw: str) -> str | None:
    """把被截断的 JSON 补全：找到最后一个"完整值"的位置截断，闭合未结束的字符串与括号。

    常见截断形态（被 max_tokens 切断）：
    - 值字符串中间:  ..."text": "镜头转到山间…   -> 丢弃半截值，保留之前完整内容
    - 数组元素中间:  ..."kw": ["a","b","c        -> 保留 ["a","b","c"]
    - key 字符串中间: ..., "kw_cn"                -> 丢弃半截 key
    原则：只保留结构完整的部分，宁可少一段也不产出非法 JSON。
    """
    start = -1
    for ch in "{[":
        i = raw.find(ch)
        if i != -1 and (start == -1 or i < start):
            start = i
    if start == -1:
        return None
    body = raw[start:]
    n = len(body)

    def next_non_space(idx: int) -> str:
        j = idx
        while j < n and body[j] in " \t\r\n":
            j += 1
        return body[j] if j < n else ""

    last_safe = 0  # body[:last_safe] 以"完整值"结尾的最大下标
    in_str = False
    esc = False
    i = 0
    while i < n:
        c = body[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
                if next_non_space(i + 1) != ":":
                    last_safe = i + 1  # 完整字符串值（非 key）
            i += 1
            continue
        if c == '"':
            in_str = True
        elif c in "}]":
            last_safe = i + 1  # 完整对象/数组值
        elif c == ",":
            last_safe = i  # 逗号前的值已完整
        i += 1

    if last_safe <= 0:
        return None
    prefix = body[:last_safe].rstrip()
    # 去掉悬空的 ":" / "," / 未开始的字符串引号
    while prefix and prefix[-1] in ":,":
        prefix = prefix[:-1].rstrip()
    if in_str:
        # 截断时若最后一个完整值后还开了新字符串，prefix 已不含它（last_safe 未更新），无需补
        pass
    # 重算前缀内的未闭合括号并补全
    stack: list[str] = []
    in_str = False
    esc = False
    for c in prefix:
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            stack.append("}")
        elif c == "[":
            stack.append("]")
        elif c in "}]":
            if stack:
                stack.pop()
    return prefix + "".join(reversed(stack))


def describe_image_via_llm(image_path: str, prompt: str) -> str:
    """用本地多模态 LLM 描述一张图片。视觉服务不可用时抛 LLMError（由调用方降级）。"""
    import base64
    from pathlib import Path
    p = Path(image_path)
    b64 = base64.b64encode(p.read_bytes()).decode()
    mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    parts = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
    ]
    return chat_vision(
        "你是视频画面标注员，用中文回答，描述要具体（主体/环境/运动/色调），不要空话。",
        parts, temperature=0.2, max_tokens=800,
    )
