# DSH 接入：bot 支持多种 agent 后端

> **项目地址**：<https://github.com/Luofeng-Cloud/WeChat-AI-AutoReply>

bot 的 agent 后端是**可插拔的**。`hermes_api` 只是一个地址——后面挂的到底是
谁，bot 不关心；只要它讲 `HermesClient` 认的那四条 HTTP 方言，就能当上游。

于是同一个 bot 可以挂多种后端：

| 后端 | 监听 | 谁提供 |
|------|------|--------|
| Hermes | `:8642` | 真实 Hermes gateway（v0.21.5） |
| DSH | `:8760` | 本插件 `dsh-wechat-aireply` |
| 任意 | 自定 | 任何实现那四条路由的服务 |

它们可以**同时在跑**，互不干扰——端口不同、进程不同、bot 侧用不同的配置文件
选一个。想切回 Hermes 就换回 `wechat_config_dev.json`，没有任何迁移成本。

bot 侧**新增了服务层**（`main_loop()` 内联启动的 Hermes Bridge + 三级降级调用），
相对上游原版是 **+194 / −23** 行；其中真正的 HTTP 服务体在独立文件
`hermes_bridge_core.py` 里。**DSH 接入这一步本身没有再改 bot 一行**——
多后端能成立，靠的正是这个服务层把 agent 后端抽象成一个地址。
详见 [4.1](#41-bot-侧已有的服务层) 和 [4.2](#42-相对上游原版bot-里加了什么)。

- 一个 DSH 插件，在 `:8760` 上讲那四条方言，让 bot 原封不动的 `HermesClient`
  把它当成 Hermes 用
- 一个启动器 `run_wechat_dsh.py`，import bot 与 bridge、覆写两个模块级全局、
  再调用同一个 `main_loop()`

> **路径约定**：本文里的相对路径以**本仓库根目录**为基准，命令示例默认你已经
> `cd` 到根目录。插件源在**兄弟仓库**，写作 `../WeChat-AI-AutoReply/...`。
>
> 本机安装位置一律写成**可解析的变量**而不是具体盘符：`~` 是用户目录，
> `%LOCALAPPDATA%` 是本机的 AppData 根，`<仓库根>` 是本文档所在仓库。
> 唯一例外是第 3 节 `--check` 的输出样本——那两行是逐字捕获的运行时内容，
> 程序打印的就是绝对路径，所以用 `<仓库根>\` 占位并在此声明。

---

## 1. 架构总览

### 方向一：入站（好友发微信 → bot → agent → 回复）

```text
   ┌──────────────┐
   │  PC 微信窗口 │
   └──────┬───────┘
          │ 离屏截图 + RapidOCR（不移动鼠标）
          ▼
┌───────────────────────────────────────────────────────────────┐
│  wechat_ai_bot_dev.py   ← DSH 接入时零改动                      │
│                            （相对上游 +194/-23，见 4.2）          │
│  main_loop() → scan_and_reply_wechat()                          │
│  白名单门禁 is_name_matched_strict() / 三元指纹去重              │
└──────┬────────────────────────────────────────────────────────┘
       │ generate_ai_reply(sender, message)
       │  └─ 路径 A（首选）：BRIDGE.chat(contact, message)
       ▼
┌───────────────────────────────────────────────────────────────┐
│  hermes_bridge_core.py   ← DSH 接入时零改动                      │
│                             （相对上游为新增文件，337 行）          │
│  HermesBridge                                              │
│   ├ SessionMap   联系人 ↔ session_id（落盘 wechat_convo_map*.json）
│   ├ HermesClient POST {hermes_api}/api/sessions/{sid}/chat
│   └ SendQueue    串行化所有 GUI 发送操作                          │
│  HTTP :8901   /health /hermes /status /send /chat                │
└──────┬────────────────────────────────────────────────────────┘
       │  HTTP  POST http://127.0.0.1:8760/api/sessions/{sid}/chat
       │         body {"message": "..."}
       ▼
┌───────────────────────────────────────────────────────────────┐
│  DSH 插件 dsh-wechat-aireply   ← 新增                            │
│  src/agent-entry.ts  以 Hermes 方言监听 127.0.0.1:8760           │
│   ├ sid → contact（stateless 解码，见第 6 节）                    │
│   └ SessionManager → ctx.agents.create / resume                 │
└──────┬────────────────────────────────────────────────────────┘
       │ ctx.agents  →  LLM provider/model
       ▼
┌───────────────────────────────────────────────────────────────┐
│  DeepSeek Harness Desktop（插件运行在 DSH 进程内）               │
└──────┬────────────────────────────────────────────────────────┘
       │ 200 {"session_id":..., "message":{"role":"assistant","content":"..."}}
       ▼
   bridge.chat() 返回 → clean_ai_reply() → send_reply_instant()
       → Win32 消息注入 → 微信聊天窗口
```

### 方向二：出站（agent 主动找人说话）

```text
┌───────────────────────────────────────────────┐
│  DSH agent（DeepSeek Harness Desktop 内）      │
└──────┬────────────────────────────────────────┘
       │ 调用工具 wechat_send(contact, text)
       ▼
┌───────────────────────────────────────────────┐
│  插件 index.ts                                 │
│  POST {bridge}/send  {"contact":"X","text":"Y"} │
│  失败时抛错：wechat bridge unreachable … 先跑    │
│              run_wechat_dsh.bat                │
└──────┬────────────────────────────────────────┘
       │ HTTP → 127.0.0.1:8901
       ▼
┌───────────────────────────────────────────────┐
│  bot 进程内 hermes_bridge_core.py（DSH 接入时未动）│
│  POST /send → 202 {"queued": true}             │
│  SendQueue.submit(contact, text)               │
└──────┬────────────────────────────────────────┘
       │ 单消费线程
       ▼
   _bridge_gui_sender() → send_reply_instant() → 微信
```

补充工具：`wechat_ask(sender, message)` → `POST {bridge}/chat` → `200 {"reply": "..."}`。
它会阻塞跑完一整轮对话（可能几十秒到几分钟），只读不发送，适合「帮我想想怎么回」。

### 三处「未改动」

| 组件 | 路径 | 状态 |
|------|------|------|
| bot | `wechat_ai_bot_dev.py` | 未改 |
| bridge | `hermes_bridge_core.py` | 未改 |
| Hermes Client 契约 | `HermesClient` 的四条路由 | 未改，插件照着实现 |

---

## 2. 端口分段约定

| 端口 | 服务 | 归属 | 说明 |
|------|------|------|------|
| `8642` | 真实 Hermes API Server（v0.21.5） | `hermes gateway` 进程 | Hermes 模式的上游，DSH 模式**不碰它** |
| `8760` | DSH 插件 agent entry（本项目） | DeepSeek Harness Desktop 进程 | DSH 模式的 `hermes_api` 指向它 |
| `8766` | 测试子进程专用 | `..\WeChat-AI-AutoReply\experiments\step_loop.py` 拉起的 `_entry_child.mjs` | 绝不用 8760，要把 8760 留给 DSH |
| `8900` | bot bridge，Hermes 模式 | `wechat_config_dev.json` → `bridge_port` | 原默认 |
| `8901` | bot bridge，DSH 模式 | `wechat_config_dsh.json` → `bridge_port` | 插件两个工具默认打这里 |
| `8907` | 测试专用一次性 bridge 端口 | `..\WeChat-AI-AutoReply\experiments\step_loop.py` | 只用来测出站方向 |

**口诀：`86xx` = Hermes 侧，`87xx` = DSH 侧，`89xx` = bot bridge 侧。**

来源：`..\WeChat-AI-AutoReply\packages\dsh-wechat-aireply\cordis.patch.yml` 顶部的注释。已废弃的
`7654` / `7655`（早期 reply-hook 方案遗留）**不要复用**。

---

## 3. 两套配置的切换

两个配置文件并存，互不影响：

| 文件 | `hermes_api` | `bridge_port` | 会话映射文件 | 用途 |
|------|--------------|---------------|--------------|------|
| `wechat_config_dev.json` | `http://127.0.0.1:8642` | `8900` | `wechat_convo_map.json` | Hermes 模式，**原有默认，一字未改** |
| `wechat_config_dsh.json` | `http://127.0.0.1:8760` | `8901` | `wechat_convo_map_dsh.json` | DSH 模式 |

其余差异：

- `wechat_config_dsh.json` 把 `hermes_cli_path` 置为 `""`，即**关掉 Hermes CLI 降级路径**
  （DSH 不可用时不再退回 12 秒的 CLI 冷启动）。
- `whitelist` 多一个「李四」。

### 启动方式

```powershell
# Hermes 模式（原样）
.\run_wechat_bot.bat

# DSH 模式
.\run_wechat_dsh.bat

# DSH 模式，只打印生效配置 + 探测上游，不进 OCR 主循环
.\run_wechat_dsh.bat --check

# 指定配置启动（可选位置参数）
python .\run_wechat_dsh.py wechat_config_dev.json
```

`--check` 的实际输出（当前 DSH 模式）：

```text
config file          : <仓库根>\wechat_config_dsh.json
convo map            : <仓库根>\wechat_convo_map_dsh.json
upstream (hermes_api): http://127.0.0.1:8760
bridge_port          : 8901
bridge_chat_timeout  : 180
hermes_cli_path      : (disabled)
whitelist            : ['张三', '李四']
upstream health      : {'ok': True, 'status': 'ok', 'service': 'dsh-wechat-aireply', ...}
OK: upstream agent entry reachable
```

判活逻辑兼容两种形状：`health.get("ok") is True`（DSH 插件）或
`health.get("status") == "ok"`（Hermes）。所以 `--check` 两边都能用。

### ⚠️ 会话映射文件不一样，别混用

`run_wechat_dsh.py` 的 `resolve()` 按配置名派生映射文件：
`wechat_config_X.json` → `wechat_convo_map_X.json`。

因此：

```text
run_wechat_dsh.py wechat_config_dev.json   →  wechat_convo_map_dev.json   ← 新的、空的
run_wechat_bot.bat                         →  wechat_convo_map.json       ← 旧的、有历史的
```

**两者不共享会话历史。** 用 `run_wechat_dsh.py wechat_config_dev.json` 跑 Hermes 模式，
会对着空映射重建 session，等于丢掉上下文，还会往同一台 Hermes 上再塞一个同名
`微信-张三` 标题（触发 bridge 的 400 冲突恢复路径）。

> **Hermes 模式请始终用 `run_wechat_bot.bat`**，不要用 `run_wechat_dsh.py` 传 dev 配置。
> `run_wechat_dsh.py` 的位置参数是给「临时切到另一份 DSH 配置」用的调试口子。

---

## 4. bot 侧的服务层，以及 DSH 接入为何没再动它

### 4.1 bot 侧已有的服务层

先说清楚现状：bot **本身就是一个服务宿主**，`main_loop()` 里内联启动了一个
HTTP 服务。改动之前它就在那儿，多后端能成立也是因为它。

```
main_loop()                                   wechat_ai_bot_dev.py
  ├─ 启动 HermesBridge(...).start(port)       :1129-1139
  │    监听 bridge_port（8900 / 8901）
  │    上游地址 = cfg["hermes_api"]           ← 可插拔的关键
  │
  └─ while True:  每 check_interval_seconds
       ├─ scan_and_reply_wechat(hwnd)        OCR 扫描
       └─ generate_ai_reply()                三级降级
```

**Bridge 路由**（`hermes_bridge_core.py`）：

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/health` | bridge 自身存活 |
| `GET` | `/hermes` | 上游 agent 状态（走后台缓存，不阻塞） |
| `GET` | `/status` | 会话映射 + 发送队列深度 |
| `POST` | `/send` | 入队一条待发消息，**立即返回** |
| `POST` | `/chat` | 阻塞跑完一整轮，返回回复 |

`/send` 只入队、立刻返回，真正打字在 GUI 线程做——所以插件侧 `timeoutMs`
给 15 秒就够，而 `/chat` 要跑完一轮，需要 180 秒。

**发送执行体**是 `_bridge_gui_sender()`（`:1094`）：拿微信窗口、截图、算布局、
复用 `send_reply_instant()`。所以出站推送和 OCR 自动回复走的是**同一条发送链路**，
包括那个"截屏找绿底气泡"的闭环验收。

**`generate_ai_reply()` 的三级降级**（`:563`）：

| 级 | 路径 | 触发条件 |
|---|---|---|
| A | `BRIDGE.chat()` | bridge 起来且返回非空 |
| B | Hermes CLI | A 失败或返回空 |
| C | OpenAI 兼容接口 | B 失败 |

⚠️ 注意人设 prompt 在这里被构建了，但**只喂给 C**。走 A 时发的是原话，
人设由 agent 后端自己负责——这正是 DSH 模式下回复偏助手口吻的原因。

| 调整 | 位置 | 作用 |
|---|---|---|
| `CONFIG_FILE` 模块级全局 | `:56` | 配置文件运行时才读，使配置可切换 |
| `BRIDGE` 模块级全局 | `:1092` | bridge 实例持有者，`generate_ai_reply` 读它 |
| 内联启动 bridge | `:1129` | 服务与 OCR 同进程，共享微信窗口操作 |

### 4.2 相对上游原版：bot 里加了什么

拿兄弟仓库（上游 clone）的 `HEAD` 比对，上游原版 `wechat_ai_bot_dev.py`
**983 行，完全没有 HTTP 服务、没有 bridge、没有任何对外接口**。
本仓库的版本是 1153 行，**+194 / −23**。

新增的全部内容：

| 新增项 | 位置 | 作用 |
|---|---|---|
| `from hermes_bridge_core import HermesBridge` | `main_loop()` 内联 | 启动服务 |
| `BRIDGE = None` | `:1092` 模块级全局 | bridge 实例持有者 |
| `def _bridge_gui_sender(contact, text)` | `:1094` | Bridge 的 GUI 发送执行体 |
| `def extract_hermes_stdout(raw)` | CLI 降级路径 | 解析 CLI 输出 |
| `HermesBridge(_bridge_gui_sender, cfg).start(port=bridge_port)` | `:1133` | 启动 bridge 服务 |
| `BRIDGE.chat(...)` + `API_CONVERSATION_HISTORY` | `generate_ai_reply()` | 降级链路的 A 级 |
| `import subprocess` / `import tempfile` | 顶部 | CLI 降级用 |

**删除的顶层定义：一个都没有。** 上游自己的 LLM 调用被完整保留，降级成了 C 级。

关键在于 **bot 只长了 170 行，真正的服务在另一个文件**——
`hermes_bridge_core.py`（337 行，独立模块）承担了全部 HTTP 职责。
bot 这边只是多了"启动它 + 调用它"两处接线。

新增的服务相关文件（不在 bot 里）：

| 文件 | 行数 | 作用 |
|------|------|------|
| `hermes_bridge_core.py` | 337 | bridge 服务本体：路由、会话映射、上游客户端 |
| `run_wechat_dsh.py` | — | 启动器：换配置后调同一个 `main_loop()` |
| `wechat_config_dsh.json` | — | DSH 后端的配置 |

> 上游 `HEAD` 为 `a9cf8bd`，比对方式：兄弟仓库执行
> `git show HEAD:wechat_ai_bot_dev.py` 取出原版再逐行 diff。

### 4.3 为什么 DSH 接入没再动 bot

因为上面这套已经把"后端"抽象成了一个地址。bot 里没有任何一处写着
"Hermes"是必须的后端——`hermes_api` 只被当作 URL 使用。

关键在于：bot 和 bridge 读全局变量的时机是**调用时**，不是 import 时。

| 模块 | 模块级全局 | 读取者 | 读取时机 |
|------|------------|--------|----------|
| `wechat_ai_bot_dev` | `CONFIG_FILE`（默认 `wechat_config_dev.json`） | `load_config()` | 每次调用时读全局 |
| `hermes_bridge_core` | `CONVO_MAP_FILE`（默认 `wechat_convo_map.json`） | `HermesBridge.__init__` | 构造时读全局 |

`load_config()` 里是 `if os.path.exists(CONFIG_FILE)`，不是 import 时把内容缓存下来；
`HermesBridge.__init__` 里是 `SessionMap(CONVO_MAP_FILE)`。所以只要在**第一次调用之前**
把这两个全局换掉，后续一切照旧。

`run_wechat_dsh.py` 的 `apply_config()` 就是这么做的：

```python
import hermes_bridge_core          # 先 import bridge
import wechat_ai_bot_dev as bot    # 再 import bot

bot.CONFIG_FILE = config_file                     # ← 覆写
hermes_bridge_core.CONVO_MAP_FILE = convo_map_file # ← 覆写
return bot, hermes_bridge_core, bot.load_config()
```

然后 `main()` 里 `bot.main_loop()`——**同一个函数体**，没有 fork、没有打补丁。

两个容易踩的细节：

1. **必须先 import `hermes_bridge_core`。** `main_loop()` 内部是
   `from hermes_bridge_core import HermesBridge`，这句话会命中 `sys.modules` 缓存。
   先 import bridge 再覆写全局，缓存里那份才是覆写后的模块对象。
2. **`hermes_api` 原样透传。** `main_loop()` 把 `load_config()` 的结果整个塞给
   `HermesBridge(_bridge_gui_sender, cfg)`，而 `HermesBridge.__init__` 只取
   `cfg.get("hermes_api", HERMES_API)` 建 `HermesClient`。启动器不需要知道后端是
   Hermes 还是 DSH——**换个配置文件就换了后端**，代码路径完全相同。

### 三级降级在 DSH 模式下的实际形态

`generate_ai_reply()` 的三档：

| 档 | 日志标记 | DSH 配置下的行为 |
|----|----------|------------------|
| A. Bridge（首选） | `🌉 [Bridge] 转发给 Hermes` | 打到 `8760`，跑一轮 DSH agent |
| B. Hermes CLI | `🤖 [Hermes] 调用 CLI` | **被禁用**（`hermes_cli_path: ""`） |
| C. OpenAI 兼容 API | `🤖 [Fallback] 调用大模型` | `api_key` 为空时不发起请求 |
| 兜底 | — | 返回 `在呢，刚才没注意看手机，怎么啦？` |

---

## 5. 插件实现的 Hermes 契约

`HermesClient` 只发四类请求，插件就只实现这四条（`src/agent-entry.ts`）。

| 方法 | 路径 | 请求体 | 成功响应 | 状态码 |
|------|------|--------|----------|--------|
| `GET` | `/health` | — | `{"ok":true,"status":"ok","service":"dsh-wechat-aireply","object":"dsh.health","sessions":N,"uptime_s":N}` | 200 |
| `POST` | `/api/sessions` | `{"title":"微信-张三"}` | `{"object":"dsh.session","session":{"id":"wc_…","title":"…","contact":"…","created_at":N},"id":"wc_…","session_id":"wc_…"}` | 200（**永远 200**） |
| `GET` | `/api/sessions?limit=100` | — | `{"object":"list","data":[{"object":"dsh.session","session":{"id":"wc_…","title":"…","agent_session_id":"…","created_at":N}}]}` | 200 |
| `POST` | `/api/sessions/{sid}/chat` | `{"message":"..."}` | `{"object":"dsh.message","session_id":"wc_…","message":{"role":"assistant","content":"..."}}` | 200 |

### 逐条说明（每一条都是承重的）

**`GET /health` 同时带 `ok` 和 `status`。**
真实 Hermes 返 `{"status":"ok"}`，插件早期版本返 `{"ok":true}`。共享启动器
`run_wechat_dsh.py --check` 和 bridge 的健康轮询两种都认，所以两个键都带上，谁都不会失联。

**`POST /api/sessions` 幂等、绝不返回 400。**
真实 API Server 对重复 title 回 400 `invalid_title`，bridge 因此有 `_find_session_by_title()`
冲突恢复路径。插件的 id 是联系人的纯函数，重复创建必然返回同一个 id，所以直接返 200，
把那条恢复路径变成死代码。顶层 `id` / `session_id` 别名是给 bridge 的多级取值兼容用的。

**`Authorization: Bearer <key>` 收下但忽略。**
bot 会带 `hermes_api_key: "wechat-local-dev"` 打过来，插件不校验，纯粹是为了让一套
启动器在两边都能用同一个 key。

**`/chat/stream`（SSE）故意不实现。**
`HermesClient.session_chat_stream()` 里有这个方法，bot 从来不调它。少实现一条路由，
少一条要测的路径。

**其余状态码**：

| 情况 | 响应 |
|------|------|
| sid 解不出来（非 `wc_` 前缀 / 非 base64url / 非规范尾部） | `404 {"error":"session not found","id":"<sid>"}` |
| `message` 缺失或全是空白 | `400 {"error":"message is required","session_id":"<sid>"}` |
| body 不是 JSON 对象（数组 / 标量 / 坏 JSON / >256KB） | `400 {"error":"…"}` |
| 未匹配任何路由 | `404 {"error":"not found"}` |
| 内部异常（agent 抛错等） | `500 {"error":"…","stack":"前 6 行"}` |

HTTP 层超时：`server.requestTimeout = turnTimeoutMs + 10000`（默认 170s + 10s），
保证 node 不会在 bridge 还在等回复时单方面掐掉 socket。

### 工具的 render 契约

`wechat_send` / `wechat_ask` 的 `output.render()` **必须返回 content block 数组**，
不能返回裸字符串：

```js
return [{ type: "text", text: String(value?.reply ?? "") }];
```

原因：`defineTool` 会把 `render()` 的返回值直接塞进 `result.content`，运行时用
`content.some(block => …)` 遍历。返回裸字符串会让整轮在
`content.some is not a function` 上死掉——而且是在工具体已经执行完之后，
副作用已经发生了。这一点由 `..\WeChat-AI-AutoReply\packages\dsh-wechat-aireply\experiments\test_entry.mjs` 的
`testToolRenderContract()` 直接对着**已部署产物**断言。

---

## 6. 会话 id 设计

```text
sid = "wc_" + base64url(utf8(contact))        （不带 = 补位）
```

例：`张三` → `wc_5byg5LiJ`

### 为什么必须可无状态解码

决定性约束有两条，缺一不可：

1. **bridge 发给 chat 端点的只有 sid**，body 里只有 `{"message": ...}`，
   **不带联系人名字**。
2. **`wechat_convo_map_*.json` 跨重启存活**。DSH 一重启，bridge 就会把一个
   新进程从没见过的 sid 发过来。

如果 sid 是不透明随机串，插件就没法把 sid 映射回联系人——要么建一张自己的
持久化表（和 bridge 的表重复，还得同步），要么让该联系人永远拿不到回复。
把联系人编进 sid，映射就是联系人的纯函数，**重启是空操作而不是会话丢失**，
两个联系人也永远不会碰撞。

### 故意严格：非规范尾部直接 404

`sidToContact()` 最后做一次**规范形式校验**：

```ts
if (contactToSid(contact) !== sid) return null;
```

原因是宽松的解码器会忽略尾部多余位：`wc_YQ` 和 `wc_YR` 在 Node 的宽容解码下
**都解出 `"a"`**。如果两个都接受，两个不同的 sid 就静默地代表同一个联系人——
这正好是要靠 sid 杜绝的串线。宁可 404，也不能猜。

同理：UTF-8 用 `TextDecoder(..., { fatal: true })`，坏字节序列被拒而不是
静默解成 `U+FFFD`；`payload.length % 4 === 1` 的 base64 根本不可能存在，直接拒。

| sid | 结果 |
|-----|------|
| `wc_5byg5LiJ` | → `张三` |
| `wc_!!!!` | 404（非 base64url 字符） |
| `notours` | 404（无 `wc_` 前缀） |
| `wc_YR` | 404（非规范尾部，`wc_YQ` 才是 `a`） |
| `wc_` | 404（空 payload） |

### ⚠️ 运维后果：bridge 不会从 404 里恢复

这一点必须写进值班手册：

- bridge 的 `_ensure_session()` 从 `wechat_convo_map_*.json` 里查到 sid 就**直接返回**，
  不会因为 404 去重建会话；
- `chat()` 把异常整个吞掉，`return ""`；
- bot 收到空回复就降级——`hermes_cli_path` 在 DSH 模式下是空的，
  `openai_api.api_key` 也是空的——最后落到兜底话术
  `在呢，刚才没注意看手机，怎么啦？`。

**结果：映射表里一条坏 sid，会让那一个联系人静默地一直收到兜底话术，
日志里只有 `chat 404 undecodable sid ... - stale map entry?` 一行。**

**处置：删掉会话映射文件即可重置**（会丢该联系人的上下文，DSH 侧下条消息重建）：

```powershell
Remove-Item .\wechat_convo_map_dsh.json
```

由于 sid 只在一种情况下会坏（映射表被手工改坏，或从 Hermes 模式直接复用
`wechat_convo_map.json`），而 `run_wechat_dsh.py` 已经按配置名分表，正常使用不该碰到。

### DSH 侧的真实 session id

sid（Hermes 方言）不是 DSH 的 session id。插件另外用
`sha256("wechat:" + contact)` 派生一个确定性的 uuid 形 session id，交给
`agents.create` / `agents.resume`：

```text
张三 → wc_5byg5LiJ  →  17971ced-d68a-73d8-178f-ba29a87f4987
```

后者可在 `GET /api/sessions` 的 `agent_session_id` 字段里看到。

---

## 7. 插件安装与部署

### 源与两份部署必须三处一致

| 角色 | 绝对路径 |
|------|----------|
| 源（唯一可编辑处） | `../WeChat-AI-AutoReply/packages/dsh-wechat-aireply` |
| DSH 安装树 | `%LOCALAPPDATA%\Programs\DeepSeek Harness Desktop\resources\app.asar.unpacked\node_modules\@ningbainb\dsh-wechat-aireply` |
| 运行 profile | `~\.dsh-community\profiles\desktop\node_modules\@ningbainb\dsh-wechat-aireply` |

> 注：`~\.dsh-community\profiles\desktop` 是**已废弃**的旧 profile，
> 里面也有一份插件副本，但不会被加载。判断哪份是活的看栈里的路径——报错的
> `file://` 路径会直接告诉你加载的是哪一份。

> 注：`../WeChat-AI-AutoReply/packages/dsh-wechat-aireply/package.json` 里的 `file:` 依赖
> （`@deepseek-ai/cordis` / `@deepseek-ai/dsh-tools`）指向
> `%LOCALAPPDATA%\Programs\DeepSeek Harness Desktop\...`，
> 即**真正在跑的那个 DSH 实例**，仅供开发期拉依赖做类型检查。

### 部署步骤

```powershell
# 1. 构建（纯 tsc，无打包器）
cd ../WeChat-AI-AutoReply/packages/dsh-wechat-aireply
pnpm install
pnpm build          # => tsc -p tsconfig.json，产物落在 dist/

# 2. 复制到两处部署目录（dist/ + package.json + cordis.patch.yml + README.md）
$src  = '../WeChat-AI-AutoReply/packages/dsh-wechat-aireply'
$dst1 = '%LOCALAPPDATA%\Programs\DeepSeek Harness Desktop\resources\app.asar.unpacked\node_modules\@ningbainb\dsh-wechat-aireply'
$dst2 = '~\.dsh-community\profiles\desktop\node_modules\@ningbainb\dsh-wechat-aireply'
foreach ($d in @($dst1, $dst2)) {
  Remove-Item "$d\*" -Recurse -Force
  Copy-Item "$src\dist" -Destination $d -Recurse -Force
  Copy-Item "$src\package.json","$src\cordis.patch.yml","$src\README.md" -Destination $d -Force
}

# 3. 确认三处哈希一致
foreach ($p in @($src,$dst1,$dst2)) { Get-FileHash "$p\dist\index.js" -Algorithm SHA256 }

# 4. 启用插件（profile patch，由 dsh.profile.bundles 管理）
#    ~\.dsh-community\profiles\desktop\cordis.patch.yml
#    - id: wechat-aireply
#      disabled: false
```

### 三条硬性要求

| # | 要求 | 原因 |
|---|------|------|
| 1 | **三处保持一致**（一次构建，三处部署） | 插件是三合一：`dist/` 装代码、`package.json` 装 `dsh.bundle.patch` 指针、profile patch 装启用位。缺任一份，DSH 要么找不到插件，要么加载了却没被启用。 |
| 2 | **所有文件 UTF-8 无 BOM** | 带 BOM 的 `.js` / `.json` 在 Node ESM 下会直接抛 `SyntaxError: Unexpected token '\uFEFF'`，插件静默不加载——只有日志里能看到。 |
| 3 | **必须完全重启 DSH Desktop** | DSH 在**冷启动时从磁盘加载插件**，没有热重载。改完代码不重启，新代码永远不生效。 |

### 当前部署状态（实测）

`dist/*` 与 `package.json` 三处 SHA256 完全一致。
**`cordis.patch.yml` 目前不一致**：两处部署目录里放的还是早期方案的旧版
（里面配的是 `endpoint: 'http://127.0.0.1:7654'`），而源码目录已经是新版
（配 `bridge` / `agentPort` / `turnTimeoutMs`）。运行时因为 schema 里已无
`endpoint` 字段、`agentPort` 也没被旧 patch 指定，实际仍落在默认值 `8760`，
所以**当前能用**——但这是巧合，不是设计。下次做「一次构建三处部署」时会被修正。

### 插件配置项（`src/index.ts` 的 `Config`）

| 键 | 默认 | 说明 |
|----|------|------|
| `bridge` | `http://127.0.0.1:8901` | bot bridge 地址，两个工具都打这里 |
| `timeoutMs` | `15000` | `wechat_send` 的 HTTP 超时（`/send` 只入队，短即可） |
| `askTimeoutMs` | `180000` | `wechat_ask` 的 HTTP 超时（`/chat` 要跑完一整轮） |
| `agentPort` | `8760` | 入站监听端口。填 `0` 由系统分配空闲端口 |
| `turnTimeoutMs` | `170000` | 单轮 agent 上限，**刻意小于** bridge 的 180s，让插件先超时 |
| `workspace` | `""` | 建会话时的 cwd，空则用进程 cwd |
| `provider` | `diy` | LLM provider，必须是已配置的 provider |
| `model` | `MiniMax-M3.1-Flash-Preview` | 模型 |

插件 `inject: ["tools", "systemPrompt", "agents"]`——三个服务到位后才装载
工具和 agent entry。

---

## 8. 排障

| 现象 | 原因 | 处理 |
|------|------|------|
| bot 起来了，每条回复都是 `在呢，刚才没注意看手机，怎么啦？` | 上游不可达，bridge 返空，DSH 模式的两级降级都被关掉（`hermes_cli_path` 空、`api_key` 空），直接落到兜底 | 跑 `run_wechat_dsh.bat --check`。看 `upstream health` 那行 |
| `8760` 拒绝连接 | ① DSH Desktop 没运行；② 插件没激活（profile patch 里 `disabled: true` 或缺 `- id: wechat-aireply`） | 确认 DSH 在跑；`Invoke-WebRequest http://127.0.0.1:8760/health` |
| 插件日志 `entry did not activate` | 依赖注入没满足，或加载阶段抛错 | 查 DSH 运行时日志里的插件 id `wechat-aireply`。常见是 profile patch 没启用、或文件带了 BOM |
| 只有一个联系人一直收兜底话术 | 会话映射里那个 sid 解不出来（陈旧/损坏） | 见第 6 节。删 `wechat_convo_map_dsh.json` 重置；日志搜 `chat 404 undecodable sid` |
| 插件能应答，但回合秒回 / 模型像没配 | `provider` / `model` 没生效。agent loop 在 `prepareRequest()` 就抛 `agent "<id>" has no provider/model`，除非 `agents.create` / `agents.resume` 显式传 `agentOptions` | 核对 `cordis.patch.yml` 与 `index.ts` `Config` 里的 `provider` / `model` 是已配置的 provider；看 DSH 日志里的 provider/model 路由 |
| 改了插件代码没效果 | DSH 冷启动才从磁盘加载，无热重载 | **完全退出并重启 DSH Desktop**，只重开项目窗口不够 |
| `8901` 拒绝连接，插件工具报 `wechat bridge unreachable` | bot 没起，或起的是 Hermes 模式（占 `8900`） | 跑 `run_wechat_dsh.bat`（DSH 模式，占 `8901`） |
| `SyntaxError: Unexpected token '\uFEFF'` | 部署文件带 BOM | 按第 7 节要求全部转 UTF-8 无 BOM 后重新复制 |
| 桥在但回复走 CLI 降级（日志 `🤖 [Hermes] 调用 CLI`） | 跑的是 Hermes 配置，或 DSH 配置里 `hermes_cli_path` 没清空 | `--check` 看 `hermes_cli_path` 那行 |
| `run_wechat_dsh.py wechat_config_dev.json` 后历史全丢 | 派生出 `wechat_convo_map_dev.json`，是另一张空表 | 改用 `run_wechat_bot.bat` 跑 Hermes 模式 |
| 8900/8901 端口被占 / 有僵尸 LISTENING | bridge 的 `allow_reuse_address=False` 是刻意的（防 Windows 重复 bind 假象） | `netstat -ano \| findstr :8901` 找到 PID，`Stop-Process` |

### 读回复的正确姿势

session 是 **append-only 事件日志**，不是消息数组：

```ts
// ✅ 对：一次调用拿整个日志
const events = session.snapshotEvents();
const reply = events
  .filter(e => e.type === "assistant/message")
  .map(e => e.data?.message)
  .map(extractMessageText)
  .pop();

// ⚠️ 兜底路径可以，但 eventAt 必须保持方法调用
for (const seq of session.surface.nodes) {
  events.push(session.eventAt(seq));
}
```

**绝对不要把 `session.eventAt` 摘出来存进局部变量再调**——它的实现是
`return this.log[seq]`，脱离接收者就会抛
`TypeError: Cannot read properties of undefined (reading 'log')`。
`this` 是承重的，测试里特意用真 class（而非箭头函数桩）来守住这一点。

---

## 9. 验证步骤

### 9.1 环境确认

```powershell
$py = 'python'

# DSH Desktop 在跑
Get-Process -Name 'DeepSeek Harness Desktop' -ErrorAction SilentlyContinue

# 两个端口都在监听
Get-NetTCPConnection -State Listen -LocalPort 8760,8901 | Format-Table -AutoSize
```

### 9.2 插件健康

```powershell
Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8760/health' | Select-Object -ExpandProperty Content
```

期望：

```json
{"ok":true,"status":"ok","service":"dsh-wechat-aireply","object":"dsh.health","sessions":1,"uptime_s":519}
```

### 9.3 bot bridge 健康 + 生效配置

```powershell
# bridge 自身存活
Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8901/health' | Select-Object -ExpandProperty Content
# {"ok": true, "service": "wechat-bridge"}

# 会话映射 + 发送队列深度
Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8901/status' | Select-Object -ExpandProperty Content
# {"sessions": {"张三": "wc_5byg5LiJ"}, "queue_size": 0}

# 上游（= DSH 插件）状态，走后台缓存，不阻塞
Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8901/hermes' | Select-Object -ExpandProperty Content

# 一条命令看清「到底跑的哪套配置 + 上游活没活」
& python .\run_wechat_dsh.py --check
```

### 9.4 建会话

```powershell
$body = [Text.Encoding]::UTF8.GetBytes('{"title":"\u5fae\u4fe1-\u6478\u5929\u826f"}')
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8760/api/sessions' `
  -ContentType 'application/json; charset=utf-8' -Body $body | ConvertTo-Json -Depth 5
```

期望：

```json
{
  "object":  "dsh.session",
  "session": { "id": "wc_5byg5LiJ", "title": "微信-张三", "contact": "张三", "created_at": 1791567318 },
  "id": "wc_5byg5LiJ",
  "session_id": "wc_5byg5LiJ"
}
```

sid 可独立复算，无需问插件：

```powershell
& python -c "import base64;c='张三';print('wc_'+base64.urlsafe_b64encode(c.encode()).decode().rstrip('='))"
# wc_5byg5LiJ
```

幂等性检查（**必须 200，不能 400**）：

```powershell
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8760/api/sessions' `
  -ContentType 'application/json; charset=utf-8' -Body $body | Select-Object -ExpandProperty session | Select-Object -ExpandProperty id
```

### 9.5 跑一轮对话

```powershell
$chat = [Text.Encoding]::UTF8.GetBytes('{"message":"\u8bf4\u4e00\u53e5\u4f60\u4eca\u5929\u8fc7\u5f97\u600e\u4e48\u6837"}')
$sw = [Diagnostics.Stopwatch]::StartNew()
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8760/api/sessions/wc_5byg5LiJ/chat' `
  -ContentType 'application/json; charset=utf-8' -Body $chat -TimeoutSec 180
$sw.Elapsed.TotalSeconds
```

期望（1–15 秒量级）：

```json
{
  "object":  "dsh.message",
  "session_id": "wc_5byg5LiJ",
  "message":  { "role": "assistant", "content": "就那样吧，上班下班，累得够呛，你呢" }
}
```

> 注意用 `-Body ([Text.Encoding]::UTF8.GetBytes(...))` 或 `\uXXXX` 转义。
> Windows PowerShell 5.1 的 `Invoke-RestMethod` 默认按 GBK 发中文，
> 会把联系人名和消息打乱。

### 9.6 出站方向（真发微信，谨慎）

```powershell
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8901/send' `
  -ContentType 'application/json; charset=utf-8' `
  -Body ([Text.Encoding]::UTF8.GetBytes('{"contact":"\u6478\u5929\u826f","text":"\u5728\u7684"}'))
# {"queued": true, "contact": "张三", "queue_size": 1}
```

这是**真发微信**。排障时只验证 `/status` 和 `/hermes` 就够了，`/send` 留给联调。

### 9.7 两套自动化测试

```powershell
# ① 协议层：直接打已部署产物，fake 掉 agents 服务，不碰微信也不烧模型
cd ../WeChat-AI-AutoReply/packages/dsh-wechat-aireply
node experiments\test_entry.mjs
```

- **49 项断言**（不是 43——除了 43 项 HTTP 方言 / sid 编解码断言，还含
  `testToolRenderContract()` 的 6 项工具 render 契约断言，直接 import
  **已部署的** `dist/index.js`）
- 覆盖：四条路由、幂等建会话、sid 编解码与严格性、404/400 边界、
  未知路由、旧 `POST /chat` 已废弃、每联系人轮次串行化、`disposeAll` 释放
- 换安装位置用 `$env:WECHAT_PLUGIN_DIR = '<路径>'`
- 期望末行：`49 passed, 0 failed`

```powershell
# ② 闭环：Python 侧跑真实 hermes_bridge_core，Node 侧跑真实已部署入口
#    只 fake 模型和 agents 服务，HTTP 走真实 socket，进程真实跨边界
& python ../WeChat-AI-AutoReply/experiments/step_loop.py
```

- **24 项断言**
- 关键点：`sys.path` 指向 `-main` 那份**未改动的** `hermes_bridge_core`，
  临时改 `CONVO_MAP_FILE` 到临时目录（不碰 bot 真实映射表），
  `hermes_api` 指向 `127.0.0.1:8766` 的子进程
- 第二阶段**重启入口子进程**并用**同一张已落盘的映射表**重建 bridge，
  验证的就是第 6 节那条契约：映射表活过进程，重启后 bridge 重放一个
  新进程没见过的 sid，而 sid 必须能自己解回联系人
- 子进程是 `..\WeChat-AI-AutoReply\experiments\_entry_child.mjs`，同样默认 import
  `%LOCALAPPDATA%\Programs\DeepSeek Harness Desktop\...` 下的已部署产物
- 期望末行：`24 passed, 0 failed`

两个脚本都**不会碰微信**，也不会烧真实模型额度。

---

## 10. 已知边界

| 项 | 状态 |
|----|------|
| `/chat/stream`（SSE 流式） | bridge 侧有客户端方法，bot 从不调用，插件**故意不实现** |
| Hermes CLI 降级 | DSH 配置里 `hermes_cli_path: ""`，已关闭 |
| OpenAI 兜底 | `api_key` 为空，不发起请求 |
| 群聊 | 白名单已支持（群名直接加），未做群内 @ 识别 |
| 多设备 | bridge 是 HTTP loopback，理论上可远程；当前单机 |
| 插件热重载 | 不支持，必须完全重启 DSH Desktop |

---

## 11. 新设备接入清单

换一台没配置过的机器时按顺序做。**第 3 步是最容易卡住的地方**。

### 11.1 前置条件

- [ ] **Windows + PC 微信已登录**。bot 靠 OCR 读窗口 + Win32 注入输入，
      没有微信接口可用，微信没登录就什么都做不了
- [ ] **Python 3.8**（`requirements.txt` 里 `numpy==1.22.4` 对 3.8 友好；
      3.9+ 装不上）
- [ ] **DSH Desktop 已安装并登录过**

### 11.2 装 Python 依赖

```powershell
python -m pip install -r requirements.txt
```

7 个包，其中 `rapidocr_onnxruntime`、`opencv-python`、`numpy` 都是 C 扩展，
**跨 Python 版本不保证 ABI 兼容**。版本已经 pin 死，照着装别升级。

### 11.3 注册插件（关键步骤）

⚠️ **不能靠手工复制文件。** profile 的 `package.json` 里插件是这样引用的：

```json
"@ningbainb/dsh-wechat-aireply": "file:.../plugin-staging/2e6aa99f.../1c7050fba..."
```

路径里那两层目录是**内容哈希**，由 DSH 自己算；profile 的 `cordis.patch.yml`
也是 `dsh.profile.bundles` 生成的。手工把文件放进 `node_modules` 不会被加载，
而且失败得很安静——不报错、不加载、端口 8760 永远不监听。

**正确做法**（DSH 桌面端）：

1. 侧边栏打开 **插件**
2. 点 **从其他来源安装**
3. 地址填**插件源目录**：
   `../WeChat-AI-AutoReply/packages/dsh-wechat-aireply`
   — 地址框要绝对路径，粘贴它的解析结果即可
4. 展开 **高级选项**，勾选
   **「这是来自本地，git 或其他来源的高级插件」**
5. 安装

装好后 DSH 会算出那两段哈希，把插件放进
`~\AppData\Roaming\@linxin666\dsh-desktop\plugin-staging\<hash>\<hash>\`。

> **DSH 按 `package.json` 的 `files` 字段决定拷什么。**
> 源目录有 9 项（`dist` `experiments` `node_modules` `src` `cordis.patch.yml`
> `package.json` `pnpm-lock.yaml` `README.md` `tsconfig.json`），
> staging 里只有 4 项——正是 `files: ["dist", "cordis.patch.yml", "README.md"]`
> 加上 `package.json` 本身。
> 所以改完代码**别指望重装**，用第 7 节的方法做增量更新。

### 11.4 配 agent 路由

插件默认 `provider: diy`、`model: MiniMax-M3.1-Flash-Preview`。新机器上
`diy` 这个 provider 需要先在 DSH 里配好 API key，否则 agent loop 会在
`prepareRequest()` 抛 `has no provider/model`，每一轮瞬间失败。

### 11.5 改写启动脚本的解释器路径

两个 `.bat` 里写死了本机解释器：

```bat
""%PYTHON%" wechat_ai_bot_dev.py
```

新机器上改成自己的路径，或者直接调 `python`。

### 11.6 建配置

复制 `wechat_config_dev.json` 为 `wechat_config_dsh.json`，改三处：

| 键 | 值 |
|---|---|
| `hermes_api` | `http://127.0.0.1:8760` |
| `bridge_port` | `8901` |
| `hermes_cli_path` | `""`（关掉，否则失败会静默回落到真实 Hermes） |

### 11.7 验证

```powershell
.\run_wechat_dsh.bat --check      # 打印生效配置 + 探测上游
```

看到 `OK: upstream agent entry reachable` 就通了。

> **注意**：DSH 模式**不需要** `hermes gateway`。本仓库 `README.md` 的「三步启动」
> 第 ① 步是给 Hermes 模式写的，DSH 模式跳过它即可。

---

## 附：文件清单

| 路径 | 作用 |
|------|------|
| `wechat_config_dev.json` | Hermes 模式配置（未改动） |
| `wechat_config_dsh.json` | DSH 模式配置 |
| `run_wechat_bot.bat` | 启动 Hermes 模式 |
| `run_wechat_dsh.bat` / `.py` | 启动 DSH 模式 / `--check` |
| `hermes_bridge_core.py` | 桥（未改动） |
| `wechat_ai_bot_dev.py` | bot（未改动） |
| `wechat_convo_map.json` | Hermes 模式联系人 ↔ session 映射 |
| `wechat_convo_map_dsh.json` | DSH 模式映射 |
| `..\WeChat-AI-AutoReply\packages\dsh-wechat-aireply\src\agent-entry.ts` | 入站 Hermes 方言服务 |
| `..\WeChat-AI-AutoReply\packages\dsh-wechat-aireply\src\session-manager.ts` | 联系人 ↔ DSH 会话，事件日志读回复 |
| `..\WeChat-AI-AutoReply\packages\dsh-wechat-aireply\src\sid.ts` | sid 编解码与严格性 |
| `..\WeChat-AI-AutoReply\packages\dsh-wechat-aireply\src\index.ts` | 插件入口、配置 schema、两个工具 |
| `..\WeChat-AI-AutoReply\packages\dsh-wechat-aireply\cordis.patch.yml` | bundle patch（端口与超时） |
| `..\WeChat-AI-AutoReply\packages\dsh-wechat-aireply\experiments\test_entry.mjs` | 协议层测试（49 项） |
| `..\WeChat-AI-AutoReply\experiments\step_loop.py` | 闭环测试（24 项） |

---

## 附录：插件源码

`../WeChat-AI-AutoReply/packages/dsh-wechat-aireply/src/` 下的全部文件。

> `index.ts` 的 `GUIDANCE` 里举例的联系人名按本文档惯例写作「张三」「李四」；
> 实际文本取自 `wechat_config_dsh.json` 的 `whitelist`。

### A1. `src/index.ts`

插件入口：配置 schema、两个工具、`ctx.effect` 编排

```ts
/**
 * dsh-wechat-aireply — bridges the standalone WeChat-AI-AutoReply bot and a
 * DSH agent, in both directions.
 *
 * Outbound (agent -> bot), through the bot's HTTP bridge on
 * `http://127.0.0.1:8901`:
 *   wechat_send  POST /send  -> 202 {queued:true}   hand a finished line over;
 *               the bridge owns a serial send queue and drives the GUI
 *   wechat_ask   POST /chat  -> 200 {reply}          block on one bridge chat
 *               turn (slow, it is a full agent round trip)
 *
 * Inbound (bot -> agent): this plugin IS the Hermes API Server the bridge
 * dials. `src/agent-entry.ts` listens on 127.0.0.1:8760 and speaks the four
 * routes `HermesClient` speaks (/health, POST /api/sessions,
 * GET /api/sessions, POST /api/sessions/{sid}/chat), with one isolated DSH
 * session per friend so two contacts never share context.
 *
 * The plugin opens a listener itself because the DSH desktop host exposes no
 * inbound endpoint of its own.
 */
import Schema from "@deepseek-ai/schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
import type { Context } from "@deepseek-ai/cordis";
import { SessionManager } from "./session-manager.js";
import { startAgentEntry } from "./agent-entry.js";
import type { AgentsService } from "./types.js";

export const name = "wechat-aireply";

/** Services required before the tool surface and agent entry can mount. */
export const inject: string[] = ["tools", "systemPrompt", "agents"];

export const Config = Schema.object({
  bridge: Schema.string()
    .default("http://127.0.0.1:8901")
    .description("Where the bot's bridge is serving (/send, /chat). Default: 127.0.0.1:8901."),
  timeoutMs: Schema.number()
    .default(15000)
    .description("HTTP timeout in ms for wechat_send; /send only enqueues. Default: 15000."),
  askTimeoutMs: Schema.number()
    .default(180000)
    .description(
      "HTTP timeout in ms for wechat_ask; /chat blocks for a whole agent turn. Default: 180000.",
    ),
  agentPort: Schema.number()
    .default(8760)
    .description(
      "Loopback port the bot's HermesClient calls to reach a DSH agent. 0 picks a free port. Default: 8760.",
    ),
  turnTimeoutMs: Schema.number()
    .default(170000)
    .description(
      "How long one agent turn may run. Kept under the bridge's 180s HTTP timeout so we fail first. Default: 170000.",
    ),
  workspace: Schema.string()
    .default("")
    .description("cwd used for sessions this plugin creates. Empty uses the process cwd."),
  provider: Schema.string()
    .default("diy")
    .description("LLM provider for the sessions this plugin creates. Must be a configured provider."),
  model: Schema.string()
    .default("MiniMax-M3.1-Flash-Preview")
    .description("Model for the sessions this plugin creates. The agent loop requires an explicit route."),
});

export type WechatAireplyConfig = {
  bridge: string;
  timeoutMs: number;
  askTimeoutMs: number;
  agentPort: number;
  turnTimeoutMs: number;
  workspace: string;
  provider: string;
  model: string;
};

/** Model-facing announcement so the agent knows the capability exists. */
const GUIDANCE =
  "本机已安装 dsh-wechat-aireply 插件（微信 AI 自动回复桥接），与本机 WeChat-AI-AutoReply bot 双向连通。bot 是独立服务：离屏监控微信（不移动鼠标，OCR 识别 + Win32 消息注入），对白名单好友（wechat_config_dev.json，如「张三」「李四」）自动回复。两个方向都走 bot 的 bridge（默认 http://127.0.0.1:8901）：(1) wechat_send(contact, text) → POST {bridge}/send，bot 把它排进 GUI 发送队列，真正敲进微信会话，返回 {queued:true}；(2) wechat_ask(sender, message) → POST {bridge}/chat，会阻塞跑完一整轮对话再返回 {reply}，所以它很慢（可能几十秒到几分钟），读回复建议、不发送。反方向上，插件自身在 127.0.0.1:8760 以 Hermes API 方言监听，bot 的 hermes_api 指向它：GET /health、POST /api/sessions、GET /api/sessions、POST /api/sessions/{sid}/chat。bot 检测到好友新消息时会反过来调它跑一轮 DSH agent 拿回复，每个好友一个独立会话（sid 由联系人名编码而来，重启后仍可解码，所以会话不会断）。限制：bot 未启动时两个工具都会连接失败，此时不要臆造回复，直接告知用户先运行 run_wechat_dsh.bat；wechat_ask 返回空字符串或兜底文案（如「在呢，刚才没注意看手机，怎么啦？」）代表下游 LLM 未配置或调用失败，应如实说明而不是当作真实建议。wechat_send 会真的发微信，属于对外可见动作，发送前应先向用户确认。用户提到「微信 / wechat / bot 回复 / 用我的口吻 / 帮我想想怎么回 / 发给某某」时即指本插件，请据此协作。";

export function apply(ctx: Context, config: WechatAireplyConfig) {
  const logger = ctx.logger("wechat-aireply");
  // Logging must never be able to break a reply turn.
  const log = (msg: string) => {
    try {
      logger.info(msg);
    } catch {
      /* the host logger is unavailable; drop the line rather than fail */
    }
  };
  const workspace = config.workspace || process.cwd();

  logger.info(
    `wechat-aireply ready: bot bridge ${config.bridge}, agent entry 127.0.0.1:${config.agentPort}, ` +
      `route ${config.provider}/${config.model}`,
  );

  // ── Inbound: bot -> agent ────────────────────────────────────────────────
  const sessions = new SessionManager(
    ctx.agents as unknown as AgentsService,
    log,
    workspace,
    { provider: config.provider, model: config.model },
  );

  // ── Outbound: agent -> bot ───────────────────────────────────────────────
  const askTool = defineTool({
    name: "wechat_ask",
    description:
      "Ask the local WeChat-AI-AutoReply bot for a suggested reply to a friend, by POSTing to the bot's bridge /chat. The bot is a separate local service that watches WeChat off-screen (OCR + Win32 injection) and auto-replies to whitelisted friends. This blocks for a whole conversation turn, so it can take a while. It only reads what the bot would say — it does NOT send anything. Prefer wechat_send when the user actually wants the message delivered.",
    parameters: {
      sender: {
        type: "string",
        description: "Friend name as set in wechat_config_dev.json (whitelist entry or friend_personas key).",
      },
      message: {
        type: "string",
        description: "The text the friend sent. Plain text, no quotes.",
      },
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        properties: { reply: { type: "string", required: true } },
      },
      render(_args, value) {
        // Must return an array of content blocks, NOT a bare string:
        // defineTool feeds this straight into result.content, and the runtime
        // walks it with `content.some(block => ...)`.
        return [
          {
            type: "text",
            text: String(
              (value as { reply?: string } | null | undefined)?.reply ?? "",
            ),
          },
        ];
      },
    },
    timeoutMs: config.askTimeoutMs,
    async execute(args) {
      const a = args as { sender: string; message: string };
      return (await callBridge(config, "/chat", { contact: a.sender, message: a.message }, config.askTimeoutMs)) as {
        reply: string;
      };
    },
  });

  const sendTool = defineTool({
    name: "wechat_send",
    description:
      "Deliver a finished line into a WeChat conversation through the local bot's bridge /send. The bot queues it and its GUI sender types it into the right chat window. This is an outward-visible action — confirm with the user before calling it.",
    parameters: {
      contact: {
        type: "string",
        description: "Friend name as shown in the WeChat sidebar; must be in the bot whitelist.",
      },
      text: {
        type: "string",
        description: "The message to send. One short natural Chinese line, no quotes, no AI self-reference.",
      },
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        properties: {
          queued: { type: "boolean", required: true },
          contact: { type: "string", required: true },
          queue_size: { type: "number" },
        },
      },
      render(_args, value) {
        // Content-block array, not a bare string — see the note on wechat_ask.
        const v = value as { queued?: boolean; contact?: string } | null | undefined;
        return [
          {
            type: "text",
            text: v?.queued ? `已排队发送给 ${v.contact}` : "发送未排队",
          },
        ];
      },
    },
    timeoutMs: config.timeoutMs,
    async execute(args) {
      const a = args as { contact: string; text: string };
      return (await callBridge(config, "/send", {
        contact: a.contact,
        text: a.text,
      }, config.timeoutMs)) as { queued: boolean; contact: string };
    },
  });

  ctx.effect(() => {
    const disposePrompt = ctx.systemPrompt.section({
      name: "plugin:dsh-wechat-aireply",
      order: 160,
      text: GUIDANCE,
    });
    const disposeAsk = ctx.tools.register(askTool);
    const disposeSend = ctx.tools.register(sendTool);
    logger.info("registered wechat_ask + wechat_send tools");
    return () => {
      disposeAsk();
      disposeSend();
      disposePrompt();
      logger.info("unregistered wechat tools");
    };
  }, "wechat-aireply: tools");

  ctx.effect(async () => {
    const entry = await startAgentEntry({
      host: "127.0.0.1",
      port: config.agentPort,
      sessions,
      turnTimeoutMs: config.turnTimeoutMs,
      log,
    });
    return async () => {
      await entry.close();
      await sessions.disposeAll();
      logger.info("agent entry closed");
    };
  }, "wechat-aireply: agent entry");
}

