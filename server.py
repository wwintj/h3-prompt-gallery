#!/usr/bin/env python3
"""MiniMax H3 提示词画廊 —— 直接读取 Google Drive 文件夹，零依赖。

运行：python3 server.py   然后打开 http://127.0.0.1:8765

首次使用需要：
  1. 把 Google Cloud 下载的 OAuth 客户端文件改名为 credentials.json 放在本目录
  2. 在网页里点「登录 Google」授权
  3. 粘贴 Drive 文件夹链接

提示词、标签等都保存在该 Drive 文件夹里的 h3-gallery-meta.json（同时本地留一份备份）。
图片/视频本身不会下载到本地，网页上看到的内容都是实时从 Drive 读取的。
"""
import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CREDS = ROOT / "credentials.json"
TOKEN = ROOT / "token.json"
CONFIG = ROOT / "config.json"
META_BACKUP = ROOT / "meta-backup.json"
META_NAME = "h3-gallery-meta.json"

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
REDIRECT_URI = f"http://127.0.0.1:{PORT}/"
# 需要读取你的现有文件，并往你的文件夹里写 meta 文件和上传的文件，所以用完整 drive 权限。
# 本程序不会删除 Drive 里的任何文件（网页上的「隐藏」只是标记在 meta 里）。
SCOPES = "https://www.googleapis.com/auth/drive"
API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
META_FIELDS = ("prompt", "negative_prompt", "model", "params", "tags", "rating", "notes", "favorite", "hidden")
COMMON = {"supportsAllDrives": "true"}

lock = threading.RLock()
pending_auth = {}  # state -> code_verifier
cache = {"items": None, "time": 0}
meta_store = {"loaded_for": None, "file_id": None, "data": {}}
thumbs = OrderedDict()  # (id, size) -> (ctype, bytes)


# ---------------- 配置 / 凭据 ----------------
def load_json(p: Path, default=None):
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return default


def save_json(p: Path, data):
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def client_info():
    c = load_json(CREDS)
    if not c:
        return None
    return c.get("installed") or c.get("web")


def config():
    return load_json(CONFIG, {}) or {}


class NeedAuth(Exception):
    pass


