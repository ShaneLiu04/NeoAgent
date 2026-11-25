// NeoAgent Studio — front-end controller (zero dependencies)

const STAGES = ["intake", "build", "review", "pipeline", "release", "observe", "learn"];
const STAGE_LABELS = {
  intake: "需求接入", build: "编码", review: "检视", pipeline: "流水线",
  release: "发布", observe: "观测", learn: "演进",
};
const ZONE_COLORS = { smart: "var(--green)", warning: "var(--orange)", dumb: "var(--red)" };

const state = { loopId: null, role: "architect", mode: "attended", stage: "", streaming: false };

// ── Utilities ─────────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);

async function apiGet(path) {
  const res = await fetch(path);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function apiPost(path, body = {}) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const icon = (name, cls = "ic") => `<svg class="${cls}"><use href="#i-${name}" /></svg>`;

function tierBadge(tier) {
  const map = { low: "ok", medium: "warn", high: "danger" };
  const label = { low: "低风险", medium: "中风险", high: "高风险" }[tier] || tier;
  return `<span class="badge ${map[tier] || "info"}">${escapeHtml(label)}</span>`;
}

let toastTimer = null;
function toast(msg, kind = "ok") {
  const el = $("toast");
  el.innerHTML = `${icon(kind === "ok" ? "check" : "shield")}<span>${escapeHtml(msg)}</span>`;
  el.classList.remove("hidden");
  el.style.animation = "none";
  void el.offsetWidth;
  el.style.animation = "";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3200);
}

