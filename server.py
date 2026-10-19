#!/usr/bin/env python3
"""Pangen Image Studio - 本地生图工作台后端
纯标准库实现：静态文件服务 + 中转 API 代理 + 图片落盘 + 任务记录持久化
"""
import base64
import email.message
import email.policy
import json
import mimetypes
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------- 配置（全部支持环境变量覆盖，便于云端部署） ----------
ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(ROOT, "static")
STORAGE_DIR = os.environ.get("STORAGE_DIR", r"D:\PangenImages")
IMG_DIR = os.path.join(STORAGE_DIR, "images")
TASKS_FILE = os.path.join(STORAGE_DIR, "tasks.json")

BASE_URL = "https://codex.pangen.dev/v1"
API_KEY = os.environ.get("PANGEN_API_KEY", "")  # 必须通过环境变量提供，不硬编码
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TIMEOUT = 300

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
ACCESS_CODE = os.environ.get("ACCESS_CODE", "")  # 旧变量：单口令不限量，仍兼容

DEFAULT_MODEL = "gpt-image-2"

# 对话编排模型：优先 6-luna，不可用自动降级
CHAT_MODELS = ["gpt-6-luna", "gpt-5.6-luna", "gpt-6", "gpt-5.6"]
CHAT_MODEL = os.environ.get("PANGEN_CHAT_MODEL", CHAT_MODELS[0])

# 参考图上限（前后端一致）
MAX_REFS = 6
MAX_UPLOAD_BYTES = 40 * 1024 * 1024   # 单次请求体上限
VISION_INLINE_LIMIT = 4 * 1024 * 1024  # 超过此体积的图不塞进多模态上下文

# 多口令 + 每日限额：PANGEN_CODES="owner:CODE1:0,friend:CODE2:30"
# 格式 标签:口令:每日张数（0=不限量）；label 为 owner 的口令可见并管理全部任务
CLIENTS = []
for _item in os.environ.get("PANGEN_CODES", "").split(","):
    _parts = _item.strip().split(":")
    if len(_parts) == 3 and _parts[1].strip():
        CLIENTS.append({"label": _parts[0].strip(), "code": _parts[1].strip(),
                        "limit": int(_parts[2].strip() or 0)})
if ACCESS_CODE and not any(c["code"] == ACCESS_CODE for c in CLIENTS):
    CLIENTS.append({"label": "owner", "code": ACCESS_CODE, "limit": 0})

MODELS = [
    {"id": "gpt-image-1", "label": "GPT-Image 1", "inpaint": False},
    {"id": "gpt-image-1.5", "label": "GPT-Image 1.5", "inpaint": False},
    {"id": "gpt-image-2", "label": "GPT-Image 2", "inpaint": False},
    {"id": "gpt-image-2.5-flare", "label": "GPT-Image 2.5 Flare", "inpaint": True},
    {"id": "gpt-image-2.5-sunburst", "label": "GPT-Image 2.5 Sunburst", "inpaint": True},
]
SIZES = ["1024x1024", "1536x1024", "1024x1536", "auto"]

_lock = threading.Lock()

# ---------- 存储 ----------
def ensure_dirs():
    os.makedirs(IMG_DIR, exist_ok=True)
    if not os.path.exists(TASKS_FILE):
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)


def load_tasks():
    with _lock:
        if not os.path.exists(TASKS_FILE):
            return []
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)


def save_tasks(tasks):
    with _lock:
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=1)


def upsert_task(task):
    tasks = load_tasks()
    tasks = [t for t in tasks if t["id"] != task["id"]]
    tasks.insert(0, task)
    save_tasks(tasks)


# ---------- 用量统计（按口令标签每日计数） ----------
USAGE_FILE = os.path.join(STORAGE_DIR, "usage.json")


def _today():
    return time.strftime("%Y-%m-%d")


