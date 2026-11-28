import indexHtml from "./assets/index.html";
import faviconSvg from "./assets/favicon.svg";

const DRIVE_API = "https://www.googleapis.com/drive/v3";
const SA_SCOPE = "https://www.googleapis.com/auth/drive.readonly";
const X_META_NAME = "x-inspirations-meta.json";
const COOKIE_NAME = "h3_guest";
const COOKIE_TTL_SEC = 30 * 86400;

const enc = new TextEncoder();

// Isolate-level warm caches (reset automatically when isolate recycles)
let saTokenCache = { token: "", expiry: 0, email: "" };
let metaCache = { loadedAt: 0, folderId: "", metaFileId: "", items: [], allowedIds: new Set() };
const thumbLinkCache = new Map(); // fid -> { link, expiry }
const loginFails = new Map();     // ip -> [ts, ...]

function b64urlBytes(bytes) {
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64urlStr(str) {
  return b64urlBytes(enc.encode(str));
}

function pemToDer(pem) {
  const clean = pem
    .replace(/-----BEGIN [^-]+-----/g, "")
    .replace(/-----END [^-]+-----/g, "")
    .replace(/\s+/g, "");
  const bin = atob(clean);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

async function hmacHex(keyStr, dataStr) {
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(keyStr),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(dataStr));
  return [...new Uint8Array(sig)].map(b => b.toString(16).padStart(2, "0")).join("");
}

function timingSafeEqualStr(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

async function passwordEquals(inputPw, expectedPw, secret) {
  const h1 = await hmacHex(secret || "h3-pw-cmp", `pw:${String(inputPw ?? "")}`);
  const h2 = await hmacHex(secret || "h3-pw-cmp", `pw:${String(expectedPw ?? "")}`);
  return timingSafeEqualStr(h1, h2);
}

function getSigningSeed(env) {
  const sec = String(env.COOKIE_SECRET || env.SA_JSON || "h3-default-seed");
  const pw = String(env.VIEW_PASSWORD || "");
  return `${sec}|h3ro|${pw}`;
}

async function makeGuestToken(env) {
  const exp = Math.floor(Date.now() / 1000) + COOKIE_TTL_SEC;
  const sig = await hmacHex(getSigningSeed(env), String(exp));
  return `${exp}.${sig}`;
}

function parseCookie(header, name) {
  if (!header) return "";
  for (const part of header.split(";")) {
    const idx = part.indexOf("=");
    if (idx < 0) continue;
    if (part.slice(0, idx).trim() === name) return part.slice(idx + 1).trim();
  }
  return "";
}

async function isGuestAuthed(request, env) {
  const pw = String(env.VIEW_PASSWORD || "");
  if (!pw) return true;
  const tok = parseCookie(request.headers.get("Cookie") || "", COOKIE_NAME);
  if (!tok || !tok.includes(".")) return false;
  const [expStr, sig] = tok.split(".", 2);
  const exp = Number(expStr);
  if (!Number.isFinite(exp) || exp < Math.floor(Date.now() / 1000)) return false;
  const expected = await hmacHex(getSigningSeed(env), expStr);
  return timingSafeEqualStr(sig, expected);
}

function getClientIp(request) {
  return (
    request.headers.get("CF-Connecting-IP") ||
    (request.headers.get("X-Forwarded-For") || "").split(",")[0].trim() ||
    "unknown"
  );
}

function jsonResp(obj, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Robots-Tag": "noindex, nofollow",
      ...extraHeaders,
    },
  });
}

