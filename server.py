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
def relay_request(path, body, headers, timeout=TIMEOUT):
    req = urllib.request.Request(
        BASE_URL.rstrip("/") + path, data=body,
        headers={"Authorization": "Bearer " + API_KEY,
                 "User-Agent": UA,
                 "Accept": "application/json", **headers},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        return None, "HTTP %s: %s" % (e.code, detail)
    except urllib.error.URLError as e:
        return None, "网络错误: %s" % e.reason


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
            with urllib.request.urlopen(
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
        if path in ("/", "/index.html"):
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
        body = self.rfile.read(length) if length else b""

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
            if len(img_files) > 10:
                return self._json(400, {"error": "参考图最多 10 张"})
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
