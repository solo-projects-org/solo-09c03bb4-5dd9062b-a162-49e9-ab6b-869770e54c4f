/* 泵站振动频带能量核对 —— 前端逻辑（原生 JS，无构建依赖）。 */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const FS = 256;
const WIN = 256;

const state = {
  overview: null,
  energy: null,
  compare: [],
  replaceTarget: null,
};

/* ------------------------------------------------------------------ */
/* API 封装                                                            */
/* ------------------------------------------------------------------ */

async function api(path, options = {}) {
  const opts = { method: options.method || "GET", headers: {}, ...options };
  if (options.json !== undefined) {
    opts.method = options.method || "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(options.json);
  }
  const resp = await fetch(path, opts);
  const ct = resp.headers.get("content-type") || "";
  const data = ct.includes("application/json")
    ? await resp.json()
    : { raw: await resp.text() };
  if (!resp.ok) {
    const err = new Error(data.error || `请求失败 HTTP ${resp.status}`);
    err.data = data;
    throw err;
  }
  return data;
}

function setMsg(el, text, kind = "") {
  if (!el) return;
  el.textContent = text || "";
  el.className = "msg" + (kind ? " " + kind : "");
}

function fmt(v, digits = 6) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  if (v !== 0 && Math.abs(v) < 1e-4) return v.toExponential(3);
  if (Math.abs(v) >= 1000) return v.toFixed(2);
  return Number(v).toFixed(digits);
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ------------------------------------------------------------------ */
/* 总览                                                                */
/* ------------------------------------------------------------------ */

async function refreshOverview() {
  state.overview = await api("/api/overview");
  renderSegments();
  renderTimeline();
}

/* ------------------------------------------------------------------ */
/* 段表格                                                              */
/* ------------------------------------------------------------------ */

function renderSegments() {
  const ov = state.overview;
  const body = $("#segments-body");
  const empty = $("#segments-empty");
  const segs = ov ? ov.segments : [];
  $("#seg-count").textContent = String(segs.length);
  empty.style.display = segs.length ? "none" : "block";

  body.innerHTML = segs.map((s) => `
    <tr data-id="${s.id}">
      <td class="mono">${s.id}</td>
      <td class="mono">${s.start_sample}</td>
      <td class="mono">${s.n_samples}</td>
      <td class="mono">[${s.start_sample}, ${s.end_sample_inclusive}]</td>
      <td class="mono">${s.duration_s.toFixed(3)}</td>
      <td>
        <div class="seg-name">${escapeHtml(s.name || "（未命名）")}</div>
        <div class="seg-file">${escapeHtml(s.source_file || "")}</div>
      </td>
      <td>
        <div class="row-actions">
          <button class="btn small ghost" data-action="replace" data-id="${s.id}" type="button">整段换报</button>
          <button class="btn small danger" data-action="delete" data-id="${s.id}" type="button">撤段</button>
        </div>
      </td>
    </tr>`).join("");
}

$("#segments-body").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-action]");
  if (!btn) return;
  const id = Number(btn.dataset.id);
  const seg = state.overview.segments.find((s) => s.id === id);
  if (btn.dataset.action === "delete") {
    const label = seg ? `#${id}（样点 ${seg.start_sample} 起 ${seg.n_samples} 点）` : `#${id}`;
    if (!window.confirm(`确认撤掉采样段 ${label}？\n与其范围相交的窗谱会一并重算 / 撤下。`)) return;
    btn.disabled = true;
    try {
      const r = await api(`/api/segments/${id}`, { method: "DELETE" });
      await afterMutation(`已撤段 #${id}；重算 ${r.reindex.considered_count} 窗，撤下 ${r.reindex.withdrawn_count} 窗。`);
    } catch (e) {
      window.alert(e.message);
      btn.disabled = false;
    }
  } else if (btn.dataset.action === "replace") {
    openReplaceModal(seg);
  }
});

/* ------------------------------------------------------------------ */
/* 上传                                                                */
/* ------------------------------------------------------------------ */

$("#upload-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.currentTarget;
  const msg = $("#upload-msg");
  setMsg(msg, "上传并重算窗谱中…", "info");
  const btn = form.querySelector('button[type="submit"]');
  btn.disabled = true;
  try {
    const fd = new FormData(form);
    const r = await api("/api/segments", { method: "POST", body: fd });
    form.reset();
    setMsg(
      msg,
      `已保存段 #${r.segment.id}；触及窗 ${r.reindex.considered_count} 个，` +
      `入库 ${r.reindex.stored_count} 个，撤下 ${r.reindex.withdrawn_count} 个。`,
      "ok"
    );
    await afterMutation(null);
  } catch (e) {
    setMsg(msg, "拒收 / 失败：" + e.message, "err");
  } finally {
    btn.disabled = false;
  }
});