function renderMarkdownLite(raw) {
  const lines = escapeHtml(raw).split("\n");
  const out = [];
  let inList = false;
  const inline = (s) =>
    s.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  for (const ln of lines) {
    const h = ln.match(/^(#{1,4})\s+(.*)$/);
    const li = ln.match(/^\s*[-*]\s+(.*)$/);
    const bq = ln.match(/^&gt;\s?(.*)$/);
    if (li) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${inline(li[1])}</li>`);
      continue;
    }
    if (inList) { out.push("</ul>"); inList = false; }
    if (h) out.push(`<div class="md-h">${inline(h[2])}</div>`);
    else if (bq) out.push(`<blockquote>${inline(bq[1])}</blockquote>`);
    else if (ln.trim()) out.push(`<p>${inline(ln)}</p>`);
  }
  if (inList) out.push("</ul>");
  return out.join("");
}

// ── Theme ─────────────────────────────────────────────────────────────────────
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("btn-theme").innerHTML = icon(theme === "dark" ? "sun" : "moon");
  localStorage.setItem("neo-theme", theme);
}
const urlTheme = new URLSearchParams(location.search).get("theme");
applyTheme(
  urlTheme || localStorage.getItem("neo-theme") ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
);
$("btn-theme").addEventListener("click", () =>
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark")
);

// ── Navigation ────────────────────────────────────────────────────────────────
function showView(tab) {
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${tab}`));
  if (tab === "skills") loadSkills();
  if (tab === "harness") loadHarness();
  if (tab === "insights") loadMetrics();
  if (tab === "loops") refreshLoops();
}
document.querySelectorAll(".nav-item").forEach((btn) => btn.addEventListener("click", () => showView(btn.dataset.tab)));
document.querySelectorAll("[data-goto]").forEach((btn) => btn.addEventListener("click", () => showView(btn.dataset.goto)));

// ── Overview ──────────────────────────────────────────────────────────────────
async function refreshOverview() {
  try {
    const ov = await apiGet("/api/overview");
    $("hdr-version").textContent = `v${ov.version} · Agentic Engineering`;
    $("ws-name").textContent = ov.workspace;
    $("ws-skills").textContent = `${ov.skills_count} 项技能`;
    $("ws-llm").textContent = ov.llm_ready ? `${ov.provider} 已连接` : "本地 Harness 引擎";
    $("ws-llm-dot").classList.toggle("ok", !!ov.llm_ready);
    const roleModel = ["architect", "reviewer", "security_auditor"].includes(state.role) ? ov.powerful_model : ov.cheap_model;
    $("chip-model-text").textContent = ov.llm_ready ? roleModel : "local-harness";
    const badge = $("nav-active-loops");
    badge.textContent = ov.active_loops;
    badge.classList.toggle("hidden", !ov.active_loops);
    state.overview = ov;
  } catch (e) {
    console.error(e);
  }
}

// ── Loops ─────────────────────────────────────────────────────────────────────
async function refreshLoops() {
  const data = await apiGet("/api/loops");
  const list = $("rail-loops");
  const hasLoops = data.loops.length > 0;
  $("loop-empty").classList.toggle("hidden", hasLoops);
  $("loop-detail").classList.toggle("hidden", !hasLoops);
  if (!hasLoops) {
    list.innerHTML = `<div class="muted small" style="padding:6px 10px">暂无闭环</div>`;
    return;
  }
  if (!state.loopId || !data.loops.some((l) => l.id === state.loopId)) state.loopId = data.loops[0].id;
  list.innerHTML = data.loops
    .map(
      (lp) => `<div class="loop-cell ${lp.id === state.loopId ? "active" : ""}" data-lid="${escapeHtml(lp.id)}">
        <div class="loop-cell-title">${escapeHtml(lp.title)}</div>
        <div class="loop-cell-meta"><span class="tier-dot ${escapeHtml(lp.tier)}"></span>
          <span class="mono">${escapeHtml(lp.id)}</span><span>·</span><span>${escapeHtml(STAGE_LABELS[lp.stage] || lp.stage)}</span></div>
      </div>`
    )
    .join("");
  list.querySelectorAll(".loop-cell").forEach((el) =>
    el.addEventListener("click", () => {
      state.loopId = el.dataset.lid;
      showView("loops");
    })
  );
  await loadSelectedLoop();
}

function renderStepper(stage) {
  const terminal = stage === "done";
  const curIdx = terminal ? STAGES.length : STAGES.indexOf(stage);
  $("stage-track").innerHTML = STAGES.map((st, idx) => {
    let cls = "step";
    if (idx < curIdx) cls += " done";
    else if (idx === curIdx) cls += stage === "escalated" || stage === "rolled_back" ? " failed" : " current";
    const inner = idx < curIdx ? icon("check") : idx + 1;
    return `<div class="${cls}"><div class="step-dot">${inner}</div>
      <div class="step-label">${STAGE_LABELS[st]}</div><div class="step-en">${st.toUpperCase()}</div></div>`;
  }).join("");
}

async function loadSelectedLoop() {
  if (!state.loopId) return;
  const { loop, artifacts } = await apiGet(`/api/loops/${encodeURIComponent(state.loopId)}`);
  renderStepper(loop.stage);
  $("ld-id").textContent = loop.id;
  $("ld-title").textContent = loop.title;
  $("ld-tier").innerHTML = tierBadge(loop.tier);
  const route = (loop.evidence?.workflow_route?.track || "full").toUpperCase();
  const stageLabel = STAGE_LABELS[loop.stage] || loop.stage.toUpperCase();
  const metas = [
    ["阶段", stageLabel], ["模式", loop.mode === "unattended" ? "自治自愈" : "有人值守"], ["SDD 轨道", route],
    ["检视轮次", loop.review_rounds], ["CI 修复", loop.pipeline_fixes],
    ["AI 成本", `$${Number(loop.total_cost_usd || 0).toFixed(3)}`],
  ];
  $("ld-meta").innerHTML = metas.map(([k, v]) => `<span class="meta">${k} <b>${escapeHtml(v)}</b></span>`).join("");

  const findings = loop.findings || [];
  $("ld-findings").innerHTML = findings.length
    ? findings
        .map((f) => {
          const st = f.status === "open" ? (f.severity === "critical" || f.severity === "major" ? "danger" : "warn") : "ok";
          const stLabel = { open: "待修复", fixed: "已修复", waived: "已豁免" }[f.status] || f.status;
          return `<div class="row">
            <span class="sev-dot ${f.status === "open" ? escapeHtml(f.severity) : "fixed"}"></span>
            <div class="row-main">
              <div class="row-title"><code>${escapeHtml(f.rule)}</code>
                <span class="badge ${f.severity === "critical" || f.severity === "major" ? "danger" : "warn"}">${escapeHtml(f.severity)}</span>
                <span class="muted small mono">${escapeHtml(f.file || "diff")}:${f.line || 0}</span></div>
              <div class="row-sub">${escapeHtml(f.message)}</div>
            </div>
            <span class="badge ${st}">${escapeHtml(stLabel)}</span>
          </div>`;
        })
        .join("")
    : `<div class="row-empty">${icon("check", "ic ic-xs")} 当前无检视问题</div>`;

  const bnd = loop.evidence?.l1_boundary;
  $("insp-boundary").innerHTML = bnd
    ? `<div><b>${escapeHtml(bnd.role)}</b></div>${(bnd.non_goals || []).map((x) => `<div>· ${escapeHtml(x)}</div>`).join("")}`
    : `<div>风险 <b>${escapeHtml(loop.tier)}</b> · 基线 <b class="mono">${escapeHtml(loop.base)}</b></div>`;

  $("insp-skills").innerHTML = (loop.activated_skills || []).length
    ? loop.activated_skills.map((s) => `<span class="skill-chip">${escapeHtml(s)}</span>`).join("")
    : `<span class="muted small">无</span>`;

  const artEl = $("insp-artifacts");
  artEl.innerHTML = artifacts.length
    ? artifacts.map((a) => `<div class="artifact" data-art="${escapeHtml(a)}">${icon("doc")}<span>${escapeHtml(a)}</span></div>`).join("")
    : `<span class="muted small">暂无产物</span>`;
  artEl.querySelectorAll(".artifact").forEach((el) =>
    el.addEventListener("click", async () => {
      artEl.querySelectorAll(".artifact").forEach((x) => x.classList.remove("active"));
      el.classList.add("active");
      const res = await apiGet(`/api/loops/${encodeURIComponent(loop.id)}/artifact?name=${encodeURIComponent(el.dataset.art)}`);
      const viewer = $("insp-artifact-viewer");
      viewer.textContent = res.content;
      viewer.classList.remove("hidden");
    })
  );
}

function setActivity(msg, kind = "running") {
  const el = $("ld-last-msg");
  el.className = `activity ${kind === "running" ? "" : kind}`;
  el.innerHTML = `${kind === "running" ? '<span class="spinner"></span>' : icon(kind === "ok" ? "check" : "shield", "ic ic-xs")}<span>${escapeHtml(msg)}</span>`;
}

// New-loop sheet
const openSheet = () => { $("new-loop-bar").classList.remove("hidden"); setTimeout(() => $("nl-title").focus(), 50); };
const closeSheet = () => $("new-loop-bar").classList.add("hidden");
$("btn-open-new-modal").addEventListener("click", openSheet);
document.querySelectorAll("[data-open-sheet]").forEach((b) => b.addEventListener("click", openSheet));
$("btn-quick-new-loop").addEventListener("click", () => { showView("loops"); openSheet(); });
$("btn-cancel-loop").addEventListener("click", closeSheet);
$("new-loop-bar").addEventListener("click", (e) => { if (e.target.id === "new-loop-bar") closeSheet(); });
document.querySelectorAll("#nl-mode .seg-item").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("#nl-mode .seg-item").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.mode = b.dataset.mode;
  })
);

