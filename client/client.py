#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jd-vision 客户端 —— 本地进程访问 10.0.0.11 上的感知服务。

分类（谁用哪个）：
  VisionClient          —— 程序内直连 HTTP（jd-automation 引擎用这个，最快）
  mcp_server.py         —— MCP stdio 工具集（供 MCP 宿主/其他 Agent 用，见同目录）
两者走同一个 HTTP API，语义一致。
"""
import base64
import json
import urllib.request

HOST = "http://10.0.0.11:8765"
TIMEOUT = 60            # 首次请求含模型懒加载，放宽到 60s


class VisionError(RuntimeError):
    pass


class VisionClient:
    """截图 → 可点元素。只做感知，返回坐标；点击由客户端 SendInput 完成。"""

    def __init__(self, host: str = HOST, timeout: int = TIMEOUT):
        self.host = host.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.host + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:                       # noqa: BLE001
            raise VisionError(f"{path}: {type(e).__name__}: {e}") from e

    @staticmethod
    def _b64(image) -> str:
        """image: bytes | base64 str | file path —— 三种都收。"""
        if isinstance(image, (bytes, bytearray)):
            return base64.b64encode(image).decode()
        s = str(image)
        if s.endswith(".png") or s.endswith(".jpg"):
            with open(s, "rb") as f:
                return base64.b64encode(f.read()).decode()
        return s                                     # 已是 base64

    def health(self) -> dict:
        with urllib.request.urlopen(self.host + "/health",
                                    timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def analyze(self, image, region=None) -> list[dict]:
        """整页（或 region=[x1,y1,x2,y2]）分析，返回 elements。

        region 用途：只 OCR 关键区域（弹层/按钮区），J1900 上能从 50s 降到几秒。
        返回坐标已偏移回全屏坐标系，可直接用于点击。
        """
        d = self._post("/v1/analyze", {"imageBase64": self._b64(image),
                                       "region": region})
        if not d.get("ok"):
            raise VisionError(d.get("error", "analyze failed"))
        return d["elements"]

    def find(self, image, text: str, fuzzy: bool = True,
             limit: int = 8, region=None) -> list[dict]:
        """按文字找元素（含模糊），按 match+score 排序。"""
        d = self._post("/v1/find", {"imageBase64": self._b64(image),
                                    "text": text, "fuzzy": fuzzy,
                                    "limit": limit, "region": region})
        if not d.get("ok"):
            raise VisionError(d.get("error", "find failed"))
        return d["matches"]

    def buttons(self, image, region=None) -> list[dict]:
        """analyze 的便捷封装：只要 kind=button 的元素。"""
        return [e for e in self.analyze(image, region=region)
                if e["kind"] == "button"]


if __name__ == "__main__":
    import sys
    vc = VisionClient()
    print("health:", vc.health())
    if len(sys.argv) > 1:                # python client.py <图片路径> [查找词]
        img = sys.argv[1]
        if len(sys.argv) > 2:
            for m in vc.find(img, sys.argv[2]):
                print(m)
        else:
            for e in vc.analyze(img):
                print(e)