async function getDriveAccessToken(env, forceRefresh = false) {
  const rawSa = String(env.SA_JSON || "").trim();
  if (!rawSa) throw new Error("Missing SA_JSON secret");
  const sa = JSON.parse(rawSa);
  const nowSec = Math.floor(Date.now() / 1000);
  if (
    !forceRefresh &&
    saTokenCache.token &&
    saTokenCache.email === sa.client_email &&
    saTokenCache.expiry > nowSec + 60
  ) {
    return saTokenCache.token;
  }
  const tokenUri = sa.token_uri || "https://oauth2.googleapis.com/token";
  const header = b64urlStr(JSON.stringify({ alg: "RS256", typ: "JWT" }));
  const claims = b64urlStr(
    JSON.stringify({
      iss: sa.client_email,
      scope: SA_SCOPE,
      aud: tokenUri,
      iat: nowSec,
      exp: nowSec + 3600,
    })
  );
  const signingInput = `${header}.${claims}`;
  const cryptoKey = await crypto.subtle.importKey(
    "pkcs8",
    pemToDer(sa.private_key),
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sigBuf = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", cryptoKey, enc.encode(signingInput));
  const assertion = `${signingInput}.${b64urlBytes(new Uint8Array(sigBuf))}`;

  const r = await fetch(tokenUri, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer",
      assertion,
    }),
  });
  if (!r.ok) {
    const txt = await r.text().catch(() => "");
    throw new Error(`Google OAuth token failed (${r.status}): ${txt.slice(0, 200)}`);
  }
  const data = await r.json();
  if (!data.access_token) throw new Error("Google OAuth returned no access_token");
  saTokenCache = {
    token: data.access_token,
    expiry: nowSec + Number(data.expires_in || 3600),
    email: sa.client_email,
  };
  return saTokenCache.token;
}

async function driveFetch(env, url, extraHeaders = {}, retry = true) {
  const token = await getDriveAccessToken(env, false);
  const headers = new Headers(extraHeaders);
  headers.set("Authorization", `Bearer ${token}`);
  const r = await fetch(url, { method: "GET", headers });
  if (r.status === 401 && retry) {
    await getDriveAccessToken(env, true);
    return driveFetch(env, url, extraHeaders, false);
  }
  return r;
}

async function loadXItems(env, force = false) {
  const folderId = String(env.X_FOLDER_ID || "").trim();
  if (!folderId) throw new Error("Missing X_FOLDER_ID");
  const ttlSec = Math.max(15, Number(env.META_TTL_SECONDS || 60) || 60);
  const now = Date.now();
  const ageSec = (now - metaCache.loadedAt) / 1000;

  if (metaCache.loadedAt > 0 && metaCache.folderId === folderId) {
    if (!force && ageSec < ttlSec) return metaCache.items;
    if (force && ageSec < 15) return metaCache.items;
  }

  let metaFileId = metaCache.folderId === folderId ? metaCache.metaFileId : "";
  if (!metaFileId) {
    const q = new URLSearchParams({
      q: `name = '${X_META_NAME}' and '${folderId}' in parents and trashed = false`,
      fields: "files(id)",
      supportsAllDrives: "true",
      includeItemsFromAllDrives: "true",
      corpora: "allDrives",
    });
    const rList = await driveFetch(env, `${DRIVE_API}/files?${q.toString()}`);
    if (!rList.ok) throw new Error(`Drive list failed (${rList.status})`);
    const dList = await rList.json();
    metaFileId = dList.files?.[0]?.id || "";
  }

  let items = [];
  if (metaFileId) {
    const rMeta = await driveFetch(env, `${DRIVE_API}/files/${encodeURIComponent(metaFileId)}?alt=media&supportsAllDrives=true`);
    if (rMeta.status === 404 && metaCache.metaFileId) {
      metaCache.metaFileId = "";
      return loadXItems(env, true);
    }
    if (!rMeta.ok) throw new Error(`Drive meta read failed (${rMeta.status})`);
    const raw = await rMeta.json();
    items = Array.isArray(raw) ? raw : (raw.items || []);
  }

  const allowedIds = new Set();
  for (const it of items) {
    if (!it || typeof it !== "object") continue;
    for (const im of (it.images || [])) {
      if (im && typeof im === "object" && im.id) allowedIds.add(String(im.id));
    }
  }

  metaCache = {
    loadedAt: Date.now(),
    folderId,
    metaFileId,
    items,
    allowedIds,
  };
  return items;
}

