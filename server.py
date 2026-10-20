#!/usr/bin/env python3
"""Pangen Image Studio - 本地生图工作台后端
纯标准库实现：静态文件服务 + 中转 API 代理 + 图片落盘 + 任务记录持久化
"""
import base64
import email.message
import email.policy
import http.client
import json
import mimetypes
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.parse
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
# 单次读写的 socket 超时；生图慢的时候可以临时放大（PANGEN_TIMEOUT=600）
TIMEOUT = int(os.environ.get("PANGEN_TIMEOUT", "300"))

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
ACCESS_CODE = os.environ.get("ACCESS_CODE", "")  # 旧变量：单口令不限量，仍兼容

DEFAULT_MODEL = "gpt-image-2"

# 对话编排模型：优先 6-luna，另一个作为并行竞速的备胎
CHAT_MODELS = ["gpt-6-luna", "gpt-5.6-luna", "gpt-6", "gpt-5.6"]
CHAT_MODEL = os.environ.get("PANGEN_CHAT_MODEL", CHAT_MODELS[0])
# 编排模型正常 4~10s 就有结果，25s 足够；竞速时超了立刻换人，绝不死等
CHAT_TIMEOUT = 25
# 带参考图时要先读图，单独给一档超时。
# 只送 640px 小图时实测 4~5s 就能出结论，40s 已是 8 倍余量，再长只是让用户干等。
VISION_TIMEOUT = 40
# 视觉环节优先用前端另存的小图（~640px），体积只有原图 1/6，
# 上游带宽有限时这是「读图慢 → 超时」的最有效解法
VISION_PREFER_THUMB = True

# 参考图上限（前后端一致）
MAX_REFS = 6
MAX_UPLOAD_BYTES = 40 * 1024 * 1024   # 单次请求体上限
VISION_INLINE_LIMIT = 4 * 1024 * 1024  # 超过此体积的图不塞进多模态上下文
# 生图响应（几 MB base64）的最长下载时间。上游回传慢时可以临时放大，
# PANGEN_IMG_DEADLINE=2400 就是「宁可等半小时也别中断」
IMAGE_DEADLINE = int(os.environ.get("PANGEN_IMG_DEADLINE", "420"))
# 输出格式/质量。链路慢的时候把 base64 体积压下来比什么都管用：
# jpeg + low 一般能把 2MB 级 PNG 压到 100~300KB。留空 = 用上游默认。
IMG_FORMAT = os.environ.get("PANGEN_IMG_FORMAT", "").strip().lower()
IMG_QUALITY = os.environ.get("PANGEN_IMG_QUALITY", "").strip().lower()

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
# 出网路径默认「直连优先，连不上再退回系统代理」，PANGEN_NET_MODE=direct|proxy 可强制。
# 2026-10-19 实测本机：直连基本可用（1.6~3.5s），系统代理 127.0.0.1:8337 多次整条挂死，
# 所以代理只作兜底，不再当默认路径。
NET_MODE = os.environ.get("PANGEN_NET_MODE", "auto").strip().lower()
_OPENER_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_OPENER_PROXY = urllib.request.build_opener()

def _opener_routes():
    """给非复用的普通 GET（下载结果图）用。"""
    if NET_MODE == "direct":
        return [_OPENER_DIRECT]
    if NET_MODE == "proxy":
        return [_OPENER_PROXY]
    return [_OPENER_DIRECT, _OPENER_PROXY]

_BASE = urllib.parse.urlparse(BASE_URL)
_HOST = _BASE.hostname or "codex.pangen.dev"
_PORT = _BASE.port or 443
_BASE_PATH = _BASE.path.rstrip("/")
# TLS 握手实测一半概率卡死，且卡住后就一直挂到超时。握手超过 8 秒基本无望，
# 与其等到 25 秒，不如早断早重试——4 次 8 秒的组合成功率远高于 1 次 25 秒。
CONNECT_TIMEOUT = 8

def _route_order():
    if NET_MODE == "proxy":
        return ["proxy"]
    if NET_MODE == "direct":
        return ["direct"]
    return ["direct", "proxy"]