/** POST JSON to the bot's bridge and surface a useful error on failure. */
async function callBridge(
  config: WechatAireplyConfig,
  route: string,
  body: unknown,
  timeoutMs: number,
): Promise<unknown> {
  const url = `${config.bridge.replace(/\/+$/, "")}${route}`;
  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (err) {
    throw new Error(
      `wechat bridge unreachable at ${config.bridge} — start it with run_wechat_dsh.bat (${(err as Error).message})`,
    );
  }
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`wechat bridge http ${response.status}: ${text.slice(0, 200)}`);
  }
  return response.json();
}
```

### A2. `src/agent-entry.ts`

入站 HTTP 服务，讲 Hermes 方言

```ts
/**
 * Inbound HTTP entry: the DSH-side stand-in for a Hermes API Server.
 *
 * The bot's bridge (`hermes_bridge_core.HermesClient`) is a fixed client — we
 * cannot change it. It speaks exactly four routes, so this entry speaks those
 * four routes and nothing else:
 *
 *   GET  /health                       liveness; polled every 15s, failures tolerated
 *   POST /api/sessions                 {title}                 -> {session:{id,...}}
 *   GET  /api/sessions?limit=100                             -> {object:"list", data:[...]}
 *   POST /api/sessions/{sid}/chat      {message}               -> {message:{role,content}}
 *
 * Dialect notes, all of them load-bearing:
 *  - `/health` must carry BOTH `ok: true` and `status: "ok"`: the real
 *    server returns `status`, our previous shape returned `ok`, and the
 *    shared launcher (`run_wechat_dsh.py --check`) accepts either.
 *  - `POST /api/sessions` is idempotent and NEVER answers 400. A 400 on a
 *    duplicate title sends the bridge down its conflict-recovery path, which
 *    is dead weight here because the id is a pure function of the contact.
 *  - The id is stateless-decodable (see `./sid.ts`) because the bridge
 *    persists contact -> id across our restarts and sends only the id.
 *  - `Authorization: Bearer <key>` is accepted and ignored; it exists so a
 *    shared launcher can pass one key everywhere.
 *
 * The SSE variant `/chat/stream` exists in the bridge but is never called by
 * the bot, so it is deliberately not implemented.
 */
