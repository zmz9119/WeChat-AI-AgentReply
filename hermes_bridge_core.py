"""
Hermes Bridge - 与 Hermes Agent 的双向通道

设计要点：
- 零额外依赖（标准库 http.server），与 bot 同架构风格
- 每个联系人 ↔ 一个 Hermes session（持久化到 json，杜绝串线）
- 被动应答：chat() 调 Hermes API Server
- 主动推送：暴露 POST /send，Hermes 侧通过 wechat_send 工具调用
"""
import os
import sys
import json
import time
import threading
import queue
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── 常量 ─────────────────────────────────────────────────────────────────────
HERMES_API = os.environ.get("HERMES_API", "http://127.0.0.1:8642")
HERMES_KEY = os.environ.get("HERMES_API_KEY", "")
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "8900"))
CONVO_MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wechat_convo_map.json")
CHAT_TIMEOUT = 180


def _log(msg):
    ts = time.strftime("[%Y-%m-%d %H:%M:%S]")
    print(f"{ts} [Bridge] {msg}", flush=True)


# ── 会话映射（联系人 <-> Hermes session_id）────────────────────────────────
class SessionMap:
    """联系人 ↔ Hermes session_id 持久化映射"""

    def __init__(self, path):
        self._path = path
        self._lock = threading.Lock()
        self._data = {}
        self._load()

    def _load(self):
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    self._data = json.load(f) or {}
                _log(f"载入会话映射: {len(self._data)} 条")
            except Exception as e:
                _log(f"映射载入失败: {e}")
                self._data = {}

    def _save(self):
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    def get(self, contact):
        with self._lock:
            return self._data.get(contact)

    def set(self, contact, session_id):
        with self._lock:
            self._data[contact] = session_id
            self._save()
            _log(f"映射 {contact} -> {session_id}")

    def all(self):
        with self._lock:
            return dict(self._data)


# ── Hermes 客户端 ──────────────────────────────────────────────────────────
class HermesClient:
    """调 Hermes API Server"""

    def __init__(self, base, key):
        self._base = base.rstrip("/")
        self._key = key

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self._key:
            h["Authorization"] = f"Bearer {self._key}"
        return h

    def _req(self, method, path, payload=None, timeout=60):
        url = f"{self._base}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body) if body.strip() else {}

    def health(self):
        try:
            return self._req("GET", "/health", timeout=5)
        except Exception as e:
            return {"error": str(e)}

    def create_session(self, title):
        return self._req("POST", "/api/sessions", {"title": title}, timeout=30)

    def session_chat(self, session_id, message, timeout=CHAT_TIMEOUT):
        """单轮同步对话"""
        return self._req("POST", f"/api/sessions/{session_id}/chat",
                         {"message": message}, timeout=timeout)

    def session_chat_stream(self, session_id, message, timeout=CHAT_TIMEOUT):
        """SSE 流式对话，逐行 yield (event, data)"""
        url = f"{self._base}/api/sessions/{session_id}/chat/stream"
        payload = json.dumps({"message": message}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers=self._headers(), method="POST")
        event = None
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if not line:
                    continue
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                    if data and data != "[DONE]":
                        yield event, data


# ── 发送队列（串行化 GUI 操作）────────────────────────────────────────────
class SendQueue:
    """所有 GUI 发送操作走这里，避免多线程抢鼠标"""

    def __init__(self, sender_fn):
        self._q = queue.Queue()
        self._sender = sender_fn
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(self, contact, text):
        self._q.put((contact, text))

    def size(self):
        return self._q.qsize()

    def _run(self):
        while True:
            contact, text = self._q.get()
            try:
                self._sender(contact, text)
            except Exception as e:
                _log(f"发送失败 [{contact}]: {e}")
            finally:
                self._q.task_done()


# ── HTTP 端点 ──────────────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    bridge = None  # 运行时注入

    def log_message(self, fmt, *args):
        pass  # 静默

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return {}

    def do_GET(self):
        b = self.bridge
        if self.path == "/health":
            # 只报 Bridge 自身存活。不在这里同步查 Hermes——
            # 主循环跑 OCR 轮询占 GIL，同步网络调用会把 HTTP 线程饿死。
            # Hermes 状态用 /hermes 端点单独查（走缓存，不阻塞）。
            self._json(200, {"ok": True, "service": "wechat-bridge"})
        elif self.path == "/hermes":
            self._json(200, {"hermes": b.hermes_cached()})
        elif self.path == "/status":
            self._json(200, {"sessions": b.smap.all(),
                             "queue_size": b.sendq.size()})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        b = self.bridge
        data = self._read_body()
        if self.path == "/send":
            contact = (data.get("contact") or data.get("to") or "").strip()
            text = (data.get("text") or data.get("message") or "").strip()
            if not contact or not text:
                self._json(400, {"error": "contact 和 text 必填"})
                return
            b.sendq.submit(contact, text)
            self._json(202, {"queued": True, "contact": contact,
                             "queue_size": b.sendq.size()})
        elif self.path == "/chat":
            # 手动触发一次对话（调试用）
            contact = data.get("contact", "").strip()
            message = data.get("message", "").strip()
            reply = b.chat(contact, message)
            self._json(200, {"reply": reply})
        else:
            self._json(404, {"error": "not found"})