# 连接复用：每次请求都重新做一次 TLS 握手，在抖动链路上就是一次次掷骰子
# （实测握手 0.5s 到直接 60s 超时）。同一线程内复用连接，成功一次之后基本不用再握手。
_TLS = threading.local()

def _proxy_target():
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        return None
    raw = proxies.get("https") or proxies.get("http")
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    u = urllib.parse.urlparse(raw)
    if not u.hostname:
        return None
    return (u.hostname, u.port or 8080)

def _conn_for(kind, timeout):
    """取（必要时新建）当前线程在该路径上的连接。返回 (conn, fresh)。

    新建的连接只带握手超时，连上之后由调用方把 socket 超时换成读写超时——
    握手卡住 8 秒基本就是死透了，等 25 秒只是让用户白等。
    """
    store = getattr(_TLS, "conns", None)
    if store is None:
        store = _TLS.conns = {}
    conn = store.get(kind)
    if conn is not None and conn.sock is None:
        store.pop(kind, None)
        conn = None
    fresh = conn is None
    if fresh:
        if kind == "direct":
            conn = http.client.HTTPSConnection(_HOST, _PORT, timeout=timeout)
        else:
            tgt = _proxy_target()
            if not tgt:
                return None, True
            conn = http.client.HTTPSConnection(tgt[0], tgt[1], timeout=timeout)
            conn.set_tunnel(_HOST, _PORT)
        store[kind] = conn
    return conn, fresh