async function isAllowedFileId(env, fid) {
  const ttlSec = Math.max(15, Number(env.META_TTL_SECONDS || 60) || 60);
  const expired = !metaCache.loadedAt || (Date.now() - metaCache.loadedAt) / 1000 >= ttlSec;
  if (!metaCache.loadedAt || (expired && !metaCache.allowedIds.has(fid))) {
    await loadXItems(env, false);
  }
  return metaCache.allowedIds.has(fid);
}

async function handleThumb(env, fid, sizeRaw) {
  const size = Math.max(100, Math.min(Number(sizeRaw || 400) || 400, 1600));
  const now = Date.now();
  let link = "";
  const cached = thumbLinkCache.get(fid);
  if (cached && cached.expiry > now) {
    link = cached.link;
  } else {
    const r = await driveFetch(
      env,
      `${DRIVE_API}/files/${encodeURIComponent(fid)}?fields=thumbnailLink&supportsAllDrives=true`
    );
    if (!r.ok) return new Response("Not Found", { status: r.status === 404 ? 404 : 502 });
    const d = await r.json();
    link = d.thumbnailLink || "";
    if (!link) return new Response("Not Found", { status: 404 });
    if (thumbLinkCache.size > 500) {
      const firstKey = thumbLinkCache.keys().next().value;
      if (firstKey) thumbLinkCache.delete(firstKey);
    }
    thumbLinkCache.set(fid, { link, expiry: now + 25 * 60 * 1000 });
  }
  const sizedUrl = link.replace(/=s\d+$/, "") + `=s${size}`;
  let imgResp = await driveFetch(env, sizedUrl);
  if (!imgResp.ok && cached) {
    thumbLinkCache.delete(fid);
    return handleThumb(env, fid, size);
  }
  if (!imgResp.ok) return new Response("Not Found", { status: 404 });
  const hasPw = Boolean(String(env.VIEW_PASSWORD || ""));
  return new Response(imgResp.body, {
    status: 200,
    headers: {
      "Content-Type": imgResp.headers.get("Content-Type") || "image/jpeg",
      "Cache-Control": hasPw ? "private, max-age=86400" : "public, max-age=86400",
      "X-Robots-Tag": "noindex, nofollow",
    },
  });
}