import { createServer, type Server } from "node:http";
import type { SessionManager } from "./session-manager.js";
import { contactToSid, contactToTitle, sidToContact, titleToContact } from "./sid.js";

const MAX_BODY_BYTES = 256 * 1024;

export interface EntryOptions {
  host: string;
  port: number;
  sessions: SessionManager;
  turnTimeoutMs: number;
  log: (msg: string) => void;
}

export interface RunningEntry {
  /** Actual bound port — differs from the requested one when 0 is passed. */
  port: number;
  close(): Promise<void>;
}

/** `POST /api/sessions/<sid>/chat`, sid is base64url so it cannot contain `/`. */
const CHAT_ROUTE = /^\/api\/sessions\/([^/]+)\/chat$/;

export function startAgentEntry(options: EntryOptions): Promise<RunningEntry> {
  const { host, port, sessions, turnTimeoutMs, log } = options;

  const server: Server = createServer((req, res) => {
    handle(req, res).catch((err) => {
      log(`entry error: ${err instanceof Error ? err.message : String(err)}`);
      // Surface where it failed — without the stack, a fault raised inside the
      // host agent runtime is indistinguishable from one of ours.
      const stack =
        err instanceof Error && typeof err.stack === "string"
          ? err.stack.split("\n").slice(0, 6).join(" | ")
          : "";
      if (!res.writableEnded) {
        respond(res, 500, {
          error: String((err as Error)?.message ?? err),
          stack: stack || undefined,
        });
      }
    });
  });
  // Must outlive one agent turn plus slack, or node aborts the socket while
  // the bridge is still blocked waiting for the reply.
  server.requestTimeout = turnTimeoutMs + 10_000;
  server.headersTimeout = 10_000;

  async function handle(
    req: import("node:http").IncomingMessage,
    res: import("node:http").ServerResponse,
  ): Promise<void> {
    const route = (req.url ?? "/").split("?")[0].replace(/\/+$/, "") || "/";

    if (req.method === "GET" && route === "/health") {
      respond(res, 200, {
        // Both keys on purpose — see the header comment.
        ok: true,
        status: "ok",
        service: "dsh-wechat-aireply",
        object: "dsh.health",
        sessions: Object.keys(sessions.listSessions()).length,
        uptime_s: Math.round(process.uptime()),
      });
      return;
    }

    if (req.method === "GET" && route === "/api/sessions") {
      // Only reachable through the bridge's conflict-recovery path, which our
      // idempotent create never triggers. Listed from live state so it still
      // answers honestly rather than echoing whatever was asked for.
      const data = Object.entries(sessions.listSessions()).map(([contact, sessionId]) => ({
        object: "dsh.session",
        session: {
          id: contactToSid(contact),
          title: contactToTitle(contact),
          // The DSH-side id, for anyone who wants to correlate.
          agent_session_id: sessionId,
          created_at: Math.floor(Date.now() / 1000),
        },
      }));
      respond(res, 200, { object: "list", data });
      return;
    }

    if (req.method === "POST" && route === "/api/sessions") {
      const payload = await readObject(req, res);
      if (payload === undefined) return; // guard already answered

      const title = typeof payload.title === "string" && payload.title.trim()
        ? payload.title.trim()
        : contactToTitle("unknown");
      const contact = titleToContact(title) || "unknown";
      const sid = contactToSid(contact);

      log(`session create "${title}" -> ${sid.slice(0, 16)}…`);
      // Always 200. `resumed` is informational only — the caller never 400s,
      // so this branch exists purely for logs and tests.
      respond(res, 200, {
        object: "dsh.session",
        session: { id: sid, title, contact, created_at: Math.floor(Date.now() / 1000) },
        // Top-level aliases: the bridge accepts `session.id`, `id` or
        // `session_id` depending on which server it is talking to.
        id: sid,
        session_id: sid,
      });
      return;
    }

    const chat = req.method === "POST" ? CHAT_ROUTE.exec(route) : null;
    if (chat) {
      const sid = decodeURIComponent(chat[1] ?? "");
      const contact = sidToContact(sid);
      if (!contact) {
        // NOTE: the bridge does NOT recover from this. Its _ensure_session()
        // returns whatever sid is in the persisted contact->sid map without
        // re-creating it, and chat() swallows the exception and returns "".
        // So an undecodable sid means that contact silently gets empty
        // replies until the map entry is deleted. We only ever hand out valid
        // wc_* ids and run_wechat_dsh.py derives a per-config map file, so
        // this should not fire; log it loudly rather than failing quietly.
        log(`chat 404 undecodable sid "${sid.slice(0, 64)}" - stale map entry?`);
        respond(res, 404, { error: "session not found", id: sid });
        return;
      }

      const payload = await readObject(req, res);
      if (payload === undefined) return;

      const message = typeof payload.message === "string" ? payload.message.trim() : "";
      if (!message) {
        respond(res, 400, { error: "message is required", session_id: sid });
        return;
      }

      log(`chat <- ${contact}: ${message.slice(0, 60)}`);
      const reply = await sessions.chat(contact, message, { timeoutMs: turnTimeoutMs });
      respond(res, 200, {
        object: "dsh.message",
        session_id: sid,
        message: { role: "assistant", content: reply },
      });
      return;
    }

    respond(res, 404, { error: "not found" });
  }

  return new Promise<RunningEntry>((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, host, () => {
      server.removeListener("error", reject);
      const address = server.address();
      const bound = typeof address === "object" && address ? address.port : port;
      log(
        `agent entry listening on http://${host}:${bound}  ` +
          `(GET /health, POST /api/sessions, GET /api/sessions, POST /api/sessions/{sid}/chat)`,
      );
      resolve({
        port: bound,
        close: () =>
          new Promise<void>((done) => {
            server.close(() => done());
          }),
      });
    });
  });

  /**
   * Read a JSON object body, answering 400 itself on a malformed or
   * non-object payload. Resolves to undefined once it has replied.
   *
   * `isinstance(payload, dict)` upstream: an array or a bare scalar is not a
   * request body we can read fields out of.
   */
  async function readObject(
    req: import("node:http").IncomingMessage,
    res: import("node:http").ServerResponse,
  ): Promise<Record<string, unknown> | undefined> {
    let payload: unknown;
    try {
      payload = await readJson(req);
    } catch (err) {
      respond(res, 400, { error: `bad json: ${String((err as Error).message ?? err)}` });
      return undefined;
    }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      respond(res, 400, { error: "body must be a JSON object" });
      return undefined;
    }
    return payload as Record<string, unknown>;
  }
}

