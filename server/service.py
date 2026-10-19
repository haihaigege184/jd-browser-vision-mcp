#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jd-vision —— 「截图 → 可点元素」感知服务（部署在 10.0.0.11 宿主机）

═══════════════════════════════════════════════════════════════
为什么需要它
═══════════════════════════════════════════════════════════════
浏览器拟人操作的感知层是 UI Automation，但有三类页面 UIA 读不到：
  1. canvas 游戏（东东农场/种豆得豆之类，DOM 里根本没有按钮节点）
  2. 会让 Chrome 无障碍提供程序崩溃的页面（如 coupon.jd.com，UIA 枚举即断连）
  3. 纯图片型按钮（雪碧图/SVG，无文字节点）

解法：客户端把整屏截图发过来，本服务做「文字检测 + 识别 + 交互分类」，
返回每个元素的包围盒和中心点坐标。客户端拿坐标走 SendInput 真实点击。

🔴 红线：本服务只做感知，**永不执行任何点击**；动手永远发生在客户端
   （真实鼠标事件）。这样即使服务被滥用，也只是"看"，不是"点"。

═══════════════════════════════════════════════════════════════
引擎选型（2026-10-20 实测定型）
═══════════════════════════════════════════════════════════════
复用 /opt/ocrvenv（原本给 easyfrp 验证码用的）：ddddocr 1.6.1 + onnxruntime。
  · det 通道：ddddocr(det=True).detection() → 文字块包围盒
  · rec 通道：ddddocr().classification(裁剪块) → 文字内容
不装 rapidocr/paddleocr：RAM 只有 1.8G，能省则省；中文短词（按钮）识别
ddddocr 足够，长句精度不重要（按钮都是 2~4 个字）。

═══════════════════════════════════════════════════════════════
接口（全部 JSON；图片用 base64）
═══════════════════════════════════════════════════════════════
GET  /health
     -> {"ok":true,"engine":"ddddocr","det_loaded":bool,"rec_loaded":bool}

POST /v1/analyze   {"imageBase64": "..."}
     -> {"ok":true,"ms":123,"width":1366,"height":768,
         "elements":[{"id":"e1","text":"立即领取","kind":"button",
                      "score":0.95,"box":[x1,y1,x2,y2],"center":[cx,cy]}]}

POST /v1/find      {"imageBase64":"...","text":"领取","fuzzy":true,"limit":5}
     -> {"ok":true,"matches":[ ...同 element 结构，按相关度排序... ]}

kind 分类词表见 classify_kind()；这是全服务最需要持续迭代的部分。

部署：
  /opt/jdvision/service.py
  venv 复用 /opt/ocrvenv
  systemctl enable --now jd-vision     （unit: /etc/systemd/system/jd-vision.service）
