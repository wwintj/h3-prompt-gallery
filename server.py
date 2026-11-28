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
import argparse
import base64
import concurrent.futures
import hashlib
import hmac
import http.client
import http.cookies
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import OrderedDict
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CREDS = ROOT / "credentials.json"
TOKEN = ROOT / "token.json"
CONFIG = ROOT / "config.json"
META_BACKUP = ROOT / "meta-backup.json"
META_NAME = "h3-gallery-meta.json"
X_FOLDER_NAME = "x-inspirations"
X_META_NAME = "x-inspirations-meta.json"
X_META_BACKUP = ROOT / "x-meta-backup.json"

PORT = 8765
BIND_HOST = "127.0.0.1"
REDIRECT_URI = f"http://127.0.0.1:{PORT}/"
READONLY_MODE = False
READONLY_PASSWORD = ""
READONLY_TITLE = ""
READONLY_TTL = 60
X_FOLDER_ID_OVERRIDE = ""
SERVICE_ACCOUNT_PATH = None
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
ALLOW_ALL_HOSTS = False
THUMB_MEM_LIMIT = 800
THUMB_CACHE_DIR = None
GUEST_COOKIE_NAME = "h3_guest"
GUEST_COOKIE_TTL = 30 * 86400
_GUEST_SECRET = b""
_LOGIN_FAILS = {}  # ip -> [ts, ...]
_SA_TOKEN_CACHE = {"access_token": "", "expiry": 0.0}

# 需要读取你的现有文件，并往你的文件夹里写 meta 文件和上传的文件，所以用完整 drive 权限。
# 本程序不会删除 Drive 里的任何文件（网页上的「隐藏」只是标记在 meta 里）。
SCOPES = "https://www.googleapis.com/auth/drive"
SA_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
META_FIELDS = ("prompt", "negative_prompt", "model", "params", "tags", "rating", "notes", "favorite", "hidden")
COMMON = {"supportsAllDrives": "true"}

lock = threading.RLock()
pending_auth = {}  # state -> code_verifier
cache = {"items": None, "time": 0}
meta_store = {"loaded_for": None, "file_id": None, "data": {}}
x_store = {"loaded_for": None, "folder_id": None, "file_id": None, "items": [], "loaded_at": 0.0, "allowed_ids": set()}
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


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _load_guest_secret() -> bytes:
    sf = ROOT / ".guest_secret"
    try:
        if sf.exists():
            raw = sf.read_text("utf-8").strip()
            if len(raw) >= 32:
                return raw.encode("utf-8")
    except Exception:
        pass
    sec = secrets.token_hex(32)
    try:
        sf.write_text(sec, encoding="utf-8")
        os.chmod(sf, 0o600)
    except Exception:
        pass
    return sec.encode("utf-8")


def _guest_signing_key() -> bytes:
    global _GUEST_SECRET
    if not _GUEST_SECRET:
        _GUEST_SECRET = _load_guest_secret()
    return hmac.new(_GUEST_SECRET, f"h3ro:{READONLY_PASSWORD}".encode("utf-8"), hashlib.sha256).digest()


def _make_guest_token() -> str:
    exp = int(time.time()) + GUEST_COOKIE_TTL
    payload = str(exp)
    sig = hmac.new(_guest_signing_key(), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify_guest_token(tok: str) -> bool:
    if not READONLY_PASSWORD:
        return True
    if not tok or "." not in tok:
        return False
    parts = tok.split(".", 1)
    try:
        exp = int(parts[0])
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    expected = hmac.new(_guest_signing_key(), parts[0].encode("ascii"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(parts[1], expected)


def _service_account_token(force_refresh=False) -> str:
    with lock:
        if not force_refresh and _SA_TOKEN_CACHE["access_token"] and _SA_TOKEN_CACHE["expiry"] > time.time() + 60:
            return _SA_TOKEN_CACHE["access_token"]
        sa = load_json(SERVICE_ACCOUNT_PATH) if SERVICE_ACCOUNT_PATH else None
        if not sa or not sa.get("client_email") or not sa.get("private_key"):
            raise NeedAuth()
        now = int(time.time())
        token_uri = sa.get("token_uri") or "https://oauth2.googleapis.com/token"
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8"))
        claims = _b64url(json.dumps({
            "iss": sa["client_email"],
            "scope": SA_READONLY_SCOPE,
            "aud": token_uri,
            "iat": now,
            "exp": now + 3600,
        }, separators=(",", ":")).encode("utf-8"))
        signing_input = f"{header}.{claims}".encode("ascii")
        tmp_key = None
        try:
            fd, tmp_key = tempfile.mkstemp(prefix="h3_sa_", suffix=".pem")
            os.chmod(tmp_key, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as kf:
                kf.write(sa["private_key"])
            proc = subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign", tmp_key],
                input=signing_input,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                timeout=15,
            )
            sig = _b64url(proc.stdout)
        finally:
            if tmp_key:
                try:
                    os.unlink(tmp_key)
                except OSError:
                    pass
        assertion = f"{header}.{claims}.{sig}"
        body = urllib.parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        }).encode("ascii")
        req = urllib.request.Request(
            token_uri,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read())
        tok = res.get("access_token")
        if not tok:
            raise NeedAuth()
        _SA_TOKEN_CACHE["access_token"] = tok
        _SA_TOKEN_CACHE["expiry"] = time.time() + int(res.get("expires_in", 3600))
        return tok


def access_token(force_refresh=False) -> str:
    if SERVICE_ACCOUNT_PATH and SERVICE_ACCOUNT_PATH.exists():
        return _service_account_token(force_refresh=force_refresh)
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
    """递归列出文件夹里所有图片和视频（跳过 X 灵感库专用子目录，保持主画廊纯净）。"""
    items, queue = [], [(root_id, "")]
    while queue:
        fid, prefix = queue.pop(0)
        for f in list_children(fid):
            mt = f.get("mimeType", "")
            if mt == FOLDER_MIME:
                if prefix == "" and f.get("name") == X_FOLDER_NAME:
                    continue
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


# ---------------- X 灵感库（Drive 独立子目录 x-inspirations） ----------------
def ensure_x_folder(root_id: str, create: bool = True):
    with lock:
        if x_store["loaded_for"] == root_id and x_store["folder_id"]:
            return x_store["folder_id"]
        res = gjson("GET", "/files", {
            "q": f"name = '{X_FOLDER_NAME}' and '{root_id}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false",
            "fields": "files(id)", "includeItemsFromAllDrives": "true", "corpora": "allDrives",
        })
        files = res.get("files", [])
        if files:
            fid = files[0]["id"]
        elif create:
            created = gjson("POST", "/files", {"fields": "id"},
                            {"name": X_FOLDER_NAME, "parents": [root_id], "mimeType": FOLDER_MIME})
            fid = created["id"]
        else:
            return None
        x_store["folder_id"] = fid
        return fid


def load_x_items(root_id: str, force: bool = False):
    with lock:
        cache_key = X_FOLDER_ID_OVERRIDE or root_id
        now = time.time()
        if x_store["loaded_for"] == cache_key and x_store["loaded_at"] > 0:
            if READONLY_MODE:
                age = now - x_store["loaded_at"]
                # 只读公开模式下自动 TTL 刷新，同时限制手动强制刷新最小间隔 15 秒防止刷爆 Drive API
                if not force and age < READONLY_TTL:
                    return list(x_store["items"])
                if force and age < 15:
                    return list(x_store["items"])
            elif not force:
                return list(x_store["items"])
        xfid = X_FOLDER_ID_OVERRIDE or ensure_x_folder(root_id, create=False)
        items = []
        meta_fid = None
        if xfid:
            res = gjson("GET", "/files", {
                "q": f"name = '{X_META_NAME}' and '{xfid}' in parents and trashed = false",
                "fields": "files(id)", "includeItemsFromAllDrives": "true", "corpora": "allDrives",
            })
            files = res.get("files", [])
            if files:
                meta_fid = files[0]["id"]
                with gopen("GET", f"{API}/files/{meta_fid}", {"alt": "media", **COMMON}) as r:
                    raw = json.loads(r.read() or b"[]")
                    items = raw if isinstance(raw, list) else raw.get("items", [])
        allowed_ids = {
            im["id"]
            for it in items
            if isinstance(it, dict)
            for im in (it.get("images") or [])
            if isinstance(im, dict) and im.get("id")
        }
        x_store.update(
            loaded_for=cache_key,
            folder_id=xfid,
            file_id=meta_fid,
            items=items,
            loaded_at=time.time(),
            allowed_ids=allowed_ids,
        )
        return list(items)


def is_allowed_x_media_id(fid: str) -> bool:
    if not READONLY_MODE:
        return True
    folder = X_FOLDER_ID_OVERRIDE or config().get("folder_id")
    if not folder:
        return False
    with lock:
        if x_store["loaded_at"] <= 0 or (time.time() - x_store["loaded_at"] >= READONLY_TTL and fid not in x_store["allowed_ids"]):
            load_x_items(folder, force=False)
        return fid in x_store["allowed_ids"]


def save_x_items(root_id: str):
    if READONLY_MODE:
        raise PermissionError("readonly mode")
    with lock:
        xfid = ensure_x_folder(root_id, create=True)
        items = x_store["items"]
        raw = json.dumps(items, ensure_ascii=False, indent=2).encode("utf-8")
        save_json(X_META_BACKUP, {"folder": root_id, "x_folder": xfid, "items": items})
        if not x_store["file_id"]:
            created = gjson("POST", "/files", {"fields": "id"},
                            {"name": X_META_NAME, "parents": [xfid], "mimeType": "application/json"})
            x_store["file_id"] = created["id"]
        with gopen("PATCH", f"{UPLOAD_API}/files/{x_store['file_id']}",
                   {"uploadType": "media", **COMMON}, raw, {"Content-Type": "application/json"}) as r:
            r.read()


_FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")


def _is_public_url(url: str) -> bool:
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    host = u.hostname.lower()
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
        return False
    try:
        ip_lit = ipaddress.ip_address(host)
        return ip_lit.is_global
    except ValueError:
        pass
    try:
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip in _FAKE_IP_NET:
                continue  # 允许本机 TUN / Fake-IP 代理域名解析
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
    except Exception:
        return False
    return True


def _detect_media_mime(data: bytes, hdr_ctype: str, url_path: str = "") -> tuple:
    """返回 (kind, mime)，kind 为 'image' / 'video' / ''。"""
    ct = (hdr_ctype or "").split(";")[0].strip().lower()
    if data.startswith(b"\xff\xd8\xff"):
        return ("image", "image/jpeg")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ("image", "image/png")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ("image", "image/webp")
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ("image", "image/gif")
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:16]
        if b"avif" in brand or b"avis" in brand:
            return ("image", "image/avif")
        if b"qt  " in brand:
            return ("video", "video/quicktime")
        return ("video", "video/mp4")
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return ("video", "video/webm")
    if ct.startswith("image/"):
        return ("image", ct)
    if ct.startswith("video/"):
        return ("video", ct)
    low = (url_path or "").lower()
    if low.endswith((".mp4", ".m4v")):
        return ("video", "video/mp4")
    if low.endswith(".mov"):
        return ("video", "video/quicktime")
    if low.endswith(".webm"):
        return ("video", "video/webm")
    return ("", "")