async function afterMutation(successText) {
  await refreshOverview();
  await runQuery(true);
  if (successText) {
    // 成功提示放在查询区上方更显眼
    setMsg($("#query-msg"), successText, "ok");
  }
}

/* ------------------------------------------------------------------ */
/* 顶部：示例 / 清空                                                   */
/* ------------------------------------------------------------------ */

$("#btn-demo").addEventListener("click", async () => {
  const has = state.overview && state.overview.segments.length > 0;
  let reset = false;
  if (has) {
    if (!window.confirm("载入跨段示例会先清空当前全部采样段与窗谱，是否继续？")) return;
    reset = true;
  }
  const btn = $("#btn-demo");
  btn.disabled = true;
  try {
    await api("/api/demo/load", { json: { reset } });
    $("#q-start").value = 0;
    $("#q-end").value = 1023;
    $("#q-flow").value = 15;
    $("#q-fhigh").value = 25;
    updateTimeHints();
    await refreshOverview();
    await runQuery();
    setMsg($("#query-msg"),
      "跨段示例已载入：A[0,300)、B[300,650)、C[700,1024)，含 50 点缺口；当前为 15–25 Hz（20 Hz 纯音）结果。",
      "ok");
  } catch (e) {
    window.alert("示例载入失败：" + e.message);
  } finally {
    btn.disabled = false;
  }
});

$("#btn-clear").addEventListener("click", async () => {
  if (!window.confirm("确认清空全部采样段和已计算的窗谱？此操作不可撤销。")) return;
  try {
    await api("/api/segments", { method: "DELETE" });
    await refreshOverview();
    await runQuery(true);
    setMsg($("#query-msg"), "已清空全部数据。", "info");
  } catch (e) {
    window.alert(e.message);
  }
});

/* ------------------------------------------------------------------ */
/* 换报弹层                                                            */
/* ------------------------------------------------------------------ */

const modal = $("#replace-modal");

function openReplaceModal(seg) {
  state.replaceTarget = seg;
  $("#replace-seg-label").textContent = `#${seg.id}`;
  $("#replace-old-start").textContent = String(seg.start_sample);
  const startInput = $("#rp-start");
  startInput.value = "";
  startInput.placeholder = `留空沿用 ${seg.start_sample}`;
  $("#rp-file").value = "";
  setMsg($("#replace-msg"), "", "");
  modal.classList.remove("hidden");
}

function closeReplaceModal() {
  modal.classList.add("hidden");
  state.replaceTarget = null;
}

$("#rp-cancel").addEventListener("click", closeReplaceModal);
modal.addEventListener("click", (e) => { if (e.target === modal) closeReplaceModal(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !modal.classList.contains("hidden")) closeReplaceModal();
});

$("#replace-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const seg = state.replaceTarget;
  if (!seg) return;
  const fileInput = $("#rp-file");
  const msg = $("#replace-msg");
  if (!fileInput.files || !fileInput.files[0]) {
    setMsg(msg, "请选择整段替换用的 CSV 文件", "err");
    return;
  }
  const fd = new FormData();
  fd.append("file", fileInput.files[0]);
  const newStart = $("#rp-start").value.trim();
  if (newStart !== "") fd.append("start_sample", newStart);

  const btn = ev.currentTarget.querySelector('button[type="submit"]');
  btn.disabled = true;
  setMsg(msg, "换报事务提交中…", "info");
  try {
    const r = await api(`/api/segments/${seg.id}/replace`, { method: "POST", body: fd });
    closeReplaceModal();
    await afterMutation(
      `段 #${seg.id} 已整段换报；旧/新范围合计触及 ${r.reindex.considered_count} 窗，` +
      `入库 ${r.reindex.stored_count}，撤下 ${r.reindex.withdrawn_count}。`
    );
  } catch (e) {
    setMsg(msg, "换报失败，旧状态保持不变：" + e.message, "err");
  } finally {
    btn.disabled = false;
  }
});

/* ------------------------------------------------------------------ */
/* 查询                                                                */
/* ------------------------------------------------------------------ */

