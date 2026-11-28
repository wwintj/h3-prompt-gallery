const H3_BASE = 'http://127.0.0.1:8765';
const $ = s => document.querySelector(s);

let clipData = null;
let domSnap = null;
let curMode = 'reply';

async function h3Api(path, body) {
  const res = await fetch(H3_BASE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-H3': '1' },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({ error: '本地 H3 画廊未返回有效响应' }));
  if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function showState(msg, isErr = false) {
  $('#formWrap').style.display = 'none';
  const box = $('#stateBox');
  box.style.display = 'block';
  box.className = 'state' + (isErr ? ' err' : '');
  box.innerHTML = msg;
}

function showMsg(html, isErr = false) {
  const box = $('#msgBox');
  box.style.display = 'block';
  box.className = 'msg ' + (isErr ? 'err' : 'ok');
  box.innerHTML = html;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// 注入到当前 X 页面执行的纯 DOM 提取器（只读取用户当前已打开的页面，不做任何绕过）
function extractXPageInTab() {
  const url = location.href;
  const m = url.match(/https?:\/\/(?:www\.|mobile\.)?(?:x\.com|twitter\.com)\/([^/?#]+)\/status\/(\d+)/i);
  if (!m) {
    return { ok: false, reason: '请先在 X (Twitter) 上点开具体某条帖子的详情页，再点击剪藏按钮。' };
  }
  const mainAuthor = m[1].toLowerCase();
  const mainTid = m[2];
  const mainUrl = `https://x.com/${m[1]}/status/${mainTid}`;

  const readRichText = el => {
    if (!el) return '';
    let out = '';
    const walk = node => {
      if (node.nodeType === 3) {
        out += node.textContent;
      } else if (node.nodeType === 1) {
        const tag = node.tagName;
        if (tag === 'IMG' && node.alt) out += node.alt;
        else if (tag === 'BR') out += '\n';
        else {
          for (const ch of node.childNodes) walk(ch);
        }
      }
    };
    walk(el);
    return out.trim();
  };

  // 获取单条 article 自身的规范状态链接（避开内嵌引用推文里的链接）
  const getOwnStatus = art => {
    const userName = art.querySelector('[data-testid="User-Name"]');
    const link = (userName && userName.querySelector('a[href*="/status/"]'))
      || (art.querySelector('time') && art.querySelector('time').closest('a[href*="/status/"]'));
    if (!link) return null;
    const href = link.getAttribute('href') || '';
    const sm = href.match(/^\/([^/?#]+)\/status\/(\d+)/i);
    if (!sm) return null;
    return { author: sm[1].toLowerCase(), rawAuthor: sm[1], tid: sm[2], url: `https://x.com/${sm[1]}/status/${sm[2]}` };
  };

  // 只取推文自己的正文节点（排除内嵌引用卡片里的 tweetText）
  const getOwnText = art => {
    const texts = [...art.querySelectorAll('[data-testid="tweetText"]')];
    for (const t of texts) {
      const quoteBox = t.closest('[role="link"]');
      if (quoteBox && art.contains(quoteBox)) continue;
      return readRichText(t);
    }
    return '';
  };

  const articles = [...document.querySelectorAll('article[data-testid="tweet"]')];
  let mainText = '';
  let mainMedia = [];
  const replyUrls = [];
  const replyTexts = [];
  const seenReplyTids = new Set([mainTid]);
  let foundMain = false;

  for (const art of articles) {
    const st = getOwnStatus(art);
    const txt = getOwnText(art);
    const isMain = (st && st.tid === mainTid) || (!foundMain && !st && articles.indexOf(art) === 0);

    if (isMain) {
      foundMain = true;
      mainText = txt;
      mainMedia = [...art.querySelectorAll('img[src*="pbs.twimg.com/media/"]')]
        .map(im => im.src)
        .filter(Boolean);
      continue;
    }

    // 主贴之后出现的同作者回复（即评论区里作者补充的提示词楼层）
    if (foundMain && st && st.author === mainAuthor && !seenReplyTids.has(st.tid)) {
      seenReplyTids.add(st.tid);
      replyUrls.push(st.url);
      replyTexts.push(txt);
    }
  }

  return {
    ok: true,
    main_url: mainUrl,
    main_author: m[1],
    main_tid: mainTid,
    main_text: mainText,
    reply_urls: replyUrls.slice(0, 5),
    reply_texts: replyTexts.slice(0, 5),
    fallback_media_urls: [...new Set(mainMedia)].slice(0, 8),
    article_count: articles.length,
  };
}

function updateCharCount() {
  const n = $('#promptIn').value.trim().length;
  $('#charCount').textContent = `${n} 字`;
}

function applyMode(mode) {
  curMode = mode;
  document.querySelectorAll('#modeSeg button').forEach(b => b.classList.toggle('on', b.dataset.mode === mode));
  if (!clipData) return;
  let txt = '';
  if (mode === 'reply') txt = clipData.reply_text || clipData.main_text || '';
  else if (mode === 'main') txt = clipData.main_text || '';
  else txt = [clipData.main_text, clipData.reply_text].filter(Boolean).join('\n\n---\n\n');
  $('#promptIn').value = txt;
  updateCharCount();
}

function renderClipUI(info) {
  clipData = info;
  $('#stateBox').style.display = 'none';
  $('#formWrap').style.display = 'block';
  $('#msgBox').style.display = 'none';
  $('#subStatus').textContent = info.main_url.replace(/^https?:\/\//, '');

  // 重复提示卡片
  const dup = info.duplicate;
  const dupCard = $('#dupCard');
  if (dup) {
    const dt = dup.created_at ? new Date(dup.created_at).toLocaleString('zh-CN', { hour12: false }) : '';
    dupCard.style.display = 'block';
    dupCard.innerHTML = `<div class="dup-title"><span>⚠️ ${esc(dup.reason)}</span><span style="font-weight:400;opacity:.85">${esc(dt)}</span></div>
      <div class="dup-snip">${esc(dup.prompt || '（已有记录无提示词）')}</div>`;
    $('#mergeBtn').style.display = '';
    $('#skipMediaWrap').style.display = '';
    $('#saveBtn').textContent = '仍然另存一条';
  } else {
    dupCard.style.display = 'none';
    $('#mergeBtn').style.display = 'none';
    $('#skipMediaWrap').style.display = 'none';
    $('#saveBtn').textContent = '保存到 H3 灵感库';
  }

  // 媒体与评论统计标签
  const pills = [];
  const ms = info.media_summary || [];
  const qms = info.quote_media_summary || [];
  const fmtMediaPill = (arr, prefix = '') => {
    const vc = arr.filter(x => x.kind === 'video').length;
    const ic = arr.length - vc;
    const out = [];
    if (vc) {
      const v0 = arr.find(x => x.kind === 'video');
      const resStr = v0 && v0.w && v0.h ? ` ${v0.w}×${v0.h}` : '';
      out.push(`<span class="pill ok">▶ ${prefix}${vc} 段最高清视频${resStr}</span>`);
    }
    if (ic) out.push(`<span class="pill ok">🖼 ${prefix}${ic} 张原图</span>`);
    return out;
  };
  pills.push(...fmtMediaPill(ms));
  if (!ms.length && qms.length) {
    pills.push(...fmtMediaPill(qms, '引用贴 '));
  }
  if (!ms.length && !qms.length && domSnap.fallback_media_urls.length) {
    pills.push(`<span class="pill ok">🖼 ${domSnap.fallback_media_urls.length} 张页面原图</span>`);
  }
  if (info.reply_count > 0) {
    pills.push(`<span class="pill ok">💬 已抓到 ${info.reply_count} 条作者评论</span>`);
  } else {
    pills.push(`<span class="pill">未发现作者评论</span>`);
  }
  $('#metaRow').innerHTML = pills.join('');

  // 主贴和引用贴同时都有媒体时，提供可勾选开关（默认勾选）
  const qWrap = $('#quoteWrap');
  if (ms.length && qms.length) {
    const qv = qms.filter(x => x.kind === 'video').length;
    const qi = qms.length - qv;
    const qDesc = [qv ? `${qv} 段视频` : '', qi ? `${qi} 张原图` : ''].filter(Boolean).join(' + ');
    $('#quoteLbl').textContent = `同时抓取引用帖子的 ${qDesc}`;
    $('#quoteChk').checked = true;
    qWrap.style.display = 'inline-flex';
  } else {
    qWrap.style.display = 'none';
  }

  // 若主贴提到了评论区但页面还没加载出作者评论，提示轻滚一下再点重新扫描
  const mentions = /评论区|评论里|留言区|楼下|提示词\s*[↓👇⬇️]|prompt\s*[↓👇⬇️]|in (?:the )?(?:comments|replies|thread)/i.test(info.main_text || '');
  const tip = $('#scrollTip');
  if (mentions && !info.reply_count) {
    tip.style.display = 'block';
    tip.textContent = '💡 主贴提到提示词在评论区，但当前页面尚未渲染出作者回复。请把 X 页面往下稍微滚动一下让评论加载出来，再点右上角「重新扫描」。';
  } else {
    tip.style.display = 'none';
  }

  // 模式切换栏（仅在主贴与评论都有内容时显示）
  const hasBoth = Boolean(info.main_text && info.reply_text);
  $('#modeRow').style.display = hasBoth ? 'flex' : 'none';
  applyMode(info.recommended_mode || (info.reply_text ? 'reply' : 'main'));
}

async function scanActiveTab() {
  showState('正在扫描当前 X 页面与评论区…');
  $('#subStatus').textContent = '扫描中…';
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.id || !/^https?:\/\/(?:www\.|mobile\.)?(?:x\.com|twitter\.com)\//i.test(tab.url || '')) {
      $('#subStatus').textContent = '不在 X 帖子页';
      return showState('请先打开一条 <b>X (Twitter)</b> 帖子详情页，再点击本插件剪藏。');
    }
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: extractXPageInTab,
    });
    const snap = results?.[0]?.result;
    if (!snap || !snap.ok) {
      $('#subStatus').textContent = '非帖子详情页';
      return showState(esc(snap?.reason || '请点开具体一条 X 帖子的详情页。'));
    }
    domSnap = snap;
    $('#subStatus').textContent = '正在展开长文与查重…';
    const info = await h3Api('/api/x/inspect_clip', {
      main_url: snap.main_url,
      reply_urls: snap.reply_urls,
      main_text: snap.main_text,
      reply_texts: snap.reply_texts,
    });
    renderClipUI(info);
  } catch (e) {
    $('#subStatus').textContent = '连接失败';
    const hint = /Failed to fetch|NetworkError/i.test(String(e.message || e))
      ? '无法连接本地 H3 画廊（<code>http://127.0.0.1:8765</code>），请确认 H3 画廊服务正在运行。'
      : esc(e.message || String(e));
    showState(hint, true);
  }
}

async function submitClip(mergeId = '') {
  if (!clipData || !domSnap) return;
  const prompt = $('#promptIn').value.trim();
  const saveBtn = $('#saveBtn');
  const mergeBtn = $('#mergeBtn');
  saveBtn.disabled = true;
  mergeBtn.disabled = true;
  const oldTxt = saveBtn.textContent;
  saveBtn.textContent = mergeId ? '合并保存中…' : '正在抓最高清媒体并存入 Drive…';
  $('#msgBox').style.display = 'none';
  try {
    const res = await h3Api('/api/x/clip', {
      main_url: clipData.main_url,
      reply_urls: domSnap.reply_urls,
      fallback_media_urls: domSnap.fallback_media_urls,
      prompt,
      tags: $('#tagsIn').value.trim(),
      notes: $('#notesIn').value.trim(),
      merge_into_id: mergeId,
      prompt_mode: mergeId ? 'replace' : 'replace',
      skip_media: Boolean(mergeId && $('#skipMediaChk').checked),
      include_quote: $('#quoteWrap').style.display === 'none' ? true : Boolean($('#quoteChk').checked),
    });
    const md = res.media_details || [];
    const resStr = md.length === 1 && md[0].width && md[0].height ? ` (${md[0].width}×${md[0].height})` : '';
    const actionTxt = res.merged ? '已合并补充到旧条目' : '已保存到 H3 𝕏 灵感库';
    const mediaTxt = md.length ? `，存入 ${md.length} 个最高清媒体${resStr}` : '';
    showMsg(`✅ ${actionTxt}${mediaTxt}！<a href="http://127.0.0.1:8765/#x" target="_blank" rel="noopener" style="margin-left:6px;color:inherit;font-weight:600">打开画廊查看 ↗</a>`);
    // 更新本地状态为已收藏，避免手抖点两次重复保存
    clipData.duplicate = {
      id: res.entry.id,
      reason: '刚刚已保存到 H3 灵感库',
      prompt: res.entry.prompt,
      created_at: res.entry.created_at,
    };
    $('#dupCard').style.display = 'block';
    $('#dupCard').innerHTML = `<div class="dup-title"><span>✅ 已收录进 H3 灵感库</span></div><div class="dup-snip">${esc(res.entry.prompt || '')}</div>`;
    $('#mergeBtn').style.display = '';
    $('#skipMediaWrap').style.display = '';
    saveBtn.textContent = '仍然另存一条';
  } catch (e) {
    showMsg('保存失败：' + esc(e.message || String(e)), true);
    saveBtn.textContent = oldTxt;
  } finally {
    saveBtn.disabled = false;
    mergeBtn.disabled = false;
  }
}

$('#modeSeg').addEventListener('click', e => {
  const b = e.target.closest('button[data-mode]');
  if (b) applyMode(b.dataset.mode);
});
$('#promptIn').addEventListener('input', updateCharCount);
$('#promptIn').addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
    e.preventDefault();
    if (clipData?.duplicate?.id) submitClip(clipData.duplicate.id);
    else submitClip('');
  }
});
$('#rescanBtn').onclick = scanActiveTab;
$('#saveBtn').onclick = () => submitClip('');
$('#mergeBtn').onclick = () => { if (clipData?.duplicate?.id) submitClip(clipData.duplicate.id); };

scanActiveTab();
