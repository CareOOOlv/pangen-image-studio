/* Pangen 生图工作台前端逻辑 */
"use strict";

const $ = (s) => document.querySelector(s);
const MODE_INPAINT_MIN = "gpt-image-2.5"; // 局部改图仅 2.5+ 模型

let state = {
  mode: "t2i",
  models: [],
  refs: [],          // 图生图参考图 File[]
  baseImage: null,   // 局部改图底图 File
  baseBitmap: null,  // HTMLImageElement
  busy: false,
};

/* ---------- 访问口令 ---------- */
const CODE_KEY = "pangen-code";
const getCode = () => localStorage.getItem(CODE_KEY) || "";

function authFetch(url, opts = {}) {
  opts.headers = Object.assign({}, opts.headers, { "X-Access-Code": getCode() });
  return fetch(url, opts);
}
const mediaUrl = (f) => `/media/${f}?t=${encodeURIComponent(getCode())}`;

function showGate(errMsg) {
  const gate = $("#gate");
  gate.hidden = false;
  $("#gate-err").textContent = errMsg || "";
  const input = $("#gate-code");
  input.value = "";
  setTimeout(() => input.focus(), 50);
}

function askGate(errMsg) {
  return new Promise((resolve) => {
    showGate(errMsg);
    const input = $("#gate-code");
    const submit = $("#gate-submit");
    const handler = () => {
      const v = input.value.trim();
      if (!v) return;
      submit.removeEventListener("click", handler);
      input.removeEventListener("keydown", onKey);
      $("#gate-err").textContent = "";
      resolve(v);
    };
    const onKey = (e) => { if (e.key === "Enter") handler(); };
    submit.addEventListener("click", handler);
    input.addEventListener("keydown", onKey);
  });
}

async function boot() {
  let code = getCode();
  for (;;) {
    if (!code) code = await askGate();
    try {
      const r = await fetch("/api/models", { headers: { "X-Access-Code": code } });
      if (r.status === 401) { code = await askGate("口令错误，请重试"); continue; }
      if (!r.ok) { code = await askGate("服务异常（" + r.status + "），请重试"); continue; }
      localStorage.setItem(CODE_KEY, code);
      break;
    } catch {
      code = await askGate("无法连接服务，请稍后重试");
    }
  }
  $("#gate").hidden = true;
  init();
}

/* ---------- 初始化 ---------- */
async function init() {
  state.defaultModel = "gpt-image-1.5";
  try {
    const r = await authFetch("/api/models");
    const data = await r.json();
    state.models = data.models;
    if (data.default) state.defaultModel = data.default;
  } catch {
    state.models = [
      { id: "gpt-image-1.5", label: "GPT-Image 1.5", inpaint: false },
      { id: "gpt-image-2.5-flare", label: "GPT-Image 2.5 Flare", inpaint: true },
    ];
  }
  const sel = $("#model");
  sel.innerHTML = state.models
    .map((m) => `<option value="${m.id}">${m.label}</option>`)
    .join("");
  sel.value = state.defaultModel;
  refreshModelOptions();

  bindTabs();
  bindRefZone();
  bindInpaintZone();
  bindGenerate();
  bindLightbox();
  $("#refresh").addEventListener("click", loadTasks);
  loadTasks();
  setInterval(loadTasks, 12000);
}

/* ---------- 模式切换 ---------- */
function bindTabs() {
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => setMode(btn.dataset.mode));
  });
}

function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll(".tab").forEach((b) => {
    const on = b.dataset.mode === mode;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on);
  });
  $("#ref-zone").hidden = mode !== "i2i";
  $("#inpaint-zone").hidden = mode !== "inpaint";
  refreshModelOptions();
  if (mode === "inpaint") {
    // 自动切到 2.5+ 模型
    if (!modelSupportsInpaint($("#model").value)) {
      const m = state.models.find((x) => x.inpaint);
      if (m) $("#model").value = m.id;
    }
  }
  updatePlaceholder();
  updateGenerateState();
}

function modelSupportsInpaint(id) {
  const m = state.models.find((x) => x.id === id);
  return !!(m && m.inpaint);
}