$("btn-init-devops").addEventListener("click", async () => {
  const res = await apiPost("/api/init");
  toast(`账本已初始化${res.created_toml ? "，已生成 devops.toml" : ""}`);
});

$("btn-create-loop").addEventListener("click", async () => {
  const title = $("nl-title").value.trim();
  if (!title) return $("nl-title").focus();
  const filesRaw = $("nl-files").value.trim();
  const files = filesRaw ? filesRaw.split(",").map((s) => s.trim()).filter(Boolean) : [];
  const { loop } = await apiPost("/api/loops", { title, files, mode: state.mode, from_git: $("nl-git").checked });
  state.loopId = loop.id;
  $("nl-title").value = "";
  closeSheet();
  await refreshOverview();
  await refreshLoops();
  setActivity(`已创建 ${loop.id} · 自动定级 ${loop.tier.toUpperCase()} · 挂载 ${loop.activated_skills.length} 项技能`, "ok");
  toast(`闭环 ${loop.id} 已创建`);
});

const ACTION_LABELS = { build: "编码", review: "独立检视", pipeline: "流水线", release: "金丝雀发布", autopilot: "Autopilot 全自动推进" };
document.querySelectorAll("[data-laction]").forEach((btn) =>
  btn.addEventListener("click", async () => {
    if (!state.loopId) return toast("请先创建或选择一个闭环", "warn");
    const action = btn.dataset.laction;
    btn.classList.add("running");
    setActivity(`正在执行 ${ACTION_LABELS[action]}…`);
    try {
      const res = await apiPost(`/api/loops/${encodeURIComponent(state.loopId)}/action`, { action, version: "v0.1.0", dry_run: true });
      const ok = res.code === 0;
      setActivity(`${ACTION_LABELS[action]} · ${(STAGE_LABELS[res.stage] || res.stage).toString()} — ${res.message}`, ok ? "ok" : "warn");
      await refreshLoops();
      await refreshOverview();
    } catch (e) {
      setActivity(`执行失败：${e.message}`, "warn");
    } finally {
      btn.classList.remove("running");
    }
  })
);