def _ext_for_mime(ctype: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/avif": ".avif",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/webm": ".webm",
    }.get(ctype, ".jpg")


def upload_bytes_to_x_folder(root_id: str, name: str, ctype: str, data: bytes) -> str:
    """将字节流直接上传到 Drive 的 x-inspirations 子目录，返回文件 ID。"""
    xfolder = ensure_x_folder(root_id, create=True)
    length = len(data)
    init_body = json.dumps({"name": name, "parents": [xfolder]}).encode()
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
    for off in range(0, length, 1 << 20):
        conn.send(data[off:off + (1 << 20)])
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    if resp.status not in (200, 201):
        raise ValueError(f"上传到 Drive 失败: {body.decode(errors='replace')[:200]}")
    return json.loads(body)["id"]


def _image_dimensions(data: bytes):
    """纯标准库读取 PNG/GIF/WebP/JPEG 像素宽高，用于返回真实分辨率。"""
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
            return struct.unpack("<HH", data[6:10])
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
            chunk = data[12:16]
            if chunk == b"VP8X":
                w = 1 + int.from_bytes(data[24:27], "little")
                h = 1 + int.from_bytes(data[27:30], "little")
                return (w, h)
            if chunk == b"VP8 " and len(data) >= 30:
                w, h = struct.unpack("<HH", data[26:30])
                return (w & 0x3FFF, h & 0x3FFF)
            if chunk == b"VP8L" and len(data) >= 25:
                b0, b1, b2, b3 = data[21:25]
                w = 1 + (((b1 & 0x3F) << 8) | b0)
                h = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
                return (w, h)
        if data.startswith(b"\xff\xd8"):
            i = 2
            n = len(data)
            while i + 4 < n:
                while i < n and data[i] != 0xFF:
                    i += 1
                while i < n and data[i] == 0xFF:
                    i += 1
                if i >= n:
                    break
                marker = data[i]
                i += 1
                if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
                    continue
                if i + 2 > n:
                    break
                seglen = struct.unpack(">H", data[i:i + 2])[0]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    if i + 7 <= n:
                        h, w = struct.unpack(">HH", data[i + 3:i + 7])
                        return (w, h)
                    break
                i += max(2, seglen)
    except Exception:
        pass
    return (0, 0)


def _res_from_url(url: str):
    """从 video.twimg.com 路径里提取分辨率 (如 /vid/avc1/1080x1920/...)。"""
    m = re.search(r"/(\d{3,4})x(\d{3,4})/", url or "")
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _upgrade_x_image_candidates(raw_url: str):
    """将 X (pbs.twimg.com) 图片强制升级到最高原始分辨率：
    - 若拖入的是浏览器转码的 format=webp 预览，优先尝试无损 png/原图 jpg 的 name=orig
    - 按 name=orig -> 4096x4096 -> large -> 原链接 依次回退
    """
    u = urllib.parse.urlparse(raw_url)
    host = (u.hostname or "").lower()
    if host == "pbs.twimg.com" and "/media/" in u.path:
        qs = urllib.parse.parse_qs(u.query, keep_blank_values=True)
        path = re.sub(r":(thumb|small|medium|large|orig)$", "", u.path)
        base_q = {k: v[0] for k, v in qs.items()}
        has_ext = bool(re.search(r"\.(jpg|jpeg|png|webp|gif)$", path, re.I))
        orig_fmt = (base_q.get("format") or "").lower()
        cands = []
        # 如果浏览器拖出的是 webp 预览，先尝试原图 png / jpg 的 name=orig
        if orig_fmt == "webp" and not has_ext:
            for try_fmt in ("png", "jpg", "webp"):
                q2 = {**base_q, "format": try_fmt, "name": "orig"}
                cands.append(urllib.parse.urlunparse((u.scheme, u.netloc, path, "", urllib.parse.urlencode(q2), "")))
        for nm in ("orig", "4096x4096", "large"):
            q2 = dict(base_q)
            q2["name"] = nm
            if "format" not in q2 and not has_ext:
                q2["format"] = "jpg"
            cand = urllib.parse.urlunparse((u.scheme, u.netloc, path, "", urllib.parse.urlencode(q2), ""))
            if cand not in cands:
                cands.append(cand)
        if raw_url not in cands:
            cands.append(raw_url)
        return cands
    if host == "pbs.twimg.com" and "/profile_images/" in u.path:
        full_path = re.sub(r"_(normal|bigger|mini|\d+x\d+)(\.[^.]+)$", r"\2", u.path)
        if full_path != u.path:
            return [urllib.parse.urlunparse((u.scheme, u.netloc, full_path, "", u.query, "")), raw_url]
    return [raw_url]


