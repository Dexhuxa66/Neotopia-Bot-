const $ = (selector) => document.querySelector(selector);

const state = {
  bots: [],
  activeId: localStorage.getItem("neo.activeBot") || "",
  world: null,
  zoom: 1,
  offsetX: 20,
  offsetY: 20,
  baseTile: 24,
  dragging: false,
  dragX: 0,
  dragY: 0,
  startX: 0,
  startY: 0,
  previewPath: null,
};

const canvas = $("#mapCanvas");
const wrap = $("#canvasWrap");
const ctx = canvas.getContext("2d");

function activeBot() {
  return state.bots.find((bot) => bot.id === state.activeId) || null;
}

function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("error", error);
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => { node.textContent = ""; }, 3500);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
  return body;
}

function renderBotSelect() {
  const select = $("#botSelect");
  select.innerHTML = "";
  for (const bot of state.bots) {
    const option = document.createElement("option");
    option.value = bot.id;
    option.textContent = `${bot.name} · ${bot.status}`;
    select.appendChild(option);
  }
  if (state.bots.length) {
    if (!state.bots.some((bot) => bot.id === state.activeId)) state.activeId = state.bots[0].id;
    select.value = state.activeId;
    localStorage.setItem("neo.activeBot", state.activeId);
  }
  renderSummary();
}

function renderSummary() {
  const bot = activeBot();
  const node = $("#botSummary");
  if (!bot) {
    node.textContent = "Belum ada bot.";
    return;
  }
  const pos = bot.position;
  const tile = pos?.tile ? `(${pos.tile[0]}, ${pos.tile[1]})` : "-";
  node.innerHTML = `<strong>${escapeHtml(bot.name)}</strong>\n` +
    `status: ${escapeHtml(bot.status)}\n` +
    `world: ${escapeHtml(String(bot.world))}\n` +
    `pos: ${pos ? `${Math.round(pos.x)}, ${Math.round(pos.y)}` : "-"} tile ${tile}`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  }[char]));
}

async function refreshBots() {
  try {
    const result = await api("/bots");
    state.bots = result.bots || [];
    renderBotSelect();
    if (state.activeId) await refreshWorld();
  } catch (error) {
    toast(error.message, true);
  }
}

async function refreshWorld() {
  if (!state.activeId) return;
  try {
    state.world = await api(`/world/${encodeURIComponent(state.activeId)}`);
    renderDropItems();
    renderPickupItems();
    renderMap();
  } catch (error) {
    toast(error.message, true);
  }
}

function renderDropItems() {
  const select = $("#dropItemType");
  if (!select) return;
  const items = (state.world?.inventory || []).filter((item) =>
    item.count > 0 && !["FIST", "WRENCH"].includes(String(item.type).toUpperCase()));
  select.innerHTML = "";
  if (!items.length) {
    select.disabled = true;
    select.required = false;
    select.add(new Option("Inventory kosong", ""));
    return;
  }
  select.disabled = false;
  select.required = true;
  for (const item of items) {
    select.add(new Option(`${item.type} (${item.count})`, item.type));
  }
}

function renderPickupItems() {
  const select = $("#pickupId");
  if (!select) return;
  select.innerHTML = "";
  select.add(new Option("Cari item terdekat", ""));
  for (const item of state.world?.droppedItems || []) {
    const label = `${item.itemType || "item"} x${item.count} · ${item.id}`;
    select.add(new Option(label, item.id));
  }
}

async function action(action, payload) {
  if (!state.activeId) throw new Error("Pilih bot dulu");
  return api(`/bots/${encodeURIComponent(state.activeId)}/${action}`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

async function planPath(payload) {
  if (!state.activeId) throw new Error("Pilih bot dulu");
  return api(`/bots/${encodeURIComponent(state.activeId)}/planpath`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

function formObject(form) {
  const result = {};
  for (const [key, value] of new FormData(form).entries()) {
    result[key] = ["x", "y", "dx", "dy", "max", "delay"].includes(key) ? Number(value) : value;
  }
  return result;
}

function bindForms() {
  const forms = [
    ["#breakForm", "break"], ["#placeForm", "place"], ["#dropForm", "drop"],
    ["#pickupForm", "collect"], ["#msgForm", "msg"],
    ["#playerForm", "gotoplayer"], ["#moveForm", "move"],
  ];
  for (const [selector, actionName] of forms) {
    $(selector).addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        await action(actionName, formObject(event.currentTarget));
        toast(`${actionName} terkirim`);
      } catch (error) {
        toast(error.message, true);
      }
    });
  }
  const planForm = $("#planForm");
  const previewBtn = $("#planPreviewBtn");
  if (previewBtn && planForm) {
    previewBtn.addEventListener("click", async () => {
      try {
        const payload = formObject(planForm);
        const result = await planPath(payload);
        state.previewPath = result.path;
        renderMap();
        toast(result.path ? `Path ditemukan: ${result.path.length} step` : "Tidak ada path ke target", !result.path);
      } catch (error) {
        toast(error.message, true);
      }
    });
    planForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const payload = formObject(planForm);
        await action("findpath", payload);
        toast(`findpath ke (${payload.x}, ${payload.y}) terkirim`);
        state.previewPath = null;
        renderMap();
      } catch (error) {
        toast(error.message, true);
      }
    });
  }
}