/* 按当前模式刷新模型下拉：局部改图模式下禁用并标注不支持的模型 */
function refreshModelOptions() {
  const sel = $("#model");
  const current = sel.value;
  sel.innerHTML = state.models
    .map((m) => {
      let label = m.label;
      let disabled = false;
      if (state.mode === "inpaint" && !m.inpaint) {
        label += "（不支持局部改图）";
        disabled = true;
      }
      return `<option value="${m.id}"${disabled ? " disabled" : ""}>${label}</option>`;
    })
    .join("");
  // 恢复原选择；若原选择已被禁用则切到第一个可用模型
  const opt = sel.querySelector(`option[value="${current}"]`);
  if (opt && !opt.disabled) {
    sel.value = current;
  } else {
    const first = state.models.find((m) => state.mode !== "inpaint" || m.inpaint);
    if (first) sel.value = first.id;
  }
}

function updatePlaceholder() {
  const tips = {
    t2i: "文生图：写下提示词，直接生成",
    i2i: "图生图：上传 1~10 张参考图，描述如何融合或改绘",
    inpaint: "局部改图：上传底图，用画笔涂抹要重绘的区域（仅 2.5+ 模型）",
  };
  $(".stage .placeholder").innerHTML =
    `<div class="ph-icon">◐</div>${tips[state.mode]}`;
}

/* ---------- 图生图参考图 ---------- */
function bindRefZone() {
  const drop = $("#ref-drop"), input = $("#ref-input");
  drop.addEventListener("click", () => input.click());
  drop.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") input.click();
  });
  input.addEventListener("change", () => addRefs([...input.files]));
  ["dragover", "dragenter"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("drag"); }));
  drop.addEventListener("drop", (e) => addRefs([...e.dataTransfer.files]));
}

function addRefs(files) {
  const imgs = files.filter((f) => f.type.startsWith("image/"));
  state.refs = [...state.refs, ...imgs].slice(0, 10);
  renderRefs();
}

function renderRefs() {
  $("#ref-count").textContent = state.refs.length;
  $("#ref-thumbs").innerHTML = state.refs
    .map((f, i) => `<div class="thumb"><img src="${URL.createObjectURL(f)}" alt="">
      <button data-i="${i}" title="移除" aria-label="移除参考图${i + 1}">×</button></div>`)
    .join("");
  $("#ref-thumbs").querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => {
      state.refs.splice(+b.dataset.i, 1);
      renderRefs();
    }));
  updateGenerateState();
}

/* ---------- 局部改图：底图 + 蒙版 ---------- */
function bindInpaintZone() {
  const drop = $("#base-drop"), input = $("#base-input");
  drop.addEventListener("click", () => input.click());
  drop.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") input.click();
  });
  input.addEventListener("change", () => {
    if (input.files[0]) loadBaseImage(input.files[0]);
  });

  const canvas = $("#mask-canvas");
  const maskCtx = maskCanvas().getContext("2d");
  let drawing = false;

  canvas.addEventListener("pointerdown", (e) => {
    drawing = true;
    canvas.setPointerCapture(e.pointerId);
    stroke(e);
  });
  canvas.addEventListener("pointermove", (e) => drawing && stroke(e));
  canvas.addEventListener("pointerup", () => (drawing = false));

  function canvasPos(e) {
    const r = canvas.getBoundingClientRect();
    return [(e.clientX - r.left) * (canvas.width / r.width),
            (e.clientY - r.top) * (canvas.height / r.height)];
  }

  function stroke(e) {
    const [x, y] = canvasPos(e);
    const r = +$("#brush").value / 2;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "rgba(245, 158, 11, .5)";
    ctx.beginPath(); ctx.arc(x, y, r, 0, 7); ctx.fill();
    maskCtx.fillStyle = "#ffffff";
    maskCtx.beginPath(); maskCtx.arc(x, y, r, 0, 7); maskCtx.fill();
  }

  $("#mask-clear").addEventListener("click", () => {
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    maskCtx.clearRect(0, 0, canvas.width, canvas.height);
  });
}

