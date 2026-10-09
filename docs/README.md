# 微信自动回复 + Hermes Agent 对接

基于纯视觉感知（OCR + 像素检测）驱动 PC 微信，把回复交给 Hermes Agent 生成。
支持每联系人独立会话、完整 agent 工具能力、Hermes 侧主动推送。

> **路径约定**：命令示例默认已在**仓库根目录**，用相对路径（`.\`、`./`）。
> `%PYTHON%` 指你自己的 Python 3.8 解释器（仓库里的 `.bat` 原本写死了
> Miniconda 的绝对路径，换机器时请改成你的）；`<hermes 安装目录>` 指
> Hermes 的安装位置，按你的实际路径填。
> 想改用 DSH 作 agent 后端，见 [dsh-integration.md](dsh-integration.md)。

---

## 快速开始

### 前置条件

| 组件 | 要求 | 验证 |
|------|------|------|
| 微信 PC 版 | 3.x / 4.0，窗口可见 | — |
| Python | 3.8+（当前用 Miniconda） | `python -V` |
| Hermes Gateway | 已装，含 api_server 平台 | `curl 127.0.0.1:8642/health` |

### 三步启动

**① 启动 Hermes gateway**（提供 API Server :8642）
```
hermes gateway
```

**② 启动 bot**（提供 Bridge :8900 + OCR 扫描）
```
.\run_wechat_bot.bat
```

脚本内容（等价于手动执行）：
```bat
@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%PYTHON%" wechat_ai_bot_dev.py
pause
```

**③ 发消息测试**
在微信里让白名单内的联系人发条消息。日志出现 `🌉 [Bridge] 转发给 Hermes:` 即链路打通。

---

## 架构

```
┌─ 感知层（wechat_ai_bot_dev.py，原项目逻辑未改动）─────────┐
│  EnumWindows → PrintWindow 离屏截图 → RapidOCR            │
│  · 侧边栏红点巡检（找未读会话）                            │
│  · 顶栏标题交叉锚定（确认当前对话对象）                    │
│  · 白名单硬门禁 is_name_matched_strict()                  │
│  · 气泡真彩流向分析（区分我方绿底 / 对方白底）             │
└───────────────────┬───────────────────────────────────────┘
                    │ {联系人, 消息}
                    ▼
┌─ Bridge 层（hermes_bridge_core.py，同进程内的独立线程）──┐
│  SessionMap    联系人 ↔ Hermes session_id（落盘 JSON）    │
│  HermesClient  调 API Server                             │
│  SendQueue     串行化所有 GUI 操作                       │
│  HTTP :8900    /health /hermes /status /send /chat       │
└───────────────────┬───────────────────────────────────────┘
                    │
                    ▼
┌─ Hermes Agent（API Server :8642，常驻 gateway 内）───────┐
│  每联系人 = 独立 session，SQLite 持久化                    │
│  完整工具：web / terminal / file / memory / delegation    │
└───────────────────────────────────────────────────────────┘
```

### 双向通道

**被动应答**（微信 → Hermes → 微信）
```
微信消息 → OCR → BRIDGE.chat() → POST /api/sessions/{id}/chat
         → 回复 → SendQueue → send_reply_instant() → 微信
```

**主动推送**（Hermes → 微信）
```
agent 调 wechat_send 工具 → POST :8900/send → SendQueue → 微信
```

两条路共用同一个 `SendQueue`——多线程同时操作鼠标键盘必然出错，必须串行。

---

## 性能对比

| 指标 | Hermes CLI（旧） | API Server + Bridge（现） |
|------|-----------------|------------------------|
| 单次延迟 | 12 秒（每次冷启动） | **5.6 秒** |
| 上下文 | 内存数组，进程重启即丢 | **SQLite 持久**，跨重启 |
| 会话隔离 | 手工维护 dict | **每联系人独立 session** |
| 工具调用 | ❌ 只能生成文本 | ✅ 全套 agent 能力 |
| 主动推送 | ❌ 不可能 | ✅ `wechat_send` 工具 |

---

## 配置文件

### `wechat_config_dev.json`（必需）

> ⚠️ `load_config()` **写死读这个文件名**。命令行 `--config xxx.json` 参数无效，
> 传了也不生效——这是原项目遗留，不要试图用参数指定其他文件。

```json
{
  "whitelist": ["张三"],
  "blacklist": ["文件传输助手", "公众号", "天良视频"],
  "whitelist_mode": true,

  "hermes_api": "http://127.0.0.1:8642",
  "hermes_api_key": "wechat-local-dev",
  "bridge_port": 8900,
  "bridge_chat_timeout": 180,

  "hermes_cli_path": "<hermes 安装目录>/venv/Scripts/hermes.exe chat",

  "openai_api": {
    "api_key": "", "api_base": "", "model": "", "temperature": 0.7
  },
  "friend_personas": { "张三": "幽默风趣的真人好友" },
  "check_interval_seconds": 0.5
}
```

**实际生效的字段**（其余未列出的走代码默认值）：

| 字段 | 默认值 | 作用 |
|------|--------|------|
| `whitelist` | `[]` | 只回复名单内的人 |
| `blacklist` | `[]` | 硬拦截，优先于白名单 |
| `whitelist_mode` | `true` | 关掉则回复所有非关键词对话 |
| `hermes_api` | `http://127.0.0.1:8642` | API Server 地址 |
| `hermes_api_key` | 环境变量 `HERMES_API_KEY` | 必填 |
| `bridge_port` | `8900` | Bridge 监听端口 |
| `bridge_chat_timeout` | `180` | 单次对话超时（秒） |
| `hermes_cli_path` | `""` | 降级路径，空则不降级 |
| `openai_api.*` | 见 `wechat_config.example.json` | 三级降级的最后一层 |
| `friend_personas` | `{}` | 每人专属人设（仅 CLI 降级路径用） |
| `check_interval_seconds` | `0.8` | 扫描间隔 |
| `ignored_keywords` | `[]` | 消息含这些词则忽略 |
| `reply_prefix` / `include_prefix` | `""` / `false` | 回复前缀 |

