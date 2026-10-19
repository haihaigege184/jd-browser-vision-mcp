#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jd-vision MCP 工具集（stdio）—— 「截图 → 带坐标的可点按钮」

═══════════════════════════════════════════════════════════════
定位
═══════════════════════════════════════════════════════════════
浏览器自动化有两层：
  感知（看）—— 本工具集：截图 → OCR → 交互分类 → 元素坐标
  动手（点）—— 客户端：拿坐标走 SendInput / 真实鼠标事件
本工具集**永远不执行点击**，只返回坐标和分类。这是安全边界：
感知层被再怎么滥用，也只是"看"。

适用场景：UI Automation 读不到的页面 —— canvas 游戏、纯图片按钮、
会让 Chrome 无障碍提供程序崩溃的页面。

═══════════════════════════════════════════════════════════════
工具分类
═══════════════════════════════════════════════════════════════
【系统】
  vision_health                    服务健康检查（是否加载了引擎）
【感知 · 全页】
  vision_analyze_page              整页元素（含 button/text/info 分类）
  vision_list_buttons              只返回可交互按钮（最常用）
【感知 · 定向】
  vision_find_text                 按文字找元素（子串+编辑距离模糊）

图片入参统一支持三种：本地路径 / base64 / (data URL 前缀可省)。
kind 词表：button=可交互  text=普通文字  info=干扰块（如「领取记录」）

═══════════════════════════════════════════════════════════════
运行
═══════════════════════════════════════════════════════════════
依赖：pip install mcp          （服务端在 10.0.0.11:8765，可用 env 覆盖）
  JD_VISION_HOST=http://10.0.0.11:8765 python mcp_server.py

MCP 配置示例（宿主 json）：
{
  "jd-vision": {
    "command": "python",
    "args": ["/path/to/mcp_server.py"],
    "env": {"JD_VISION_HOST": "http://10.0.0.11:8765"}
  }
}
"""
import os
import sys
from pathlib import Path

# 允许 `python mcp_server.py` 直接跑（client.py 在同目录）
sys.path.insert(0, str(Path(__file__).parent))

from client import VisionClient, VisionError  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402

vc = VisionClient(os.environ.get("JD_VISION_HOST", "http://10.0.0.11:8765"))

mcp = FastMCP(
    "jd-vision",
    instructions=(
        "截图感知工具集：把浏览器截图转成带坐标的可点元素。"
        "只做感知不执行点击；坐标返回后由客户端用真实鼠标事件点击。"
    ),
)


# ─────────────── 【系统】───────────────

@mcp.tool()
def vision_health() -> str:
    """检查视觉服务是否在线、OCR 引擎是否已加载。"""
    try:
        return str(vc.health())
    except VisionError as e:
        return f"OFFLINE: {e}"


# ─────────────── 【感知 · 全页】───────────────

@mcp.tool()
def vision_analyze_page(image: str) -> str:
    """整页分析：返回全部文字元素（id/text/kind/score/box/center）。

    image: 本地图片路径或 base64。
    kind:  button=可交互按钮 / text=普通文字 / info=干扰块（已领取、规则等）。
    center 即建议点击坐标（客户端应再 hover 瞄准后点击）。
    """
    try:
        els = vc.analyze(image)
        return f"共 {len(els)} 个元素：\n" + "\n".join(
            f"{e['id']} [{e['kind']} {e['score']:.2f}] \"{e['text']}\" "
            f"center={e['center']} box={e['box']}" for e in els)
    except VisionError as e:
        return f"ERROR: {e}"


@mcp.tool()
def vision_list_buttons(image: str) -> str:
    """只列出可交互按钮（kind=button，按 score 降序）。日常首选入口。"""
    try:
        els = [e for e in vc.analyze(image) if e["kind"] == "button"]
        if not els:
            return "未发现可交互按钮（可能整页都是 canvas 图片，或词表未覆盖）"
        return f"发现 {len(els)} 个按钮：\n" + "\n".join(
            f"{e['id']} [{e['score']:.2f}] \"{e['text']}\" "
            f"center={e['center']}" for e in els)
    except VisionError as e:
        return f"ERROR: {e}"


# ─────────────── 【感知 · 定向】───────────────

@mcp.tool()
def vision_find_text(image: str, text: str, fuzzy: bool = True) -> str:
    """按文字找元素并返回坐标。fuzzy=True 支持子串与编辑距离匹配。

    例：vision_find_text(img, "领取") → 所有含"领取"的按钮按相关度排序。
    """
    try:
        ms = vc.find(image, text, fuzzy=fuzzy)
        if not ms:
            return f"未找到含「{text}」的元素"
        return f"找到 {len(ms)} 个匹配：\n" + "\n".join(
            f"{m['id']} match={m['match']:.2f} [{m['kind']}] \"{m['text']}\" "
            f"center={m['center']}" for m in ms)
    except VisionError as e:
        return f"ERROR: {e}"


if __name__ == "__main__":
    mcp.run()