async function handleMedia(request, env, fid) {
  const fwdHeaders = {};
  const rangeHdr = request.headers.get("Range");
  if (rangeHdr) fwdHeaders["Range"] = rangeHdr;
  const r = await driveFetch(
    env,
    `${DRIVE_API}/files/${encodeURIComponent(fid)}?alt=media&supportsAllDrives=true`,
    fwdHeaders
  );
  if (r.status === 416) {
    return new Response(null, { status: 416, headers: { "Content-Length": "0" } });
  }
  if (!r.ok && r.status !== 206) {
    return new Response("Upstream media error", { status: r.status === 404 ? 404 : 502 });
  }
  const hasPw = Boolean(String(env.VIEW_PASSWORD || ""));
  const outHeaders = new Headers({
    "Accept-Ranges": "bytes",
    "Content-Disposition": "inline",
    "Cache-Control": hasPw ? "private, max-age=3600" : "public, max-age=3600",
    "X-Robots-Tag": "noindex, nofollow",
  });
  for (const k of ["Content-Type", "Content-Length", "Content-Range"]) {
    const v = r.headers.get(k);
    if (v) outHeaders.set(k, v);
  }
  return new Response(r.body, { status: r.status, headers: outHeaders });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method.toUpperCase();

    try {
      if (method === "GET" || method === "HEAD") {
        if (path === "/") {
          return new Response(method === "HEAD" ? null : indexHtml, {
            status: 200,
            headers: {
              "Content-Type": "text/html; charset=utf-8",
              "Cache-Control": "no-cache",
              "X-Robots-Tag": "noindex, nofollow",
            },
          });
        }
        if (path === "/favicon.svg" || path === "/favicon.ico") {
          return new Response(method === "HEAD" ? null : faviconSvg, {
            status: 200,
            headers: {
              "Content-Type": "image/svg+xml",
              "Cache-Control": "public, max-age=86400",
            },
          });
        }
        if (path === "/api/status") {
          const pwReq = Boolean(String(env.VIEW_PASSWORD || ""));
          const authed = await isGuestAuthed(request, env);
          return jsonResp({
            readonly: true,
            password_required: pwReq,
            guest_authenticated: authed,
            has_credentials: true,
            authorized: true,
            folder_id: "x-readonly",
            folder_name: "𝕏 灵感库",
            title: String(env.SITE_TITLE || "𝕏 灵感库").trim() || "𝕏 灵感库",
          });
        }
        if (path === "/api/x/items") {
          if (!(await isGuestAuthed(request, env))) {
            return jsonResp({ error: "guest_auth_required" }, 401);
          }
          const items = await loadXItems(env, url.searchParams.has("refresh"));
          return jsonResp(items);
        }
        const mThumb = path.match(/^\/thumb\/([A-Za-z0-9_-]+)$/);
        if (mThumb) {
          if (!(await isGuestAuthed(request, env))) {
            return jsonResp({ error: "guest_auth_required" }, 401);
          }
          if (!(await isAllowedFileId(env, mThumb[1]))) {
            return new Response("Forbidden", { status: 403 });
          }
          return handleThumb(env, mThumb[1], url.searchParams.get("s"));
        }
        const mMedia = path.match(/^\/media\/([A-Za-z0-9_-]+)$/);
        if (mMedia) {
          if (!(await isGuestAuthed(request, env))) {
            return jsonResp({ error: "guest_auth_required" }, 401);
          }
          if (!(await isAllowedFileId(env, mMedia[1]))) {
            return new Response("Forbidden", { status: 403 });
          }
          return handleMedia(request, env, mMedia[1]);
        }
        return new Response("Forbidden", { status: 403 });
      }

      if (method === "POST") {
        if (request.headers.get("X-H3") !== "1") {
          return new Response("Forbidden", { status: 403 });
        }
        const isHttps = url.protocol === "https:";
        const secFlag = isHttps ? "; Secure" : "";

        if (path === "/api/guest_login") {
          const expectedPw = String(env.VIEW_PASSWORD || "");
          if (!expectedPw) return jsonResp({ ok: true });

          const ip = getClientIp(request);
          const now = Date.now();
          const prev = (loginFails.get(ip) || []).filter(t => now - t < 300000);
          loginFails.set(ip, prev);
          if (prev.length >= 15) {
            return jsonResp({ error: "密码错误次数过多，请 5 分钟后再试" }, 429);
          }

          let body = {};
          try {
            body = await request.json();
          } catch (_) {}
          const ok = await passwordEquals(body.password, expectedPw, env.COOKIE_SECRET);
          if (!ok) {
            prev.push(now);
            loginFails.set(ip, prev);
            return jsonResp({ error: "密码错误，请重试" }, 403);
          }
          loginFails.delete(ip);
          const tok = await makeGuestToken(env);
          const cookie = `${COOKIE_NAME}=${tok}; Path=/; Max-Age=${COOKIE_TTL_SEC}; HttpOnly; SameSite=Lax${secFlag}`;
          return jsonResp({ ok: true }, 200, { "Set-Cookie": cookie });
        }

        if (path === "/api/guest_logout") {
          const cookie = `${COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax${secFlag}`;
          return jsonResp({ ok: true }, 200, { "Set-Cookie": cookie });
        }

        return new Response("Forbidden", { status: 403 });
      }

      return new Response("Forbidden", { status: 403 });
    } catch (e) {
      return jsonResp({ error: String(e?.message || e || "Internal error") }, 502);
    }
  },
};