function readQueryInputs() {
  const s = Number($("#q-start").value);
  const e = Number($("#q-end").value);
  const fl = Number($("#q-flow").value);
  const fh = Number($("#q-fhigh").value);
  if (!Number.isInteger(s) || s < 0) throw new Error("起始样点必须是非负整数");
  if (!Number.isInteger(e) || e < 0 || e < s) throw new Error("结束样点（含）必须是 ≥ 起始样点的非负整数");
  if (!Number.isFinite(fl) || !Number.isFinite(fh) || fl < 0 || fh > 128 || fl > fh) {
    throw new Error("频带需满足 0 ≤ 下限 ≤ 上限 ≤ 128 Hz");
  }
  return { s, e, fl, fh };
}

async function runQuery(silentIfInvalid = false) {
  const msg = $("#query-msg");
  let q;
  try {
    q = readQueryInputs();
  } catch (e) {
    if (!silentIfInvalid) setMsg(msg, e.message, "err");
    return null;
  }
  const params = new URLSearchParams({
    start_sample: q.s, end_sample: q.e, f_low: q.fl, f_high: q.fh,
  });
  try {
    state.energy = await api("/api/energy?" + params.toString());
    renderResult();
    renderTimeline();
    return state.energy;
  } catch (e) {
    state.energy = null;
    renderResult();
    if (!silentIfInvalid) setMsg(msg, e.message, "err");
    return null;
  }
}

$("#query-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  setMsg($("#query-msg"), "", "");
  runQuery();
});

function updateTimeHints() {
  const s = Number($("#q-start").value);
  const e = Number($("#q-end").value);
  if (Number.isFinite(s)) $("#q-start-time").textContent = `${(s / FS).toFixed(3)} s`;
  if (Number.isFinite(e)) {
    $("#q-end-time").textContent =
      `样点时刻 ${(e / FS).toFixed(3)} s；区间末端 ${((e + 1) / FS).toFixed(3)} s`;
  }
}
["q-start", "q-end"].forEach((id) => $("#" + id).addEventListener("input", updateTimeHints));

$$(".chip[data-band]").forEach((chip) => {
  chip.addEventListener("click", () => {
    const [lo, hi] = chip.dataset.band.split(",");
    $("#q-flow").value = lo;
    $("#q-fhigh").value = hi;
    setMsg($("#query-msg"), "", "");
    runQuery();
  });
});

$$(".chip[data-range]").forEach((chip) => {
  chip.addEventListener("click", () => {
    const spec = chip.dataset.range;
    if (spec === "all") {
      const maxEnd = state.overview ? state.overview.max_segment_end : 0;
      if (!maxEnd) { window.alert("还没有采样段"); return; }
      $("#q-start").value = 0;
      $("#q-end").value = maxEnd - 1;
    } else {
      const [s, e] = spec.split(",");
      $("#q-start").value = s;
      $("#q-end").value = e;
    }
    updateTimeHints();
    setMsg($("#query-msg"), "", "");
    runQuery();
  });
});

/* ------------------------------------------------------------------ */
/* 结果渲染                                                            */
/* ------------------------------------------------------------------ */