def load_usage():
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def add_usage(label, n):
    with _lock:
        usage = load_usage()
        day_use = usage.get(_today(), {})
        day_use[label] = day_use.get(label, 0) + n
        usage[_today()] = day_use
        for k in sorted(usage.keys())[:-60]:  # 只保留最近 60 天
            usage.pop(k)
        with open(USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(usage, f, ensure_ascii=False, indent=1)


def quota_of(client):
    used = load_usage().get(_today(), {}).get(client["label"], 0)
    limit = client["limit"]
    return {"label": client["label"], "date": _today(), "used": used,
            "limit": limit, "remaining": None if limit <= 0 else max(0, limit - used)}


def quota_block(client, n):
    """返回 None 表示放行；否则返回拒绝原因文案。"""
    if client["limit"] <= 0:
        return None
    used = load_usage().get(_today(), {}).get(client["label"], 0)
    if used + n > client["limit"]:
        return ("今日额度已用完（%d/%d 张），额度每天 0 点重置，请明天再来。"
                % (used, client["limit"]))
    return None


# ---------- 中转 API ----------
# 走系统默认的网络路径（有 http(s)_proxy 就用代理——云端无代理时自动直连）。
# 注意：不要用 ProxyHandler({}) 强制直连，某些沙箱只放行代理出口，直连会卡在 TLS 握手。
_OPENER = urllib.request.build_opener()


def relay_request(path, body, headers, timeout=TIMEOUT, retries=2):
    """调用中转。

    连接层失败（TLS 握手超时 / 连接被拒）时请求还没送达上游，重试不会造成重复扣费，
    因此这类失败无论耗时多久都重试；已建立连接后的读超时不重试。
    """
    last = "未知错误"
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            BASE_URL.rstrip("/") + path, data=body,
            headers={"Authorization": "Bearer " + API_KEY,
                     "User-Agent": UA,
                     "Accept": "application/json", **headers},
            method="POST")
        t0 = time.time()
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            return None, "HTTP %s: %s" % (e.code, detail)
        except urllib.error.URLError as e:
            last = "网络错误: %s" % e.reason
        except Exception as e:
            last = "请求失败: %s" % e

        elapsed = time.time() - t0
        connect_level = ("handshake" in last or "refused" in last
                         or "10061" in last or "getaddrinfo" in last
                         or "Connection reset" in last or elapsed < 8)
        sys.stderr.write("[relay] %s 失败(%.1fs, 可重试=%s): %s\n"
                         % (path, elapsed, connect_level, last[:120]))
        if attempt < retries and connect_level:
            time.sleep(1.2)
            continue
        break
    return None, last


def relay_chat(messages, tools=None, timeout=150, model=None):
    """调用对话补全（OpenAI 兼容）。返回 (data, err)。"""
    payload = {"model": model or CHAT_MODEL, "messages": messages}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return relay_request("/chat/completions", json.dumps(payload).encode("utf-8"),
                         {"Content-Type": "application/json"}, timeout=timeout)


def mime_from_bytes(raw):
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def build_relay_multipart(fields, files):
    """files: list of (field_name, filename, data_bytes[, content_type])"""
    boundary = "----StudioBoundary" + uuid.uuid4().hex
    buf = bytearray()
    for k, v in fields.items():
        buf += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                % (boundary, k, v)).encode("utf-8")
    for item in files:
        name, filename, data = item[0], item[1], item[2]
        ctype = item[3] if len(item) > 3 else mime_from_bytes(data)
        buf += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                "Content-Type: %s\r\n\r\n"
                % (boundary, name, filename, ctype)).encode("utf-8")
        buf += data + b"\r\n"
    buf += ("--%s--\r\n" % boundary).encode("utf-8")
    return bytes(buf), "multipart/form-data; boundary=%s" % boundary


def parse_multipart(body, content_type):
    msg = email.message.Message()
    msg["Content-Type"] = content_type
    fields, files = {}, []
    for part in msg.get_payload() if False else []:
        pass
    # 用 email 解析器处理（policy=default 才有 iter_parts）
    parser_msg = email.message_from_bytes(
        b"Content-Type: " + content_type.encode("latin-1") +
        b"\r\nMIME-Version: 1.0\r\n\r\n" + body,
        policy=email.policy.default)
    for part in parser_msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        if filename:
            files.append((name, filename, payload))
        else:
            fields[name] = payload.decode("utf-8", errors="replace")
    return fields, files


def ext_from_bytes(raw):
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    return ".png"