def token_request(params: dict) -> dict:
    ci = client_info()
    params = {**params, "client_id": ci["client_id"], "client_secret": ci.get("client_secret", "")}
    req = urllib.request.Request(
        ci.get("token_uri", "https://oauth2.googleapis.com/token"),
        data=urllib.parse.urlencode(params).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def access_token(force_refresh=False) -> str:
    with lock:
        tok = load_json(TOKEN)
        if not tok or not client_info():
            raise NeedAuth()
        if force_refresh or tok.get("expiry", 0) < time.time() + 60:
            if not tok.get("refresh_token"):
                raise NeedAuth()
            try:
                new = token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
            except urllib.error.HTTPError:
                TOKEN.unlink(missing_ok=True)  # 刷新令牌失效（例如测试模式 7 天过期），需要重新登录
                raise NeedAuth()
            tok["access_token"] = new["access_token"]
            tok["expiry"] = time.time() + new.get("expires_in", 3600)
            save_json(TOKEN, tok)
            os.chmod(TOKEN, 0o600)
        return tok["access_token"]


def gopen(method, url, params=None, body=None, headers=None, retry=True):
    """带授权的请求，返回 urllib 响应对象（调用方负责关闭）。"""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    h = dict(headers or {})
    h["Authorization"] = "Bearer " + access_token()
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        return urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 401 and retry:
            access_token(force_refresh=True)
            return gopen(method, url, None, body, headers, retry=False)
        raise


def gjson(method, path, params=None, body=None, base=API):
    data = json.dumps(body).encode() if body is not None else None
    hdr = {"Content-Type": "application/json"} if data else {}
    with gopen(method, base + path, {**COMMON, **(params or {})}, data, hdr) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def api_error(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        try:
            return json.loads(e.read()).get("error", {}).get("message", str(e))
        except Exception:
            return str(e)
    return str(e)


# ---------------- Drive 文件 ----------------
FILE_FIELDS = ("id,name,mimeType,size,createdTime,modifiedTime,thumbnailLink,"
               "imageMediaMetadata(width,height),videoMediaMetadata(width,height,durationMillis)")


def list_children(folder_id):
    out, token = [], None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": f"nextPageToken,files({FILE_FIELDS})",
            "pageSize": "1000",
            "includeItemsFromAllDrives": "true",
            "corpora": "allDrives",
        }
        if token:
            params["pageToken"] = token
        res = gjson("GET", "/files", params)
        out += res.get("files", [])
        token = res.get("nextPageToken")
        if not token:
            return out


def scan_folder(root_id):
    """递归列出文件夹里所有图片和视频。"""
    items, queue = [], [(root_id, "")]
    while queue:
        fid, prefix = queue.pop(0)
        for f in list_children(fid):
            mt = f.get("mimeType", "")
            if mt == FOLDER_MIME:
                queue.append((f["id"], prefix + f["name"] + "/"))
            elif mt.startswith("image/") or mt.startswith("video/"):
                f["folder"] = prefix.rstrip("/")
                items.append(f)
    return items


def load_meta(folder_id, force=False):
    with lock:
        if meta_store["loaded_for"] == folder_id and not force:
            return meta_store["data"]
        res = gjson("GET", "/files", {
            "q": f"name = '{META_NAME}' and '{folder_id}' in parents and trashed = false",
            "fields": "files(id)", "includeItemsFromAllDrives": "true", "corpora": "allDrives",
        })
        files = res.get("files", [])
        data = {}
        fid = files[0]["id"] if files else None
        if fid:
            with gopen("GET", f"{API}/files/{fid}", {"alt": "media", **COMMON}) as r:
                data = json.loads(r.read() or b"{}")
        meta_store.update(loaded_for=folder_id, file_id=fid, data=data)
        return data


def save_meta(folder_id):
    with lock:
        data = meta_store["data"]
        raw = json.dumps(data, ensure_ascii=False, indent=2).encode()
        save_json(META_BACKUP, {"folder": folder_id, "meta": data})
        if not meta_store["file_id"]:
            created = gjson("POST", "/files", {"fields": "id"},
                            {"name": META_NAME, "parents": [folder_id], "mimeType": "application/json"})
            meta_store["file_id"] = created["id"]
        with gopen("PATCH", f"{UPLOAD_API}/files/{meta_store['file_id']}",
                   {"uploadType": "media", **COMMON}, raw, {"Content-Type": "application/json"}) as r:
            r.read()


def get_items(force=False):
    folder = config().get("folder_id")
    if not folder:
        return []
    with lock:
        fresh = cache["items"] is not None and time.time() - cache["time"] < 60
        if fresh and not force:
            files = cache["items"]
        else:
            files = scan_folder(folder)
            cache.update(items=files, time=time.time())
    meta = load_meta(folder, force=force)
    out = []
    for f in files:
        vm = f.get("videoMediaMetadata") or {}
        im = f.get("imageMediaMetadata") or {}
        out.append({
            "id": f["id"],
            "name": f["name"],
            "folder": f.get("folder", ""),
            "type": "video" if f["mimeType"].startswith("video/") else "image",
            "size": int(f.get("size", 0) or 0),
            "mtime": f.get("createdTime") or f.get("modifiedTime"),
            "width": vm.get("width") or im.get("width"),
            "height": vm.get("height") or im.get("height"),
            "duration": (int(vm["durationMillis"]) / 1000) if vm.get("durationMillis") else None,
            "has_thumb": bool(f.get("thumbnailLink")),
            "meta": meta.get(f["id"], {}),
        })
    return out


def parse_folder_id(s: str):
    s = s.strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", s) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", s)
    if m:
        return m.group(1)
    return s if re.fullmatch(r"[A-Za-z0-9_-]{10,}", s) else None