function renderResult() {
  const box = $("#result");
  const r = state.energy;
  $("#btn-add-compare").disabled = !r;

  if (!r) {
    box.innerHTML = `<div class="result-placeholder">选择样点区间与频带后点击「查询能量」。</div>`;
    return;
  }

  const missing = r.status !== "ok";
  const band = r.band;
  const bins = band.bin_frequencies_hz || [];
  let binText;
  if (bins.length === 0) binText = "该频带内没有整数 Hz 谱线";
  else if (bins.length <= 15) binText = bins.map((f) => f.toFixed(0)).join(", ") + " Hz";
  else binText = bins.slice(0, 5).map((f) => f.toFixed(0)).join(", ") +
    " … " + bins.slice(-5).map((f) => f.toFixed(0)).join(", ") + ` Hz（共 ${bins.length} 条）`;

  const banner = missing ? `
    <div class="status-banner missing">
      <span class="big">缺测</span>
      <span>${escapeHtml(r.message)}<br>
      <span class="sub">区间 [${r.interval.start_sample}, ${r.interval.end_sample_inclusive}] 内无完整且无缺样的窗。</span></span>
    </div>` : `
    <div class="status-banner ok">
      <span class="big">有效</span>
      <span>${escapeHtml(r.message)}，以下为这些窗带内均方值的算术平均。</span>
    </div>`;

  const avg = missing ? null : r.avg_band_ms;
  const rms = missing ? null : r.band_rms;

  const stats = `
    <div class="stat-grid">
      <div class="stat ${missing ? "" : "ok"}">
        <span class="k">完整落入的有效窗数</span>
        <span class="v">${r.window_count}<span class="u"> 个</span></span>
      </div>
      <div class="stat accent">
        <span class="k">平均带内均方值</span>
        <span class="v">${missing ? '<span class="tag-missing" style="font-size:16px">缺测</span>' : fmt(avg)}</span>
      </div>
      <div class="stat">
        <span class="k">平均带内 RMS</span>
        <span class="v">${missing ? "—" : fmt(rms)}${missing ? "" : '<span class="u"> (同单位)</span>'}</span>
      </div>
      <div class="stat">
        <span class="k">纳入谱线数</span>
        <span class="v">${bins.length}<span class="u"> 条 / 129</span></span>
      </div>
    </div>
    <p class="bin-line">频带 ${band.f_low}–${band.f_high} Hz（闭区间，按谱线中心频率）→ ${escapeHtml(binText)}。
    单边谱已对非直流 / 非 Nyquist 谱线加倍，全频带均方和等于时域均方。</p>`;

  let detail = "";
  if (!missing && r.windows.length) {
    detail = `
    <details class="win-details" open>
      <summary>逐窗明细（${r.windows.length}）</summary>
      <div class="table-wrap win-table">
        <table class="seg-table">
          <thead><tr>
            <th>窗号 k</th><th>样点区间</th><th>时间区间 s</th>
            <th class="num">带内均方</th><th class="num">带内 RMS</th><th class="num">全窗均方</th>
          </tr></thead>
          <tbody>
          ${r.windows.map((w) => `
            <tr>
              <td class="mono">${w.window_index}</td>
              <td class="mono">[${w.start_sample}, ${w.end_sample_inclusive}]</td>
              <td class="mono">${w.start_time_s.toFixed(3)}–${w.end_time_s.toFixed(3)}</td>
              <td class="num">${fmt(w.band_ms)}</td>
              <td class="num">${fmt(w.band_rms)}</td>
              <td class="num">${fmt(w.total_ms)}</td>
            </tr>`).join("")}
          </tbody>
        </table>
      </div>
    </details>`;
  }

  box.innerHTML = banner + stats + detail;
}

/* ------------------------------------------------------------------ */
/* 对照表                                                              */
/* ------------------------------------------------------------------ */

$("#btn-add-compare").addEventListener("click", () => {
  const r = state.energy;
  if (!r) return;
  state.compare.push({
    key: Date.now() + Math.random(),
    s: r.interval.start_sample,
    e: r.interval.end_sample_inclusive,
    fl: r.band.f_low,
    fh: r.band.f_high,
    count: r.window_count,
    avg: r.avg_band_ms,
    rms: r.band_rms,
    status: r.status,
  });
  renderCompare();
});

$("#btn-clear-compare").addEventListener("click", () => {
  state.compare = [];
  renderCompare();
});

function renderCompare() {
  const wrap = $("#compare-wrap");
  wrap.classList.toggle("hidden", state.compare.length === 0);
  $("#compare-body").innerHTML = state.compare.map((c) => `
    <tr>
      <td class="mono">${c.s}–${c.e}</td>
      <td class="mono">${(c.s / FS).toFixed(2)}–${((c.e + 1) / FS).toFixed(2)}</td>
      <td class="mono">${c.fl}–${c.fh}</td>
      <td class="num">${c.count}</td>
      <td class="num">${c.avg === null ? '<span class="tag-missing">缺测</span>' : fmt(c.avg)}</td>
      <td class="num">${c.rms === null ? "—" : fmt(c.rms)}</td>
      <td>${c.status === "ok" ? '<span class="tag-ok">有效</span>' : '<span class="tag-missing">缺测</span>'}</td>
    </tr>`).join("");
}

/* ------------------------------------------------------------------ */
/* 时间线                                                              */
/* ------------------------------------------------------------------ */

function currentQueryInterval() {
  if (state.energy) {
    return {
      start: state.energy.interval.start_sample,
      endInclusive: state.energy.interval.end_sample_inclusive,
    };
  }
  const s = Number($("#q-start").value);
  const e = Number($("#q-end").value);
  if (Number.isInteger(s) && Number.isInteger(e) && e >= s) return { start: s, endInclusive: e };
  return null;
}