function maskCanvas() {
  let c = document.getElementById("off-mask");
  if (!c) {
    c = document.createElement("canvas");
    c.id = "off-mask";
    c.style.display = "none";
    document.body.appendChild(c);
  }
  return c;
}

function loadBaseImage(file) {
  const img = new Image();
  img.onload = () => {
    state.baseImage = file;
    state.baseBitmap = img;
    const canvas = $("#mask-canvas");
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(img, 0, 0);
    maskCanvas().width = img.naturalWidth;
    maskCanvas().height = img.naturalHeight;
    $("#mask-editor").hidden = false;
    $("#base-drop").querySelector(".hint").textContent =
      `${file.name}（${img.naturalWidth}×${img.naturalHeight}）— 点击可更换`;
    updateGenerateState();
  };
  img.src = URL.createObjectURL(file);
}

function hasMaskStrokes() {
  const c = maskCanvas();
  if (!c.width) return false;
  const data = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
  for (let i = 3; i < data.length; i += 4) if (data[i] > 0) return true;
  return false;
}

/* ---------- 生成 ---------- */
function updateGenerateState() {
  const btn = $("#generate");
  let block = "";
  if (state.mode === "i2i" && state.refs.length === 0)
    block = "请先添加至少 1 张参考图";
  if (state.mode === "inpaint") {
    if (!state.baseImage) block = "请先上传底图";
    else if (!modelSupportsInpaint($("#model").value))
      block = "局部改图仅支持 2.5 及以上模型，请切换模型";
  }
  btn.disabled = !!block && !state.busy;
  btn.title = block;
  if (block) $(".status").textContent = "";
}

function bindGenerate() {
  $("#model").addEventListener("change", updateGenerateState);
  $("#generate").addEventListener("click", generate);
}

async function generate() {
  if (state.busy) return;
  const prompt = $("#prompt").value.trim();
  if (!prompt) { setStatus("请先填写提示词", "err"); $("#prompt").focus(); return; }
  if (state.mode === "i2i" && !state.refs.length)
    return setStatus("请先添加参考图", "err");
  if (state.mode === "inpaint" && !state.baseImage)
    return setStatus("请先上传底图", "err");

  state.busy = true;
  const btn = $("#generate");
  btn.disabled = true;
  btn.classList.add("loading");
  btn.textContent = "生成中…（通常 30~120 秒）";
  setStatus('<span class="spinner"></span>正在生成，请勿关闭页面…');

  try {
    let resp, task;
    const common = {
      prompt,
      model: $("#model").value,
      size: $("#size").value,
      n: $("#count").value,
      quality: $("#quality").value,
    };
    if (state.mode === "t2i") {
      resp = await authFetch("/api/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(common),
      });
    } else {
      const fd = new FormData();
      Object.entries(common).forEach(([k, v]) => fd.append(k, v));
      if (state.mode === "i2i") {
        state.refs.forEach((f) => fd.append("images", f));
      } else {
        fd.append("images", state.baseImage);
        const blob = await new Promise((res) => maskCanvas().toBlob(res, "image/png"));
        fd.append("mask", blob, "mask.png");
      }
      resp = await authFetch("/api/edit", { method: "POST", body: fd });
    }
    const data = await resp.json().catch(() => ({}));
    task = data.task;
    if (task && task.status === "done") {
      showResult(task);
      setStatus(`完成，耗时 ${task.duration}s`, "ok");
    } else {
      setStatus("生成失败：" + ((task && task.error) || data.error || "未知错误"), "err");
    }
  } catch (e) {
    setStatus("请求失败：" + e.message, "err");
  } finally {
    state.busy = false;
    btn.disabled = false;
    btn.classList.remove("loading");
    btn.textContent = "生 成";
    loadTasks();
  }
}

function setStatus(html, cls = "") {
  const el = $("#status");
  el.className = "status " + cls;
  el.innerHTML = html;
}