function tileSize() { return state.baseTile * state.zoom; }

function screenToTile(clientX, clientY) {
  const rect = canvas.getBoundingClientRect();
  const x = (clientX - rect.left - state.offsetX) / tileSize();
  const y = (clientY - rect.top - state.offsetY) / tileSize();
  return { x: Math.floor(x), y: Math.floor(y) };
}

function tileToScreen(x, y) {
  return { x: state.offsetX + x * tileSize(), y: state.offsetY + y * tileSize() };
}

function resizeCanvas() {
  const rect = wrap.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * ratio));
  canvas.height = Math.max(1, Math.floor(rect.height * ratio));
  canvas.style.width = `${rect.width}px`;
  canvas.style.height = `${rect.height}px`;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  renderMap();
}

function blockColor(name, id) {
  const value = String(name || "").toUpperCase();
  if (!id || value === "AIR") return "#101821";
  if (value.includes("STONE") || value.includes("BEDROCK")) return "#596778";
  if (value.includes("GRASS")) return "#4d9862";
  if (value.includes("DIRT")) return "#946f4d";
  if (value.includes("TREE")) return "#735b42";
  return "#55718c";
}

function renderMap() {
  if (!ctx || !state.world) return;
  const rect = wrap.getBoundingClientRect();
  ctx.clearRect(0, 0, rect.width, rect.height);
  const width = state.world.worldSize?.width || 100;
  const height = state.world.worldSize?.height || 60;
  const size = tileSize();
  const blocks = state.world.blocks || {};
  const names = state.world.blockNames || {};

  ctx.save();
  ctx.strokeStyle = "#263544";
  ctx.lineWidth = 1;
  for (let x = 0; x <= width; x++) {
    const sx = state.offsetX + x * size;
    ctx.beginPath(); ctx.moveTo(sx, state.offsetY); ctx.lineTo(sx, state.offsetY + height * size); ctx.stroke();
  }
  for (let y = 0; y <= height; y++) {
    const sy = state.offsetY + y * size;
    ctx.beginPath(); ctx.moveTo(state.offsetX, sy); ctx.lineTo(state.offsetX + width * size, sy); ctx.stroke();
  }
  for (const [index, id] of Object.entries(blocks)) {
    const numeric = Number(index);
    const x = numeric % width;
    const y = Math.floor(numeric / width);
    if (!id || x < 0 || y < 0 || x >= width || y >= height) continue;
    const pos = tileToScreen(x, y);
    ctx.fillStyle = blockColor(names[String(id)], id);
    ctx.fillRect(pos.x + 1, pos.y + 1, Math.max(1, size - 2), Math.max(1, size - 2));
  }

  drawMarker(state.world.position, "#55c7a4", "B");
  for (const player of state.world.players || []) drawMarker(player, "#f2c36b", "P");
  drawPreviewPath();
  ctx.restore();
}