# ---------------- HTTP 服务 ----------------
PAGE_CACHE_HEADERS = {"Cache-Control": "no-cache"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data: bytes, ctype: str, extra=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, loc):
        self.send_response(302)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def host_ok(self):
        # 防止 DNS 重绑定：只接受本机地址访问
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    # ---------- GET ----------
    def do_GET(self):
        if not self.host_ok():
            return self.send_error(403)
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        try:
            if url.path == "/":
                if "code" in qs or "error" in qs:
                    return self.oauth_callback(qs)
                return self.send_bytes((ROOT / "index.html").read_bytes(), "text/html; charset=utf-8",
                                       PAGE_CACHE_HEADERS)
            if url.path in ("/favicon.svg", "/favicon.ico"):
                return self.send_bytes((ROOT / "favicon.svg").read_bytes(), "image/svg+xml",
                                       {"Cache-Control": "no-cache"})
            if url.path == "/auth/login":
                return self.oauth_start()
            if url.path == "/api/status":
                return self.send_json(self.status())
            if url.path == "/api/items":
                return self.send_json(get_items(force="refresh" in qs))
            m = re.fullmatch(r"/thumb/([A-Za-z0-9_-]+)", url.path)
            if m:
                return self.thumb(m.group(1), int(qs.get("s", ["400"])[0]))
            m = re.fullmatch(r"/media/([A-Za-z0-9_-]+)", url.path)
            if m:
                return self.stream(m.group(1))
            self.send_error(404)
        except NeedAuth:
            self.send_json({"error": "need_auth"}, 401)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.send_json({"error": api_error(e)}, 502)

    def status(self):
        st = {"has_credentials": bool(client_info()), "authorized": False,
              "folder_id": config().get("folder_id"), "folder_name": config().get("folder_name"),
              "title": config().get("title", ""),
              "redirect_uri": REDIRECT_URI, "app_dir": str(ROOT)}
        if st["has_credentials"] and TOKEN.exists():
            try:
                access_token()
                st["authorized"] = True
            except NeedAuth:
                pass
        return st

    def oauth_start(self):
        ci = client_info()
        if not ci:
            return self.redirect("/")
        state = secrets.token_urlsafe(16)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        pending_auth[state] = verifier
        params = {
            "client_id": ci["client_id"], "redirect_uri": REDIRECT_URI, "response_type": "code",
            "scope": SCOPES, "access_type": "offline", "prompt": "consent", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        }
        self.redirect(ci.get("auth_uri", "https://accounts.google.com/o/oauth2/auth") + "?" +
                      urllib.parse.urlencode(params))

    def oauth_callback(self, qs):
        state = qs.get("state", [""])[0]
        verifier = pending_auth.pop(state, None)
        if "error" in qs or not verifier:
            return self.redirect("/?auth_error=" + urllib.parse.quote(qs.get("error", ["状态校验失败，请重试"])[0]))
        try:
            tok = token_request({"grant_type": "authorization_code", "code": qs["code"][0],
                                 "redirect_uri": REDIRECT_URI, "code_verifier": verifier})
        except urllib.error.HTTPError as e:
            return self.redirect("/?auth_error=" + urllib.parse.quote(api_error(e)))
        save_json(TOKEN, {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
                          "expiry": time.time() + tok.get("expires_in", 3600)})
        os.chmod(TOKEN, 0o600)
        self.redirect("/")

    def thumb(self, fid, size):
        size = max(100, min(size, 1600))
        key = (fid, size)
        with lock:
            hit = thumbs.get(key)
            if hit:
                thumbs.move_to_end(key)
        if not hit:
            f = gjson("GET", f"/files/{fid}", {"fields": "thumbnailLink"})
            link = f.get("thumbnailLink")
            if not link:
                return self.send_error(404)
            link = re.sub(r"=s\d+$", "", link) + f"=s{size}"
            with gopen("GET", link) as r:
                hit = (r.headers.get("Content-Type", "image/jpeg"), r.read())
            with lock:
                thumbs[key] = hit
                while len(thumbs) > 800:  # 只在内存里缓存，重启即清空，不占硬盘
                    thumbs.popitem(last=False)
        self.send_bytes(hit[1], hit[0], {"Cache-Control": "private, max-age=86400"})

    def stream(self, fid):
        """把 Drive 文件原样转发给浏览器，支持 Range（视频可以拖动进度条）。"""
        hdr = {}
        if self.headers.get("Range"):
            hdr["Range"] = self.headers["Range"]
        try:
            r = gopen("GET", f"{API}/files/{fid}", {"alt": "media", **COMMON}, headers=hdr)
        except urllib.error.HTTPError as e:
            if e.code == 416:
                self.send_response(416)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            raise
        with r:
            self.send_response(r.status)
            for k in ("Content-Type", "Content-Length", "Content-Range"):
                if r.headers.get(k):
                    self.send_header(k, r.headers[k])
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # ---------- POST ----------
    def read_body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def do_POST(self):
        # 自定义请求头 + Host 校验：阻止其他网站偷偷调用本地接口
        if not self.host_ok() or self.headers.get("X-H3") != "1":
            return self.send_error(403)
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        try:
            if url.path == "/api/folder":
                fid = parse_folder_id(json.loads(self.read_body()).get("folder", ""))
                if not fid:
                    return self.send_json({"error": "没认出文件夹链接，请复制 Drive 里文件夹页面的地址"}, 400)
                f = gjson("GET", f"/files/{fid}", {"fields": "id,name,mimeType"})
                if f.get("mimeType") != FOLDER_MIME:
                    return self.send_json({"error": "这个链接不是文件夹"}, 400)
                save_json(CONFIG, {**config(), "folder_id": fid, "folder_name": f["name"]})
                cache.update(items=None)
                meta_store.update(loaded_for=None)
                return self.send_json({"ok": True, "folder_name": f["name"]})

            if url.path == "/api/title":
                title = str(json.loads(self.read_body()).get("title", "")).strip()[:40]
                save_json(CONFIG, {**config(), "title": title})
                return self.send_json({"ok": True, "title": title})

            if url.path == "/api/logout":
                TOKEN.unlink(missing_ok=True)
                return self.send_json({"ok": True})

            if url.path == "/api/meta":
                data = json.loads(self.read_body())
                folder = config()["folder_id"]
                with lock:
                    store = load_meta(folder)
                    m = store.setdefault(data["id"], {})
                    for k in META_FIELDS:
                        if k in data.get("meta", {}):
                            m[k] = data["meta"][k]
                    if data.get("name"):
                        m["_name"] = data["name"]  # 方便人工查看 meta 文件
                    save_meta(folder)
                    return self.send_json(m)

            if url.path == "/api/upload":
                return self.upload(qs)
        except NeedAuth:
            return self.send_json({"error": "need_auth"}, 401)
        except Exception as e:
            return self.send_json({"error": api_error(e)}, 502)
        self.send_error(404)

    def upload(self, qs):
        """浏览器 -> 本服务 -> Drive，边收边传，不在本地落盘。"""
        folder = config()["folder_id"]
        name = qs.get("name", ["file"])[0]
        ctype = self.headers.get("Content-Type") or "application/octet-stream"
        length = int(self.headers.get("Content-Length", 0))
        init_body = json.dumps({"name": name, "parents": [folder]}).encode()
        with gopen("POST", f"{UPLOAD_API}/files", {"uploadType": "resumable", **COMMON}, init_body,
                   {"Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Type": ctype, "X-Upload-Content-Length": str(length)}) as r:
            session = r.headers["Location"]
        u = urllib.parse.urlparse(session)
        conn = http.client.HTTPSConnection(u.netloc, timeout=300)
        conn.putrequest("PUT", u.path + "?" + u.query)
        conn.putheader("Content-Length", str(length))
        conn.putheader("Content-Type", ctype)
        conn.endheaders()
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(1 << 20, remaining))
            if not chunk:
                break
            conn.send(chunk)
            remaining -= len(chunk)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        if resp.status not in (200, 201):
            return self.send_json({"error": body.decode(errors="replace")}, 502)
        fid = json.loads(body)["id"]
        meta = json.loads(qs.get("meta", ["{}"])[0])
        with lock:
            store = load_meta(folder)
            store[fid] = {k: meta[k] for k in META_FIELDS if meta.get(k)} | {"_name": name}
            save_meta(folder)
            cache.update(items=None)
        return self.send_json({"id": fid})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"画廊已启动: http://127.0.0.1:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