function respond(
  res: import("node:http").ServerResponse,
  status: number,
  body: unknown,
): void {
  if (res.writableEnded) return;
  const data = Buffer.from(JSON.stringify(body), "utf8");
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": String(data.length),
  });
  res.end(data);
}

async function readJson(req: import("node:http").IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    const buf = chunk as Buffer;
    size += buf.length;
    if (size > MAX_BODY_BYTES) throw new Error("body too large");
    chunks.push(buf);
  }
  if (!chunks.length) return {};
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}
```

### A3. `src/session-manager.ts`

联系人 ↔ DSH 会话，跑一轮 agent，读事件日志

```ts
/**
 * Contact -> DSH session isolation.
 *
 * Mirrors the session strategy of the hermes bridge: one long-lived session
 * per WeChat contact, keyed by a deterministic id so a restart can resume
 * the same conversation instead of orphaning it. That is what keeps two
 * friends' contexts from bleeding into each other.
 *
 * The DSH agent API used here follows dsh-qqbot's inbound transport, which is
 * the reference implementation for "external message -> agent turn":
 *
 *   agents.get(sessionId)                       // live agent?
 *   await agents.resume({ resumeSessionId })    // persisted session
 *   await agents.create({ sessionId, meta })    // brand new
 *   agent.followup(createUserMessage(...))
 *   await agent.whenIdle()
 *   agent.session.events                        // read the reply back
 */
