/* ============================================================
   Pangen Studio · 对话式工作台前端
   - 口令鉴权（localStorage 记住）
   - 多会话本地存储
   - 多参考图上传（最多 6 张，前端压缩 + 序号 + 可删）
   - 调 /api/chat，服务端由 gpt-6-luna 决定文生图/改图
   ============================================================ */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const LS_CODE = 'pangen.code';
  const LS_SESS = 'pangen.sessions.v1';
  const LS_CUR = 'pangen.current.v1';
  const LS_PREF = 'pangen.prefs.v1';

  const state = {
    code: '',
    models: [],
    maxRefs: 6,
    defaultModel: 'gpt-image-2',
    chatModel: 'gpt-6-luna',
    model: '',
    size: '1024x1024',
    count: 1,
    refs: [],           // {id, blob, url, thumb}
    sessions: [],
    currentId: '',
    busy: false,
    abort: null,
    usage: null,
  };

  const SIZE_LABEL = {
    '1024x1024': '1:1',
    '1536x1024': '3:2',
    '1024x1536': '2:3',
  };
  const SIZE_RATIO = {
    '1024x1024': '1 / 1',
    '1536x1024': '3 / 2',
    '1024x1536': '2 / 3',
  };

  /* ---------------- 工具 ---------------- */
  const uid = () => Math.random().toString(36).slice(2, 10);
  const mediaUrl = (f) => '/media/' + encodeURIComponent(f) + '?t=' + encodeURIComponent(state.code);
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

  function hhmm(ts) {
    const d = new Date(ts || Date.now());
    return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2);
  }

  function dayLabel(ts) {
    const d = new Date(ts);
    const now = new Date();
    const same = (a, b) => a.toDateString() === b.toDateString();
    if (same(d, now)) return '今天';
    const y = new Date(now.getTime() - 864e5);
    if (same(d, y)) return '昨天';
    return (d.getMonth() + 1) + ' 月 ' + d.getDate() + ' 日';
  }

  function loadJSON(key, fallback) {
    try { return JSON.parse(localStorage.getItem(key)) ?? fallback; }
    catch { return fallback; }
  }

  function saveSessions() {
    try {
      localStorage.setItem(LS_SESS, JSON.stringify(state.sessions));
    } catch {
      // 配额超了：丢掉老会话里的图片再存一次
      state.sessions.forEach((s, i) => {
        if (i === 0) return;
        s.messages.forEach((m) => {
          m.imgs = [];
          if (m.meta) m.meta.prompt = '';
        });
      });
      try { localStorage.setItem(LS_SESS, JSON.stringify(state.sessions)); } catch {}
    }
  }

  /* ---------------- 口令 ---------------- */
  function showGate(msg) {
    $('gate').hidden = false;
    $('gate-err').textContent = msg || '';
    setTimeout(() => $('gate-code').focus(), 60);
  }

  async function api(path, opts = {}) {
    const o = { ...opts, headers: { ...(opts.headers || {}), 'X-Access-Code': state.code } };
    return fetch(path, o);
  }

  async function enter(code) {
    state.code = code;
    const r = await api('/api/models');
    if (r.status === 401) {
      state.code = '';
      localStorage.removeItem(LS_CODE);
      showGate('口令不对，请重新输入');
      return false;
    }
    const data = await r.json();
    state.models = data.models || [];
    state.maxRefs = data.max_refs || 6;
    state.defaultModel = data.default || state.models[0]?.id || 'gpt-image-2';
    state.chatModel = data.chat_model || 'gpt-6-luna';
    if (!state.models.some((m) => m.id === state.model)) {
      state.model = state.defaultModel;
    }
    localStorage.setItem(LS_CODE, code);
    $('gate').hidden = true;
    syncComposer();
    renderAll();
    refreshUsage();
    return true;
  }

  async function refreshUsage() {
    try {
      const r = await api('/api/usage');
      if (!r.ok) return;
      const d = await r.json();
      state.usage = d.usage;
      renderQuota();
    } catch {}
  }

  /* ---------------- 配额 ---------------- */
  function renderQuota() {
    const u = state.usage;
    const chip = $('quota-chip');
    if (!u) return;
    const unlimited = !u.limit || u.limit <= 0;
    $('quota-used').textContent = String(u.used);
    $('quota-limit').textContent = unlimited ? '∞' : String(u.limit);
    $('qc-num').textContent = unlimited ? (u.used + ' / ∞') : (u.used + ' / ' + u.limit);
    const bar = $('qc-bar');
    const pct = unlimited ? 0 : Math.min(100, Math.round((u.used / u.limit) * 100));
    bar.querySelector('i').style.width = unlimited ? '0%' : Math.max(2, pct) + '%';
    bar.classList.toggle('low', !unlimited && u.remaining <= Math.max(1, u.limit * 0.2));
    bar.classList.toggle('dead', !unlimited && u.remaining <= 0);
    chip.classList.toggle('warn', !unlimited && u.remaining <= Math.max(1, u.limit * 0.2));
    chip.classList.toggle('dead', !unlimited && u.remaining <= 0);
    $('qc-foot').textContent = unlimited
      ? '口令不限量 · 每天 0 点统计重置'
      : (u.remaining <= 0 ? '今日已用完，明天 0 点重置' : '剩 ' + u.remaining + ' 张 · 每天 0 点重置');
  }

  /* ---------------- 会话 ---------------- */
  function currentSession() {
    return state.sessions.find((s) => s.id === state.currentId) || null;
  }

  function newSession(silent) {
    const s = {
      id: uid(),
      title: '新对话',
      at: Date.now(),
      messages: [],
    };
    state.sessions.unshift(s);
    state.currentId = s.id;
    localStorage.setItem(LS_CUR, s.id);
    saveSessions();
    if (!silent) {
      renderSessions();
      renderStream();
      $('input').focus();
    }
    return s;
  }

  function titleOf(s) {
    return s.title || '新对话';
  }

  function renderSessions() {
    const box = $('sessions');
    if (!state.sessions.length) {
      box.innerHTML = '<div class="sess-label">还没有对话，点上面开始</div>';
      return;
    }
    let html = '';
    let lastDay = '';
    state.sessions.forEach((s) => {
      const d = dayLabel(s.at);
      if (d !== lastDay) {
        html += '<div class="sess-label">' + esc(d) + '</div>';
        lastDay = d;
      }
      html += '<button class="sess' + (s.id === state.currentId ? ' active' : '') +
        '" data-id="' + s.id + '"><span>' + esc(titleOf(s)) + '</span>' +
        '<i class="del" data-del="' + s.id + '" title="删除">✕</i></button>';
    });
    box.innerHTML = html;
  }

  /* ---------------- 渲染对话流 ---------------- */
  function renderAll() {
    renderSessions();
    renderStream();
  }

  function renderStream() {
    const wrap = $('stream-inner');
    const s = currentSession();
    if (!s || (!s.messages.length && !state.busy)) {
      wrap.innerHTML = heroHTML();
      bindHero();
      return;
    }
    wrap.innerHTML = s.messages.map(msgHTML).join('') +
      (state.busy ? pendingHTML() : '');
    bindStream();
    scrollBottom();
  }

  function heroHTML() {
    return `
      <div class="hero">
        <div class="mark">◐</div>
        <h1>说一句话，图就出来了</h1>
        <p>发文字直接出图；发图片（最多 6 张）让我按你的描述改图或融合。</p>
        <div class="sub">当前对话模型 ${esc(state.chatModel)} · 出图模型 ${esc(modelLabel(state.model))}</div>
        <div class="chips">
          <button class="chip" data-fill="按这个场景图出一版：冬季拼图地垫，儿童房木地板，暖光，俯拍，电商主图风格">冬季拼图地垫场景图</button>
          <button class="chip" data-fill="把这张图改成纯白背景的商品主图，保留商品本体和细节，去掉杂乱的背景和阴影">改成白底主图</button>
          <button class="chip" data-fill="换成北欧家居场景，浅色墙、木质地板、自然侧光，商品放在画面中心偏下">换成北欧家居场景</button>
          <button class="chip" data-fill="出一张 3:2 横版 banner，左边留白放文案，右边放商品，色调统一为暖米色">横版 banner</button>
        </div>
      </div>`;
  }

  function modelLabel(id) {
    const m = state.models.find((x) => x.id === id);
    return m ? m.label : (id || 'GPT-Image 2');
  }

  function msgHTML(m) {
    if (m.role === 'user') {
      const imgs = (m.imgs || []).length
        ? '<div class="msg-imgs">' + m.imgs.map((u) =>
            '<img src="' + esc(u) + '" alt="参考图">').join('') + '</div>'
        : '';
      return `<div class="msg user">
        ${imgs}
        ${m.text ? '<div class="bubble">' + esc(m.text) + '</div>' : ''}
        <div class="time">${hhmm(m.at)}</div>
      </div>`;
    }
    return `<div class="msg assistant">
      <div class="assistant-row">
        <div class="avatar">◐</div>
        <div class="assistant-body">
          ${m.text ? '<div class="bubble">' + esc(m.text) + '</div>' : ''}
          ${m.error ? noticeHTML('err', '生成失败', m.error) : ''}
          ${m.quota ? noticeHTML('run', '额度不足', m.quota) : ''}
          ${(m.images || []).length ? resultHTML(m) : ''}
        </div>
      </div>
      <div class="time">${hhmm(m.at)}</div>
    </div>`;
  }

  function noticeHTML(kind, title, text) {
    return `<div class="notice ${kind}">
      <div><div class="n-title">${esc(title)}</div><div>${esc(text)}</div></div>
    </div>`;
  }

  function resultHTML(m) {
    const n = Math.min(4, m.images.length);
    const ratio = SIZE_RATIO[m.meta?.size] || '1 / 1';
    const cells = m.images.map((f, i) => `
      <div class="ph" style="aspect-ratio:${n === 1 ? ratio : '1 / 1'}">
        <img src="${esc(mediaUrl(f))}" alt="生成结果 ${i + 1}" data-full="${esc(f)}" loading="lazy">
      </div>`).join('');
    const meta = [];
    if (m.meta?.model) meta.push(modelLabel(m.meta.model));
    if (m.meta?.size) meta.push(SIZE_LABEL[m.meta.size] || m.meta.size);
    if (m.images.length) meta.push(m.images.length + ' 张');
    if (m.meta?.duration) meta.push(m.meta.duration + 's');
    return `<div class="result-card">
      <div class="result-grid n${n}">${cells}</div>
      <div class="meta">
        ${meta.map((t) => '<span class="tag">' + esc(t) + '</span>').join('')}
        <span style="margin-left:auto;display:flex;gap:8px">
          ${m.meta?.prompt ? '<button class="act" data-copy="' + esc(m.id) + '">复制 prompt</button>' : ''}
        </span>
      </div>
      <div class="actions">
        <button class="act" data-dl="${esc(m.id)}">⤓ 下载全部</button>
        <button class="act" data-again="${esc(m.id)}">↻ 再来一张</button>
        <button class="act" data-open="${esc(m.id)}">⤢ 打开原图</button>
      </div>
    </div>`;
  }

  function pendingHTML() {
    return `<div class="msg assistant">
      <div class="assistant-row">
        <div class="avatar">◐</div>
        <div class="assistant-body">
          <div class="skeleton-card">
            <div class="sk-img"></div>
            <div class="sk-line"></div>
            <div class="sk-line w60"></div>
            <div class="sk-foot">
              <span>${esc(state.chatModel)} 正在理解需求…</span>
              <button class="act" id="cancel-btn">取消</button>
            </div>
          </div>
        </div>
      </div>
    </div>`;
  }

  function bindHero() {
    $('stream-inner').querySelectorAll('.chip').forEach((b) => {
      b.addEventListener('click', () => {
        $('input').value = b.dataset.fill || '';
        autoSize();
        syncComposer();
        $('input').focus();
      });
    });
  }

  function bindStream() {
    const root = $('stream-inner');
    root.querySelectorAll('[data-full]').forEach((img) => {
      img.addEventListener('click', () => openLightbox(mediaUrl(img.dataset.full)));
    });
    root.querySelectorAll('[data-dl]').forEach((b) => {
      b.addEventListener('click', () => downloadAll(b.dataset.dl));
    });
    root.querySelectorAll('[data-open]').forEach((b) => {
      b.addEventListener('click', () => {
        const m = findMsg(b.dataset.open);
        if (m?.images?.length) openLightbox(mediaUrl(m.images[0]));
      });
    });
    root.querySelectorAll('[data-again]').forEach((b) => {
      b.addEventListener('click', () => again(b.dataset.again));
    });
    root.querySelectorAll('[data-copy]').forEach((b) => {
      b.addEventListener('click', () => {
        const m = findMsg(b.dataset.copy);
        if (m?.meta?.prompt) {
          navigator.clipboard?.writeText(m.meta.prompt);
          b.textContent = '已复制';
          setTimeout(() => { b.textContent = '复制 prompt'; }, 1400);
        }
      });
    });
    $('cancel-btn')?.addEventListener('click', abortRun);
  }

  function findMsg(id) {
    const s = currentSession();
    return s?.messages.find((m) => m.id === id);
  }

  function scrollBottom() {
    const st = $('stream');
    requestAnimationFrame(() => { st.scrollTop = st.scrollHeight; });
  }

  /* ---------------- 灯箱 ---------------- */
  function openLightbox(src) {
    const lb = $('lightbox');
    lb.querySelector('img').src = src;
    lb.hidden = false;
  }

  function downloadAll(msgId) {
    const m = findMsg(msgId);
    if (!m?.images?.length) return;
    m.images.forEach((f, i) => {
      setTimeout(() => {
        const a = document.createElement('a');
        a.href = mediaUrl(f);
        a.download = f;
        document.body.appendChild(a);
        a.click();
        a.remove();
      }, i * 260);
    });
  }

  function again(msgId) {
    const s = currentSession();
    if (!s) return;
    const idx = s.messages.findIndex((m) => m.id === msgId);
    if (idx < 1) return;
    // 往上找最近一条用户消息
    for (let i = idx; i >= 0; i--) {
      const m = s.messages[i];
      if (m.role === 'user') {
        if ((m.imgs || []).length) {
          toast('这条是带参考图的，请重新上传图片后再试');
          return;
        }
        send(m.text);
        return;
      }
    }
  }

  function toast(text) {
    const el = document.createElement('div');
    el.className = 'notice run';
    el.style.cssText = 'position:fixed;left:50%;bottom:110px;transform:translateX(-50%);z-index:150;max-width:90vw';
    el.textContent = text;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 2600);
  }

  /* ---------------- 参考图 ---------------- */
  async function downscale(file, maxEdge, quality) {
    const bitmap = await createImageBitmap(file);
    const scale = Math.min(1, maxEdge / Math.max(bitmap.width, bitmap.height));
    const w = Math.max(1, Math.round(bitmap.width * scale));
    const h = Math.max(1, Math.round(bitmap.height * scale));
    const cv = document.createElement('canvas');
    cv.width = w; cv.height = h;
    const ctx = cv.getContext('2d');
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, w, h);
    ctx.drawImage(bitmap, 0, 0, w, h);
    bitmap.close?.();
    const blob = await new Promise((res) => cv.toBlob(res, 'image/jpeg', quality));
    return blob;
  }

  async function blobToDataURL(blob) {
    return new Promise((res) => {
      const fr = new FileReader();
      fr.onload = () => res(fr.result);
      fr.readAsDataURL(blob);
    });
  }

  async function addFiles(fileList) {
    const files = Array.from(fileList || []).filter((f) => f.type.startsWith('image/'));
    if (!files.length) return;
    let blocked = 0;
    for (const f of files) {
      if (state.refs.length >= state.maxRefs) { blocked++; continue; }
      try {
        const blob = await downscale(f, 1400, 0.9);
        const thumbBlob = await downscale(f, 256, 0.82);
        // 另存一张 640px 的「阅读版」：只给编排模型看，体积约原图 1/6，
        // 上游带宽紧张时能把读图耗时压下来，避免整轮超时
        const readBlob = await downscale(f, 640, 0.8);
        state.refs.push({
          id: uid(), blob, url: URL.createObjectURL(blob), read: readBlob,
          thumb: await blobToDataURL(thumbBlob),
        });
      } catch (e) {
        toast('有一张图片读不出来，换一张试试');
      }
    }
    if (blocked) toast('参考图最多 ' + state.maxRefs + ' 张，多出来的 ' + blocked + ' 张没有加进去');
    renderRefs();
    syncComposer();
  }

  function renderRefs() {
    const box = $('refs');
    if (!state.refs.length) { box.hidden = true; box.innerHTML = ''; return; }
    box.hidden = false;
    box.innerHTML = state.refs.map((r, i) => `
      <div class="ref">
        <img src="${esc(r.thumb)}" alt="参考图 ${i + 1}">
        <span class="idx">${i + 1}</span>
        <button class="rm" data-rm="${r.id}" title="移除">✕</button>
      </div>`).join('');
    box.querySelectorAll('[data-rm]').forEach((b) => {
      b.addEventListener('click', () => {
        const i = state.refs.findIndex((r) => r.id === b.dataset.rm);
        if (i >= 0) {
          URL.revokeObjectURL(state.refs[i].url);
          state.refs.splice(i, 1);
        }
        renderRefs();
        syncComposer();
      });
    });
  }

  /* ---------------- 输入区 ---------------- */
  function autoSize() {
    const t = $('input');
    t.style.height = 'auto';
    t.style.height = Math.min(168, t.scrollHeight) + 'px';
  }

  function syncComposer() {
    const hasText = $('input').value.trim().length > 0;
    const hasRef = state.refs.length > 0;
    $('send').disabled = state.busy || (!hasText && !hasRef);
    $('ref-count').textContent = state.refs.length + '/' + state.maxRefs;
    $('add-ref').disabled = state.refs.length >= state.maxRefs;
    $('add-ref').classList.toggle('on', hasRef);
    $('model-label').textContent = modelLabel(state.model);
    $('pill-size').firstChild.textContent = SIZE_LABEL[state.size] || state.size;
    $('pill-count').firstChild.textContent = state.count + ' 张';
  }

  /* ---------------- 弹层 ---------------- */
  let popTarget = null;

  function closePop() {
    $('pop').hidden = true;
    popTarget = null;
  }

  function openPop(anchor, items, title) {
    const pop = $('pop');
    pop.innerHTML = (title ? '<div class="pop-title">' + esc(title) + '</div>' : '') +
      items.map((it) => `<button data-v="${esc(it.value)}" class="${it.sel ? 'sel' : ''}">
        ${esc(it.label)}${it.hint ? '<small>' + esc(it.hint) + '</small>' : ''}</button>`).join('');
    pop.hidden = false;
    const r = anchor.getBoundingClientRect();
    const pr = pop.getBoundingClientRect();
    let left = r.left;
    if (left + pr.width > window.innerWidth - 12) left = window.innerWidth - pr.width - 12;
    let top = r.top - pr.height - 8;
    if (top < 64) top = r.bottom + 8;
    pop.style.left = Math.max(10, left) + 'px';
    pop.style.top = top + 'px';
    pop.querySelectorAll('button[data-v]').forEach((b) => {
      b.addEventListener('click', () => {
        const v = b.dataset.v;
        if (popTarget === 'model') { state.model = v; savePrefs(); }
        if (popTarget === 'size') { state.size = v; savePrefs(); }
        if (popTarget === 'count') { state.count = Number(v); savePrefs(); }
        closePop();
        syncComposer();
      });
    });
    popTarget = title;
    return pop;
  }

  function savePrefs() {
    try {
      localStorage.setItem(LS_PREF, JSON.stringify({
        model: state.model, size: state.size, count: state.count,
      }));
    } catch {}
  }

  /* ---------------- 发送 ---------------- */
  function abortRun() {
    state.abort?.abort();
  }

  async function send(textOverride) {
    if (state.busy) return;
    const text = (textOverride ?? $('input').value).trim();
    const refs = state.refs.slice();
    if (!text && !refs.length) return;

    let s = currentSession();
    if (!s) s = newSession(true);

    const userMsg = {
      id: uid(), role: 'user', at: Date.now(),
      text: text || '根据这些参考图帮我出图',
      imgs: refs.map((r) => r.thumb),
    };
    s.messages.push(userMsg);
    if (s.messages.length === 1) {
      s.title = (text || '图片改图').slice(0, 22);
      s.at = Date.now();
    }
    saveSessions();
    state.busy = true;
    $('input').value = '';
    autoSize();
    state.refs = [];
    renderRefs();
    syncComposer();
    renderSessions();
    renderStream();

    const history = s.messages
      .slice(0, -1)
      .filter((m) => m.role === 'user' || m.role === 'assistant')
      .slice(-10)
      .map((m) => ({ role: m.role, content: m.text || '' }))
      .filter((m) => m.content);

    const fd = new FormData();
    fd.append('message', userMsg.text);
    fd.append('model', state.model);
    fd.append('size', state.size);
    fd.append('n', String(state.count));
    fd.append('history', JSON.stringify(history));
    refs.forEach((r, i) => fd.append('images', r.blob, 'ref-' + (i + 1) + '.jpg'));
    refs.forEach((r, i) => { if (r.read) fd.append('thumbs', r.read, 'read-' + (i + 1) + '.jpg'); });

    state.abort = new AbortController();
    try {
      const r = await api('/api/chat', { method: 'POST', body: fd, signal: state.abort.signal });
      const data = await r.json().catch(() => ({}));
      const now = Date.now();
      // 后端失败时 error 可能是字符串（具体原因）或旧版的布尔 true，两种都得显示成人话
      const reason = typeof data.error === 'string' ? data.error.trim() : '';
      if (!r.ok) {
        if (r.status === 401) { state.busy = false; renderStream(); showGate('口令已失效，请重新输入'); return; }
        if (r.status === 429) {
          s.messages.push({ id: uid(), role: 'assistant', at: now, text: '', quota: data.error || '今日额度已用完' });
          state.usage = data.usage || state.usage;
        } else {
          s.messages.push({ id: uid(), role: 'assistant', at: now,
            text: data.reply || '',
            error: reason || (data.error ? '请求失败 HTTP ' + r.status : '') });
        }
      } else {
        const imgs = data.images || [];
        const t = (data.tasks || [])[0];
        const msg = {
          id: uid(), role: 'assistant', at: now,
          text: data.reply || '',
          images: imgs,
          meta: null,
        };
        if (imgs.length && t) {
          msg.meta = {
            model: t.model, size: t.size, duration: t.duration,
            prompt: t.prompt || '',
          };
        }
        if (data.quota) {
          msg.quota = data.reply || '今日额度已用完';
          msg.text = '';
        } else if (data.error && !imgs.length) {
          msg.error = reason || (t && typeof t.error === 'string' && t.error)
            || '生成失败，请稍后重试';
        }
        s.messages.push(msg);
        if (data.usage) state.usage = data.usage;
      }
      renderQuota();
    } catch (e) {
      const aborted = e.name === 'AbortError';
      s.messages.push({
        id: uid(), role: 'assistant', at: Date.now(), text: '',
        error: aborted
          ? '已停止等待。服务端可能仍在生成，若最终出图会占用额度，可在任务记录里查看。'
          : ('网络中断：' + (e.message || 'unknown')),
      });
    } finally {
      state.busy = false;
      state.abort = null;
      saveSessions();
      renderStream();
      syncComposer();
      refreshUsage();
    }
  }

  /* ---------------- 事件绑定 ---------------- */
  function bindEvents() {
    // 口令门
    $('gate-submit').addEventListener('click', async () => {
      const v = $('gate-code').value.trim();
      if (!v) return;
      $('gate-err').textContent = '';
      const ok = await enter(v);
      if (!ok) $('gate-code').value = '';
    });
    $('gate-code').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') $('gate-submit').click();
    });

    // 会话
    $('new-chat').addEventListener('click', () => newSession());
    $('sessions').addEventListener('click', (e) => {
      const del = e.target.closest('[data-del]');
      if (del) {
        e.stopPropagation();
        const id = del.dataset.del;
        state.sessions = state.sessions.filter((s) => s.id !== id);
        if (state.currentId === id) {
          state.currentId = state.sessions[0]?.id || '';
          if (!state.currentId) newSession(true);
        }
        saveSessions();
        renderAll();
        return;
      }
      const item = e.target.closest('[data-id]');
      if (item) {
        state.currentId = item.dataset.id;
        localStorage.setItem(LS_CUR, state.currentId);
        renderAll();
        closeSidebar();
      }
    });

    // 侧栏抽屉
    $('menu-btn').addEventListener('click', () => {
      $('sidebar').classList.add('open');
      $('scrim').hidden = false;
    });
    $('scrim').addEventListener('click', closeSidebar);

    // 输入
    const input = $('input');
    input.addEventListener('input', () => { autoSize(); syncComposer(); });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        if (!$('send').disabled) send();
      }
    });
    input.addEventListener('focus', () => $('input-box').classList.add('focus'));
    input.addEventListener('blur', () => $('input-box').classList.remove('focus'));
    $('send').addEventListener('click', () => send());

    // 参考图
    $('add-ref').addEventListener('click', () => $('file-input').click());
    $('file-input').addEventListener('change', async (e) => {
      await addFiles(e.target.files);
      e.target.value = '';
    });

    // 拖拽 + 粘贴
    const box = $('input-box');
    ['dragenter', 'dragover'].forEach((ev) => box.addEventListener(ev, (e) => {
      e.preventDefault(); box.classList.add('drag');
    }));
    ['dragleave', 'drop'].forEach((ev) => box.addEventListener(ev, (e) => {
      e.preventDefault(); box.classList.remove('drag');
    }));
    box.addEventListener('drop', (e) => {
      if (e.dataTransfer?.files?.length) addFiles(e.dataTransfer.files);
    });
    document.addEventListener('paste', (e) => {
      const items = e.clipboardData?.items;
      if (!items) return;
      const files = [];
      for (const it of items) {
        if (it.kind === 'file' && it.type.startsWith('image/')) {
          const f = it.getAsFile();
          if (f) files.push(f);
        }
      }
      if (files.length) { e.preventDefault(); addFiles(files); }
    });

    // 参数胶囊
    $('pill-model').addEventListener('click', (e) => {
      e.stopPropagation();
      if (popTarget === 'model') return closePop();
      openPop($('pill-model'), state.models.map((m) => ({
        value: m.id, label: m.label, sel: m.id === state.model,
        hint: m.id === state.defaultModel ? '默认' : '',
      })), 'model');
    });
    $('pill-size').addEventListener('click', (e) => {
      e.stopPropagation();
      if (popTarget === 'size') return closePop();
      openPop($('pill-size'), [
        { value: '1024x1024', label: '1:1 方图', sel: state.size === '1024x1024', hint: '1024' },
        { value: '1536x1024', label: '3:2 横图', sel: state.size === '1536x1024', hint: '1536×1024' },
        { value: '1024x1536', label: '2:3 竖图', sel: state.size === '1024x1536', hint: '1024×1536' },
      ], 'size');
    });
    $('pill-count').addEventListener('click', (e) => {
      e.stopPropagation();
      if (popTarget === 'count') return closePop();
      openPop($('pill-count'), [1, 2, 3, 4].map((n) => ({
        value: String(n), label: n + ' 张', sel: state.count === n,
      })), 'count');
    });
    document.addEventListener('click', () => closePop());
    $('pop').addEventListener('click', (e) => e.stopPropagation());

    // 灯箱
    $('lightbox').addEventListener('click', () => { $('lightbox').hidden = true; });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        $('lightbox').hidden = true;
        closePop();
        closeSidebar();
      }
    });

    // 口令切换
    $('who').addEventListener('click', () => {
      if (confirm('要换一个访问口令吗？当前对话会保留在本机。')) {
        localStorage.removeItem(LS_CODE);
        state.code = '';
        showGate('');
      }
    });
  }

  function closeSidebar() {
    $('sidebar').classList.remove('open');
    $('scrim').hidden = true;
  }

  function seedDemo() {
    const s = currentSession();
    if (!s || s.messages.length) return;
    s.title = '冬季拼图地垫场景图';
    s.messages = [
      { id: uid(), role: 'user', at: Date.now() - 42000,
        text: '按这个场景出一版：冬季拼图地垫，儿童房木地板，暖光，俯拍，电商主图风格',
        imgs: ['20261004-141619-1.png', '20261004-140836-1.png', '20261004-140649-1.png']
          .map(mediaUrl) },
      { id: uid(), role: 'assistant', at: Date.now() - 12000,
        text: '已按冬季儿童房场景出图，暖光俯拍，保留拼图纹理，可点击放大看细节。',
        images: state.demoImages && state.demoImages.length
          ? state.demoImages.slice(0, 4)
          : ['20261004-144254-1.png', '20261004-143828-1.png',
             '20261004-141619-1.png', '20261004-140836-1.png'],
        meta: { model: state.model || 'gpt-image-2', size: '1024x1024', duration: 48,
                prompt: 'Top-down shot of a winter-themed puzzle play mat in a kids room, warm light, e-commerce hero image, cozy rug, soft shadows, wooden floor' } },
    ];
    saveSessions();
  }

  /* ---------------- 启动 ---------------- */
  async function init() {
    const pref = loadJSON(LS_PREF, {});
    state.sessions = loadJSON(LS_SESS, []);
    state.currentId = localStorage.getItem(LS_CUR) || '';
    if (!state.sessions.length) {
      newSession(true);
    } else if (!state.sessions.find((s) => s.id === state.currentId)) {
      state.currentId = state.sessions[0].id;
    }
    if (pref.model) state.model = pref.model;
    if (pref.size) state.size = pref.size;
    if (pref.count) state.count = pref.count;

    bindEvents();
    autoSize();

    // 支持链接直接带口令：?t=xxx 或 #t=xxx
    const urlCode = new URLSearchParams(location.search).get('t')
      || (location.hash.startsWith('#t=') ? location.hash.slice(3) : '')
      || '';
    const code = urlCode || localStorage.getItem(LS_CODE) || '';
    if (code) {
      const ok = await enter(code);
      if (ok && new URLSearchParams(location.search).has('demo')) {
        seedDemo();
        renderAll();
        setTimeout(() => { const st = $('stream'); if (st) st.scrollTop = 0; }, 120);
      }
      if (ok && urlCode) {
        try { history.replaceState(null, '', location.pathname); } catch {}
      }
      if (!ok) renderAll();
      return;
    }
    showGate('');
    renderAll();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