def _drop_conn(kind):
    store = getattr(_TLS, "conns", None) or {}
    conn = store.pop(kind, None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass

def relay_request(path, body, headers, timeout=TIMEOUT, retries=3, deadline=0):
    """调用中转。

    连接层失败（TLS 握手超时 / 连接被拒）时请求还没送达上游，重试不会造成重复扣费，
    因此这类失败无论耗时多久都重试，并且换一条出网路径再试；
    连接已复用、请求可能已送达之后的超时不重试也不换路径——重发会重复扣费。

    deadline > 0 时限制整个响应的最长下载时间——生图接口返回几 MB base64，
    网络差时会慢速滴流，单靠 socket timeout 拦不住。
    """
    url_path = _BASE_PATH + path
    hdr = {"Authorization": "Bearer " + API_KEY, "User-Agent": UA,
           "Accept": "application/json", "Content-Type": "application/json"}
    hdr.update(headers)

    routes = _route_order()
    last = "未知错误"
    for ri, kind in enumerate(routes):
        connect_level = True
        for attempt in range(retries + 1):
            t0 = time.time()
            hard_stop = t0 + deadline if deadline else 0

            # 第一段：建立连接。失败说明请求根本没送出去，重试不会重复扣费。
            try:
                conn, fresh = _conn_for(kind, CONNECT_TIMEOUT)
                if conn is None:
                    last = "网络错误: 没有可用的代理"
                    connect_level = False
                    break
                if conn.sock is None:
                    conn.connect()               # 只受 CONNECT_TIMEOUT 约束
            except Exception as e:
                _drop_conn(kind)
                last = "连接失败: %s" % e
                connect_level = True
                elapsed = time.time() - t0
                sys.stderr.write("[relay] %s 失败(%s, %.1fs, 可重试=True): %s\n"
                                 % (path, "直连" if kind == "direct" else "代理",
                                    elapsed, str(last)[:120]))
                if attempt < retries:
                    time.sleep(0.8)
                    continue
                break

            # 第二段：请求已发出去，之后的超时不再重试（可能已受理，重发会重复扣费）
            try:
                conn.sock.settimeout(timeout)    # 连上后换成读写超时
                conn.request("POST", url_path, body=body, headers=hdr)
                resp = conn.getresponse()
                resp_t = time.time() - t0
                if deadline:
                    # 生图要区分「上游算得慢」和「响应下载慢」，两者解法完全相反
                    sys.stderr.write("[relay] %s 响应头 %.1fs status=%s len=%s\n"
                                     % (path, resp_t, resp.status,
                                        resp.getheader("Content-Length") or "?"))
                if resp.status >= 400:
                    detail = resp.read().decode("utf-8", errors="replace")[:300]
                    _drop_conn(kind)
                    return None, "HTTP %s: %s" % (resp.status, detail)
                if not hard_stop:
                    raw = resp.read()
                else:
                    buf = bytearray()
                    next_note = time.time() + 20
                    while True:
                        # socket 超时不能超过剩余时限，否则流一停滞就要干等到 socket 超时
                        left = hard_stop - time.time()
                        if left <= 0:
                            raise RuntimeError("下载超时：响应超过 %d 秒仍未传完" % deadline)
                        try:
                            conn.sock.settimeout(max(1.0, min(timeout, left)))
                        except Exception:
                            pass
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        buf += chunk
                        if time.time() >= next_note:
                            next_note = time.time() + 20
                            sys.stderr.write("[relay] %s 已下载 %d KB (%.0fs)\n"
                                             % (path, len(buf) // 1024, time.time() - t0))
                    raw = bytes(buf)
                    sys.stderr.write("[relay] %s 下载完成 %d KB (%.1fs, 首包 %.1fs)\n"
                                     % (path, len(raw) // 1024, time.time() - t0, resp_t))
                if resp.will_close:
                    _drop_conn(kind)
                return json.loads(raw.decode("utf-8")), None
            except Exception as e:
                last = "请求失败: %s" % e
                _drop_conn(kind)

            elapsed = time.time() - t0
            connect_level = False
            sys.stderr.write("[relay] %s 失败(%s, %.1fs, 可重试=False): %s\n"
                             % (path, "直连" if kind == "direct" else "代理",
                                elapsed, str(last)[:120]))
            break
        # 只有连接层失败才换路径；读超时优先当成上游慢，不重复提交
        if connect_level and ri < len(routes) - 1:
            sys.stderr.write("[relay] 换备用出网路径重试 %s\n" % path)
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
    openers = _opener_routes()
    for i, item in enumerate(data_list):
        if item.get("b64_json"):
            raw = base64.b64decode(item["b64_json"])
        elif item.get("url"):
            raw = None
            for op in openers:
                try:
                    with op.open(
                            urllib.request.Request(item["url"], headers={"User-Agent": UA}),
                            timeout=120) as r:
                        raw = r.read()
                    if raw:
                        break
                except Exception as e:
                    sys.stderr.write("[img] 下载结果图失败(%s): %s\n"
                                     % ("直连" if op is _OPENER_DIRECT else "代理", e))
            if not raw:
                continue
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
            quality = fields.get("quality") or IMG_QUALITY
            if quality:
                payload["quality"] = quality
            fmt = fields.get("output_format") or IMG_FORMAT
            if fmt:
                payload["output_format"] = fmt
            body = json.dumps(payload).encode("utf-8")
            data, err = relay_request("/images/generations", body,
                                      {"Content-Type": "application/json"},
                                      deadline=IMAGE_DEADLINE)
        else:
            mp_fields = {"model": fields["model"], "prompt": fields["prompt"],
                         "size": fields.get("size", "auto"),
                         "n": str(int(fields.get("n") or 1))}
            quality = fields.get("quality") or IMG_QUALITY
            if quality:
                mp_fields["quality"] = quality
            fmt = fields.get("output_format") or IMG_FORMAT
            if fmt:
                mp_fields["output_format"] = fmt
            relay_files = [("image[]", fn, data) for fn, data in files["images"]]
            if files.get("mask"):
                relay_files.append(("mask", "mask.png", files["mask"]))
            body, ctype = build_relay_multipart(mp_fields, relay_files)
            data, err = relay_request("/images/edits", body, {"Content-Type": ctype},
                                      deadline=IMAGE_DEADLINE)
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

def _chat_once(model, messages, timeout, vision_ok=True):
    """单次编排调用，返回统一结构（不管成功失败）"""
    t0 = time.time()
    data, err = relay_chat(messages, None, timeout=timeout, model=model)
    if err or not data or not data.get("choices"):
        return {"model": model, "err": err or "对话接口返回为空", "plan": None,
                "text": "", "vision": vision_ok, "cost": round(time.time() - t0, 1)}
    text = ((data["choices"][0].get("message") or {}).get("content") or "").strip()
    return {"model": model, "err": None, "plan": _extract_json(text), "text": text,
            "vision": vision_ok, "cost": round(time.time() - t0, 1)}

def chat_round(messages):
    """并行竞速编排。

    串行降级的毛病是必须等前面模型超时（最长上百秒）才轮到下一个；
    这里同时问两个模型，谁先给出可用的结构化指令就用谁，最坏等待压到单次超时以内。
    带参考图时，第二个模型走「无图版」请求，顺带覆盖读图不被支持的降级路径。
    返回 {model, plan, text, vision, err, cost}
    """
    multi = any(isinstance(m.get("content"), list) for m in messages)
    plain = _strip_vision(messages) if multi else None

    jobs = []
    for i, model in enumerate(CHAT_MODELS[:2]):
        use_plain = multi and i > 0
        jobs.append((model, plain if use_plain else messages, not use_plain))

    timeout = VISION_TIMEOUT if multi else CHAT_TIMEOUT
    q = queue.Queue()

    def worker(model, msgs, vision_ok):
        try:
            q.put(_chat_once(model, msgs, timeout, vision_ok))
        except Exception as e:
            q.put({"model": model, "err": "请求异常: %s" % e, "plan": None,
                   "text": "", "vision": vision_ok, "cost": 0})

    for model, msgs, vok in jobs:
        threading.Thread(target=worker, args=(model, msgs, vok), daemon=True).start()

    deadline = time.time() + timeout + 3
    got, first = 0, None
    while got < len(jobs):
        remain = deadline - time.time()
        if remain <= 0:
            break
        try:
            r = q.get(timeout=remain)
        except queue.Empty:
            break
        got += 1
        if r.get("plan"):
            return r
        if first is None:
            first = r
    return first or {"model": CHAT_MODEL, "err": "编排模型无响应", "plan": None,
                     "text": "", "vision": True, "cost": 0}

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

def run_chat(client, fields, img_files, vision_files=None):
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
    if VISION_PREFER_THUMB and vision_files and len(vision_files) == len(img_files):
        vis_files = vision_files[:MAX_REFS]
    else:
        vis_files = img_files
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
    for fn, data in vis_files:
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

    res = chat_round(messages)
    text = (res.get("text") or "").strip()
    plan = res.get("plan")
    sys.stderr.write("[chat] winner=%s vision=%s plan=%s cost=%ss\n"
                     % (res["model"], res.get("vision"),
                        "y" if plan else "n", res.get("cost")))
    base = {"images": [], "tasks": [], "usage": quota_of(client),
            "chat_model": res["model"], "vision": res.get("vision", True),
            "refs": len(img_files)}

    # 编排模型没给出可用指令时，只要用户既给了图又给了文字，就直接执行。
    # 一次读图超时不该把整轮请求判死 —— 这是「带图就失败」的根因。
    if not plan and img_files and message:
        plan = {"action": "edit", "prompt": message[:1200],
                "size": pref_size, "n": pref_n, "reply": "", "_fallback": True}
        sys.stderr.write("[chat] 编排失败，降级直连改图（提示词未优化）\n")

    if not plan and not text:
        base.update({"reply": "对话服务暂时不可用（%s）。把画面要求直接用文字写清楚再发一次，"
                              "我不走编排、直接出图。" % (res.get("err") or "无响应")[:120],
                     "error": True, "chat_model": CHAT_MODEL})
        return base

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
        if reply:
            base["reply"] = reply
        elif plan.get("_fallback"):
            base["reply"] = "编排模型这次没响应，我直接拿你的原话当提示词出图了。"
        else:
            base["reply"] = "已生成 %d 张，看看效果。" % len(task["images"])
    else:
        base["reply"] = ""
        base["error"] = task.get("error") or "生成失败，请稍后重试"
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
            vision_files = [(fn, data) for (name, fn, data) in files
                            if name in ("thumbs", "thumbs[]", "thumb[]", "thumb")]
            if len(img_files) > MAX_REFS:
                return self._json(400, {"error": "参考图最多 %d 张" % MAX_REFS})
            if not (fields.get("message") or "").strip() and not img_files:
                return self._json(400, {"error": "消息不能为空"})
            result = run_chat(client, fields, img_files, vision_files)
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