import { createHash } from "node:crypto";
import { createUserMessage } from "@deepseek-ai/dsh-llm";
import { SessionId } from "@deepseek-ai/dsh-session";
import type { AgentSession, AgentsService, ChatOptions, SessionRecord } from "./types.js";

/** Deterministic uuid-shaped id so restarts resume the same session. */
function deriveSessionId(contact: string): string {
  const hash = createHash("sha256").update(`wechat:${contact}`).digest("hex");
  return `${hash.slice(0, 8)}-${hash.slice(8, 12)}-${hash.slice(12, 16)}-${hash.slice(16, 20)}-${hash.slice(20, 32)}`;
}

/** Pull plain text out of a message's content-block array. */
function extractMessageText(message: unknown): string {
  const blocks = (message as { content?: unknown })?.content;
  if (!Array.isArray(blocks)) return "";
  const parts: string[] = [];
  for (const block of blocks as Array<{ type?: string; text?: string }>) {
    if (block?.type === "text" && block.text) parts.push(block.text);
  }
  return parts.join("\n").trim();
}

/**
 * Read the agent's reply back out of the session.
 *
 * The session is an append-only event log, not a message array: iterate
 * `session.surface.nodes` (ordered seqs) and pull each event with
 * `session.eventAt(seq)`, whose payload lives under `data.message`. This is
 * the same traversal dsh-agent-loop uses internally, so it sees exactly the
 * events the model saw.
 */