document.querySelectorAll("[data-approve]").forEach((btn) =>
  btn.addEventListener("click", async () => {
    if (!state.loopId) return;
    const by = $("ap-by").value.trim() || "tech-lead";
    const res = await apiPost(`/api/loops/${encodeURIComponent(state.loopId)}/action`, { action: "approve", point: btn.dataset.approve, by });
    toast(res.message);
    await refreshLoops();
  })
);

$("btn-submit-feedback").addEventListener("click", async () => {
  if (!state.loopId) return;
  setActivity("正在聚类分诊线上反馈…");
  try {
    const res = await apiPost(`/api/loops/${encodeURIComponent(state.loopId)}/action`, { action: "feedback", feedback_text: $("fb-input").value.trim() });
    setActivity(res.message, "ok");
    $("fb-input").value = "";
    await refreshLoops();
    await refreshOverview();
  } catch (e) {
    setActivity(`反馈分诊：${e.message}`, "warn");
  }
});

// ── Agent Session (SSE + reasoning disclosure) ────────────────────────────────
function updateContextMeter(st) {
  const ratio = Math.max(0, Math.min(1, st.ratio || 0));
  const pct = Math.max(1, Math.round(ratio * 100));
  const zone = (st.zone || "smart").toLowerCase();
  const ring = $("ctx-ring");
  ring.style.strokeDashoffset = String(314.16 * (1 - Math.max(ratio, 0.01)));
  ring.style.stroke = ZONE_COLORS[zone] || ZONE_COLORS.smart;
  $("ctx-pct").textContent = `${pct}%`;
  const zoneEl = $("ctx-zone");
  zoneEl.textContent = zone.toUpperCase();
  zoneEl.style.color = ZONE_COLORS[zone];
  $("ctx-tokens").textContent = `${(st.used_tokens || 0).toLocaleString()} / ${(st.window_tokens || 128000).toLocaleString()} tok`;
  $("chip-ctx").textContent = `上下文 ${pct}%`;
}

document.querySelectorAll("#role-seg .seg-item").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("#role-seg .seg-item").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.role = b.dataset.role;
    refreshOverview();
  })
);