def _inspect_hls_best(m3u8_url: str):
    """解析 HLS master m3u8，返回最高分辨率流的 (w, h, bandwidth, video_m3u8, audio_m3u8)。"""
    if not m3u8_url or not _is_public_url(m3u8_url):
        return None
    try:
        req = urllib.request.Request(m3u8_url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://x.com/"})
        with urllib.request.urlopen(req, timeout=12) as r:
            text = r.read(256 * 1024).decode("utf-8", "replace")
    except Exception:
        return None
    audio_groups = {}
    for line in text.splitlines():
        if line.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in line:
            mg = re.search(r'GROUP-ID="([^"]+)"', line)
            mu = re.search(r'URI="([^"]+)"', line)
            if mg and mu:
                audio_groups[mg.group(1)] = urllib.parse.urljoin(m3u8_url, mu.group(1))
    best = None
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for idx, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:") and idx + 1 < len(lines) and not lines[idx + 1].startswith("#"):
            mr = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            mb = re.search(r"(?:^|,)BANDWIDTH=(\d+)", line)
            ma = re.search(r'AUDIO="([^"]+)"', line)
            w, h = (int(mr.group(1)), int(mr.group(2))) if mr else (0, 0)
            bw = int(mb.group(1)) if mb else 0
            v_uri = urllib.parse.urljoin(m3u8_url, lines[idx + 1])
            a_uri = audio_groups.get(ma.group(1), "") if ma else ""
            cand = (w * h, bw, w, h, v_uri, a_uri)
            if not best or cand[:2] > best[:2]:
                best = cand
    return best


def _download_hls_via_ffmpeg(video_m3u8: str, audio_m3u8: str, root_folder: str, stem_hint: str, w: int, h: int, orig_url: str):
    """使用本机 ffmpeg 无损封装最高分辨率 HLS (如 1080p/4K) 为 MP4 并直传 Drive。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("未找到 ffmpeg")
    if not _is_public_url(video_m3u8) or (audio_m3u8 and not _is_public_url(audio_m3u8)):
        raise ValueError("不支持的流地址")
    fd, tmp_path = tempfile.mkstemp(prefix="h3_xvid_", suffix=".mp4")
    os.close(fd)
    try:
        cmd = [ffmpeg, "-y", "-v", "error", "-headers", "Referer: https://x.com/\r\n", "-i", video_m3u8]
        if audio_m3u8:
            cmd += ["-headers", "Referer: https://x.com/\r\n", "-i", audio_m3u8, "-c", "copy", "-bsf:a", "aac_adtstoasc"]
        else:
            cmd += ["-c", "copy"]
        cmd += ["-movflags", "+faststart", tmp_path]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) < 1024:
            raise ValueError(f"ffmpeg 封装失败: {(r.stderr or '')[:160]}")
        size = os.path.getsize(tmp_path)
        if size > 150 * 1024 * 1024:
            raise ValueError("视频超过 150 MB 上限")
        data = Path(tmp_path).read_bytes()
        stem = re.sub(r"[^A-Za-z0-9_.-]", "_", stem_hint or "x_vid")[:48] or "x_vid"
        name = f"{stem}_{w}x{h}_{int(time.time()) % 100000}.mp4" if (w and h) else f"{stem}_{int(time.time()) % 100000}.mp4"
        fid = upload_bytes_to_x_folder(root_folder, name, "video/mp4", data)
        return {"type": "video", "id": fid, "name": name, "ctype": "video/mp4", "size": len(data), "width": w, "height": h, "orig_url": orig_url}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _download_single_media(raw_url: str, root_folder: str, meta_w: int = 0, meta_h: int = 0):
    last_err = "无法下载媒体"
    for cand in _upgrade_x_image_candidates(raw_url):
        if not _is_public_url(cand):
            last_err = "不支持的地址"
            continue
        try:
            req = urllib.request.Request(cand, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Accept": "*/*",
                "Referer": "https://x.com/",
            })
            with urllib.request.urlopen(req, timeout=60) as r:
                hdr_ct = r.headers.get("Content-Type", "")
                max_bytes = 150 * 1024 * 1024 if ("video" in hdr_ct.lower() or re.search(r"\.(mp4|mov|m4v|webm)(\?|$)", cand, re.I)) else 40 * 1024 * 1024
                data = r.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ValueError(f"文件超过 {max_bytes // (1024 * 1024)} MB 上限")
                url_path = urllib.parse.urlparse(cand).path
                kind, ctype = _detect_media_mime(data, hdr_ct, url_path)
                if not kind:
                    last_err = "目标链接不是有效的图片或视频文件"
                    continue
                path_base = os.path.basename(url_path) or ("x_video" if kind == "video" else "x_image")
                stem = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.splitext(path_base)[0])[:48] or ("x_vid" if kind == "video" else "x_ref")
                name = f"{stem}_{int(time.time()) % 100000}{_ext_for_mime(ctype)}"
                if kind == "video":
                    uw, uh = _res_from_url(cand)
                    w, h = (uw or meta_w), (uh or meta_h)
                    fid = upload_bytes_to_x_folder(root_folder, name, ctype, data)
                    return {"type": "video", "id": fid, "name": name, "ctype": ctype, "size": len(data), "width": w, "height": h, "orig_url": cand}
                iw, ih = _image_dimensions(data)
                w, h = (iw or meta_w), (ih or meta_h)
                b64 = base64.b64encode(data).decode("ascii")
                return {"type": "image", "name": name, "ctype": ctype, "size": len(data), "width": w, "height": h, "data_url": f"data:{ctype};base64,{b64}", "orig_url": cand}
        except Exception as e:
            last_err = str(e)
    raise ValueError(last_err)


def _extract_article_text(art) -> str:
    if not isinstance(art, dict):
        return ""
    parts = []
    if art.get("title"):
        parts.append(str(art["title"]).strip())
    content = art.get("content")
    if isinstance(content, dict) and isinstance(content.get("blocks"), list):
        blocks = [str(b.get("text") or "").strip() for b in content["blocks"] if isinstance(b, dict)]
        body = "\n".join(x for x in blocks if x)
        if body:
            parts.append(body)
    elif art.get("preview_text"):
        parts.append(str(art["preview_text"]).strip())
    return "\n\n".join(p for p in parts if p)


def _media_dedup_key(url: str, mid: str = "") -> str:
    if mid:
        return "id:" + str(mid)
    u = urllib.parse.urlparse(url)
    path = re.sub(r":(thumb|small|medium|large|orig)$", "", u.path)
    return f"url:{(u.hostname or '').lower()}{path}"


def _fetch_tweet_json(api_u: str):
    try:
        req = urllib.request.Request(api_u, headers={"User-Agent": "Mozilla/5.0 H3Gallery/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _pick_best_video_meta(item: dict):
    """从 fxtwitter 的 variants + formats + url 中选出像素面积最大、码率最高的 MP4，同时记录 m3u8 供升级检测。"""
    mp4_cands = []
    m3u8_url = ""
    raw_list = list(item.get("variants") or []) + list(item.get("formats") or [])
    for v in raw_list:
        if not isinstance(v, dict) or not v.get("url"):
            continue
        u = v["url"]
        ct = (v.get("content_type") or v.get("container") or "").lower()
        if "m3u8" in ct or "mpegurl" in ct or ".m3u8" in u.lower():
            if not m3u8_url:
                m3u8_url = u
            continue
        if "mp4" in ct or ".mp4" in u.lower():
            w, h = _res_from_url(u)
            br = int(v.get("bitrate") or 0)
            mp4_cands.append((w * h, br, w, h, u))
    if item.get("url"):
        u0 = item["url"]
        if ".m3u8" in u0.lower():
            m3u8_url = m3u8_url or u0
        else:
            w, h = _res_from_url(u0)
            if not (w and h):
                w, h = int(item.get("width") or 0), int(item.get("height") or 0)
            mp4_cands.append((w * h, 0, w, h, u0))
    if mp4_cands:
        mp4_cands.sort(key=lambda x: (x[0], x[1]), reverse=True)
        _, _, bw, bh, best_u = mp4_cands[0]
        return best_u, m3u8_url, bw or int(item.get("width") or 0), bh or int(item.get("height") or 0)
    return m3u8_url, m3u8_url, int(item.get("width") or 0), int(item.get("height") or 0)


def _collect_media_from_fx(tw: dict, add_fn):
    if not isinstance(tw, dict):
        return
    media_obj = tw.get("media") or {}
    all_m = media_obj.get("all") or []
    src_list = all_m if all_m else (list(media_obj.get("photos") or []) + list(media_obj.get("videos") or []))
    for item in src_list:
        mid = str(item.get("id") or "")
        if item.get("type") in ("video", "gif") or item.get("variants") or item.get("formats"):
            best_u, m3u8_u, vw, vh = _pick_best_video_meta(item)
            add_fn(_media_dedup_key(best_u or m3u8_u, mid), "video", best_u, m3u8_u, vw, vh, mid)
        elif item.get("url"):
            u = item["url"]
            add_fn(_media_dedup_key(u, mid), "image", u, "", item.get("width") or 0, item.get("height") or 0, mid)
    art_cover = ((tw.get("article") or {}).get("cover_media") or {}).get("media_info") or {}
    if art_cover.get("original_img_url"):
        u = art_cover["original_img_url"]
        add_fn(_media_dedup_key(u), "image", u, "", art_cover.get("original_img_width") or 0, art_cover.get("original_img_height") or 0)


def _collect_media_from_vx(d_vx: dict, add_fn, existing_len: int = 0):
    if not isinstance(d_vx, dict):
        return
    added = 0
    for m_it in d_vx.get("media_extended") or []:
        t = m_it.get("type")
        u = m_it.get("url")
        if t in ("image", "video", "gif") and u:
            mid = str(m_it.get("id_str") or "")
            sz = m_it.get("size") or {}
            uw, uh = _res_from_url(u)
            w, h = (uw or sz.get("width") or 0), (uh or sz.get("height") or 0)
            add_fn(_media_dedup_key(u, mid), "video" if t in ("video", "gif") else "image", u, "", w, h, mid)
            added += 1
    if not existing_len and not added:
        for u in d_vx.get("mediaURLs") or []:
            if "twimg.com" in u:
                kind = "video" if "video.twimg.com" in u else "image"
                uw, uh = _res_from_url(u)
                add_fn(_media_dedup_key(u), kind, u, "", uw, uh)


def _make_spec_collector():
    specs = []
    by_key = {}
    def add_spec(key, kind, url, m3u8="", w=0, h=0, mid=""):
        if not url and not m3u8:
            return
        cur = by_key.get(key)
        if not cur:
            spec = {"key": key, "kind": kind, "url": url, "m3u8": m3u8, "w": int(w or 0), "h": int(h or 0), "id": str(mid or "")}
            by_key[key] = spec
            specs.append(spec)
        else:
            new_area = int(w or 0) * int(h or 0)
            cur_area = cur["w"] * cur["h"]
            if m3u8 and not cur["m3u8"]:
                cur["m3u8"] = m3u8
            if url and (not cur["url"] or new_area > cur_area):
                cur["url"] = url
                cur["w"] = int(w or 0)
                cur["h"] = int(h or 0)
    return specs, add_spec


def _parse_x_tweet_full(user: str, tid: str):
    """并行请求 fxtwitter + vxtwitter，同时解析主贴与嵌套引用贴 (quote/qrt) 的最高清媒体与正文。"""
    fx_u = f"https://api.fxtwitter.com/{user}/status/{tid}"
    vx_u = f"https://api.vxtwitter.com/{user}/status/{tid}"
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_fx = pool.submit(_fetch_tweet_json, fx_u)
        f_vx = pool.submit(_fetch_tweet_json, vx_u)
        d_fx = f_fx.result()
        d_vx = f_vx.result()

    text_candidates = []
    media_specs, add_main = _make_spec_collector()
    quote_specs, add_quote = _make_spec_collector()
    quote_texts = []
    quote_url = ""
    parent_user, parent_tid = "", ""

    if isinstance(d_fx, dict) and isinstance(d_fx.get("tweet"), dict):
        tw = d_fx["tweet"]
        if tw.get("replying_to_status"):
            parent_tid = str(tw["replying_to_status"])
            parent_user = str(tw.get("replying_to") or user)
        for cand_t in (tw.get("text"), _extract_article_text(tw.get("article"))):
            if cand_t and str(cand_t).strip():
                text_candidates.append(str(cand_t).strip())
        _collect_media_from_fx(tw, add_main)
        qtw = tw.get("quote")
        if isinstance(qtw, dict):
            if qtw.get("url"):
                quote_url = str(qtw["url"])
            for qt in (qtw.get("text"), _extract_article_text(qtw.get("article"))):
                if qt and str(qt).strip():
                    quote_texts.append(str(qt).strip())
            _collect_media_from_fx(qtw, add_quote)

    if isinstance(d_vx, dict) and ("text" in d_vx or "media_extended" in d_vx or "mediaURLs" in d_vx or "qrt" in d_vx):
        if not parent_tid and d_vx.get("replyingToID"):
            parent_tid = str(d_vx["replyingToID"])
            parent_user = str(d_vx.get("replyingTo") or user)
        for cand_t in (d_vx.get("text"), _extract_article_text(d_vx.get("article"))):
            if cand_t and str(cand_t).strip():
                text_candidates.append(str(cand_t).strip())
        _collect_media_from_vx(d_vx, add_main, len(media_specs))
        qrt = d_vx.get("qrt")
        if not quote_url and d_vx.get("qrtURL"):
            quote_url = str(d_vx["qrtURL"])
        if isinstance(qrt, dict):
            if not quote_url and qrt.get("tweetURL"):
                quote_url = str(qrt["tweetURL"])
            if qrt.get("text") and str(qrt["text"]).strip():
                quote_texts.append(str(qrt["text"]).strip())
            _collect_media_from_vx(qrt, add_quote, len(quote_specs))

    # 如果只拿到引用链接但未内联展开媒体，自动查一次引用贴获取最高清视频/原图
    if quote_url and not quote_specs:
        qu, qid = _parse_url_user_tid(quote_url)
        if qid and qid != str(tid):
            try:
                q_fx = _fetch_tweet_json(f"https://api.fxtwitter.com/{qu}/status/{qid}")
                if isinstance(q_fx, dict) and isinstance(q_fx.get("tweet"), dict):
                    _collect_media_from_fx(q_fx["tweet"], add_quote)
            except Exception:
                pass

    return {
        "text": max(text_candidates, key=len) if text_candidates else "",
        "media": media_specs,
        "parent_user": parent_user,
        "parent_tid": parent_tid,
        "quote_url": quote_url,
        "quote_text": max(quote_texts, key=len) if quote_texts else "",
        "quote_media": quote_specs,
    }


def _parse_x_tweet_pair(user: str, tid: str):
    info = _parse_x_tweet_full(user, tid)
    return info["text"], info["media"], info["parent_user"], info["parent_tid"]


def _download_spec_best(spec: dict, root_folder: str):
    """按最高分辨率策略下载单条媒体：
    - 视频：若 HLS m3u8 存在比直链 MP4 更高分辨率的流（如 1080p/4K），优先用 ffmpeg 无损封装最高清流；否则直接下载最高码率 MP4。
    - 图片：强制升级 name=orig（并绕过 webp 预览取原图）。
    """
    if spec.get("kind") == "video" and spec.get("m3u8"):
        mp4_u = spec.get("url") or ""
        mw, mh = _res_from_url(mp4_u)
        if not (mw and mh):
            mw, mh = spec.get("w") or 0, spec.get("h") or 0
        hls = _inspect_hls_best(spec["m3u8"])
        # 只要 HLS 分辨率严格高于直链 MP4（或根本没有直链 MP4），就用 ffmpeg 无损封装最高清 HLS 流
        if hls and (not mp4_u or mp4_u.lower().endswith(".m3u8") or hls[0] > (mw * mh)):
            _, _, hw, hh, v_uri, a_uri = hls
            try:
                return _download_hls_via_ffmpeg(v_uri, a_uri, root_folder, spec.get("id") or "x_vid", hw, hh, spec["m3u8"])
            except Exception:
                if not mp4_u or ".m3u8" in mp4_u.lower():
                    raise
    return _download_single_media(spec["url"], root_folder, spec.get("w") or 0, spec.get("h") or 0)


def fetch_from_x_or_url(raw_url: str, root_folder: str, skip_urls=None, include_quote: bool = False):
    """支持直接拖入 X 图片、直链 MP4 视频，或直接拖入/粘贴 X 推文/评论链接（自动提取完整长文、引用贴媒体与最高分辨率原图/视频）。"""
    raw_url = (raw_url or "").strip()
    if not raw_url:
        raise ValueError("缺少图片或视频链接")
    skip_keys = {_media_dedup_key(u) for u in (skip_urls or []) if u}
    # 检测是否是 X / Twitter 推文页面链接 (例如 https://x.com/user/status/1234567890)
    m_tw = re.search(r"https?://(?:www\.|mobile\.)?(?:x\.com|twitter\.com)/([^/]+)/status/(\d+)", raw_url)
    if m_tw:
        user, tid = m_tw.group(1), m_tw.group(2)
        tweet_url = f"https://x.com/{user}/status/{tid}"
        info = _parse_x_tweet_full(user, tid)
        tweet_text = info["text"]
        media_specs = list(info["media"])
        p_user, p_tid = info["parent_user"], info["parent_tid"]
        quote_url = info["quote_url"]
        quote_media = list(info["quote_media"])

        # 如果粘贴的是评论区（回复）链接，向上追溯上级主贴（最多 3 层）：
        # - 本条评论无媒体时，自动抓回上级主贴的媒体；
        # - 同一作者自回复且上级主贴也有媒体（如主贴放成品视频、评论放参考图/深度视频+提示词），自动一并合并抓取；
        # - 跨作者回复且本条已有媒体时，仅在 include_quote=True 时合并上级媒体，否则记录供前端一键补抓。
        hops = 0
        thread_parts = [tweet_text] if tweet_text else []
        cur_author = user.lower()
        extra_related_media = []
        extra_related_url = ""
        parent_media_merged = False
        while p_tid and hops < 3:
            is_same_author = (p_user or "").lower() == cur_author
            if media_specs and not is_same_author and not include_quote:
                # 跨作者回复且本条已有媒体：仅探测一层父贴，给前端提示可一键拉取
                p_info = _parse_x_tweet_full(p_user or user, p_tid)
                if p_info["media"]:
                    extra_related_media.extend(p_info["media"])
                    extra_related_url = f"https://x.com/{p_user or user}/status/{p_tid}"
                if p_info["quote_media"] and not quote_media:
                    quote_url = p_info["quote_url"]
                    quote_media = list(p_info["quote_media"])
                break
            hops += 1
            p_info = _parse_x_tweet_full(p_user or user, p_tid)
            if p_info["quote_media"] and not quote_media:
                quote_url = p_info["quote_url"]
                quote_media = list(p_info["quote_media"])
            if p_info["media"]:
                if not media_specs:
                    media_specs = list(p_info["media"])
                else:
                    # 主贴成品视频放在前面，评论参考素材跟在后面
                    media_specs = list(p_info["media"]) + media_specs
                    parent_media_merged = True
                break
            if p_info["text"] and is_same_author:
                thread_parts.insert(0, p_info["text"])
            p_user, p_tid = p_info["parent_user"], p_info["parent_tid"]
        tweet_text = "\n\n".join(p for p in thread_parts if p)

        # 引用贴/上级关联媒体处理：主贴本身没媒体时自动抓；主贴已有媒体且 include_quote=True 时一并抓
        used_quote = parent_media_merged
        if quote_media and (not media_specs or include_quote):
            media_specs = media_specs + quote_media
            used_quote = True
        if extra_related_media and include_quote:
            media_specs = extra_related_media + media_specs
            used_quote = True

        remaining_extra = ([] if used_quote else (quote_media + extra_related_media))
        remaining_url = quote_url or extra_related_url

        fetched = []
        seen_urls = set()
        for spec in media_specs:
            url_key = _media_dedup_key(spec.get("url") or spec.get("m3u8") or "")
            if url_key in seen_urls or url_key in skip_keys or len(fetched) >= 8:
                continue
            seen_urls.add(url_key)
            try:
                fetched.append(_download_spec_best(spec, root_folder))
            except Exception:
                pass
        if not fetched and not tweet_text:
            raise ValueError("未能从该 X 链接提取到图片或视频，建议直接把 X 图片拖进来或粘贴推文链接")
        thread_ids = []
        for xid in [tid, p_tid]:
            if xid and xid not in thread_ids:
                thread_ids.append(str(xid))
        return {
            "images": fetched,
            "tweet_text": tweet_text,
            "source_url": tweet_url,
            "from_reply": hops > 0,
            "thread_ids": thread_ids,
            "used_quote": used_quote,
            "quote_url": remaining_url,
            "quote_media_count": len(remaining_extra),
            "quote_has_video": any(sp.get("kind") == "video" for sp in remaining_extra),
        }
    # 直接图片或视频链接
    media = _download_single_media(raw_url, root_folder)
    return {"images": [media], "tweet_text": "", "source_url": "", "thread_ids": [], "used_quote": False, "quote_url": "", "quote_media_count": 0, "quote_has_video": False}


# ---------------- X 去重检测 & Chrome 插件一键剪藏 ----------------
def _extract_tweet_id(url: str) -> str:
    m = re.search(r"/status/(\d+)", str(url or ""))
    return m.group(1) if m else ""


def _norm_prompt_text(text: str) -> str:
    s = re.sub(r"https?://\S+", "", str(text or "")).lower()
    return re.sub(r"[\s\W_]+", "", s)


def _dup_summary(it: dict, reason: str) -> dict:
    imgs = it.get("images") or []
    first = imgs[0] if imgs else {}
    return {
        "id": it.get("id", ""),
        "prompt": (it.get("prompt") or "")[:240],
        "source_url": it.get("source_url") or "",
        "created_at": it.get("created_at") or "",
        "media_count": len(imgs),
        "thumb_id": first.get("id", ""),
        "thumb_type": first.get("type") or ("video" if re.search(r"\.(mp4|mov|m4v|webm)$", first.get("name", ""), re.I) else "image"),
        "reason": reason,
    }


def find_x_duplicate(items: list, urls=None, thread_ids=None, prompt: str = "", ignore_id: str = ""):
    """按推文/评论 ID 集合优先、其次按归一化提示词文本检测已有重复条目。"""
    cand_ids = set()
    for u in (urls or []):
        tid = _extract_tweet_id(u)
        if tid:
            cand_ids.add(tid)
    for xid in (thread_ids or []):
        if xid:
            cand_ids.add(str(xid))

    if cand_ids:
        for it in items:
            if ignore_id and it.get("id") == ignore_id:
                continue
            it_ids = set(str(x) for x in (it.get("thread_ids") or []) if x)
            stid = _extract_tweet_id(it.get("source_url") or "")
            if stid:
                it_ids.add(stid)
            if cand_ids & it_ids:
                return _dup_summary(it, "同一条 X 帖子已收藏过")

    np = _norm_prompt_text(prompt)
    if len(np) >= 24:
        for it in items:
            if ignore_id and it.get("id") == ignore_id:
                continue
            ep = _norm_prompt_text(it.get("prompt") or "")
            if len(ep) < 24:
                continue
            if np == ep or (len(np) >= 50 and len(ep) >= 50 and np[:50] == ep[:50]):
                return _dup_summary(it, "已存在相同提示词")
            shorter, longer = (np, ep) if len(np) <= len(ep) else (ep, np)
            if len(shorter) >= 36 and shorter in longer and (len(shorter) / len(longer)) >= 0.78:
                return _dup_summary(it, "已存在高度相似的提示词")
    return None


def _parse_url_user_tid(url: str):
    m = re.search(r"https?://(?:www\.|mobile\.)?(?:x\.com|twitter\.com)/([^/\s?#]+)/status/(\d+)", str(url or ""))
    return (m.group(1), m.group(2)) if m else ("", "")


def inspect_x_clip(data: dict, items: list):
    """Chrome 插件预览接口：不下载视频文件，秒级展开被折叠的作者评论全文并做重复校验。"""
    main_url = str(data.get("main_url") or "").strip()
    reply_urls = [str(u).strip() for u in (data.get("reply_urls") or []) if str(u).strip()][:5]
    dom_main_text = str(data.get("main_text") or "").strip()
    dom_reply_texts = [str(t).strip() for t in (data.get("reply_texts") or []) if str(t).strip()]

    m_user, m_tid = _parse_url_user_tid(main_url)
    canonical_main = f"https://x.com/{m_user}/status/{m_tid}" if m_tid else main_url

    # 并行拉取主贴 + 最多 4 条作者评论的完整 Note Tweet 原文（解决页面「显示更多」折叠截断问题）
    tasks = []
    if m_tid:
        tasks.append(("main", m_user, m_tid))
    seen_rtids = {m_tid} if m_tid else set()
    for ru in reply_urls:
        ru_u, ru_id = _parse_url_user_tid(ru)
        if ru_id and ru_id not in seen_rtids and len(tasks) < 5:
            seen_rtids.add(ru_id)
            tasks.append(("reply", ru_u, ru_id))

    api_results = {}
    if tasks:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(5, len(tasks))) as pool:
            fmap = {pool.submit(_parse_x_tweet_full, u, tid): (role, tid) for role, u, tid in tasks}
            for fut, meta in fmap.items():
                try:
                    api_results[meta] = fut.result()
                except Exception:
                    api_results[meta] = {"text": "", "media": [], "parent_user": "", "parent_tid": "", "quote_url": "", "quote_text": "", "quote_media": []}

    main_info = api_results.get(("main", m_tid), {"text": "", "media": [], "parent_user": "", "parent_tid": "", "quote_url": "", "quote_text": "", "quote_media": []})
    api_main_text = main_info["text"]
    main_specs = list(main_info["media"])
    p_user, p_tid = main_info["parent_user"], main_info["parent_tid"]
    quote_url = main_info["quote_url"]
    quote_specs = list(main_info["quote_media"])
    main_text = api_main_text if len(api_main_text) >= len(dom_main_text) else dom_main_text

    # 如果用户是在某条评论详情页点插件，自动溯源父贴媒体（即使当前评论自己也有参考图/视频，同作者自回复也会合并主贴成品视频！）
    thread_ids = list(seen_rtids)
    if p_tid and p_tid not in thread_ids:
        thread_ids.append(p_tid)
    if p_tid and (not main_specs or (p_user or "").lower() == m_user.lower()):
        try:
            p_info = _parse_x_tweet_full(p_user or m_user, p_tid)
            if p_info["media"]:
                main_specs = list(p_info["media"]) + main_specs
            if not quote_specs and p_info["quote_media"]:
                quote_url = p_info["quote_url"]
                quote_specs = list(p_info["quote_media"])
        except Exception:
            pass

    # 合并作者评论：优先取 API 完整全文（防止 DOM 被折叠截断），若 API 没取到则用 DOM 文本
    full_replies = []
    reply_specs = []
    for idx, ru in enumerate(reply_urls):
        _, ru_id = _parse_url_user_tid(ru)
        r_info = api_results.get(("reply", ru_id), {"text": "", "media": [], "quote_url": "", "quote_media": []})
        dom_t = dom_reply_texts[idx] if idx < len(dom_reply_texts) else ""
        best_t = r_info["text"] if len(r_info["text"]) >= len(dom_t) else dom_t
        if best_t and best_t not in full_replies:
            full_replies.append(best_t)
        reply_specs.extend(r_info["media"])
        if not quote_specs and r_info.get("quote_media"):
            quote_url = r_info.get("quote_url") or quote_url
            quote_specs = list(r_info["quote_media"])
    # 兜底：若存在未对应 URL 的 DOM 作者评论，也并入
    for dom_t in dom_reply_texts[len(reply_urls):]:
        if dom_t and dom_t not in full_replies:
            full_replies.append(dom_t)

    reply_text = "\n\n".join(full_replies)
    # 主贴媒体 + 同作者评论里的参考图/参考视频一并汇总（去重）
    own_specs = []
    seen_own = set()
    for sp in (main_specs + reply_specs):
        uk = _media_dedup_key(sp.get("url") or sp.get("m3u8") or "", sp.get("id") or "")
        if uk not in seen_own:
            seen_own.add(uk)
            own_specs.append(sp)

    # 智能推荐默认提示词：若存在作者评论且主贴较短或提到评论区/↓，默认选中评论区提示词
    mentions_reply = bool(re.search(r"评论区|评论里|留言区|楼下|提示词\s*[↓👇⬇️]|prompt\s*[↓👇⬇️]|in (?:the )?(?:comments|replies|thread)", main_text, re.I))
    if reply_text and (mentions_reply or len(reply_text) > len(main_text) * 1.2 or len(main_text) < 120):
        rec_mode = "reply"
        rec_prompt = reply_text
    elif main_text:
        rec_mode = "main"
        rec_prompt = main_text
    else:
        rec_mode = "reply" if reply_text else "main"
        rec_prompt = reply_text or main_text

    dup = find_x_duplicate(items, urls=[canonical_main] + reply_urls, thread_ids=thread_ids, prompt=rec_prompt)

    def summarize(specs):
        return [{"kind": sp.get("kind") or "image", "w": sp.get("w") or 0, "h": sp.get("h") or 0} for sp in specs[:8]]

    media_summary = summarize(own_specs)
    quote_media_summary = summarize(quote_specs)

    return {
        "main_url": canonical_main,
        "thread_ids": thread_ids,
        "main_text": main_text,
        "reply_text": reply_text,
        "reply_count": len(full_replies),
        "recommended_mode": rec_mode,
        "recommended_prompt": rec_prompt,
        "media_count": len(media_summary),
        "media_summary": media_summary,
        "quote_url": quote_url,
        "quote_media_count": len(quote_media_summary),
        "quote_media_summary": quote_media_summary,
        "duplicate": dup,
    }


def _download_spec_to_drive(spec: dict, root_folder: str):
    """直接将单条最高分辨率图片或视频下载并上传至 Drive x-inspirations 子目录，返回 {id, name, type, width, height}。"""
    res = _download_spec_best(spec, root_folder)
    if res.get("type") == "video" and res.get("id"):
        return {"id": res["id"], "name": res["name"], "type": "video", "width": res.get("width", 0), "height": res.get("height", 0)}
    # 图片：直接解码 bytes 上传 Drive，避免中转浏览器
    data_url = res.get("data_url") or ""
    if "," in data_url:
        b64 = data_url.split(",", 1)[1]
        raw = base64.b64decode(b64)
        fid = upload_bytes_to_x_folder(root_folder, res["name"], res.get("ctype") or "image/jpeg", raw)
        return {"id": fid, "name": res["name"], "type": "image", "width": res.get("width", 0), "height": res.get("height", 0)}
    raise ValueError("图片数据无效")


def execute_x_clip(root_folder: str, data: dict):
    """Chrome 插件一键保存/合并接口：下载最高清原图与视频直传 Drive，并写入 x-inspirations-meta.json。"""
    main_url = str(data.get("main_url") or "").strip()
    reply_urls = [str(u).strip() for u in (data.get("reply_urls") or []) if str(u).strip()][:5]
    fallback_media_urls = [str(u).strip() for u in (data.get("fallback_media_urls") or []) if str(u).strip()][:8]
    prompt = str(data.get("prompt") or "")
    tags_raw = data.get("tags", [])
    if isinstance(tags_raw, str):
        tags = [t.strip() for t in re.split(r"[,，]", tags_raw) if t.strip()]
    else:
        tags = [str(t).strip() for t in (tags_raw or []) if str(t).strip()]
    notes = str(data.get("notes") or "")
    merge_into_id = str(data.get("merge_into_id") or "").strip()
    prompt_mode = str(data.get("prompt_mode") or "replace")  # replace | append | keep
    skip_media = bool(data.get("skip_media", False))
    include_quote = data.get("include_quote", True)

    m_user, m_tid = _parse_url_user_tid(main_url)
    canonical_main = f"https://x.com/{m_user}/status/{m_tid}" if m_tid else main_url
    thread_ids = []
    for u in [canonical_main] + reply_urls:
        xid = _extract_tweet_id(u)
        if xid and xid not in thread_ids:
            thread_ids.append(xid)

    uploaded_media = []
    if not skip_media:
        media_specs = []
        quote_specs = []
        if m_tid:
            m_info = _parse_x_tweet_full(m_user, m_tid)
            media_specs = list(m_info["media"])
            quote_specs = list(m_info["quote_media"])
            p_user, p_tid = m_info["parent_user"], m_info["parent_tid"]
            if p_tid and p_tid not in thread_ids:
                thread_ids.append(p_tid)
            hops = 0
            while p_tid and hops < 2:
                is_same = (p_user or "").lower() == m_user.lower()
                if media_specs and not is_same and not include_quote:
                    break
                hops += 1
                p_info = _parse_x_tweet_full(p_user or m_user, p_tid)
                if p_info["quote_media"] and not quote_specs:
                    quote_specs = list(p_info["quote_media"])
                if p_info["media"]:
                    media_specs = list(p_info["media"]) + media_specs
                    break
                p_user, p_tid = p_info["parent_user"], p_info["parent_tid"]
        if reply_urls:
            for ru in reply_urls[:3]:
                ru_u, ru_id = _parse_url_user_tid(ru)
                if ru_id and ru_id != m_tid:
                    r_info = _parse_x_tweet_full(ru_u, ru_id)
                    media_specs.extend(r_info["media"])
                    if not quote_specs and r_info["quote_media"]:
                        quote_specs = list(r_info["quote_media"])
        if quote_specs and (not media_specs or include_quote):
            media_specs = media_specs + quote_specs
        if not media_specs and fallback_media_urls:
            for fu in fallback_media_urls:
                media_specs.append({"key": _media_dedup_key(fu), "kind": "image", "url": fu, "m3u8": "", "w": 0, "h": 0, "id": ""})

        seen_keys = set()
        for sp in media_specs:
            uk = _media_dedup_key(sp.get("url") or sp.get("m3u8") or "")
            if uk in seen_keys or len(uploaded_media) >= 8:
                continue
            seen_keys.add(uk)
            try:
                uploaded_media.append(_download_spec_to_drive(sp, root_folder))
            except Exception:
                pass

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with lock:
        items = load_x_items(root_folder)
        if merge_into_id:
            target = next((it for it in items if it.get("id") == merge_into_id), None)
            if not target:
                raise ValueError("要合并的旧条目不存在")
            old_p = (target.get("prompt") or "").strip()
            new_p = prompt.strip()
            if new_p and prompt_mode == "replace":
                target["prompt"] = new_p
            elif new_p and prompt_mode == "append" and new_p not in old_p:
                target["prompt"] = (old_p + "\n\n" + new_p).strip() if old_p else new_p
            if uploaded_media:
                target.setdefault("images", []).extend(
                    [{"id": m["id"], "name": m["name"], "type": m["type"]} for m in uploaded_media]
                )
            if tags:
                merged_tags = list(target.get("tags") or [])
                for t in tags:
                    if t not in merged_tags:
                        merged_tags.append(t)
                target["tags"] = merged_tags
            if notes and notes not in (target.get("notes") or ""):
                target["notes"] = ((target.get("notes") or "") + "\n" + notes).strip()
            old_tids = [str(x) for x in (target.get("thread_ids") or []) if x]
            for xid in thread_ids:
                if xid not in old_tids:
                    old_tids.append(xid)
            if old_tids:
                target["thread_ids"] = old_tids
            if not target.get("source_url") and canonical_main:
                target["source_url"] = canonical_main
            target["updated_at"] = now_iso
            x_store["items"] = items
            save_x_items(root_folder)
            return {"entry": target, "merged": True, "media_details": uploaded_media}

        entry = {
            "id": "x_" + secrets.token_hex(6),
            "prompt": prompt,
            "negative_prompt": "",
            "source_url": canonical_main,
            "tags": tags,
            "notes": notes,
            "favorite": False,
            "images": [{"id": m["id"], "name": m["name"], "type": m["type"]} for m in uploaded_media],
            "thread_ids": thread_ids,
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        items.insert(0, entry)
        x_store["items"] = items
        save_x_items(root_folder)
        return {"entry": entry, "merged": False, "media_details": uploaded_media}


# ---------------- HTTP 服务 ----------------
PAGE_CACHE_HEADERS = {"Cache-Control": "no-cache"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send_ext_cors(self):
        # 仅放行本机 Chrome 扩展 Origin，绝不对普通网页开放 CORS；公网只读模式彻底关闭扩展 CORS
        if READONLY_MODE:
            return
        origin = self.headers.get("Origin") or ""
        if origin.startswith("chrome-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-H3")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _client_ip(self) -> str:
        cf_ip = (self.headers.get("CF-Connecting-IP") or "").strip()
        if cf_ip:
            return cf_ip
        xff = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        return xff or (self.client_address[0] if self.client_address else "unknown")

    def _is_guest_authed(self) -> bool:
        if not READONLY_MODE or not READONLY_PASSWORD:
            return True
        raw_cookie = self.headers.get("Cookie") or ""
        if not raw_cookie:
            return False
        c = http.cookies.SimpleCookie()
        try:
            c.load(raw_cookie)
        except Exception:
            return False
        m = c.get(GUEST_COOKIE_NAME)
        return bool(m and _verify_guest_token(m.value))

    def _is_https_req(self) -> bool:
        proto = (self.headers.get("X-Forwarded-Proto") or "").lower()
        cf_vis = (self.headers.get("CF-Visitor") or "").lower()
        return proto == "https" or '"https"' in cf_vis

    def do_OPTIONS(self):
        if not self.host_ok():
            return self.send_error(403)
        self.send_response(204)
        self._send_ext_cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_json(self, obj, code=200, extra=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self._send_ext_cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if READONLY_MODE:
            self.send_header("X-Robots-Tag", "noindex, nofollow")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data: bytes, ctype: str, extra=None):
        self.send_response(200)
        self._send_ext_cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if READONLY_MODE:
            self.send_header("X-Robots-Tag", "noindex, nofollow")
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
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if READONLY_MODE:
            return ALLOW_ALL_HOSTS or host in ALLOWED_HOSTS
        # 防止 DNS 重绑定：本机模式只接受本机地址访问
        return host in ("127.0.0.1", "localhost")

    # ---------- GET ----------
    def do_GET(self):
        if not self.host_ok():
            return self.send_error(403)
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        try:
            if url.path == "/":
                if not READONLY_MODE and ("code" in qs or "error" in qs):
                    return self.oauth_callback(qs)
                return self.send_bytes((ROOT / "index.html").read_bytes(), "text/html; charset=utf-8",
                                       PAGE_CACHE_HEADERS)
            if url.path in ("/favicon.svg", "/favicon.ico"):
                return self.send_bytes((ROOT / "favicon.svg").read_bytes(), "image/svg+xml",
                                       {"Cache-Control": "no-cache"})
            if url.path == "/api/status":
                return self.send_json(self.status())
            if READONLY_MODE:
                if url.path == "/api/x/items":
                    if not self._is_guest_authed():
                        return self.send_json({"error": "guest_auth_required"}, 401)
                    folder = X_FOLDER_ID_OVERRIDE or config().get("folder_id")
                    return self.send_json(load_x_items(folder, force="refresh" in qs) if folder else [])
                m = re.fullmatch(r"/thumb/([A-Za-z0-9_-]+)", url.path)
                if m:
                    if not self._is_guest_authed():
                        return self.send_json({"error": "guest_auth_required"}, 401)
                    if not is_allowed_x_media_id(m.group(1)):
                        return self.send_error(403)
                    return self.thumb(m.group(1), int(qs.get("s", ["400"])[0]))
                m = re.fullmatch(r"/media/([A-Za-z0-9_-]+)", url.path)
                if m:
                    if not self._is_guest_authed():
                        return self.send_json({"error": "guest_auth_required"}, 401)
                    if not is_allowed_x_media_id(m.group(1)):
                        return self.send_error(403)
                    return self.stream(m.group(1))
                return self.send_error(403)
            if url.path == "/auth/login":
                return self.oauth_start()
            if url.path == "/api/items":
                return self.send_json(get_items(force="refresh" in qs))
            if url.path == "/api/x/items":
                folder = config().get("folder_id")
                return self.send_json(load_x_items(folder, force="refresh" in qs) if folder else [])
            m = re.fullmatch(r"/api/x/download/([A-Za-z0-9_-]+)", url.path)
            if m:
                return self.download_drive_file(m.group(1), qs.get("name", ["media"])[0])
            m = re.fullmatch(r"/api/x/download_zip/([A-Za-z0-9_-]+)", url.path)
            if m:
                return self.download_x_zip(m.group(1))
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
        if READONLY_MODE:
            return {
                "readonly": True,
                "password_required": bool(READONLY_PASSWORD),
                "guest_authenticated": self._is_guest_authed(),
                "has_credentials": True,
                "authorized": True,
                "folder_id": "x-readonly",
                "folder_name": "𝕏 灵感库",
                "title": READONLY_TITLE or config().get("title") or "𝕏 灵感库",
            }
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
        disk_bin = (THUMB_CACHE_DIR / f"{fid}_{size}.bin") if THUMB_CACHE_DIR else None
        disk_meta = (THUMB_CACHE_DIR / f"{fid}_{size}.ct") if THUMB_CACHE_DIR else None
        if not hit and disk_bin and disk_bin.is_file():
            try:
                ct = disk_meta.read_text("utf-8").strip() if (disk_meta and disk_meta.is_file()) else "image/jpeg"
                hit = (ct, disk_bin.read_bytes())
                with lock:
                    thumbs[key] = hit
                    while len(thumbs) > THUMB_MEM_LIMIT:
                        thumbs.popitem(last=False)
            except Exception:
                hit = None
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
                while len(thumbs) > THUMB_MEM_LIMIT:
                    thumbs.popitem(last=False)
            if disk_bin and THUMB_CACHE_DIR:
                try:
                    THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    tmp_b = disk_bin.with_suffix(".tmp")
                    tmp_b.write_bytes(hit[1])
                    os.replace(tmp_b, disk_bin)
                    if disk_meta:
                        disk_meta.write_text(hit[0], encoding="utf-8")
                    # 轻量磁盘缓存保护（1c1g VPS 防止占满磁盘，超过 1200 个缩略图清理最旧一批）
                    bins = list(THUMB_CACHE_DIR.glob("*.bin"))
                    if len(bins) > 1200:
                        bins.sort(key=lambda p: p.stat().st_mtime)
                        for old in bins[:200]:
                            old.unlink(missing_ok=True)
                            old.with_suffix(".ct").unlink(missing_ok=True)
                except Exception:
                    pass
        cc = "public, max-age=86400" if (READONLY_MODE and not READONLY_PASSWORD) else "private, max-age=86400"
        self.send_bytes(hit[1], hit[0], {"Cache-Control": cc})

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
            cc = "public, max-age=3600" if (READONLY_MODE and not READONLY_PASSWORD) else "private, max-age=3600"
            self.send_header("Cache-Control", cc)
            if READONLY_MODE:
                self.send_header("Content-Disposition", "inline")
                self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.end_headers()
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def download_drive_file(self, fid: str, filename: str):
        """以附件形式下载单个原始 Drive 文件（保持原画质）。"""
        safe_name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", (filename or "media").strip()) or "media"
        ascii_name = re.sub(r"[^A-Za-z0-9_.-]", "_", safe_name) or "media"
        with gopen("GET", f"{API}/files/{fid}", {"alt": "media", **COMMON}) as r:
            self.send_response(200)
            self.send_header("Content-Type", r.headers.get("Content-Type") or "application/octet-stream")
            if r.headers.get("Content-Length"):
                self.send_header("Content-Length", r.headers["Content-Length"])
            self.send_header("Content-Disposition", f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(safe_name)}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def download_x_zip(self, entry_id: str):
        """将单条 X 灵感的全部原图/视频（按当前顺序编号）+ 提示词文本打包为无压缩高速 ZIP 下载。"""
        folder = config().get("folder_id")
        if not folder:
            return self.send_error(404)
        items = load_x_items(folder)
        entry = next((it for it in items if it.get("id") == entry_id), None)
        if not entry:
            return self.send_error(404)
        imgs = entry.get("images") or []

        def fetch_one(idx_im):
            idx, im = idx_im
            raw_name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", str(im.get("name") or f"media_{idx+1}")).strip()
            with gopen("GET", f"{API}/files/{im['id']}", {"alt": "media", **COMMON}) as r:
                return (idx, raw_name, r.read())

        downloaded = []
        if imgs:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(imgs))) as pool:
                for res in pool.map(fetch_one, enumerate(imgs)):
                    downloaded.append(res)
            downloaded.sort(key=lambda x: x[0])

        lines = []
        if entry.get("prompt"):
            lines.append(entry["prompt"].strip())
        if entry.get("negative_prompt"):
            lines.append("\n[反向提示词]\n" + entry["negative_prompt"].strip())
        if entry.get("source_url"):
            lines.append("\n[X 原帖链接]\n" + entry["source_url"].strip())
        if entry.get("tags"):
            lines.append("\n[标签]\n" + ", ".join(entry["tags"]))
        if entry.get("notes"):
            lines.append("\n[备注]\n" + entry["notes"].strip())
        prompt_bytes = ("\n".join(lines).strip() + "\n").encode("utf-8")

        spool = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)
        try:
            folder_prefix = f"h3_{entry_id}"
            with zipfile.ZipFile(spool, "w", compression=zipfile.ZIP_STORED) as zf:
                zf.writestr(f"{folder_prefix}/00_prompt.txt", prompt_bytes)
                for idx, raw_name, data in downloaded:
                    zf.writestr(f"{folder_prefix}/{idx + 1:02d}_{raw_name}", data)
            size = spool.tell()
            spool.seek(0)
            zip_name = f"{folder_prefix}.zip"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{zip_name}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while True:
                chunk = spool.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        finally:
            spool.close()

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
            if READONLY_MODE:
                if url.path == "/api/guest_login":
                    if not READONLY_PASSWORD:
                        return self.send_json({"ok": True})
                    ip = self._client_ip()
                    now = time.time()
                    with lock:
                        fails = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < 300]
                        _LOGIN_FAILS[ip] = fails
                        if len(fails) >= 15:
                            return self.send_json({"error": "密码错误次数过多，请 5 分钟后再试"}, 429)
                    try:
                        body = json.loads(self.read_body() or b"{}")
                    except Exception:
                        body = {}
                    pw = str(body.get("password", ""))
                    if not hmac.compare_digest(pw, READONLY_PASSWORD):
                        with lock:
                            _LOGIN_FAILS.setdefault(ip, []).append(now)
                        return self.send_json({"error": "密码错误，请重试"}, 403)
                    with lock:
                        _LOGIN_FAILS.pop(ip, None)
                    tok = _make_guest_token()
                    sec_flag = "; Secure" if self._is_https_req() else ""
                    cookie = f"{GUEST_COOKIE_NAME}={tok}; Path=/; Max-Age={GUEST_COOKIE_TTL}; HttpOnly; SameSite=Lax{sec_flag}"
                    return self.send_json({"ok": True}, extra={"Set-Cookie": cookie})
                if url.path == "/api/guest_logout":
                    sec_flag = "; Secure" if self._is_https_req() else ""
                    cookie = f"{GUEST_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{sec_flag}"
                    return self.send_json({"ok": True}, extra={"Set-Cookie": cookie})
                return self.send_error(403)
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
                x_store.update(loaded_for=None, folder_id=None, file_id=None, items=[])
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

            if url.path == "/api/x/fetch_url":
                body = json.loads(self.read_body() or b"{}")
                folder = config()["folder_id"]
                try:
                    return self.send_json(fetch_from_x_or_url(body.get("url", ""), folder, body.get("skip_urls"), bool(body.get("include_quote", False))))
                except ValueError as ve:
                    return self.send_json({"error": str(ve)}, 400)

            if url.path == "/api/x/check_dup":
                body = json.loads(self.read_body() or b"{}")
                folder = config().get("folder_id")
                items = load_x_items(folder) if folder else []
                urls = list(body.get("urls") or [])
                if body.get("url"):
                    urls.append(body["url"])
                dup = find_x_duplicate(items, urls=urls, thread_ids=body.get("thread_ids"), prompt=body.get("prompt", ""), ignore_id=body.get("ignore_id", ""))
                return self.send_json({"duplicate": dup})

            if url.path == "/api/x/inspect_clip":
                body = json.loads(self.read_body() or b"{}")
                folder = config().get("folder_id")
                items = load_x_items(folder) if folder else []
                try:
                    return self.send_json(inspect_x_clip(body, items))
                except ValueError as ve:
                    return self.send_json({"error": str(ve)}, 400)

            if url.path == "/api/x/clip":
                body = json.loads(self.read_body() or b"{}")
                folder = config()["folder_id"]
                try:
                    return self.send_json(execute_x_clip(folder, body))
                except ValueError as ve:
                    return self.send_json({"error": str(ve)}, 400)

            if url.path == "/api/x/merge":
                data = json.loads(self.read_body() or b"{}")
                folder = config()["folder_id"]
                eid = str(data.get("id") or "").strip()
                now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                with lock:
                    items = load_x_items(folder)
                    target = next((it for it in items if it.get("id") == eid), None)
                    if not target:
                        return self.send_json({"error": "未找到要合并的旧条目"}, 404)
                    old_p = (target.get("prompt") or "").strip()
                    new_p = str(data.get("prompt") or "").strip()
                    pmode = str(data.get("prompt_mode") or "append")
                    if new_p and pmode == "replace":
                        target["prompt"] = new_p
                    elif new_p and pmode == "append" and new_p not in old_p:
                        target["prompt"] = (old_p + "\n\n" + new_p).strip() if old_p else new_p
                    existing_img_ids = {im.get("id") for im in (target.get("images") or [])}
                    for im in (data.get("images") or []):
                        if isinstance(im, dict) and im.get("id") and im["id"] not in existing_img_ids:
                            nm = str(im.get("name") or "media.jpg")
                            mtype = str(im.get("type") or ("video" if re.search(r"\.(mp4|mov|m4v|webm)$", nm, re.I) else "image"))
                            target.setdefault("images", []).append({"id": str(im["id"]), "name": nm, "type": mtype})
                            existing_img_ids.add(im["id"])
                    tags_raw = data.get("tags", [])
                    add_tags = [t.strip() for t in re.split(r"[,，]", tags_raw) if t.strip()] if isinstance(tags_raw, str) else [str(t).strip() for t in (tags_raw or []) if str(t).strip()]
                    if add_tags:
                        cur_tags = list(target.get("tags") or [])
                        for t in add_tags:
                            if t not in cur_tags:
                                cur_tags.append(t)
                        target["tags"] = cur_tags
                    notes = str(data.get("notes") or "").strip()
                    if notes and notes not in (target.get("notes") or ""):
                        target["notes"] = ((target.get("notes") or "") + "\n" + notes).strip()
                    tids = [str(x) for x in (target.get("thread_ids") or []) if x]
                    for xid in (data.get("thread_ids") or []):
                        if xid and str(xid) not in tids:
                            tids.append(str(xid))
                    su = str(data.get("source_url") or "").strip()
                    stid = _extract_tweet_id(su)
                    if stid and stid not in tids:
                        tids.append(stid)
                    if tids:
                        target["thread_ids"] = tids
                    if su and not target.get("source_url"):
                        target["source_url"] = su
                    target["updated_at"] = now_iso
                    x_store["items"] = items
                    save_x_items(folder)
                    return self.send_json(target)

            if url.path == "/api/x/trash_file":
                body = json.loads(self.read_body() or b"{}")
                fid = str(body.get("id") or "").strip()
                if re.fullmatch(r"[A-Za-z0-9_-]{10,}", fid):
                    try:
                        gjson("PATCH", f"/files/{fid}", None, {"trashed": True})
                    except Exception:
                        pass
                return self.send_json({"ok": True})

            if url.path == "/api/x/upload_image":
                return self.upload_x_image(qs)

            if url.path == "/api/x/save":
                data = json.loads(self.read_body() or b"{}")
                folder = config()["folder_id"]
                now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                with lock:
                    items = load_x_items(folder)
                    eid = str(data.get("id") or "").strip()
                    existing = next((it for it in items if it.get("id") == eid), None) if eid else None
                    tags_raw = data.get("tags", [])
                    if isinstance(tags_raw, str):
                        tags = [t.strip() for t in re.split(r"[,，]", tags_raw) if t.strip()]
                    else:
                        tags = [str(t).strip() for t in (tags_raw or []) if str(t).strip()]
                    imgs = []
                    for im in (data.get("images") or []):
                        if isinstance(im, dict) and im.get("id"):
                            nm = str(im.get("name") or "media.jpg")
                            mtype = str(im.get("type") or ("video" if re.search(r"\.(mp4|mov|m4v|webm)$", nm, re.I) else "image"))
                            imgs.append({"id": str(im["id"]), "name": nm, "type": mtype})
                    tids = list(existing.get("thread_ids") or []) if existing else []
                    for xid in (data.get("thread_ids") or []):
                        if xid and str(xid) not in tids:
                            tids.append(str(xid))
                    su = str(data.get("source_url") or "").strip()
                    stid = _extract_tweet_id(su)
                    if stid and stid not in tids:
                        tids.append(stid)
                    entry = {
                        "id": existing["id"] if existing else ("x_" + secrets.token_hex(6)),
                        "prompt": str(data.get("prompt") or ""),
                        "negative_prompt": str(data.get("negative_prompt") or ""),
                        "source_url": su,
                        "tags": tags,
                        "notes": str(data.get("notes") or ""),
                        "favorite": bool(data.get("favorite", False)),
                        "images": imgs,
                        "thread_ids": tids,
                        "created_at": existing.get("created_at", now_iso) if existing else now_iso,
                        "updated_at": now_iso,
                    }
                    if existing:
                        idx = items.index(existing)
                        items[idx] = entry
                    else:
                        items.insert(0, entry)
                    x_store["items"] = items
                    save_x_items(folder)
                    return self.send_json(entry)

            if url.path == "/api/x/delete":
                data = json.loads(self.read_body() or b"{}")
                eid = str(data.get("id") or "").strip()
                trash_files = bool(data.get("trash_files", False))
                folder = config()["folder_id"]
                with lock:
                    items = load_x_items(folder)
                    target = next((it for it in items if it.get("id") == eid), None)
                    if not target:
                        return self.send_json({"error": "未找到该条目"}, 404)
                    x_store["items"] = [it for it in items if it.get("id") != eid]
                    save_x_items(folder)
                if trash_files:
                    for im in (target.get("images") or []):
                        try:
                            gjson("PATCH", f"/files/{im['id']}", None, {"trashed": True})
                        except Exception:
                            pass
                return self.send_json({"ok": True})
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

    def upload_x_image(self, qs):
        """将 X 灵感参考图/视频直接流式上传到 Drive 的 x-inspirations 子目录，不占本地盘。"""
        root_folder = config()["folder_id"]
        xfolder = ensure_x_folder(root_folder, create=True)
        name = qs.get("name", ["x_ref.jpg"])[0]
        ctype = self.headers.get("Content-Type") or "application/octet-stream"
        length = int(self.headers.get("Content-Length", 0))
        init_body = json.dumps({"name": name, "parents": [xfolder]}).encode()
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
        mtype = "video" if (ctype.lower().startswith("video/") or re.search(r"\.(mp4|mov|m4v|webm)$", name, re.I)) else "image"
        return self.send_json({"id": fid, "name": name, "type": mtype})


def _configure_runtime():
    global PORT, BIND_HOST, REDIRECT_URI, CONFIG, READONLY_MODE, READONLY_PASSWORD
    global READONLY_TITLE, READONLY_TTL, X_FOLDER_ID_OVERRIDE, SERVICE_ACCOUNT_PATH
    global ALLOWED_HOSTS, ALLOW_ALL_HOSTS, THUMB_MEM_LIMIT, THUMB_CACHE_DIR

    ap = argparse.ArgumentParser(description="MiniMax H3 提示词画廊 / 𝕏 灵感库")
    ap.add_argument("port", nargs="?", type=int, default=None, help="监听端口 (默认 8765)")
    ap.add_argument("--bind", default=os.environ.get("H3_BIND", "127.0.0.1"), help="绑定地址 (默认 127.0.0.1)")
    ap.add_argument("--readonly", action="store_true", default=(os.environ.get("H3_READONLY") == "1"),
                    help="启用公网只读访客模式（仅暴露 𝕏 灵感库，禁用全部写/下载/私人画廊接口）")
    ap.add_argument("--config", default=os.environ.get("H3_CONFIG", ""), help="配置文件路径")
    ap.add_argument("--password", default=None, help="只读模式访问密码（任意字符，留空则免密）")
    ap.add_argument("--allowed-hosts", default=None, help="允许访问的域名（逗号分隔，或 * 放行全部）")
    ap.add_argument("--x-folder-id", default=None, help="直接指定 x-inspirations 的 Drive 文件夹 ID")
    ap.add_argument("--service-account", default=None, help="Google 只读服务账号 JSON 路径")
    ap.add_argument("--title", default=None, help="覆盖网页标题")
    ap.add_argument("--ttl", type=int, default=None, help="只读模式自动刷新 Drive 间隔秒数 (默认 60)")
    ap.add_argument("--thumb-cache-dir", default=None, help="只读模式缩略图磁盘缓存目录")
    args = ap.parse_args()

    READONLY_MODE = bool(args.readonly)
    BIND_HOST = args.bind or "127.0.0.1"
    if args.config:
        CONFIG = Path(args.config).expanduser().resolve()
    elif READONLY_MODE and (ROOT / "readonly-config.json").exists():
        CONFIG = ROOT / "readonly-config.json"

    cfg = config()
    PORT = args.port or int(os.environ.get("H3_PORT") or cfg.get("port") or (8766 if READONLY_MODE else 8765))
    REDIRECT_URI = f"http://127.0.0.1:{PORT}/"

    if READONLY_MODE:
        pw = args.password if args.password is not None else os.environ.get("H3_PASSWORD")
        if pw is None:
            pw = str(cfg.get("password") or "")
        READONLY_PASSWORD = str(pw)

        READONLY_TITLE = (
            args.title if args.title is not None
            else (os.environ.get("H3_TITLE") or str(cfg.get("title") or "𝕏 灵感库"))
        ).strip()
        READONLY_TTL = max(15, int(args.ttl or os.environ.get("H3_TTL") or cfg.get("ttl") or 60))

        xfid_raw = args.x_folder_id or os.environ.get("H3_X_FOLDER_ID") or str(cfg.get("x_folder_id") or "")
        X_FOLDER_ID_OVERRIDE = parse_folder_id(xfid_raw) or xfid_raw.strip()

        sa_raw = args.service_account or os.environ.get("H3_SERVICE_ACCOUNT") or str(cfg.get("service_account") or "")
        if sa_raw:
            p_sa = Path(sa_raw).expanduser()
            SERVICE_ACCOUNT_PATH = p_sa if p_sa.is_absolute() else (ROOT / p_sa).resolve()
        elif (ROOT / "service-account.json").exists():
            SERVICE_ACCOUNT_PATH = ROOT / "service-account.json"

        hosts_raw = args.allowed_hosts if args.allowed_hosts is not None else os.environ.get("H3_ALLOWED_HOSTS")
        if hosts_raw is None:
            hosts_raw = cfg.get("allowed_hosts")
        hosts = {"127.0.0.1", "localhost"}
        if isinstance(hosts_raw, list):
            items_h = [str(x).strip().lower() for x in hosts_raw if str(x).strip()]
        elif isinstance(hosts_raw, str):
            items_h = [x.strip().lower() for x in hosts_raw.split(",") if x.strip()]
        else:
            items_h = []
        if "*" in items_h:
            ALLOW_ALL_HOSTS = True
        else:
            for h in items_h:
                hosts.add(h.split(":")[0])
        ALLOWED_HOSTS = hosts

        THUMB_MEM_LIMIT = 120
        tdir = args.thumb_cache_dir or os.environ.get("H3_THUMB_CACHE_DIR") or str(cfg.get("thumb_cache_dir") or "/tmp/h3_thumb_cache")
        THUMB_CACHE_DIR = Path(tdir).expanduser() if tdir else None


if __name__ == "__main__":
    _configure_runtime()
    srv = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    if READONLY_MODE:
        sa_info = f"ServiceAccount={SERVICE_ACCOUNT_PATH.name}" if (SERVICE_ACCOUNT_PATH and SERVICE_ACCOUNT_PATH.exists()) else "OAuth(token.json)"
        pw_info = "已启用访问密码" if READONLY_PASSWORD else "无密码公开"
        hosts_info = "*" if ALLOW_ALL_HOSTS else ",".join(sorted(ALLOWED_HOSTS))
        print(f"𝕏 灵感库 [公网只读模式] 已启动: http://{BIND_HOST}:{PORT} | {pw_info} | {sa_info} | Hosts={hosts_info} | TTL={READONLY_TTL}s", flush=True)
    else:
        print(f"画廊已启动: http://{BIND_HOST}:{PORT}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