function lastAssistantText(agent: unknown): string {
  let reply = "";
  for (const message of assistantMessages(agent)) {
    const text = extractMessageText(message);
    if (text) reply = text;
  }
  return reply;
}

/**
 * Every assistant message currently on the session, in log order.
 *
 * Read through `snapshotEvents()` — one call for the whole log. The
 * surface.nodes + eventAt() walk is kept as a fallback, but it MUST stay a
 * method call: `eventAt` is `return this.log[seq]`, so hoisting it into a
 * local drops the receiver and throws `reading 'log'`.
 */
function assistantMessages(agent: unknown): unknown[] {
  const session = (agent as { session?: AgentSession })?.session;
  if (!session) return [];

  let events: Array<{ type?: string; data?: { message?: unknown } }>;
  if (typeof session.snapshotEvents === "function") {
    events = session.snapshotEvents() as typeof events;
  } else {
    const nodes = session.surface?.nodes;
    if (!nodes || typeof session.eventAt !== "function") return [];
    events = [];
    for (const seq of nodes) {
      events.push(
        session.eventAt(seq) as { type?: string; data?: { message?: unknown } },
      );
    }
  }

  return events
    .filter((event) => event?.type === "assistant/message")
    .map((event) => event.data?.message);
}

export class SessionManager {
  private readonly records = new Map<string, SessionRecord>();
  /** Per-contact serial queue: one turn at a time, same as the GUI send queue. */
  private readonly chains = new Map<string, Promise<unknown>>();