async function sendChatMessage(text) {
  if (state.streaming) return;
  state.streaming = true;
  $("chat-send").disabled = true;
  $("chat-hero").classList.add("hidden");
  const feed = $("chat-feed");
  const scroller = $("chat-scroll");

  const userDiv = document.createElement("div");
  userDiv.className = "msg-user";
  userDiv.textContent = text;
  feed.appendChild(userDiv);

  const bot = document.createElement("div");
  bot.className = "msg-ai";
  bot.innerHTML = `
    <img src="/logo.svg" class="msg-ai-avatar" alt="" />
    <div class="msg-ai-main">
      <div class="msg-ai-head">NeoAgent <span class="badge">${escapeHtml($(`role-seg`).querySelector(".active").textContent)}</span></div>
      <details class="thought" open>
        <summary>${icon("compose", "ic spark")}<span class="thinking-label">正在推理…</span></summary>
        <div class="thought-body"></div>
      </details>
      <div class="tool-area"></div>
      <div class="answer"><span class="caret"></span></div>
    </div>`;
  feed.appendChild(bot);
  scroller.scrollTop = scroller.scrollHeight;

  const thought = bot.querySelector(".thought");
  const thoughtLabel = bot.querySelector(".thought summary span");
  const thoughtBody = bot.querySelector(".thought-body");
  const toolArea = bot.querySelector(".tool-area");
  const answerEl = bot.querySelector(".answer");
  const t0 = performance.now();
  let answerRaw = "";
  let steps = 0;
  let folded = false;

  const fold = () => {
    if (folded) return;
    folded = true;
    const secs = ((performance.now() - t0) / 1000).toFixed(1);
    thoughtLabel.className = "";
    thoughtLabel.textContent = steps ? `已推理 ${steps} 步 · ${secs}s` : `已推理 · ${secs}s`;
    thought.open = false;
  };

  try {
    const res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session_id: "studio-main", role: state.role }),
    });
    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() || "";
      for (const frame of frames) {
        let evName = "message";
        let dataStr = "";
        for (const line of frame.split("\n")) {
          if (line.startsWith("event:")) evName = line.slice(6).trim();
          else if (line.startsWith("data:")) dataStr += line.slice(5).trim();
        }
        if (!dataStr) continue;
        const payload = JSON.parse(dataStr);
        if (evName === "reasoning") {
          steps += (payload.delta.match(/\n/g) || []).length;
          thoughtBody.textContent += payload.delta || "";
        } else if (evName === "token") {
          fold();
          answerRaw += payload.delta || "";
          answerEl.innerHTML = renderMarkdownLite(answerRaw) + '<span class="caret"></span>';
        } else if (evName === "tool_start") {
          toolArea.insertAdjacentHTML("beforeend", `<span class="tool-pill">${icon("cpu", "ic ic-xs")}${escapeHtml(payload.name || "tool")}</span>`);
        } else if (evName === "context_status") {
          updateContextMeter(payload);
        } else if (evName === "error") {
          answerRaw += `\n> ${payload.message}`;
          answerEl.innerHTML = renderMarkdownLite(answerRaw);
        }
        scroller.scrollTop = scroller.scrollHeight;
      }
    }
  } finally {
    fold();
    if (!thoughtBody.textContent) thought.remove();
    answerEl.innerHTML = renderMarkdownLite(answerRaw);
    state.streaming = false;
    $("chat-send").disabled = false;
  }
}

$("chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("chat-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  input.style.height = "";
  await sendChatMessage(text);
});
$("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("chat-form").requestSubmit();
  }
});
$("chat-input").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = `${Math.min(180, e.target.scrollHeight)}px`;
});
document.querySelectorAll("[data-prompt]").forEach((btn) => btn.addEventListener("click", () => sendChatMessage(btn.dataset.prompt)));