> ⚠️ `ai_engine` 字段代码**从不读取**，是原项目遗留的死配置，可忽略。

| 字段 | 作用 |
|------|------|
| `whitelist` | 只回复名单内的人。群名可直接加，无需特殊标识 |
| `blacklist` | 硬拦截，在白名单判断之前生效 |
| `hermes_api` | Hermes API Server 地址 |
| `hermes_api_key` | 必填，无 key 一律 401（连 127.0.0.1 也一样） |
| `hermes_cli_path` | **降级用**。Bridge 不可用时退回 CLI（12 秒） |
| `bridge_chat_timeout` | 单次对话超时秒数 |

### `wechat_convo_map.json`（自动维护）

```json
{ "张三": "api_1791563112_fdce742a" }
```

联系人 ↔ session_id 映射。**不要手删**，删了会重建 session（丢上下文）。

---

## Bridge HTTP API

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/health` | Bridge 存活（**不查 Hermes**，实测 2ms 返回） |
| GET | `/hermes` | Hermes 状态（走后台缓存，不阻塞） |
| GET | `/status` | 会话映射 + 队列长度 |
| POST | `/send` | 主动推送 `{"contact":"X","text":"Y"}` |
| POST | `/chat` | 手动触发对话（调试用） |

```bash
curl http://127.0.0.1:8900/status
curl -X POST http://127.0.0.1:8900/send \
  -H "Content-Type: application/json" \
  -d '{"contact":"张三","text":"在吗"}'
```

---

## Hermes 侧配置

### 启用 API Server

写进 `~/.hermes/config.yaml` 的 **`platforms.api_server.extra`**（不是 `gateway.api_server`）：

```yaml
platforms:
  api_server:
    enabled: true
    extra:
      host: 127.0.0.1
      port: 8642
      key: wechat-local-dev
```

验证：
```bash
grep -i api_server ~/.hermes/logs/gateway.log
# 期望：[Api_Server] API server listening on http://127.0.0.1:8642
```

### 微信推送插件

`~/.hermes/plugins/wechat_bridge/` 提供 `wechat_send` / `wechat_status` 两个工具。

启用需**两步**（缺一不可）：
```yaml
plugins:
  enabled: [wechat_bridge]
platform_toolsets:
  cli: [ ..., wechat_bridge ]     # ← 少了这行，模型看不到工具
```

> 只写 `plugins.enabled` 不够。工具名还要进入会话的 toolset 选择集合，
> 详见 `hermes-custom-tool-playbook` skill 的「坑 1」。

---

## 排障

| 现象 | 原因 | 处理 |
|------|------|------|
| 日志只有启动横幅 | 微信窗口类名不匹配 | 确认是 3.x/4.0；新版 `Weixin.exe` 类名不同 |
| 一直走 `🤖 [Hermes] CLI` | Bridge 没起来或连不上 | `curl :8900/health`；查日志有无 `🌉 Hermes Bridge` |
| 消息没反应 | 读的是默认白名单 | 确认 `wechat_config_dev.json` 存在（`--config` 无效！） |
| Bridge `/health` 超时 | 老版本同步查 Hermes 饿死 | 已修；确认代码是最新版 |
| 8900 有多个 LISTENING | Windows `allow_reuse_address` 僵尸 | 已修；`taskkill` 清残留后重启 |
| `UnicodeDecodeError` | Windows GBK 解码 UTF-8 | 已修（`errors="replace"`） |
| CLI 报 usage 错误 | PATH 命中旧版 hermes | 配置写**绝对路径**；`where hermes` 有 5 个结果 |
| gateway 反复退出 | 未查明 | 看 `~/.hermes/logs/gateway.log` 尾部 |

### 快速诊断命令

```bash
# 微信窗口能否找到
tasklist | grep -iE "wechat|weixin"

# Bridge 是否在线
curl -s http://127.0.0.1:8900/health

# Hermes 是否在线
curl -s http://127.0.0.1:8642/health

# 会话映射
curl -s http://127.0.0.1:8900/status

# bot 日志
tail -30 ./wechat_bot_dev.log

# 停掉旧进程
python kill_wechat_bot_dev.py
```

---

## 端口一览

| 端口 | 服务 | 归属进程 |
|------|------|---------|
| 8642 | Hermes API Server | `hermes gateway` |
| 8900 | 微信 Bridge | `wechat_ai_bot_dev.py` |

---

## 已知限制

- **依赖环境不同**：bot 跑在 Miniconda（Python 3.8），Hermes 在自己的 venv（3.11）。
  因此 Bridge 走 HTTP 而非 import——C 扩展跨版本不保证 ABI 兼容。
- **bot 需手动启动**：未做开机自启。
- **`.venv` 未装完**：`opencv-python`（40MB）下载慢，目前用 Miniconda。
- **群聊需在白名单**：群名直接加进 `whitelist` 即可，不需要特殊处理。
- **侧边栏可见性**：只扫描前 ~15 个会话，滚不动的看不到。