  constructor(
    private readonly agents: AgentsService,
    private readonly log: (msg: string) => void,
    private readonly workspace: string,
    /** Model route handed to agents.create/resume.
     *  Required: without it the loop has no provider/model and
     *  prepareRequest() throws before any turn can run. */
    private readonly agentOptions: { provider: string; model: string },
  ) {}

  listSessions(): Record<string, string> {
    const out: Record<string, string> = {};
    for (const [contact, record] of this.records) out[contact] = record.sessionId;
    return out;
  }

  private async resolve(contact: string): Promise<SessionRecord> {
    const cached = this.records.get(contact);
    if (cached) return cached;

    const sessionId = SessionId(deriveSessionId(contact));
    const live = this.agents.get(sessionId);
    let agent = live;
    let handle = live ? { agent: live, dispose: async () => {} } : null;

    if (!agent) {
      try {
        const resumed = await this.agents.resume({
          resumeSessionId: sessionId,
          agentOptions: this.agentOptions,
        });
        agent = resumed.agent;
        handle = resumed;
        this.log(`resumed session for ${contact} (${sessionId})`);
      } catch {
        const created = await this.agents.create({
          sessionId,
          meta: { cwd: this.workspace },
          agentOptions: this.agentOptions,
        });
        agent = created.agent;
        handle = created;
        this.log(`created session for ${contact} (${sessionId})`);
      }
    } else {
      this.log(`reusing live agent for ${contact}`);
    }

    const record: SessionRecord = { sessionId, agent, handle: handle! };
    this.records.set(contact, record);
    return record;
  }

  /**
   * Run one turn for a contact and return the agent's reply.
   *
   * Turns for the same contact are serialised through a promise chain so a
   * second message can never interleave with the first turn's tool calls.
   */
  chat(contact: string, message: string, options: ChatOptions = {}): Promise<string> {
    const previous = this.chains.get(contact) ?? Promise.resolve();
    const next = previous.then(
      () => this.runTurn(contact, message, options),
      () => this.runTurn(contact, message, options),
    );
    // Keep the chain alive but never let a rejection leak to the next caller.
    this.chains.set(
      contact,
      next.catch(() => undefined),
    );
    return next;
  }

  private async runTurn(
    contact: string,
    message: string,
    options: ChatOptions,
  ): Promise<string> {
    const record = await this.resolve(contact);
    const before = assistantMessages(record.agent).length;

    record.agent.followup(
      createUserMessage({
        content: [{ type: "text", text: message }],
        source: { kind: "user" },
      }),
    );

    await withTimeout(record.agent.whenIdle(), options.timeoutMs ?? 120_000);

    const reply = lastAssistantText(record.agent);
    if (!reply && assistantMessages(record.agent).length <= before) {
      throw new Error(
        `agent produced no assistant reply (${describeAgent(record.agent)})`,
      );
    }
    this.log(`${contact}: replied ${reply.length} chars`);
    return reply;
  }

  /** Tear every agent down; called when the plugin fiber unloads. */
  async disposeAll(): Promise<void> {
    for (const [, record] of this.records) {
      try {
        await record.handle.dispose();
      } catch {
        // Disposal is best-effort — the runtime is already going away.
      }
    }
    this.records.clear();
    this.chains.clear();
  }
}

/**
 * Render whatever the agent actually looks like after a turn, so a failed
 * reply is diagnosable from the HTTP error body alone.
 */