# ── Bridge 主体 ────────────────────────────────────────────────────────────
class HermesBridge:
    def __init__(self, gui_sender, config=None):
        """
        gui_sender: callable(contact, text) -> bool
            实际执行 GUI 发送（由 bot 注入，复用现有粘贴发送链路）
        """
        cfg = config or {}
        self.hermes = HermesClient(
            cfg.get("hermes_api", HERMES_API),
            cfg.get("hermes_api_key", HERMES_KEY),
        )
        self.smap = SessionMap(CONVO_MAP_FILE)
        self.sendq = SendQueue(gui_sender)
        self._server = None
        self._hermes_health = {"status": "unknown"}
        self._health_ts = 0.0
        self._start_health_poller()

    def _start_health_poller(self):
        """后台线程定期刷新 Hermes 健康状态，供 /hermes 端点读取。

        必须异步：bot 主循环跑 OCR 轮询占 GIL，HTTP 线程里的同步
        网络调用会被饿死（表现为连接建立但响应永不到）。
        """
        def poll():
            while True:
                try:
                    self._hermes_health = self.hermes.health()
                except Exception as e:
                    self._hermes_health = {"error": str(e)}
                self._health_ts = time.time()
                time.sleep(15)
        threading.Thread(target=poll, daemon=True).start()

    def hermes_cached(self):
        """读缓存的 Hermes 健康状态，5 秒内直接复用，绝不阻塞。"""
        if time.time() - self._health_ts > 5:
            self._hermes_health = self.hermes.health()
            self._health_ts = time.time()
        return dict(self._hermes_health, checked_at=self._health_ts)

    def size(self):
        return self.sendq.size()

    def _ensure_session(self, contact):
        """拿到联系人的 session_id，没有就建一个"""
        sid = self.smap.get(contact)
        if sid:
            return sid
        title = f"微信-{contact}"
        try:
            res = self.hermes.create_session(title)
        except urllib.error.HTTPError as e:
            # 标题已被占用（进程重启后旧 session 还在）：找回来复用，而不是报错
            if e.code != 400:
                raise
            res = self._find_session_by_title(title)
            if not res:
                # 占用者可能是同名但不同联系人，退化成唯一标题重试一次
                res = self.hermes.create_session(f"{title}-{int(time.time())}")
        # API Server 返回 {"object":"hermes.session","session":{...,"id":...}}
        # —— id 嵌在 session 里，不是顶层。同时兼容顶层/别名的写法。
        node = res.get("session") if isinstance(res.get("session"), dict) else res
        sid = node.get("id") or node.get("session_id")
        if not sid:
            raise RuntimeError(f"创建 session 失败，响应结构异常: {res}")
        self.smap.set(contact, sid)
        return sid

    def _find_session_by_title(self, title):
        """按标题找回已存在的 session（用于处理 400 标题冲突）"""
        try:
            listing = self.hermes._req("GET", "/api/sessions?limit=100", timeout=15)
        except Exception:
            return None
        items = listing.get("data") or listing.get("sessions") or []
        if isinstance(items, dict):
            items = items.get("data", [])
        for it in items:
            node = it.get("session") if isinstance(it.get("session"), dict) else it
            if node.get("title") == title and node.get("id"):
                return node
        return None

    def chat(self, contact, message, timeout=CHAT_TIMEOUT):
        """被动应答：联系人发来消息 → Hermes 生成回复"""
        try:
            sid = self._ensure_session(contact)
            _log(f"chat -> {contact} (session {sid[:8]})")
            res = self.hermes.session_chat(sid, message, timeout=timeout)
            # 响应结构: {"object":..., "session_id":..., "message":{"role","content"},...}
            # content 嵌在 message 里；同时兼容 content/reply 顶层写法。
            reply = res.get("content") or res.get("reply")
            if not reply and isinstance(res.get("message"), dict):
                reply = res["message"].get("content")
            if not reply and isinstance(res.get("message"), str):
                reply = res["message"]
            return str(reply or "").strip()
        except Exception as e:
            _log(f"chat 失败 [{contact}]: {e}")
            return ""

    def start(self, port=BRIDGE_PORT):
        handler = type("H", (_Handler,), {"bridge": self})
        # allow_reuse_address=False：Windows 上设 True 会让重复 bind 的僵尸
        # 进程也显示 LISTENING，连接随机落到死进程，表现为「连上了没响应」。
        class _Srv(ThreadingHTTPServer):
            allow_reuse_address = False
            daemon_threads = True
        self._server = _Srv(("127.0.0.1", port), handler)
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()
        _log(f"Bridge 已启动: http://127.0.0.1:{port}")
        _log(f"  被动应答: chat(contact, message)")
        _log(f"  主动推送: POST /send  {json.dumps({'contact':'X','text':'Y'}, ensure_ascii=False)}")
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            _log("Bridge 已停止")