def save_result_images(data_list):
    saved = []
    ts = time.strftime("%Y%m%d-%H%M%S")
    for i, item in enumerate(data_list):
        if item.get("b64_json"):
            raw = base64.b64decode(item["b64_json"])
        elif item.get("url"):
            with _OPENER.open(
                    urllib.request.Request(item["url"], headers={"User-Agent": UA}),
                    timeout=120) as r:
                raw = r.read()
        else:
            continue
        fname = "%s-%d%s" % (ts, i + 1, ext_from_bytes(raw))
        path = os.path.join(IMG_DIR, fname)
        with open(path, "wb") as f:
            f.write(raw)
        saved.append(fname)
    return saved


def run_generation(task_id, mode, fields, files=None, client_label=""):
    """执行生成并更新任务状态。mode: t2i | i2i | inpaint"""
    task = {"id": task_id, "mode": mode, "status": "running", "client": client_label,
            "created_at": time.time(), **{k: fields.get(k, "") for k in
            ("prompt", "model", "size", "quality", "n")}}
    upsert_task(task)
    t0 = time.time()
    try:
        if mode == "t2i":
            payload = {"model": fields["model"], "prompt": fields["prompt"],
                       "size": fields.get("size", "auto"), "n": int(fields.get("n") or 1)}
            if fields.get("quality"):
                payload["quality"] = fields["quality"]
            body = json.dumps(payload).encode("utf-8")
            data, err = relay_request("/images/generations", body,
                                      {"Content-Type": "application/json"})
        else:
            mp_fields = {"model": fields["model"], "prompt": fields["prompt"],
                         "size": fields.get("size", "auto"),
                         "n": str(int(fields.get("n") or 1))}
            if fields.get("quality"):
                mp_fields["quality"] = fields["quality"]
            relay_files = [("image[]", fn, data) for fn, data in files["images"]]
            if files.get("mask"):
                relay_files.append(("mask", "mask.png", files["mask"]))
            body, ctype = build_relay_multipart(mp_fields, relay_files)
            data, err = relay_request("/images/edits", body, {"Content-Type": ctype})
        if err:
            raise RuntimeError(err)
        saved = save_result_images(data.get("data", []))
        if not saved:
            raise RuntimeError("响应中没有图片数据")
        task.update({"status": "done", "images": saved,
                     "duration": round(time.time() - t0, 1),
                     "usage": data.get("usage")})
        add_usage(client_label, len(saved))
    except Exception as e:
        task.update({"status": "error", "error": str(e)[:500],
                     "duration": round(time.time() - t0, 1)})
    upsert_task(task)
    return task


# ---------- 对话编排（LLM 输出结构化指令 → 执行生图） ----------
# 说明：本中转的对话模型对 tools 参数支持不稳定（带 tools 会挂起），
# 因此改成让模型直接输出 JSON 指令，服务端解析后执行，链路更稳也更快。
CHAT_SYSTEM = """你是 Pangen 生图工作台的助手，服务电商卖家的日常出图需求。用户用中文跟你说话。

你必须只输出一个 JSON 对象，不要输出任何其它文字，也不要用 markdown 代码块包裹。格式：

{"action":"generate|edit|answer","prompt":"英文画面描述","size":"1024x1024","n":1,"reply":"给用户看的中文一句话"}

字段规则：
- action
  · generate —— 用户要一张新图，且这次没有参考图
  · edit —— 用户带了参考图（1~6 张），要你按描述改造、替换、融合它们，或基于它们出图
  · answer —— 用户在闲聊/提问，或需求信息严重不足需要先追问；此时绝对不要出图
- prompt：只在 generate / edit 时填。必须是英文的详细画面描述，包含主体、材质、构图、镜头、光线、色彩、风格、背景。要按专业图像 prompt 重写，不要照中文语序直译。answer 时填空字符串 ""。
- size：方图 1024x1024（默认）、横图 1536x1024、竖图 1024x1536。
- n：张数，默认 1，最多 4。用户没说就 1。
- reply：一句中文，20~40 字。generate/edit 时说明你做了什么，不要复述 prompt，不要编造画面里没有的细节；answer 时直接回答或只提 1 个关键追问。
"""

ALLOWED_SIZES = ("1024x1024", "1536x1024", "1024x1536")