function describeAgent(agent: unknown): string {
  const session = (agent as { session?: AgentSession })?.session;
  if (!session) return "agent has no session";

  let events: Array<{ type?: string }> | undefined;
  if (typeof session.snapshotEvents === "function") {
    events = session.snapshotEvents() as Array<{ type?: string }>;
  } else {
    const nodes = session.surface?.nodes;
    if (nodes && typeof session.eventAt === "function") {
      events = [];
      for (const seq of nodes) {
        events.push(session.eventAt(seq) as { type?: string });
      }
    }
  }
  if (!events) {
    return `session ${String(session.id)}: no readable event source ` +
      `(snapshotEvents=${typeof session.snapshotEvents}, ` +
      `surface=${session.surface ? "present" : "missing"}, ` +
      `eventAt=${typeof session.eventAt})`;
  }
  const counts = new Map<string, number>();
  for (const event of events) {
    const type = event?.type ?? "?";
    counts.set(type, (counts.get(type) ?? 0) + 1);
  }
  return `session ${String(session.id)}: ` +
    `[${[...counts].map(([k, v]) => `${k}x${v}`).join(",")}]`;
}

/** Reject with a clear message rather than hanging the HTTP request forever. */
function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  if (!Number.isFinite(ms) || ms <= 0) return promise;
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`agent turn timed out after ${ms}ms`)), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (err) => {
        clearTimeout(timer);
        reject(err);
      },
    );
  });
}
```

### A4. `src/sid.ts`

sid 编解码与严格性

```ts
/**
 * Stateless session ids for the hermes API dialect.
 *
 * The bot's bridge persists `contact -> session_id` in
 * `wechat_convo_map_*.json`, which survives our process. After a DSH restart
 * the bridge therefore replays an id this process has never seen, and the
 * chat request carries ONLY that id — the contact name is not in the
 * payload. So the id has to decode back to the contact on its own:
 *
 *     sid = "wc_" + base64url(utf8(contact))      (padding stripped)
 *
 * That keeps the mapping a pure function of the contact, so a restart is a
 * no-op instead of a session loss, and two contacts can never collide.
 */

/** Marker namespace so a stray non-WeChat id is rejected instead of guessed at. */
const PREFIX = "wc_";

/** base64url alphabet. `=` padding is never produced or accepted. */
const BASE64URL = /^[A-Za-z0-9_-]+$/;

/**
 * Encode a WeChat contact into a hermes session id.
 *
 * Deterministic and stateless: same contact in, same id out, forever.
 */
export function contactToSid(contact: string): string {
  // toString("base64url") already omits `=` padding.
  return PREFIX + Buffer.from(contact, "utf8").toString("base64url");
}

/**
 * Decode a session id back to its contact, or null when it is not ours.
 *
 * Returning null (rather than throwing) keeps the caller on a single
 * documented path: answer `404 {"error": "session not found"}`.
 *
 * WARNING: the bridge does NOT recover from that 404. Its _ensure_session()
 * returns whatever sid is in the persisted contact->sid map without
 * re-creating it, and chat() swallows the exception and returns "". So a
 * stale map entry means that contact silently gets empty replies until the
 * entry is deleted from wechat_convo_map_*.json. This is why sids are
 * self-describing — a restart never needs the map to be rewritten.
 */
export function sidToContact(sid: string): string | null {
  if (typeof sid !== "string" || !sid.startsWith(PREFIX)) return null;

  const payload = sid.slice(PREFIX.length);
  if (!payload || !BASE64URL.test(payload)) return null;

  // Re-add the padding `base64url` strips, so the decoder gets a well-formed
  // unit. `payload.length % 4 === 1` cannot be produced by base64 at all.
  const remainder = payload.length % 4;
  if (remainder === 1) return null;
  const padded = remainder === 0 ? payload : payload + "=".repeat(4 - remainder);

  let contact: string;
  try {
    // fatal:true so a byte sequence that is not valid UTF-8 is rejected
    // instead of silently decoding to U+FFFD.
    contact = new TextDecoder("utf-8", { fatal: true }).decode(
      Buffer.from(padded, "base64url"),
    );
  } catch {
    return null;
  }
  if (!contact) return null;

  // Canonical-form check: rejects trailing bits that Node's lenient decoder
  // ignores (e.g. "wc_YQ" and "wc_YR" both decode to "a").
  if (contactToSid(contact) !== sid) return null;

  return contact;
}

/**
 * Recover the contact from a session title of the form `微信-<contact>`,
 * which is what the bridge sends when it creates a session.
 *
 * A title without the prefix is taken as the bare contact name, so a
 * hand-rolled or already-persisted title still resolves.
 */
export function titleToContact(title: string): string {
  const trimmed = title.trim();
  return trimmed.startsWith("微信-") ? trimmed.slice("微信-".length) : trimmed;
}

/** The title the bridge would use for a contact. Inverse of `titleToContact`. */
export function contactToTitle(contact: string): string {
  return `微信-${contact}`;
}
```

### A5. `src/types.ts`

内部接口

```ts
/**
 * Local type surface for the DSH services this plugin touches.
 *
 * The `@deepseek-ai/*` packages ship no declarations in this install, so the
 * shapes below are declared here instead of being imported. They mirror the
 * runtime contract used by `@tencent-connect/dsh-qqbot`'s inbound transport,
 * which is the reference implementation for driving an agent from outside.
 */

export interface AgentHandle {
  dispose(): Promise<void>;
}

/**
 * The session is an append-only event log.
 *
 * `snapshotEvents()` returns the whole log as a frozen array — the cheap
 * read. `surface.nodes` + `eventAt(seq)` is the per-event walk; `eventAt`
 * is `return this.log[seq]`, so it MUST be invoked as a method or `this`
 * is lost. Both read paths are marked deprecated upstream but remain the
 * only synchronous options, so prefer `snapshotEvents`.
 */
export interface AgentSession {
  id?: unknown;
  snapshotEvents?(fromSeq?: number, toSeqExclusive?: number): unknown[];
  surface?: { nodes?: Iterable<number> };
  eventAt?(seq: number): unknown;
}

export interface AgentLike {
  followup(message: unknown): void;
  cancel?(input: unknown): void;
  whenIdle(): Promise<void>;
  session?: AgentSession;
}

export interface CreateOptions {
  sessionId: string;
  meta?: Record<string, unknown>;
  agentOptions?: { provider?: string; model?: string };
}

export interface ResumeOptions {
  resumeSessionId: string;
  agentOptions?: { provider?: string; model?: string };
}

export interface AgentsService {
  get(sessionId: string): AgentLike | undefined;
  create(options: CreateOptions): Promise<{ agent: AgentLike } & AgentHandle>;
  resume(options: ResumeOptions): Promise<{ agent: AgentLike } & AgentHandle>;
}

export interface SessionRecord {
  sessionId: string;
  agent: AgentLike;
  handle: AgentHandle;
}

export interface ChatOptions {
  timeoutMs?: number;
}
```

### A6. `src/types-shim.d.ts`

`@deepseek-ai/*` 的本地环境声明

```ts
// -----------------------------------------------------------------------------
// Local type shims for the DSH packages that ship without .d.ts files
// inside this build (or whose .d.ts are not copied through pnpm's
// file: protocol). Runtime shapes are pinned by the DSH desktop client
// we are paired with; these shims only declare the surface this plugin
// actually uses.
//
// `@deepseek-ai/schemastery` ships proper d.ts in the original DSH
// install but pnpm file: install drops them. We re-declare the slice
// we touch (`.object()`, `.string().default().description()`,
// `.number().default().description()`).
//
// `@deepseek-ai/cordis` and `@deepseek-ai/dsh-tools` are DSH
// framework packages whose actual sources are not exposed for build
// either. We model only what `apply()` and `defineTool()` use.
// -----------------------------------------------------------------------------

declare module "@deepseek-ai/schemastery" {
  export interface SchemaType<T = unknown> {
    default(value: T): SchemaType<T>;
    description(text: string): SchemaType<T>;
  }
  export type SchemaShape = Record<string, SchemaType<any>>;
  export type Infer<S> = S extends SchemaShape
    ? { [K in keyof S]: S[K] extends SchemaType<infer V> ? V : never }
    : never;
  export interface SchemaNamespace {
    string(): SchemaType<string>;
    number(): SchemaType<number>;
    object<S extends SchemaShape>(shape: S): SchemaType<Infer<S>>;
  }
  const Schema: SchemaNamespace;
  export default Schema;
}

declare module "@deepseek-ai/dsh-tools" {
  export type ToolParameterSpec = Record<string, unknown>;
  export type ToolOutputSpec = unknown;

  /**
   * What `output.render` must return. defineTool assigns the render result
   * straight to `result.content`, and the runtime walks it as
   * `content.some(block => block.type === "image")` — so a bare string
   * crashes the turn AFTER the tool body already ran.
   */
  export interface ToolContentBlock {
    type: string;
    text: string;
  }

  export interface DefineToolOptions<TArgs = any, TOutput = any> {
    name: string;
    description: string;
    parameters: ToolParameterSpec;
    output: {
      schema: ToolOutputSpec;
      render(args: TArgs, value: TOutput): ToolContentBlock[];
    };
    timeoutMs?: number;
    execute(args: TArgs, exec: unknown): Promise<TOutput>;
  }

  export function defineTool<TArgs = any, TOutput = any>(
    options: DefineToolOptions<TArgs, TOutput>,
  ): {
    name: string;
    description: string;
    parameters: ToolParameterSpec;
    output: {
      schema: ToolOutputSpec;
      render(args: TArgs, value: TOutput): ToolContentBlock[];
    };
    timeoutMs?: number;
    execute(args: TArgs, exec: unknown): Promise<TOutput>;
  };
}

declare module "@deepseek-ai/dsh-llm" {
  export function createUserMessage(input: {
    content: Array<{ type: string; text?: string }>;
    source?: { kind?: string };
  }): unknown;
}

declare module "@deepseek-ai/dsh-session" {
  export function SessionId(id: string): string;
}

declare module "@deepseek-ai/cordis" {
  export interface Logger {
    info(...args: unknown[]): void;
    warn(...args: unknown[]): void;
    error(...args: unknown[]): void;
    debug(...args: unknown[]): void;
  }

  export interface Context {
    logger(name: string): Logger;
    tools: {
      register(tool: unknown): () => void;
    };
    systemPrompt: {
      section(options: { name: string; order: number; text: string }): () => void;
    };
    agents: unknown;
    effect(
      setup: (ctx: Context) => (() => void) | Promise<() => void | Promise<void>>,
      name: string,
    ): () => void;
  }
}
```