// ── Skills ────────────────────────────────────────────────────────────────────
async function loadSkills() {
  const qs = new URLSearchParams({ query: $("sk-query").value.trim(), stage: state.stage, files: $("sk-files").value.trim() });
  const data = await apiGet(`/api/skills?${qs}`);
  const listEl = $("skills-list");
  listEl.innerHTML =
    `<div class="row"><div class="row-main row-sub">已挂载 <b>${data.total}</b> 项技能 · 当前匹配 <b>${data.skills.length}</b> 项</div></div>` +
    data.skills
      .map(
        (s) => `<div class="row clickable" data-sname="${escapeHtml(s.name)}">
          <div class="row-main">
            <div class="row-title">${escapeHtml(s.name)} <span class="cat-badge">${escapeHtml(s.category)}</span></div>
            <div class="row-sub">${escapeHtml(s.description)}</div>
          </div>
        </div>`
      )
      .join("");
  listEl.querySelectorAll("[data-sname]").forEach((el) =>
    el.addEventListener("click", () => {
      listEl.querySelectorAll(".row").forEach((r) => r.classList.remove("selected"));
      el.classList.add("selected");
      openSkillDetail(el.dataset.sname);
    })
  );
}

async function openSkillDetail(name, ref = "") {
  const d = await apiGet(`/api/skills/detail?${new URLSearchParams({ name, ref })}`);
  const v = d.validation;
  const refs = (d.references || []).map((r) => `<span class="ref-chip" data-sref="${escapeHtml(r)}">${icon("doc", "ic ic-xs")} ${escapeHtml(r)}</span>`).join("");
  $("skill-reader").innerHTML = `
    <div class="reader-head">
      <div><div class="muted small">${ref ? "Tier 3 · references/" + escapeHtml(ref) : "Tier 2 · SKILL.md"}</div><h2>${escapeHtml(d.skill.name)}</h2></div>
      <span class="badge ${v.passed ? "ok" : "danger"}">${v.passed ? "F1–F4 通过" : "F1–F4 未通过"}</span>
    </div>
    ${refs ? `<div class="ref-chips">${refs}</div>` : ""}
    <div class="reader-body answer">${renderMarkdownLite(d.content)}</div>`;
  $("skill-reader").querySelectorAll("[data-sref]").forEach((ch) => ch.addEventListener("click", () => openSkillDetail(name, ch.dataset.sref)));
}

document.querySelectorAll("#sk-stage .pill-tab").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("#sk-stage .pill-tab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.stage = b.dataset.stage;
    loadSkills();
  })
);
$("btn-filter-skills").addEventListener("click", loadSkills);
$("sk-query").addEventListener("keydown", (e) => { if (e.key === "Enter") loadSkills(); });
$("btn-validate-skills").addEventListener("click", async () => {
  const res = await apiPost("/api/skills/validate");
  const rows = res.reports
    .map((r) => `<div class="row"><span class="sev-dot ${r.passed ? "fixed" : "critical"}"></span>
      <div class="row-main"><div class="row-title">${escapeHtml(r.name)} <span class="cat-badge">${escapeHtml(r.category)}</span></div>
      <div class="row-sub">F1 ${r.F1_frontmatter ? "✓" : "✗"} · F2 ${r.F2_name ? "✓" : "✗"} · F3 ${r.F3_name_matches_dir ? "✓" : "✗"} · F4 ${r.F4_description ? "✓" : "✗"} · ${r.line_count} 行</div></div></div>`)
    .join("");
  $("skill-reader").innerHTML = `
    <div class="muted small">Harness L6 · 技能机械合规</div><h2>F1–F4 校验报告</h2>
    <div class="validate-hero" style="margin:16px 0">
      <div><div class="big-num" style="color:var(--green-ink)">${res.passed}</div><div class="muted small">通过</div></div>
      <div><div class="big-num">${res.total}</div><div class="muted small">总计</div></div>
      <div><div class="big-num" style="color:${res.failed ? "var(--orange-ink)" : "var(--text-3)"}">${res.failed}</div><div class="muted small">待修复</div></div>
    </div>
    <div class="card list" style="box-shadow:none">${rows}</div>`;
  toast(`F1–F4 校验完成：${res.passed}/${res.total} 通过`);
});