def _extract_json(text):
    """从模型输出里抠出第一个完整 JSON 对象。"""
    if not text:
        return None
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1] if "```" in raw[3:] else raw.lstrip("`")
        raw = raw.strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    a, b = raw.find("{"), raw.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        obj = json.loads(raw[a:b + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _strip_vision(messages):
    """去掉多模态图片，只留文字（模型不支持读图时的降级）。"""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            texts, n_img = [], 0
            for p in c:
                if p.get("type") == "text":
                    texts.append(p.get("text", ""))
                elif p.get("type") == "image_url":
                    n_img += 1
            if n_img:
                texts.append("（用户上传了 %d 张参考图，你看不到图片内容，按用户文字描述处理）" % n_img)
            out.append(dict(m, content="\n".join(t for t in texts if t)))
        else:
            out.append(m)
    return out


def chat_round(messages):
    """带降级的对话调用：换模型 → 去多模态 → 再换模型。返回 (data, err, model, vision_ok)。"""
    attempts = []
    multi = any(isinstance(m.get("content"), list) for m in messages)
    for model in CHAT_MODELS[:3]:
        attempts.append((model, messages, True))
        if multi:
            attempts.append((model, _strip_vision(messages), False))
    last_err = ""
    for model, msgs, vision_ok in attempts:
        data, err = relay_chat(msgs, None, timeout=100, model=model)
        if not err and data and data.get("choices"):
            return data, None, model, vision_ok
        last_err = err or "对话接口返回为空"
    return None, last_err, CHAT_MODEL, True


def exec_plan(plan, client, img_model, img_files):
    """按模型给的指令执行生图。返回 (task, err)。"""
    prompt = (plan.get("prompt") or "").strip()
    if not prompt:
        return None, "模型没有给出有效的 prompt"
    action = plan.get("action")
    size = plan.get("size") or "1024x1024"
    if size not in ALLOWED_SIZES:
        size = "1024x1024"
    try:
        n = max(1, min(4, int(plan.get("n") or 1)))
    except (TypeError, ValueError):
        n = 1

    err = quota_block(client, n)
    if err:
        return None, err

    fields = {"prompt": prompt, "model": img_model, "size": size, "n": str(n)}
    if plan.get("quality") in ("high", "medium", "low"):
        fields["quality"] = plan["quality"]

    task_id = uuid.uuid4().hex[:12]
    if action == "edit" and img_files:
        return run_generation(task_id, "i2i", fields,
                              {"images": img_files[:MAX_REFS]},
                              client_label=client["label"]), None
    return run_generation(task_id, "t2i", fields, client_label=client["label"]), None


def run_chat(client, fields, img_files):
    """一轮对话：LLM 决策 →（需要时）执行生图 → 返回文案与图片。"""
    message = (fields.get("message") or "").strip()
    img_model = fields.get("model") or DEFAULT_MODEL
    if img_model not in [m["id"] for m in MODELS]:
        img_model = DEFAULT_MODEL

    history = []
    try:
        raw_hist = json.loads(fields.get("history") or "[]")
        if isinstance(raw_hist, list):
            for h in raw_hist[-12:]:
                role, text = h.get("role"), h.get("content") or ""
                if role in ("user", "assistant") and isinstance(text, str) and text.strip():
                    history.append({"role": role, "content": text[:3000]})
    except Exception:
        history = []

    img_files = img_files[:MAX_REFS]
    q = quota_of(client)
    quota_txt = ("口令不限量" if q["limit"] <= 0
                 else "今日已用 %d/%d 张，剩 %d 张" % (q["used"], q["limit"], q["remaining"]))

    # 界面上的画幅/张数胶囊作为默认值，模型没被明确要求时照用
    pref_size = fields.get("size") or "1024x1024"
    if pref_size not in ALLOWED_SIZES:
        pref_size = "1024x1024"
    try:
        pref_n = max(1, min(4, int(fields.get("n") or 1)))
    except (TypeError, ValueError):
        pref_n = 1

    system = (CHAT_SYSTEM + "\n【当前状态】这一轮用户附了 %d 张参考图；%s；出图模型 %s。\n"
              "【界面默认】画幅 %s、张数 %d —— 用户没有明确要求时就用这组值。\n"
              "【输出】只输出那个 JSON 对象。"
              % (len(img_files), quota_txt, img_model, pref_size, pref_n))

    user_parts = []
    if message:
        user_parts.append({"type": "text", "text": message})
    for fn, data in img_files:
        if len(data) <= VISION_INLINE_LIMIT:
            user_parts.append({"type": "image_url", "image_url": {
                "url": "data:%s;base64,%s" % (mime_from_bytes(data),
                                              base64.b64encode(data).decode())}})
    if img_files:
        user_parts.append({"type": "text", "text":
                           "（以上是 %d 张参考图，按上传顺序）" % len(img_files)})

    messages = [{"role": "system", "content": system}] + history
    if len(user_parts) > 1:
        messages.append({"role": "user", "content": user_parts})
    else:
        messages.append({"role": "user", "content": message})

    data, err, model_used, vision_ok = chat_round(messages)
    sys.stderr.write("[chat] llm model=%s vision=%s err=%s\n"
                     % (model_used, vision_ok, (err or "")[:120]))
    if err:
        return {"reply": "对话服务暂时不可用：%s" % err[:180], "images": [], "tasks": [],
                "usage": q, "chat_model": CHAT_MODEL, "error": True}

    text = (data["choices"][0].get("message") or {}).get("content") or ""
    text = text.strip()
    plan = _extract_json(text)
    base = {"images": [], "tasks": [], "usage": quota_of(client),
            "chat_model": model_used, "vision": vision_ok, "refs": len(img_files)}

    if not plan:
        # 模型没按格式回 —— 当成普通回答，不白扣额度
        base.update({"reply": text or "没听清，能再说具体点吗？", "action": "answer"})
        return base

    action = plan.get("action")
    reply = (plan.get("reply") or "").strip()

    if action not in ("generate", "edit"):
        base.update({"reply": reply or text or "你想让我怎么改？", "action": "answer"})
        return base

    task, terr = exec_plan(plan, client, img_model, img_files)
    if terr:
        base.update({"reply": terr, "action": "blocked", "quota": True,
                     "usage": quota_of(client)})
        return base

    base["tasks"] = [task]
    base["action"] = action
    if task.get("status") == "done" and task.get("images"):
        base["images"] = task["images"]
        base["reply"] = reply or ("已生成 %d 张，看看效果。" % len(task["images"]))
    else:
        base["reply"] = ""
        base["error"] = True
    base["usage"] = quota_of(client)
    return base


# ---------- HTTP Handler ----------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype=None):
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            self._json(404, {"error": "not found"})
            return
        ctype = ctype or mimetypes.guess_type(path)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(raw)

    def _client(self):
        """返回匹配的口令客户端 {label, code, limit}；不匹配返回 None。
        未配置任何口令时放行为 owner（本地单人模式）。"""
        if not CLIENTS:
            return {"label": "owner", "code": "", "limit": 0}
        header = self.headers.get("X-Access-Code")
        query = self.path.split("?", 1)
        qcode = ""
        if len(query) == 2:
            for kv in query[1].split("&"):
                if kv.startswith("t="):
                    qcode = kv[2:]
        for c in CLIENTS:
            if header == c["code"] or (qcode and qcode == c["code"]):
                return c
        return None

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html", "/chat"):
            return self._file(os.path.join(STATIC_DIR, "chat.html"), "text/html; charset=utf-8")
        if path == "/classic":
            return self._file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
        if path.startswith("/static/"):
            name = os.path.basename(path)
            return self._file(os.path.join(STATIC_DIR, name))
        if path == "/api/health":
            return self._json(200, {"ok": True})
        if path.startswith(("/api/", "/media/")):
            client = self._client()
            if not client:
                return self._json(401, {"error": "口令错误或缺失"})
            if path == "/api/models":
                return self._json(200, {"models": MODELS, "sizes": SIZES,
                                        "default": DEFAULT_MODEL,
                                        "max_refs": MAX_REFS,
                                        "chat_model": CHAT_MODEL,
                                        "locked": bool(CLIENTS)})
            if path == "/api/usage":
                return self._json(200, {"usage": quota_of(client)})
            if path == "/api/tasks":
                tasks = load_tasks()
                if client["label"] != "owner":
                    tasks = [t for t in tasks if t.get("client") == client["label"]]
                return self._json(200, {"tasks": tasks})
            if path.startswith("/media/"):
                name = os.path.basename(path)
                return self._file(os.path.join(IMG_DIR, name))
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        client = self._client()
        if not client:
            return self._json(401, {"error": "口令错误或缺失"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD_BYTES:
            return self._json(413, {"error": "上传内容过大（上限 %d MB）" % (MAX_UPLOAD_BYTES // 1048576)})
        body = self.rfile.read(length) if length else b""

        if path == "/api/chat":
            sys.stderr.write("[chat] enter content-type=%r len=%d\n"
                             % (self.headers.get("Content-Type", "")[:60], length))
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" in ctype:
                fields, files = parse_multipart(body, ctype)
            else:
                try:
                    fields = json.loads(body.decode("utf-8"))
                except Exception:
                    return self._json(400, {"error": "invalid json"})
                files = []
            img_files = [(fn, data) for (name, fn, data) in files
                         if name in ("images", "images[]", "image[]", "image")]
            if len(img_files) > MAX_REFS:
                return self._json(400, {"error": "参考图最多 %d 张" % MAX_REFS})
            if not (fields.get("message") or "").strip() and not img_files:
                return self._json(400, {"error": "消息不能为空"})
            result = run_chat(client, fields, img_files)
            return self._json(200 if not result.get("error") else 502, result)

        if path == "/api/generate":
            try:
                fields = json.loads(body.decode("utf-8"))
            except Exception:
                return self._json(400, {"error": "invalid json"})
            fields["model"] = fields.get("model") or DEFAULT_MODEL
            if fields["model"] not in [m["id"] for m in MODELS]:
                return self._json(400, {"error": "未知模型: %s" % fields["model"]})
            err = quota_block(client, int(fields.get("n") or 1))
            if err:
                return self._json(429, {"error": err, "usage": quota_of(client)})
            task_id = uuid.uuid4().hex[:12]
            task = run_generation(task_id, "t2i", fields, client_label=client["label"])
            code = 200 if task["status"] == "done" else 502
            return self._json(code, {"task": task})

        if path == "/api/edit":
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype:
                return self._json(400, {"error": "expected multipart"})
            fields, files = parse_multipart(body, ctype)
            img_files = [(fn, data) for (name, fn, data) in files
                         if name in ("images", "images[]", "image[]", "image")]
            mask_data = next((data for (name, fn, data) in files if name == "mask"), None)
            if not img_files:
                return self._json(400, {"error": "缺少图片"})
            if len(img_files) > MAX_REFS:
                return self._json(400, {"error": "参考图最多 %d 张" % MAX_REFS})
            fields["model"] = fields.get("model") or DEFAULT_MODEL
            if fields["model"] not in [m["id"] for m in MODELS]:
                return self._json(400, {"error": "未知模型: %s" % fields["model"]})
            err = quota_block(client, int(fields.get("n") or 1))
            if err:
                return self._json(429, {"error": err, "usage": quota_of(client)})
            payload = {"images": img_files}
            if mask_data:
                payload["mask"] = mask_data
            task_id = uuid.uuid4().hex[:12]
            task = run_generation(task_id, "inpaint" if mask_data else "i2i", fields, payload,
                                  client_label=client["label"])
            code = 200 if task["status"] == "done" else 502
            return self._json(code, {"task": task})

        if path == "/api/tasks/delete":
            try:
                tid = json.loads(body.decode("utf-8"))["id"]
            except Exception:
                return self._json(400, {"error": "invalid json"})
            all_tasks = load_tasks()
            target = next((t for t in all_tasks if t["id"] == tid), None)
            if client["label"] != "owner" and (
                    not target or target.get("client") != client["label"]):
                return self._json(403, {"error": "无权删除该记录"})
            tasks = [t for t in all_tasks if t["id"] != tid]
            save_tasks(tasks)
            return self._json(200, {"ok": True})

        self._json(404, {"error": "not found"})


def main():
    ensure_dirs()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("Pangen Image Studio: http://%s:%d  (storage: %s, clients: %s)"
          % (HOST, PORT, STORAGE_DIR,
             ", ".join("%s(limit=%d)" % (c["label"], c["limit"]) for c in CLIENTS)
             or "none"))
    srv.serve_forever()


if __name__ == "__main__":
    main()