"""
import base64
import difflib
import hashlib
import io
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from PIL import Image

MAX_IMG_BYTES = 8 * 1024 * 1024      # 8MB，整屏 PNG 足够
HOST = "0.0.0.0"
PORT = 8765

# ═══ 提速三件套（2026-10-20 实测定型）═══
# 1) 结果缓存：客户端对同一张截图经常连发 analyze + find("领取")，
#    J1900 全页 OCR 要 ~50s，缓存让第二次调用变 0s。LRU 4 张。
# 2) max_side=1150：OCR 前把大图等比缩小，rec 计算量明显下降；
#    实测 1150 对按钮级文字零损失（返回坐标已映射回原图尺寸）。
# 3) region 模式（analyze 已支持）：只 OCR 关键区域，日常必用。
CACHE_MAX = 4
_analyze_cache: dict[str, dict] = {}          # key -> analyze 结果
_cache_order: list[str] = []                  # 简易 LRU

OCR_MAX_SIDE = 1150


def _cache_key(image_bytes: bytes, region) -> str:
    h = hashlib.sha1(image_bytes).hexdigest()[:16]
    return f"{h}:{region}"


def _cache_get(key: str):
    if key in _analyze_cache:
        _cache_order.remove(key)
        _cache_order.append(key)              # 触摸即续命
        d = dict(_analyze_cache[key])
        d["cached"] = True
        return d
    return None


def _cache_put(key: str, data: dict):
    _analyze_cache[key] = {k: v for k, v in data.items() if k != "ms"}
    _cache_order.append(key)
    while len(_cache_order) > CACHE_MAX:
        old = _cache_order.pop(0)
        _analyze_cache.pop(old, None)

# ═══ 引擎链（2026-10-20 定型）：RapidOCR 优先，ddddocr 兜底 ═══
# 实测：ddddocr det 对 UI 截图的小字（12px 导航栏）漏检严重（整页只出 6 块），
# 放大 2x/3x 都没用；RapidOCR(PaddleOCR onnx) 对截图是主力，ddddocr 留作
# rapidocr 加载失败时的降级（比如极端小内存机器装不上）。
_rapid = None
_det = None
_rec = None
_load_err = None
ENGINE = "rapidocr"


def get_rapid():
    global _rapid, _load_err, ENGINE
    if _rapid is None and _load_err is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            # use_cls=False：截图文字不会倒置，cls 纯浪费（每块省 ~30% rec 时间）
            # intra_op_num_threads=2：J1900 只有 4 核且常年 load>3，线程少了反而快
            _rapid = RapidOCR(use_cls=False, intra_op_num_threads=2)
            ENGINE = "rapidocr"
        except Exception as e:                       # noqa: BLE001
            _load_err = f"rapidocr load: {type(e).__name__}: {e}"
    return _rapid


def get_det():                                       # ddddocr 兜底通道
    global _det, _load_err, ENGINE
    if _det is None and _load_err is None:
        try:
            import ddddocr
            _det = ddddocr.DdddOcr(det=True, show_ad=False)
            if ENGINE != "rapidocr":
                ENGINE = "ddddocr"
        except Exception as e:                       # noqa: BLE001
            _load_err = f"det load: {type(e).__name__}: {e}"
    return _det


def get_rec():
    global _rec, _load_err
    if _rec is None and _load_err is None:
        try:
            import ddddocr
            _rec = ddddocr.DdddOcr(show_ad=False)
        except Exception as e:                       # noqa: BLE001
            _load_err = f"rec load: {type(e).__name__}: {e}"
    return _rec


# ═════════════════════════════════════════════════════════════
# 交互分类词表（持续迭代区）
# ═════════════════════════════════════════════════════════════

# 强动作词：整词等于它 → button，score 0.95
BUTTON_EXACT = {
    "领取", "立即领取", "免费领取", "去领取", "点击领取", "一键领取",
    "签到", "立即签到", "去签到", "已签到",
    "浇水", "一键浇水", "去浇水", "施肥", "一键施肥", "收获", "一键收获",
    "领水滴", "收水滴", "去逛逛", "去浏览", "去完成", "去邀请", "邀请好友",
    "收下", "拆红包", "开宝箱", "兑换", "做任务", "去使用", "立即使用",
    "种植", "催熟", "领取奖励", "每日签到", "连续签到",
}

# 弱动作前缀：以它开头且 ≤8 字 → button，score 0.75
BUTTON_PREFIX = ("立即", "免费", "一键", "点击", "去", "领", "收", "抢", "开", "抽")

# 品牌词：出现即 button（score 0.85），常见于活动页角标
BUTTON_CONTAIN = ("领券", "优惠券领取", "免费领", "水滴", "领京豆")

# 非交互干扰：出现这些词的块降级为 text（比如「已领取」「领取记录」）
NON_ACTION = ("已领取", "领取记录", "领取成功", "明细", "规则", "记录", "说明")

PRICE_RE = re.compile(r"[¥￥]|\d+\.\d{2}")    # 价格 → 商品卡，不是按钮


def classify_kind(text: str, box, img_w: int) -> tuple[str, float]:
    """按词表 + 几何特征给文字块分类。

    返回 (kind, score)。kind ∈ button / text / info。
    几何规则：按钮通常窄（宽 < 1/4 屏），活动页大 banner 上的字不算按钮。
    """
    t = text.strip()
    if not t:
        return "text", 0.0
    w = box[2] - box[0]

    # 干扰词优先：降级
    if any(k in t for k in NON_ACTION):
        return "info", 0.3
    if PRICE_RE.search(t) or len(t) > 20:
        return "text", 0.2

    if t in BUTTON_EXACT:
        return "button", 0.95
    if any(k in t for k in BUTTON_CONTAIN) and len(t) <= 10:
        return "button", 0.85
    if t.startswith(BUTTON_PREFIX) and len(t) <= 8:
        return "button", 0.75
    # 窄块 + 短文本 + 动作词沾边 → 弱按钮
    if len(t) <= 6 and w < img_w * 0.25 and any(
            k in t for k in ("领", "签", "浇", "收", "去", "换", "抽")):
        return "button", 0.6
    return "text", 0.3


def analyze(image_bytes: bytes, region=None, max_side: int = OCR_MAX_SIDE) -> dict:
    """主流程：缓存 → 缩放 → 检测+识别 → 分类。

    region:   [x1,y1,x2,y2] 只分析某一块，坐标自动偏移回全屏系（提速主力）。
    max_side: OCR 前等比缩到该边长以内，识别后坐标映射回原图（1150 实测零损失）。
    同图同 region 的请求直接走缓存（analyze+find 连发场景从 2×50s 变 50s+0s）。
    """
    t0 = time.time()
    key = _cache_key(image_bytes, region)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    W, H = img.size                       # 全屏尺寸（最终坐标系）
    ox, oy = 0, 0
    if region:
        x1, y1, x2, y2 = [max(0, int(v)) for v in region]
        x2, y2 = min(x2, W), min(y2, H)
        if x2 - x1 > 8 and y2 - y1 > 8:
            img = img.crop((x1, y1, x2, y2))
            ox, oy = x1, y1

    # 大图缩小：rec 计算量与像素面积成正比，识别完坐标再放大回去
    scale = 1.0
    if max(img.size) > max_side:
        scale = max_side / max(img.size)
        img = img.resize((round(img.width * scale),
                          round(img.height * scale)), Image.LANCZOS)

    png = io.BytesIO()
    img.save(png, format="PNG")
    image_bytes = png.getvalue()

    raw = []                      # [(box(x1,y1,x2,y2), text, score)]
    rapid = get_rapid()
    if rapid is not None:
        result, _elapse = rapid(image_bytes)
        for item in result or []:
            pts, text, score = item[0], item[1], float(item[2])
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            raw.append(([int(min(xs)), int(min(ys)),
                         int(max(xs)), int(max(ys))],
                        str(text).strip(), score))
    else:
        det, rec = get_det(), get_rec()
        if det is None or rec is None:
            return {"ok": False, "error": _load_err or "engine not ready"}
        for b in det.detection(image_bytes):
            x1, y1, x2, y2 = [int(v) for v in b]
            crop = img.crop((max(0, x1 - 2), max(0, y1 - 2),
                             min(W, x2 + 2), min(H, y2 + 2)))
            buf = io.BytesIO()
            crop.save(buf, format="PNG")
            try:
                text = (rec.classification(buf.getvalue()) or "").strip()
            except Exception:                        # noqa: BLE001
                text = ""
            if text:
                raw.append(((x1, y1, x2, y2), text, 0.8))

    elements = []
    for i, (box, text, score) in enumerate(raw):
        if not text:
            continue
        # 缩放映射回原图 + region 偏移回全屏坐标系
        x1 = round(box[0] / scale) + ox
        y1 = round(box[1] / scale) + oy
        x2 = round(box[2] / scale) + ox
        y2 = round(box[3] / scale) + oy
        kind, kscore = classify_kind(text, box, W)
        elements.append({
            "id": f"e{i + 1}",
            "text": text,
            "kind": kind,
            "score": round(min(score, 1.0) * 0.5 + kscore * 0.5, 3),
            "box": [x1, y1, x2, y2],
            "center": [(x1 + x2) // 2, (y1 + y2) // 2],
        })

    # 按分数降序，便于客户端取最优
    elements.sort(key=lambda e: (-e["score"], e["box"][1]))
    data = {"ok": True, "ms": int((time.time() - t0) * 1000),
            "engine": ENGINE,
            "width": W, "height": H, "elements": elements}
    _cache_put(key, data)
    return data


def find(image_bytes: bytes, text: str, fuzzy: bool = True,
         limit: int = 8, region=None) -> dict:
    """在整页（或 region）里找指定文字的元素。fuzzy=True 用子串+编辑距离。"""
    data = analyze(image_bytes, region=region)
    if not data.get("ok"):
        return data
    q = text.strip()
    out = []
    for e in data["elements"]:
        t = e["text"]
        if t == q:
            m = 1.0
        elif q in t or (fuzzy and t in q):
            m = 0.9
        else:
            m = difflib.SequenceMatcher(None, q, t).ratio()
        if m >= (0.55 if fuzzy else 0.999):
            e2 = dict(e)
            e2["match"] = round(m, 3)
            out.append(e2)
    out.sort(key=lambda e: (-e["match"], -e["score"]))
    return {"ok": True, "ms": data.get("ms", 0), "width": data["width"],
            "height": data["height"], "matches": out[:limit]}


# ═══ HTTP 层 ═══

class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                                # noqa: N802
        if self.path == "/health":
            self._send(200, {"ok": True, "engine": ENGINE,
                             "rapid_loaded": _rapid is not None,
                             "det_loaded": _det is not None,
                             "load_err": _load_err})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):                               # noqa: N802
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > MAX_IMG_BYTES:
                self._send(413, {"ok": False, "error": "image too large"})
                return
            req = json.loads(self.rfile.read(n).decode("utf-8"))
            img = base64.b64decode(req.get("imageBase64", ""))
            region = req.get("region")
            if self.path == "/v1/analyze":
                self._send(200, analyze(img, region=region))
            elif self.path == "/v1/find":
                self._send(200, find(img, req.get("text", ""),
                                     bool(req.get("fuzzy", True)),
                                     int(req.get("limit", 8)),
                                     region=region))
            else:
                self._send(404, {"ok": False, "error": "not found"})
        except Exception as e:                       # noqa: BLE001
            self._send(500, {"ok": False,
                             "error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt, *args):               # noqa: A003
        pass                                         # 静默访问日志


def _warmup():
    """启动即后台加载模型（否则第一个请求要背 10~50s 的冷启动）。"""
    def _run():
        get_rapid() or (get_det(), get_rec())
        try:
            img = Image.new("RGB", (64, 32), "white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            analyze(buf.getvalue())
            print("warmup done", flush=True)
        except Exception as e:                       # noqa: BLE001
            print("warmup err:", e, flush=True)
    import threading
    threading.Thread(target=_run, daemon=True).start()


if __name__ == "__main__":
    print(f"jd-vision listening on {HOST}:{PORT}", flush=True)
    _warmup()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