function renderTimeline() {
  const ov = state.overview;
  const tl = $("#timeline");
  if (!ov) return;
  const segs = ov.segments || [];
  const expected = ov.expected_window_starts || [];

  if (!segs.length) {
    tl.innerHTML = `<div class="tl-empty-note">暂无段数据。载入跨段示例后可看到跨段有效窗与缺口窗。</div>`;
    return;
  }

  const q = currentQueryInterval();
  const maxWinEnd = expected.length ? Math.max(...expected) + WIN : 0;
  let domain = Math.max(ov.max_segment_end || 0, maxWinEnd, q ? q.endInclusive + 1 : 0, WIN);
  domain = Math.max(WIN, Math.ceil(domain / 128) * 128);

  const pct = (samples) => (samples / domain) * 100;

  // 坐标轴刻度：自动选 128 的倍数步长，保持约 8~16 个刻度
  const steps = [128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288];
  let tickStep = steps[steps.length - 1];
  for (const step of steps) {
    if (domain / step <= 14) { tickStep = step; break; }
  }
  let ticksHtml = "";
  for (let s = 0; s <= domain; s += tickStep) {
    ticksHtml += `<div class="tl-tick major" style="left:${pct(s)}%"><span>${s} · ${(s / FS).toFixed(1)}s</span></div>`;
  }

  const highlightHtml = q ? `
    <div class="tl-highlight" style="left:${pct(q.start)}%;width:${Math.max(pct(q.endInclusive + 1 - q.start), 0.2)}%"></div>` : "";

  const segBlocks = segs.map((s) => `
    <div class="tl-seg"
         style="left:${pct(s.start_sample)}%;width:${pct(s.n_samples)}%"
         title="段 #${s.id} ${escapeHtml(s.name || "")}&#10;样点 [${s.start_sample}, ${s.end_sample_inclusive}]，${s.n_samples} 点 / ${s.duration_s.toFixed(3)} s">
      #${s.id} ${escapeHtml(s.name || "")}
    </div>`).join("");

  const validSet = new Set(ov.valid_window_starts || []);
  const lane = (parity) => {
    const blocks = expected
      .filter((w0) => (w0 / 128) % 2 === parity)
      .map((w0) => {
        const k = w0 / 128;
        const valid = validSet.has(w0);
        const widthPct = pct(WIN);
        const compact = widthPct < 3.5;
        return `
        <div class="tl-win ${valid ? "valid" : "invalid"} ${compact ? "compact" : ""}"
             data-window-start="${w0}"
             style="left:${pct(w0)}%;width:${widthPct}%"
             title="窗 #${k} [${w0}, ${w0 + WIN - 1}] / ${(w0 / FS).toFixed(3)}–${((w0 + WIN) / FS).toFixed(3)} s&#10;${valid ? "有效：256 点齐全，谱已在库（点击查此窗）" : "残缺：有缺样，谱已撤下（点击设为此区间）"}">
          #${k}
        </div>`;
      }).join("");
    return `
      <div class="tl-row">
        <div class="tl-row-label">${parity === 0 ? "偶编号窗" : "奇编号窗"}</div>
        <div class="tl-track" style="--hop-pct:${pct(128)}%">${blocks}${highlightHtml}</div>
      </div>`;
  };

  tl.innerHTML = `
    <div class="tl-axis" style="margin-left:56px;position:relative;height:34px;">${ticksHtml}</div>
    <div class="tl-row">
      <div class="tl-row-label">采样段</div>
      <div class="tl-track" style="height:30px;--hop-pct:${pct(128)}%">${segBlocks}${highlightHtml}</div>
    </div>
    ${lane(0)}
    ${lane(1)}`;

  // 点击窗块 -> 查询该窗
  $$(".tl-win", tl).forEach((el) => {
    el.addEventListener("click", () => {
      const w0 = Number(el.dataset.windowStart);
      $("#q-start").value = w0;
      $("#q-end").value = w0 + WIN - 1;
      updateTimeHints();
      setMsg($("#query-msg"), "", "");
      runQuery();
    });
  });
}

/* ------------------------------------------------------------------ */
/* 启动                                                                */
/* ------------------------------------------------------------------ */

updateTimeHints();
refreshOverview().catch((e) => {
  $("#segments-empty").textContent = "加载总览失败：" + e.message;
});
api("/api/config").then((c) => {
  $("#db-path").textContent = c.db_path;
}).catch(() => {});
runQuery(true);