function drawPreviewPath() {
  if (!state.previewPath || state.previewPath.length === 0) return;
  const size = tileSize();

  ctx.save();
  ctx.strokeStyle = "#4fd2ff";
  ctx.lineWidth = 3;
  ctx.setLineDash([6, 4]);
  ctx.beginPath();
  const start = state.world.position ? state.world.position.tile : [0, 0];
  let first = start;
  if (first && typeof first[0] === "number") {
    const p = tileToScreen(first[0], first[1]);
    ctx.moveTo(p.x + size / 2, p.y + size / 2);
  }
  for (const [direction, kind, dest] of state.previewPath) {
    const p = tileToScreen(dest[0], dest[1]);
    ctx.lineTo(p.x + size / 2, p.y + size / 2);
  }
  ctx.stroke();
  ctx.setLineDash([]);

  for (const [i, [, kind, dest]] of state.previewPath.entries()) {
    const p = tileToScreen(dest[0], dest[1]);
    ctx.fillStyle = kind === "jump" ? "#ffb454" : kind === "drop" ? "#f26d6d" : "#4fd2ff";
    ctx.beginPath();
    ctx.arc(p.x + size / 2, p.y + size / 2, Math.max(4, size / 6), 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#071017";
    ctx.font = `${Math.max(8, size / 5)}px sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(String(i + 1), p.x + size / 2, p.y + size / 2);
  }
  ctx.restore();
}

function drawMarker(position, color, label) {
  if (!position) return;
  const x = position.x ?? 0;
  const y = position.y ?? 0;
  const pos = { x: state.offsetX + (x / 32) * tileSize(), y: state.offsetY + (y / 32) * tileSize() };
  const radius = Math.max(4, Math.min(8, tileSize() / 3));
  ctx.fillStyle = color;
  ctx.beginPath(); ctx.arc(pos.x + tileSize() / 2, pos.y + tileSize() / 2, radius, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = "#071017";
  ctx.font = `${Math.max(9, radius + 3)}px sans-serif`;
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillText(label, pos.x + tileSize() / 2, pos.y + tileSize() / 2);
}

function bindCanvas() {
  canvas.addEventListener("mousemove", (event) => {
    if (state.dragging) {
      state.offsetX += event.clientX - state.dragX;
      state.offsetY += event.clientY - state.dragY;
      state.dragX = event.clientX; state.dragY = event.clientY;
      renderMap();
      return;
    }
    const tile = screenToTile(event.clientX, event.clientY);
    const width = state.world?.worldSize?.width || 100;
    const id = state.world?.blocks?.[String(tile.y * width + tile.x)] || 0;
    const name = state.world?.blockNames?.[String(id)] || (id ? "unknown" : "AIR");
    $("#tileInfo").textContent = `tile (${tile.x}, ${tile.y}) · block ${name} · ID ${id}`;
  });
  canvas.addEventListener("pointerdown", (event) => {
    state.dragging = true;
    state.dragX = event.clientX; state.dragY = event.clientY;
    state.startX = event.clientX; state.startY = event.clientY;
    wrap.classList.add("dragging"); canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointerup", async (event) => {
    const moved = Math.abs(event.clientX - state.startX) + Math.abs(event.clientY - state.startY);
    state.dragging = false; wrap.classList.remove("dragging");
    canvas.releasePointerCapture(event.pointerId);
    if (moved > 5) return;
    const tile = screenToTile(event.clientX, event.clientY);
    try { await action("findpath", tile); toast(`findpath ke (${tile.x}, ${tile.y}) terkirim`); }
    catch (error) { toast(error.message, true); }
  });
  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    const before = screenToTile(event.clientX, event.clientY);
    const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15;
    state.zoom = Math.max(0.35, Math.min(4, state.zoom * factor));
    const rect = canvas.getBoundingClientRect();
    state.offsetX = event.clientX - rect.left - before.x * tileSize();
    state.offsetY = event.clientY - rect.top - before.y * tileSize();
    renderMap();
  }, { passive: false });
}

async function startOrStop(start) {
  if (!state.activeId) return toast("Pilih bot dulu", true);
  try {
    await api(`/bots/${encodeURIComponent(state.activeId)}/${start ? "start" : "stop"}`, { method: "POST" });
    toast(start ? "Bot dimulai" : "Bot dihentikan");
    await refreshBots();
  } catch (error) { toast(error.message, true); }
}

function connectRealtime() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/ws`);
  socket.onmessage = async (event) => {
    const data = JSON.parse(event.data);
    if (data.type !== "snapshot") return;
    state.bots = data.bots || [];
    renderBotSelect();
    await refreshWorld();
  };
  socket.onclose = () => window.setTimeout(connectRealtime, 1500);
}

$("#botSelect").addEventListener("change", async (event) => {
  state.activeId = event.target.value;
  localStorage.setItem("neo.activeBot", state.activeId);
  await refreshWorld();
});
$("#generateBtn").addEventListener("click", async () => {
  try { await api("/bots/generate", { method: "POST", body: JSON.stringify({ start: true }) }); await refreshBots(); toast("Guest dibuat"); }
  catch (error) { toast(error.message, true); }
});
$("#startBtn").addEventListener("click", () => startOrStop(true));
$("#stopBtn").addEventListener("click", () => startOrStop(false));

bindForms();
bindCanvas();
window.addEventListener("resize", resizeCanvas);
resizeCanvas();
refreshBots();
connectRealtime();