// ── Harness ───────────────────────────────────────────────────────────────────
async function loadHarness() {
  const { layers } = await apiGet("/api/harness");
  const L = layers;
  const items = [
    ["L1", "信息边界 · 四层仓库", L.L1_information_boundary.status, `文档拓扑：${L.L1_information_boundary.repo_scaffold.mode} · ${L.L1_information_boundary.defined_stage_boundaries.length} 个阶段边界`],
    ["L2", "工具系统 · 结果提炼", L.L2_tool_distillation.status, "超时重试守护 · 单行 grep 日志 · 输出蒸馏"],
    ["L3", "编排 · 三条件子代理", L.L3_orchestration_and_subagents.status, "自包含输入 ∧ 结构化输出 ∧ 高独立性"],
    ["L4", "记忆 · 40% 上下文红线", L.L4_memory_and_context_40pct.status, `Smart ≤ ${L.L4_memory_and_context_40pct.smart_ceiling * 100}% · Reset > ${L.L4_memory_and_context_40pct.hard_reset_ceiling * 100}% · 思维链折叠`],
    ["L5", "独立评估 · 读写分离", L.L5_independent_evaluation.status, "生成者与评估者上下文物理隔离 · 阶段门禁"],
    ["L6", "机械约束 · DLP", L.L6_mechanical_constraints_and_dlp.status, `哈希链 ${L.L6_mechanical_constraints_and_dlp.audit_events_verified} 条 · 隐私泄露 ${L.L6_mechanical_constraints_and_dlp.privacy_violations_count} · 技能 ${L.L6_mechanical_constraints_and_dlp.skills_f1_f4_compliant}`],
    ["L7", "SAFE 自演进", L.L7_self_evolution_safe.status, `待确认 ${L.L7_self_evolution_safe.pending_proposals} · 已固化 ${L.L7_self_evolution_safe.applied_proposals}`],
  ];
  const okStates = ["aligned", "active", "healthy"];
  $("harness-cards").innerHTML = items
    .map(
      ([no, title, st, desc]) => `<div class="card layer">
        <div class="layer-top"><span class="layer-no">${no}</span><span class="badge ${okStates.includes(st) ? "ok" : "warn"}">${escapeHtml(st)}</span></div>
        <div class="layer-title">${escapeHtml(title)}</div>
        <div class="layer-desc">${escapeHtml(desc)}</div>
      </div>`
    )
    .join("");

  const evo = L.L7_self_evolution_safe.proposals;
  const all = [
    ...(evo.pending || []).map((p) => ({ ...p, _state: "pending" })),
    ...(evo.applied || []).map((p) => ({ ...p, _state: "applied" })),
  ];
  $("evo-list").innerHTML = all.length
    ? all
        .map(
          (p) => `<div class="row">
            <span class="sev-dot ${p._state === "pending" ? "minor" : "fixed"}"></span>
            <div class="row-main">
              <div class="row-title"><code>${escapeHtml(p.id)}</code><span class="badge info">${escapeHtml(p.scope)}</span><span class="muted small">${escapeHtml(p.trigger_signal)} → <span class="mono">${escapeHtml(p.target_artifact)}</span></span></div>
              <div class="row-sub">${escapeHtml(p.addition_summary)}</div>
            </div>
            ${p._state === "pending" ? `<button class="btn sm btn-primary" data-evo-approve="${escapeHtml(p.id)}">确认固化</button>` : `<span class="badge ok">已应用 · ${escapeHtml(p.approved_by)}</span>`}
          </div>`
        )
        .join("")
    : `<div class="row-empty">暂无演进提案。重复缺陷（3-Strike）或金丝雀回滚会自动生成预防性提案。</div>`;
  $("evo-list").querySelectorAll("[data-evo-approve]").forEach((b) =>
    b.addEventListener("click", async () => {
      await apiPost("/api/harness/evolution/approve", { proposal_id: b.dataset.evoApprove, by: "architect" });
      toast(`提案 ${b.dataset.evoApprove} 已固化`);
      loadHarness();
    })
  );
}

