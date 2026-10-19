# jd-browser-vision-mcp

**浏览器 AI 操作的视觉感知层**：把「无按钮页面」的截图（canvas 游戏、纯图片按钮、
UIA 读不到/一读就崩的页面）转换成**带点击坐标的可交互按钮清单**，通过 MCP 工具集
或 HTTP 接口供上层 Agent 直接使用。

> 🔴 安全边界：本工具集**只做感知（看），永远不执行点击**。
> 动手永远由客户端完成（推荐 SendInput 真实鼠标事件 / 拟人引擎）。
> 这样即使感知层被滥用，最坏结果也只是"看到了什么"，不会误点任何东西。

## 它解决什么问题

浏览器自动化的感知层通常有三条路，各有死角：

| 感知方式 | 死角 |
|---|---|
| DOM / CDP | 需要调试端口（有自动化指纹）；canvas 游戏无 DOM 节点 |
| UI Automation | canvas 无按钮节点；部分页面会让 Chrome 无障碍提供程序直接崩溃 |
| **视觉（本工具集）** | 上面两类页面的兜底：截图 → OCR → 交互分类 → 坐标 |

## 架构

```
┌─ 客户端（你的电脑）──────────────┐   ┌─ 感知服务（任意 Linux 主机）─┐
│ 截图(SendInput 引擎/MCP 截图)    │   │  server/service.py           │
│   ↓ base64                      │ → │  ddddocr det+rec             │
│ mcp/mcp_server.py（MCP 工具集）  │   │  文字块 → 交互分类           │
│ client/client.py（HTTP 直连）    │   │  button/text/info + 坐标     │
└──────────────────────────────────┘   └──────────────────────────────┘
        ↓ 拿坐标后由你自己的引擎拟人点击（Move 悬停瞄准 → Click）
```

## 工具集分类

### 【系统】
| 工具 | 说明 |
|---|---|
| `vision_health` | 服务在线 / OCR 引擎加载状态 |

### 【感知 · 全页】
| 工具 | 说明 |
|---|---|
| `vision_analyze_page` | 整页元素：`{id, text, kind, score, box, center}` |
| `vision_list_buttons` | 只返回 `kind=button`（日常首选） |

### 【感知 · 定向】
| 工具 | 说明 |
|---|---|
| `vision_find_text` | 按文字找元素（子串 + 编辑距离模糊），返回坐标 |

HTTP API 三个端点都支持可选 `region:[x1,y1,x2,y2]` —— 只 OCR 该子区域，
返回坐标已偏移回全屏坐标系（弱 CPU 机器上从 ~50s 降到 ~10s 的关键）。

`kind` 词表：**button** = 可交互按钮；**text** = 普通文字；**info** = 干扰块
（"已领取""领取记录""规则"这类降级块）。词表在 `server/service.py` 的
`classify_kind()`，是持续迭代的核心区。

## 部署

### 1. 感知服务（Linux 主机，1GB 内存即可）

```bash
pip install ddddocr onnxruntime opencv-python-headless pillow   # venv 推荐
sudo cp server/service.py /opt/jdvision/service.py
# systemd unit 见下（MemoryMax=700M 防止挤爆小内存机器）
sudo systemctl enable --now jd-vision
curl http://127.0.0.1:8765/health
```

systemd unit：

```ini
[Unit]
Description=jd-vision: screenshot -> clickable elements
After=network.target

[Service]
ExecStart=/opt/yourvenv/bin/python /opt/jdvision/service.py
Restart=always
RestartSec=3
MemoryMax=700M

[Install]
WantedBy=multi-user.target
```

### 2. MCP 工具集（本机，接你的 MCP 宿主）

```bash
pip install mcp
```

MCP 配置：

```json
{
  "jd-vision": {
    "command": "python",
    "args": ["/path/to/mcp/mcp_server.py"],
    "env": { "JD_VISION_HOST": "http://10.0.0.11:8765" }
  }
}
```

### 3. HTTP 直连（不走 MCP 时）

```python
from client.client import VisionClient
vc = VisionClient("http://10.0.0.11:8765")
buttons = vc.buttons(screenshot_bytes)      # [{text,kind,score,box,center}]
# 然后由你的引擎：Move 悬停(center) → 停 0.15~0.5s → Click(center)
```

## 实测记录（2026-10-20）

- 京东领券中心 `pro.jd.com` 活动页：UIA 枚举即让 Chrome 无障碍提供程序崩溃
  （`0x80070057` → MCP 断连），**视觉链路完整跑通**：识别出 6 个「立即领取」按钮，
  拟人点击后券到账（按钮「立即领取」→「去使用」，无成功弹窗，这是最可靠的成功判据）。
- 引擎链：**RapidOCR(PaddleOCR onnx) 主力** —— UI 截图上 132 元素 vs ddddocr det 的 6 个
  （12px 导航小字 ddddocr 漏检严重，放大 2x/3x 无效）；ddddocr 保留为降级通道。
- 性能（Celeron J1900 4 核，load 常年 >3）：
  - 全页 1366×768：~50s（rec 是大头，130+ 块 × ~0.3s）
  - **region 区域模式**：只 OCR 子区域，坐标自动偏移回全屏 → 小区域 ~10s，日常必用
  - `use_cls=False`（截图文字不会倒置）+ `intra_op_num_threads=2`（弱 CPU 上反而更快）
  - 服务启动即后台 warmup，第一个请求不再背冷启动
- 排障：`/health` 看引擎加载；`d["engine"]` 确认当前通道。

## 已知局限 / 迭代方向

- ddddocr det 对**密集小字**（搜索结果页商品标题流）漏检较多，按钮级大字可靠；
  后续可加 PaddleOCR 通道做密集页兜底。
- 词表目前面向中文电商（领取/签到/浇水/兑换…），欢迎按自己场景扩充
  `BUTTON_EXACT / BUTTON_PREFIX / BUTTON_CONTAIN / NON_ACTION`。
- 暂无模板匹配通道（固定图标按钮如"+"）——计划中。
- 截图坐标 = 屏幕绝对坐标（主屏），多显示器/缩放场景需客户端自行换算。

## License

MIT