/* ---------- 结果展示 ---------- */
function showResult(task) {
  const imgs = task.images
    .map((f) => `<img src="${mediaUrl(f)}" alt="生成结果" loading="lazy">`)
    .join("");
  const chips = [
    `<span class="meta-chip">${task.model}</span>`,
    `<span class="meta-chip">${task.size}</span>`,
    `<span class="meta-chip">${task.duration}s</span>`,
  ];
  if (task.usage) chips.push(`<span class="meta-chip">${task.usage.total_tokens} tokens</span>`);
  task.images.forEach((f, i) => {
    const name = `pangen-${task.id}-${i + 1}.png`;
    chips.push(`<a class="meta-chip dl-chip" href="${mediaUrl(f)}" download="${name}">⬇ 保存图片${task.images.length > 1 ? " " + (i + 1) : ""}</a>`);
  });
  $("#stage").innerHTML =
    `<div class="result-multi">${imgs}</div>` +
    `<div class="result-meta">${chips.join("")}</div>`;
  $("#stage").querySelectorAll("img").forEach((img) =>
    img.addEventListener("click", () => openLightbox(img.src)));
}

/* ---------- 任务历史 ---------- */
async function loadTasks() {
  try {
    const r = await authFetch("/api/tasks");
    const { tasks } = await r.json();
    renderTasks(tasks);
  } catch { /* 服务未就绪时静默 */ }
}

function renderTasks(tasks) {
  const list = $("#task-list");
  const done = tasks.filter((t) => t.status === "done").length;
  $("#task-stats").innerHTML = tasks.length
    ? `记录 <b>${tasks.length}</b> · 成功 <b>${done}</b>`
    : "";
  if (!tasks.length) {
    list.innerHTML = '<div class="empty"><span class="ph-icon">◇</span>暂无任务</div>';
    return;
  }
  const modeName = { t2i: "文生图", i2i: "图生图", inpaint: "局部改图" };
  list.innerHTML = tasks
    .map((t) => {
      const thumb =
        t.status === "done" && t.images && t.images.length
          ? `<img class="timg" src="${mediaUrl(t.images[0])}" alt="">`
          : t.status === "running"
          ? `<div class="timg run-t"><span class="spinner"></span></div>`
          : `<div class="timg err-t">!</div>`;
      const time = new Date((t.created_at || 0) * 1000).toLocaleString("zh-CN", {
        month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
      });
      const errLine =
        t.status === "error"
          ? `<div class="tmeta warn" style="color:var(--danger)">${(t.error || "").slice(0, 60)}</div>`
          : "";
      return `<div class="task" data-id="${t.id}" tabindex="0" role="button"
        aria-label="查看任务${t.id}">
        ${thumb}
        <div class="tinfo">
          <div class="tprompt" title="${(t.prompt || "").replace(/"/g, "&quot;")}">${t.prompt || ""}</div>
          <div class="tmeta">
            <span class="badge ${t.status}">${modeName[t.mode] || t.mode} · ${
              t.status === "done" ? "完成" : t.status === "running" ? "进行中" : "失败"
            }</span>
            <span>${t.model}</span><span>${time}</span>
            ${t.duration ? `<span>${t.duration}s</span>` : ""}
          </div>
          ${errLine}
        </div>
        <button class="del" data-del="${t.id}" title="删除记录" aria-label="删除任务记录">×</button>
      </div>`;
    })
    .join("");

  list.querySelectorAll(".task").forEach((el) => {
    const id = el.dataset.id;
    const open = () => {
      const t = tasks.find((x) => x.id === id);
      if (t && t.status === "done") showResult(t);
    };
    el.addEventListener("click", (e) => {
      if (e.target.dataset.del) return;
      open();
    });
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter") open();
    });
  });
  list.querySelectorAll("[data-del]").forEach((b) =>
    b.addEventListener("click", async (e) => {
      e.stopPropagation();
      await authFetch("/api/tasks/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: b.dataset.del }),
      });
      loadTasks();
    }));
}

/* ---------- 灯箱 ---------- */
function bindLightbox() {
  const lb = $("#lightbox");
  lb.addEventListener("click", () => (lb.hidden = true));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") lb.hidden = true;
  });
}
function openLightbox(src) {
  const lb = $("#lightbox");
  lb.querySelector("img").src = src;
  lb.hidden = false;
}

boot();