$("btn-diagnose-scaffold").addEventListener("click", async () => {
  const res = await apiPost("/api/harness/scaffold", { apply: false });
  const miss = res.diagnosis.missing_paths.length;
  toast(`L1 诊断：${res.diagnosis.mode} · 缺失 ${miss} 项 · 反模式 ${res.diagnosis.anti_patterns.length} 项`, miss ? "warn" : "ok");
});
$("btn-apply-scaffold").addEventListener("click", async () => {
  const res = await apiPost("/api/harness/scaffold", { apply: true });
  await loadHarness();
  toast(res.created.length ? `已补齐 ${res.created.length} 个四层文档骨架文件` : "四层文档已全部对齐");
});

// ── Insights ──────────────────────────────────────────────────────────────────
async function loadMetrics() {
  const m = await apiGet("/api/metrics");
  const d = m.dora || {};
  const a = m.autonomy || {};
  const lv = m.ledger_verification || {};
  const fmt = (v, u = "") => (v === null || v === undefined ? "—" : `${v}${u}`);
  const pct = (v) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);
  const dur = (h) => {
    if (h === null || h === undefined) return "—";
    if (h >= 1) return `${h} h`;
    const min = Math.round(h * 60);
    return min >= 1 ? `${min} 分钟` : "< 1 分钟";
  };
  const kpis = [
    ["账本完整性", lv.valid ? "已验证" : "被篡改", `${lv.events || 0} 条 SHA-256 链式事件`, lv.valid],
    ["变更前置时间", dur(d.lead_time_hours_median), "需求接入 → 上线观测（中位数）"],
    ["部署频率", fmt(d.deploys_per_week, " /周"), `累计闭环 ${m.loops_total}`],
    ["变更失败率", pct(d.change_failure_rate), `自动回滚 ${a.rollback_count || 0} 次`],
    ["AI 自动收敛率", pct(a.auto_converge_rate), `平均检视 ${fmt(a.avg_review_rounds)} 轮`, a.auto_converge_rate === 1],
    ["AI 自愈成功率", pct(a.self_heal_rate), `平均 CI 修复 ${fmt(a.avg_pipeline_fixes)} 轮`, a.self_heal_rate === 1],
  ];
  $("metrics-cards").innerHTML = kpis
    .map(
      ([label, val, sub, good]) => `<div class="card kpi">
        <div class="kpi-label">${escapeHtml(label)}</div>
        <div class="kpi-val ${good ? "good" : ""}">${escapeHtml(val)}</div>
        <div class="kpi-sub">${escapeHtml(sub)}</div>
      </div>`
    )
    .join("");
}

$("btn-refresh-metrics").addEventListener("click", loadMetrics);
$("btn-scan-privacy").addEventListener("click", async () => {
  const res = await apiPost("/api/harness/privacy-scan");
  const box = $("privacy-scan-result");
  if (res.clean) {
    box.innerHTML = `<div class="privacy-icon">${icon("shield", "ic ic-lg")}</div>
      <div><h3>0 处泄露 · 扫描通过 <span class="badge ok">PASS</span></h3>
      <p class="muted">源码、配置与文档中未发现个人机器路径、内网地址、凭据或自定义敏感词。</p></div>`;
    toast("全仓隐私扫描通过：0 处泄露");
  } else {
    box.innerHTML = `<div class="privacy-icon bad">${icon("shield", "ic ic-lg")}</div>
      <div><h3>发现 ${res.count} 处待清理 <span class="badge danger">BLOCKED</span></h3>
      ${res.violations.map((v) => `<div class="vio"><code>${escapeHtml(v.file)}:${v.line}</code> · ${escapeHtml(v.rule)} · ${escapeHtml(v.message)}</div>`).join("")}</div>`;
    toast(`隐私扫描发现 ${res.count} 处问题`, "warn");
  }
});

// ── Boot ──────────────────────────────────────────────────────────────────────
refreshOverview();
refreshLoops();
apiGet("/api/context?session_id=studio-main").then(updateContextMeter).catch(() => {});
