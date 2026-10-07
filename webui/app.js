/* Let AI Read Video! Web UI 前端逻辑（原生 JS，零框架）。
 * 模块：tabs / 环境能力 / 选集 / 设置联动 / 任务提交与通道锁 / 轮询退避 /
 *       任务列表（排队/取消/重试）/ 历史恢复 / 结果归组 / PDF 导出 /
 *       帧图查看器（网格/大图/恢复）/ 文字稿阅读区。
 */
(function () {
  "use strict";

  // ================= 基础工具 =================
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function toast(msg, isErr) {
    var t = el("div", "toast" + (isErr ? " err" : ""), msg);
    $("toast-box").appendChild(t);
    setTimeout(function () { t.remove(); }, 2200);
  }
  function fieldErr(id, msg) { $(id).textContent = msg || ""; }
  function debounce(fn, ms) {
    var t = null;
    return function () {
      var args = arguments;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(null, args); }, ms);
    };
  }
  function cleanPath(v) {   // U05：Windows 引号清理
    return v.trim().replace(/^"+|"+$/g, "").replace(/^'+|'+$/g, "");
  }
  function apiPost(path, payload) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (r) { return r.json(); });
  }
  function fmtT(seconds) {
    var total = Math.max(0, Math.floor(Number(seconds) || 0));
    var h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
    function pad(n) { return (n < 10 ? "0" : "") + n; }
    return h ? (h + ":" + pad(m) + ":" + pad(s)) : (pad(m) + ":" + pad(s));
  }
  function fmtDur(sec) {
    var t = Math.round(sec);
    var h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
    return h ? h + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0")
             : m + ":" + String(s).padStart(2, "0");
  }

  // ================= 状态 =================
  var currentSource = "url";       // url | file | cache（U05）
  var currentTab = "single";       // single | multi | batch（url 来源下）
  var pollTimer = null;
  var pollDelay = 1000;            // U10 指数退避
  var laneJobs = { transcript: null, frames: null };
  var laneByJob = {};
  var jobCache = {};               // jobId → 最近快照
  var jobOrder = [];
  var renderedJobs = {};
  var resultsFeed = [];            // T03：{jobId, mediaKey, results}，按媒体键归组
  var taskRows = {};               // jobId → 最近任务快照（任务列表渲染）
  var probeInfo = null;            // U06 多P解析结果

  // ================= 标签页（U11 tablist 语义 + 方向键） =================
  function setupTabs(tablistSel, onSwitch) {
    var tabs = Array.from(document.querySelectorAll(tablistSel + " [role=tab]"));
    tabs.forEach(function (tab, i) {
      tab.addEventListener("click", function () { onSwitch(tab, tabs); });
      tab.addEventListener("keydown", function (e) {
        if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
        e.preventDefault();
        var next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
        next.focus();
        onSwitch(next, tabs);
      });
    });
  }
  function markSelected(tab, tabs) {
    tabs.forEach(function (t) { t.setAttribute("aria-selected", t === tab ? "true" : "false"); });
  }
  setupTabs(".tabs[aria-label='来源类型']", function (tab, tabs) {
    markSelected(tab, tabs);
    currentSource = tab.dataset.source;
    ["url", "file", "cache"].forEach(function (s) { $("source-" + s).hidden = s !== currentSource; });
    updateSummary();
    savePrefs();
  });
  setupTabs(".tabs[aria-label='链接处理方式']", function (tab, tabs) {
    markSelected(tab, tabs);
    currentTab = tab.dataset.tab;
    ["single", "multi", "batch"].forEach(function (name) {
      $("panel-" + name).hidden = name !== currentTab;
    });
    savePrefs();
  });

  // ================= U09 环境能力视图 =================
  function capRow(name, value, state) {
    var tr = el("tr");
    tr.appendChild(el("td", "cap-name", name));
    var td = el("td");
    td.appendChild(el("span", "dot " + (state || "skip")));
    td.appendChild(document.createTextNode(value));
    tr.appendChild(td);
    return tr;
  }
  function renderHealth(h) {
    var box = $("env-content");
    box.innerHTML = "";
    var table = el("table");
    var t = h.tools || {}, caps = h.capabilities || {};
    function toolOk(name) { return t[name] ? "就绪" : "缺失"; }
    table.appendChild(capRow("ffmpeg / ffprobe / yt-dlp",
      ["ffmpeg", "ffprobe", "yt-dlp"].map(toolOk).join(" / "),
      t.ffmpeg && t.ffprobe ? "ok" : "err"));
    table.appendChild(capRow("抽帧能力",
      caps.frames_ready ? "可用" : "不可用（缺 ffmpeg/ffprobe，请先安装）",
      caps.frames_ready ? "ok" : "err"));
    table.appendChild(capRow("转写能力（faster-whisper）",
      caps.transcribe_ready ? "已安装" : "缺失（仅影响文字稿，纯抽帧不受影响）",
      caps.transcribe_ready ? "ok" : "warn"));
    table.appendChild(capRow("显卡检测",
      caps.gpu_detected ? "检测到：" + (caps.gpu_devices || []).join("、")
                        : (h.gpu && h.gpu.hint) || "未检测到",
      caps.gpu_detected ? "ok" : "skip"));
    table.appendChild(capRow("CUDA 运行库",
      caps.cuda_devices === null || caps.cuda_devices === undefined
        ? "未知（未安装运行库或检测失败）"
        : "可用，可见设备 " + caps.cuda_devices + " 个",
      caps.cuda_devices ? "ok" : "skip"));
    table.appendChild(capRow("模型缓存",
      caps.models_cached === null || caps.models_cached === undefined
        ? "未知"
        : (caps.models_cached.length
            ? "已缓存 " + caps.models_cached.join("、")
            : "未缓存（首次转写自动下载，约 75MB–1.5GB）"),
      caps.models_cached && caps.models_cached.length ? "ok" : "skip"));
    box.appendChild(table);
    var missingCritical = !(t.ffmpeg && t.ffprobe);
    $("env-dot").className = "dot " + (missingCritical ? "err" : ((h.missing || []).length ? "warn" : "ok"));
    var strip = $("env-warn");
    if (missingCritical) {
      strip.textContent = "缺少 ffmpeg/ffprobe，抽帧不可用；点右上角「环境状态」查看安装方法。";
      strip.classList.add("show");
    } else if ((h.missing || []).length) {
      strip.textContent = "可选组件缺失：" + h.missing.join("、") + "（详见右上角「环境状态」）。";
      strip.classList.add("show");
    } else {
      strip.classList.remove("show");
    }
  }
  function fetchHealth(open) {
    fetch("/api/health").then(function (r) { return r.json(); }).then(function (h) {
      renderHealth(h);
      if (open) { $("env-panel").hidden = false; $("env-toggle").setAttribute("aria-expanded", "true"); }
    }).catch(function () {
      $("env-content").textContent = "检测失败：未知（可点「重新检测」重试）";
      $("env-dot").className = "dot warn";
    });
  }
  $("env-toggle").addEventListener("click", function () {
    var panel = $("env-panel");
    panel.hidden = !panel.hidden;
    $("env-toggle").setAttribute("aria-expanded", panel.hidden ? "false" : "true");
    if (!panel.hidden) fetchHealth(false);
  });
  $("env-recheck").addEventListener("click", function () { fetchHealth(true); });
  $("copy-install").addEventListener("click", function () {
    copyText($("install-cmd").textContent);
  });
  fetchHealth(false);

  // ================= U06 多P解析与勾选 =================
  var plChecks = [];
  $("probe-btn").addEventListener("click", function () {
    var url = $("multi-url").value.trim();
    fieldErr("err-multi", "");
    if (!url) { fieldErr("err-multi", "请先填写视频链接"); return; }
    $("probe-btn").disabled = true;
    $("probe-status").textContent = "解析中（只探测不下载）…";
    apiPost("/api/probe", { input: url }).then(function (data) {
      $("probe-btn").disabled = false;
      if (!data.ok) {
        $("probe-status").textContent = "";
        fieldErr("err-multi", "解析失败：" + (data.error || "未知错误"));
        return;
      }
      probeInfo = data;
      var pl = data.playlist;
      if (!pl || !pl.items || !pl.items.length) {
        $("probe-status").textContent = "单视频：" + (data.title || "") +
          (data.duration ? "（" + Math.round(data.duration) + "s）" : "") + "，无需选集";
        $("playlist-area").hidden = true;
        return;
      }
      $("probe-status").textContent = "共 " + pl.count + " 集：" + (data.title || "");
      renderPlaylist(pl.items);
    }).catch(function (e) {
      $("probe-btn").disabled = false;
      $("probe-status").textContent = "";
      fieldErr("err-multi", "解析请求失败: " + e);
    });
  });
  function renderPlaylist(items) {
    var list = $("pl-list");
    list.innerHTML = "";
    plChecks = items.map(function (it) {
      var row = el("label", "pl-item");
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.dataset.index = it.index;
      row.appendChild(cb);
      row.appendChild(el("span", null, "第 " + it.index + " 集 " + (it.title || "")));
      row.appendChild(el("span", "pl-dur",
        typeof it.duration === "number" ? fmtDur(it.duration) : "时长未知"));
      list.appendChild(row);
      return cb;
    });
    $("playlist-area").hidden = false;   // U06：默认不全选
    updatePlSummary();
  }
  function selectedIndexes() {
    return plChecks.filter(function (c) { return c.checked; })
      .map(function (c) { return parseInt(c.dataset.index, 10); })
      .sort(function (a, b) { return a - b; });
  }
  function updatePlSummary() {
    var sel = selectedIndexes();
    $("pl-summary").textContent = sel.length
      ? "已选 " + sel.length + " 集 → " + segmentsToText(sel)
      : "未选择（勾选后才生成，不会默认全跑）";
  }
  $("pl-all").addEventListener("click", function () {
    plChecks.forEach(function (c) { c.checked = true; });
    updatePlSummary();
  });
  $("pl-none").addEventListener("click", function () {
    plChecks.forEach(function (c) { c.checked = false; });
    updatePlSummary();
  });
  $("pl-list").addEventListener("change", updatePlSummary);
  function toSegments(indexes) {
    var segs = [];
    indexes.forEach(function (i) {
      var last = segs[segs.length - 1];
      if (last && i === last[1] + 1) last[1] = i;
      else segs.push([i, i]);
    });
    return segs;
  }
  function segToExpr(seg) { return seg[0] === seg[1] ? String(seg[0]) : seg[0] + "-" + seg[1]; }
  function segmentsToText(indexes) {
    return toSegments(indexes).map(segToExpr).join(", ");
  }

  // ================= U06 批量行检查 =================
  var URLISH = /^https?:\/\/\S+$/i;
  function checkBatch() {
    var lines = $("batch-urls").value.split(/\r?\n/).map(function (s) { return s.trim(); })
      .filter(function (s) { return s; });
    var seen = {}, dup = 0, invalid = 0;
    lines.forEach(function (s) {
      if (seen[s]) dup++; else seen[s] = 1;
      if (!URLISH.test(s)) invalid++;
    });
    var parts = ["有效 " + (lines.length - invalid) + " 行"];
    if (dup) parts.push("重复 " + dup + " 行（会提醒，不强制拦截）");
    if (invalid) parts.push("无效 " + invalid + " 行");
    $("batch-check").textContent = lines.length ? parts.join(" · ") : "";
  }
  $("batch-urls").addEventListener("input", debounce(checkBatch, 300));

  // ================= U05 本地路径存在性提示 =================
  function bindPathCheck(inputId, hintId, expectKind) {
    $(inputId).addEventListener("input", debounce(function () {
      var p = cleanPath($(inputId).value);
      if (!p) { $(hintId).textContent = ""; return; }
      apiPost("/api/check_path", { path: p }).then(function (d) {
        if (!d.ok || !d.exists) { $(hintId).textContent = "⚠ 路径不存在"; return; }
        if (expectKind && d.kind !== expectKind) {
          $(hintId).textContent = "⚠ 应为" + (expectKind === "dir" ? "目录" : "文件") +
            "，当前是" + (d.kind === "dir" ? "目录" : "文件");
          return;
        }
        $(hintId).textContent = "✓ 存在（" + (d.kind === "dir" ? "目录" : "文件") + "）";
      }).catch(function () { $(hintId).textContent = ""; });
    }, 400));
  }
  bindPathCheck("file-path", "file-check", "file");
  bindPathCheck("cache-path", "cache-check", "dir");

  // ================= U04 设置联动 + localStorage 偏好（T01：仅存偏好） =================
  var kwEnabled = $("kw-enabled"), kwInput = $("kw-input");
  var dedupEnabled = $("dedup-enabled"), dedupSlider = $("dedup-threshold");
  function keywordRequested() { return kwEnabled.checked && !!kwInput.value.trim(); }
  function currentRule() { return document.querySelector('input[name="frame-rule"]:checked').value; }
  kwEnabled.addEventListener("change", function () {
    kwInput.disabled = !kwEnabled.checked;
    fieldErr("err-kw", "");
    updateButtons();
    updateSummary();
    savePrefs();
  });
  kwInput.addEventListener("input", function () { updateButtons(); updateSummary(); savePrefs(); });
  dedupEnabled.addEventListener("change", function () {
    dedupSlider.disabled = !dedupEnabled.checked;
    updateSummary();
    savePrefs();
  });
  document.querySelectorAll('input[name="frame-rule"]').forEach(function (r) {
    r.addEventListener("change", function () {
      var sel = currentRule();
      $("frame-count").disabled = sel !== "count";
      $("frame-interval").disabled = sel !== "interval";
      updateSummary();
      savePrefs();
    });
  });
  dedupSlider.addEventListener("input", function () {
    $("dedup-val").textContent = dedupSlider.value + "%";
    updateSummary();
    savePrefs();
  });
  function sourceLabel() {
    if (currentSource === "file") return "本地文件";
    if (currentSource === "cache") return "B站缓存";
    return { single: "单个视频", multi: "多P", batch: "批量" }[currentTab];
  }
  function updateSummary() {
    var parts = [sourceLabel()];
    var rule = currentRule();
    if (rule === "count") parts.push("目标 " + ($("frame-count").value || "?") + " 帧");
    else if (rule === "interval") parts.push("每 " + ($("frame-interval").value || "?") + "s 一帧");
    else parts.push("正常截取");
    parts.push($("frame-width").value + " px");
    if (dedupEnabled.checked) parts.push("去重 " + dedupSlider.value + "%");
    if (kwEnabled.checked) {
      var kw = kwInput.value.trim();
      parts.push(kw ? "关键字：" + kw.split(/[\s,，、]+/).slice(0, 3).join("/")
        : "关键字（未填写）");
    }
    $("settings-summary").textContent = "当前设置：" + parts.join(" · ");
  }
  function savePrefs() {
    // T01：localStorage 只存表单偏好，不作任务记录
    try {
      localStorage.setItem("vw-prefs", JSON.stringify({
        source: currentSource, tab: currentTab, rule: currentRule(),
        frame_count: $("frame-count").value, frame_interval: $("frame-interval").value,
        frame_width: $("frame-width").value,
        kw_on: kwEnabled.checked, kw: kwInput.value,
        dedup_on: dedupEnabled.checked, dedup_t: dedupSlider.value
      }));
    } catch (e) { /* 隐私模式等场景忽略 */ }
  }
  function loadPrefs() {
    var p;
    try { p = JSON.parse(localStorage.getItem("vw-prefs") || "null"); } catch (e) { p = null; }
    if (!p) return;
    if (p.source && document.querySelector('[data-source="' + p.source + '"]')) {
      document.querySelector('[data-source="' + p.source + '"]').click();
    }
    if (p.tab && document.querySelector('[data-tab="' + p.tab + '"]')) {
      document.querySelector('[data-tab="' + p.tab + '"]').click();
    }
    if (p.rule && document.querySelector('input[name="frame-rule"][value="' + p.rule + '"]')) {
      document.querySelector('input[name="frame-rule"][value="' + p.rule + '"]').click();
    }
    if (p.frame_count) $("frame-count").value = p.frame_count;
    if (p.frame_interval) $("frame-interval").value = p.frame_interval;
    if (["512", "768", "1024"].indexOf(String(p.frame_width)) >= 0) $("frame-width").value = p.frame_width;
    if (typeof p.kw === "string") kwInput.value = p.kw;
    if (p.kw_on) { kwEnabled.checked = true; kwInput.disabled = false; }
    if (p.dedup_on) { dedupEnabled.checked = true; dedupSlider.disabled = false; }
    if (p.dedup_t) { dedupSlider.value = p.dedup_t; $("dedup-val").textContent = p.dedup_t + "%"; }
    updateSummary();
    updateButtons();
    // 上面的 tab/radio 点击会触发 savePrefs；恢复完后重写完整偏好，避免第二次刷新回默认。
    savePrefs();
  }

  // ================= 进度条 =================
  $("frame-width").addEventListener("change", function () { updateSummary(); savePrefs(); });
  var LANES = {
    transcript: { name: "文字稿", fill: $("fill-transcript"), pct: $("pct-transcript"),
      stage: $("stage-transcript"), bar: $("bar-transcript") },
    frames: { name: "关键帧", fill: $("fill-frames"), pct: $("pct-frames"),
      stage: $("stage-frames"), bar: $("bar-frames") }
  };
  function renderLane(lane, ch) {
    var L = LANES[lane];
    var state = ch ? ch.state : "idle";
    var pct = ch && typeof ch.percent === "number" ? ch.percent : null;
    if (state === "running" && pct === null) {
      L.fill.className = "fill indeterminate";
      L.fill.style.width = "30%";
    } else {
      L.fill.className = "fill " + state;
      L.fill.style.width = (pct || 0) + "%";
    }
    L.pct.textContent = (pct === null || state === "idle" || state === "skipped")
      ? "" : Math.round(pct) + "%";
    L.stage.textContent = ch ? (ch.stage || "") : "空闲";
    L.stage.className = "lane-stage " + state;
    L.bar.setAttribute("aria-valuenow", pct === null ? 0 : Math.round(pct));
    L.bar.setAttribute("aria-valuetext", L.stage.textContent);
  }

  // ================= 按钮通道锁（C08） =================
  var actionButtons = document.querySelectorAll("[data-action]");
  var submittingAction = null;
  function requiredChannels(action) {
    if (action === "transcript") return ["transcript"];
    if (action === "frames") return keywordRequested() ? ["transcript", "frames"] : ["frames"];
    return ["transcript", "frames"];
  }
  function updateButtons() {
    actionButtons.forEach(function (btn) {
      var action = btn.dataset.action;
      var lanes = requiredChannels(action);
      var busyLane = null;
      lanes.forEach(function (l) { if (laneJobs[l] && !busyLane) busyLane = l; });
      btn.disabled = !!submittingAction || !!busyLane;
      if (submittingAction) btn.title = "提交中…";
      else if (busyLane) {
        btn.title = (busyLane === "transcript" && action !== "transcript")
          ? "等待文字稿完成（关键字定位依赖文字稿）"
          : LANES[busyLane].name + "通道在跑";
      } else btn.title = "";
    });
  }

  // ================= 任务提交 =================
  var logEl = $("log"), resultsEl = $("results");

  function buildPayload(action) {
    var payload = {
      want_transcript: action !== "frames",
      want_frames: action !== "transcript"
    };
    if (currentSource === "file") {
      payload.mode = "single";
      payload.url = cleanPath($("file-path").value);
    } else if (currentSource === "cache") {
      payload.mode = "single";
      payload.url = cleanPath($("cache-path").value);
    } else if (currentTab === "single") {
      payload.mode = "single";
      payload.url = $("single-url").value.trim();
    } else if (currentTab === "multi") {
      var sel = selectedIndexes();
      if (sel.length) {
        var segs = toSegments(sel);
        if (segs.length === 1) {
          payload.mode = "multi";
          payload.url = $("multi-url").value.trim();
          payload.item = segToExpr(segs[0]);
        } else {
          payload.mode = "batch";
          var url = $("multi-url").value.trim();
          payload.urls = segs.map(function (s) { return { url: url, item: segToExpr(s) }; });
        }
      } else {
        payload.mode = "multi";
        payload.url = $("multi-url").value.trim();
        payload.item = $("multi-item").value.trim();
      }
    } else {
      payload.mode = "batch";
      payload.urls = $("batch-urls").value.split(/\r?\n/)
        .map(function (s) { return s.trim(); }).filter(function (s) { return s; });
    }
    if (!payload.want_frames) return payload;
    var rule = currentRule();
    payload.frame_rule = { type: rule };
    payload.frame_width = Number($("frame-width").value);
    if (rule === "count") payload.frame_rule.value = $("frame-count").value.trim();
    else if (rule === "interval") payload.frame_rule.value = $("frame-interval").value.trim();
    if (kwEnabled.checked) {
      payload.keyword = kwInput.value.trim();
      if (payload.keyword && !payload.want_transcript) payload.want_transcript = true;
    }
    payload.dedup = { enabled: dedupEnabled.checked,
                      threshold: parseInt(dedupSlider.value, 10) / 100 };
    return payload;
  }

  function clientValidate(payload, action) {
    if (currentSource === "file") {
      if (!payload.url) { fieldErr("err-file", "请填写本地文件路径"); return false; }
    } else if (currentSource === "cache") {
      if (!payload.url) { fieldErr("err-cache", "请填写缓存目录路径"); return false; }
    } else if (payload.mode === "batch") {
      if (!payload.urls.length) { fieldErr("err-batch", "请粘贴至少一个视频链接（每行一个）"); return false; }
    } else if (currentTab === "multi") {
      if (!payload.url) { fieldErr("err-multi", "请填写视频链接"); return false; }
      if (!payload.item) {
        fieldErr("err-multi", "请先「解析分P」勾选，或手填集数范围（不会默认全部处理）");
        return false;
      }
    } else if (!payload.url) {
      fieldErr("err-single", "请填写视频链接");
      return false;
    }
    // U04：只生成文字稿且不做关键字帧时，不校验无关抽帧设置
    var needFramesCheck = payload.want_frames;
    if (needFramesCheck) {
      if (payload.frame_rule.type === "count") {
        var n = Number(payload.frame_rule.value);
        if (!Number.isInteger(n) || n < 1 || n > 100) {
          fieldErr("err-frame-rule", "目标帧数需为 1–100 的整数");
          return false;
        }
        payload.frame_rule.value = n;
      } else if (payload.frame_rule.type === "interval") {
        var x = Number(payload.frame_rule.value);
        if (!(x >= 0.5 && x <= 600)) {
          fieldErr("err-frame-rule", "自定义间隔需为 0.5–600 秒");
          return false;
        }
        payload.frame_rule.value = x;
      }
      if (kwEnabled.checked && !payload.keyword) {
        fieldErr("err-kw", "已启用关键字定位，请填写关键字");
        return false;
      }
    }
    ["err-single", "err-multi", "err-batch", "err-file", "err-cache",
     "err-frame-rule", "err-kw", "err-actions"].forEach(function (id) { fieldErr(id, ""); });
    return true;
  }

  actionButtons.forEach(function (btn) {
    btn.addEventListener("click", function () {
      var action = btn.dataset.action;
      if (submittingAction) return;
      var payload = buildPayload(action);
      if (!clientValidate(payload, action)) return;
      submitPayload(payload, action);
    });
  });

  function submitPayload(payload, action) {
    var lanes = [];
    if (payload.want_transcript || (payload.want_frames && payload.keyword)) lanes.push("transcript");
    if (payload.want_frames) lanes.push("frames");
    for (var i = 0; i < lanes.length; i++) {
      if (laneJobs[lanes[i]]) { updateButtons(); return; }   // C08 覆盖保护
    }
    submittingAction = action || "both";
    updateButtons();
    apiPost("/api/jobs", payload).then(function (data) {
      if (!data.ok) { fieldErr("err-actions", data.error || "任务创建失败"); return; }
      attachJob(data.job_id, payload, lanes);
      startPolling();
    }).catch(function (e) { fieldErr("err-actions", "请求失败: " + e); })
      .finally(function () {
        submittingAction = null;
        updateButtons();
      });
  }

  function attachJob(jobId, payload, lanes) {
    var wasIdle = !activeJobIds().length;
    lanes.forEach(function (l) { laneJobs[l] = jobId; });
    laneByJob[jobId] = lanes;
    if (wasIdle) { jobOrder = []; jobCache = {}; }
    if (jobOrder.indexOf(jobId) < 0) jobOrder.push(jobId);
    updateButtons();
  }

  // ================= 轮询（U10：退避 + 404 终结） =================
  function activeJobIds() {
    var ids = [];
    ["transcript", "frames"].forEach(function (l) {
      var id = laneJobs[l];
      if (id && ids.indexOf(id) < 0) ids.push(id);
    });
    return ids;
  }
  function renderLogs() {
    // 多通道可以归属同一个 job，按 jobOrder 每个任务只渲染一次。
    var atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
    var lines = [];
    jobOrder.forEach(function (id) {
      var job = jobCache[id];
      if (!job) return;
      if (jobOrder.length > 1) lines.push("—— 任务 " + id + " ——");
      Array.prototype.push.apply(lines, job.log || []);
    });
    logEl.textContent = lines.length ? lines.join("\n") : "等待任务日志…";
    if (atBottom) logEl.scrollTop = logEl.scrollHeight;
  }
  function startPolling() {
    if (!pollTimer) pollTick();
  }
  function pollTick() {
    var ids = activeJobIds();
    if (!ids.length) { pollTimer = null; renderTaskList(); return; }
    var pending = ids.length;
    ids.forEach(function (id) {
      fetch("/api/jobs/" + id).then(function (r) {
        if (r.status === 404) {
          jobCache[id] = jobCache[id] || { log: [] };
          finishJob(id, { ok: false, status: "error", results: [],
                          error: "任务不存在（服务可能已重启）" });
          renderLogs();
          return null;
        }
        return r.json();
      }).then(function (job) {
        if (!job || !job.ok) return;
        $("conn-lost").hidden = true;
        pollDelay = 1000;
        jobCache[id] = job;
        taskRows[id] = job;
        (laneByJob[id] || []).forEach(function (lane) {
          if (job.progress && job.progress[lane]) renderLane(lane, job.progress[lane]);
        });
        renderLogs();
        renderTaskList();
        if (job.status !== "running" && job.status !== "queued" && job.status !== "cancelling") {
          finishJob(id, job);
        }
      }).catch(function (err) {
        console.error("任务更新失败", err);
        $("conn-lost").hidden = false;
        pollDelay = Math.min(pollDelay * 2, 10000);   // U10 指数退避
      }).finally(function () {
        if (--pending === 0) pollTimer = setTimeout(pollTick, pollDelay);
      });
    });
  }

  var STATUS_TEXT = {
    queued: "排队中", running: "运行中", cancelling: "取消中", cancelled: "已取消",
    done: "完成", partial: "部分完成", error: "失败", interrupted: "已中断（服务重启）"
  };
  var STATUS_DOT = { done: "ok", error: "err", partial: "warn", cancelled: "skip",
                     interrupted: "warn" };

  function finishJob(id, job) {
    taskRows[id] = job;
    if (!renderedJobs[id]) {
      renderedJobs[id] = true;
      feedResults(id, job);
      if (job.status === "partial") toast(job.error || "部分完成", true);
      if (job.status === "error" && job.error) toast(job.error, true);
      if (job.status === "cancelled") toast("任务已取消");
      if (job.status === "interrupted") toast("任务已中断（服务重启）", true);
    }
    (laneByJob[id] || []).forEach(function (lane) {
      if (laneJobs[lane] === id) laneJobs[lane] = null;
    });
    updateButtons();
    renderTaskList();
  }

  // ================= 任务列表（T01/T02：排队位置 + 取消 + 重试） =================
  function renderTaskList() {
    var box = $("task-list");
    box.innerHTML = "";
    var ids = Object.keys(taskRows);
    if (!ids.length) return;
    // 活动任务在前（按 jobOrder），其余按创建时间倒序
    ids.sort(function (a, b) {
      var sa = taskRows[a].status, sb = taskRows[b].status;
      var aActive = (sa === "running" || sa === "queued" || sa === "cancelling") ? 0 : 1;
      var bActive = (sb === "running" || sb === "queued" || sb === "cancelling") ? 0 : 1;
      if (aActive !== bActive) return aActive - bActive;
      return String(taskRows[b].created_at || "").localeCompare(String(taskRows[a].created_at || ""));
    });
    ids.slice(0, 8).forEach(function (id) {
      var job = taskRows[id];
      var row = el("div", "task-row");
      row.appendChild(el("span", "dot " + (STATUS_DOT[job.status] || "skip")));
      var firstTitle = null;
      (job.results || []).forEach(function (r) { if (!firstTitle && r.title) firstTitle = r.title; });
      var params = job.params || {};
      var name = firstTitle || (params.urls && params.urls[0]
        ? (typeof params.urls[0] === "string" ? params.urls[0] : params.urls[0].url)
        : id);
      row.appendChild(el("span", "task-title", String(name))).title = String(name);
      var st = STATUS_TEXT[job.status] || job.status;
      if (job.status === "queued" && job.queue_position) st += " #" + job.queue_position;
      row.appendChild(el("span", "task-status", st));
      if (job.status === "running" || job.status === "queued") {
        var cancelBtn = el("button", "btn small danger", "取消");
        cancelBtn.addEventListener("click", function () { cancelJob(id); });
        row.appendChild(cancelBtn);
      }
      if (job.status === "error" || job.status === "cancelled" || job.status === "interrupted") {
        var retryBtn = el("button", "btn small", "重试");
        retryBtn.addEventListener("click", function () { retryJob(id); });
        row.appendChild(retryBtn);
      }
      box.appendChild(row);
    });
  }

  function cancelJob(id) {
    apiPost("/api/jobs/" + id + "/cancel", {}).then(function (d) {
      if (!d.ok) toast(d.error || "取消失败", true);
    }).catch(function (e) { toast("取消请求失败: " + e, true); });
  }

  function retryJob(id) {
    // T04：以相同参数新建任务
    var job = taskRows[id];
    if (!job || !job.params) { toast("缺少任务参数，无法重试", true); return; }
    var p = Object.assign({}, job.params);
    if (p.mode !== "batch" && !p.url && p.urls && p.urls.length) {
      p.url = typeof p.urls[0] === "string" ? p.urls[0] : p.urls[0].url;
    }
    var action = p.want_transcript && p.want_frames ? "both"
      : (p.want_transcript ? "transcript" : "frames");
    submitPayload(p, action);
  }

  // ================= T01 历史恢复 =================
  function loadHistory() {
    fetch("/api/jobs?limit=10").then(function (r) { return r.json(); }).then(function (data) {
      if (!data.ok) return;
      (data.jobs || []).forEach(function (job) {
        taskRows[job.job_id] = job;
        if (job.status === "running" || job.status === "queued" || job.status === "cancelling") {
          // 同一会话刷新：恢复通道与轮询
          var lanes = [];
          if (job.params && job.params.want_transcript) lanes.push("transcript");
          if (job.params && job.params.want_frames) lanes.push("frames");
          laneByJob[job.job_id] = lanes;
          lanes.forEach(function (l) { laneJobs[l] = job.job_id; });
          if (jobOrder.indexOf(job.job_id) < 0) jobOrder.push(job.job_id);
          jobCache[job.job_id] = job;
          (job.progress || {}) && lanes.forEach(function (l) {
            if (job.progress[l]) renderLane(l, job.progress[l]);
          });
        } else {
          // 完成/部分完成/中断：结果区恢复（产物还在磁盘）
          renderedJobs[job.job_id] = true;
          feedResults(job.job_id, job);
        }
      });
      renderTaskList();
      updateButtons();
      if (activeJobIds().length) startPolling();
    }).catch(function () { /* 历史恢复失败不影响使用 */ });
  }

  // ================= T03 结果归组 =================
  function feedResults(jobId, job) {
    var results = job.results || [];
    if (!results.length && !job.error) return;
    resultsFeed.push({
      jobId: jobId,
      mediaKey: job.media_key || ("job:" + jobId),
      error: (job.status === "error" || job.status === "interrupted") ? job.error : null,
      status: job.status,
      createdAt: job.created_at || "",
      results: results
    });
    resultsFeed.sort(function (a, b) { return b.createdAt.localeCompare(a.createdAt); });
    renderResults();
  }

  function renderResults() {
    resultsEl.innerHTML = "";
    var groups = [], seen = {};
    resultsFeed.forEach(function (feed) {
      var k = feed.mediaKey;
      if (!seen[k]) { seen[k] = { key: k, feeds: [] }; groups.push(seen[k]); }
      seen[k].feeds.push(feed);
    });
    groups.forEach(function (g) { resultsEl.appendChild(renderGroup(g)); });
  }

  function renderGroup(g) {
    var card = el("div", "group-card");
    var title = el("div", "group-title");
    var firstOk = null, firstTitle = null;
    g.feeds.forEach(function (f) {
      (f.results || []).forEach(function (r) {
        if (!firstOk && r.ok) firstOk = r;
        if (!firstTitle && r.title) firstTitle = r.title;
      });
    });
    title.appendChild(el("span", "dot " + (firstOk ? "ok" : "err")));
    title.appendChild(document.createTextNode(firstTitle || "未命名来源"));
    card.appendChild(title);
    g.feeds.forEach(function (feed) {
      if (feed.error) {
        card.appendChild(el("div", "row status-err",
          (STATUS_TEXT[feed.status] || "失败") + "：" + feed.error));
      }
      (feed.results || []).forEach(function (r) {
        card.appendChild(renderResultRow(r, feed.jobId));
      });
    });
    return card;
  }

  function renderResultRow(r, jobId) {
    var box = el("div", "result-row");
    var title = el("div", "title");
    title.appendChild(el("span", "dot " + (r.ok ? "ok" : "err")));
    title.appendChild(document.createTextNode(r.title || r.url || "未命名"));
    box.appendChild(title);
    if (!r.ok && r.error) {
      var errRow = el("div", "row status-err", "错误：" + r.error);
      box.appendChild(errRow);
      if (r.download_error && r.download_error.hint) {
        box.appendChild(el("div", "row hint", "建议：" + r.download_error.hint));
      }
      var retryBtn = el("button", "btn small", "重试");
      retryBtn.addEventListener("click", function () { retryJob(jobId); });
      box.appendChild(el("div", "row")).appendChild(retryBtn);
    }
    if (r.reused_from) {
      box.appendChild(el("div", "row hint", "复用同源媒体（免重复下载）"));
    }
    if (r.run_dir) {
      var row = el("div", "row");
      row.appendChild(el("span", null, "产物目录："));
      var pathSpan = el("span", "path", r.run_dir);
      pathSpan.title = r.run_dir;
      row.appendChild(pathSpan);
      var copyBtn = el("button", "link-btn", "复制路径");
      copyBtn.addEventListener("click", function () { copyText(r.run_dir); });
      row.appendChild(copyBtn);
      box.appendChild(row);
    }
    var links = el("div", "row");
    var friendly = r.friendly || {};
    var available = r.availability || {};
    var transcriptPath = available.transcript_path || friendly.transcript_txt || r.transcript_txt;
    var framesPath = available.frames_path || friendly.frames_dir || r.frames_dir;
    var arts = r.artifacts || {};
    var tOk = arts.transcript ? arts.transcript.status === "succeeded" : !!transcriptPath;
    var fOk = arts.frames ? arts.frames.status === "succeeded" : !!framesPath;
    if (transcriptPath && tOk) {
      if (available.transcript !== false) {
        var a = el("a", null, "预览文字稿");
        a.href = "/api/files?path=" + encodeURIComponent(transcriptPath);
        a.target = "_blank";
        links.appendChild(a);
      } else links.appendChild(el("span", "hint", "文字稿：文件已移动或不存在"));
      // U07：阅读区入口（结构化 segments + 搜索/联动帧图）
      var readerBtn = el("button", "link-btn", "阅读文字稿");
      readerBtn.disabled = available.transcript === false;
      if (readerBtn.disabled) readerBtn.title = available.reason;
      readerBtn.addEventListener("click", function () { openReader(r, readerBtn); });
      links.appendChild(readerBtn);
    }
    if (framesPath && fOk) {
      var fbText, fbNote = null;
      if (r.dedup) {
        fbText = "浏览关键帧（" + r.dedup.kept_count + " 张）";
        if (r.dedup.dropped_count > 0) {
          fbNote = "原始 " + r.dedup.total_count + "，隐藏 " + r.dedup.dropped_count;
        }
      } else {
        var frameCount = (friendly.frame_count !== undefined) ? friendly.frame_count : r.frame_count;
        fbText = "浏览关键帧" + (typeof frameCount === "number" ? "（" + frameCount + " 帧）" : "");
      }
      var fb = el("button", "link-btn", fbText);
      fb.disabled = available.frames === false;
      if (fb.disabled) {
        fb.title = available.reason;
        links.appendChild(el("span", "hint", "关键帧：文件已移动或不存在"));
      }
      fb.addEventListener("click", function () {
        openFrames(framesPath, r.frames_json, r.run_dir, fb);
      });
      links.appendChild(fb);
      if (fbNote) links.appendChild(el("span", "hint", fbNote));
    }
    if (r.ok && r.run_dir && framesPath && fOk) {
      links.appendChild(makeExportPdfButton(r.run_dir, available.pdf !== false));
    }
    if (links.childNodes.length) box.appendChild(links);
    if (r.ok && ((arts.transcript || {}).status === "failed" || (arts.frames || {}).status === "failed"
                || (tOk && available.transcript === false) || (fOk && available.frames === false))) {
      var retry = el("button", "btn small", "重新生成");
      retry.addEventListener("click", function () { retryJob(jobId); });
      box.appendChild(el("div", "row")).appendChild(retry);
    }
    ["transcript", "frames"].forEach(function (k) {
      var art = arts[k];
      if (!art || art.status === "succeeded") return;
      var name = k === "transcript" ? "文字稿" : "关键帧";
      var label = { skipped: "已跳过", empty: "空结果", failed: "失败" }[art.status] || art.status;
      var row = el("div", "row");
      row.appendChild(el("span", "dot " + (art.status === "failed" ? "err" : "skip")));
      row.appendChild(document.createTextNode(
        name + "：" + label + (art.reason ? "（" + art.reason + "）" : "")));
      box.appendChild(row);
    });
    if (r.keyword) {
      box.appendChild(el("div", "row",
        "关键字命中 " + (r.keyword.total_matched === undefined ? r.keyword.matched : r.keyword.total_matched) +
        " 句，取样 " + r.keyword.matched + " 处，新增 " + r.keyword.added + " 帧"));
      if (r.keyword_report) box.appendChild(makeKeywordDetails(r));
    }
    if (r.dedup) {
      var dd = el("div", "row", "去重：保留 " + r.dedup.kept_count + " / 剔除 " + r.dedup.dropped_count);
      dd.dataset.dedupFor = r.run_dir || "";
      box.appendChild(dd);
    }
    return box;
  }

  // ================= 导出 PDF =================
  function makeExportPdfButton(runDir, available) {
    var group = el("span", "pdf-actions");
    var layout = el("select", "pdf-layout");
    layout.setAttribute("aria-label", "PDF 每页帧数");
    [6, 2, 1].forEach(function (n) {
      var option = el("option", null, "每页 " + n + " 帧");
      option.value = n;
      layout.appendChild(option);
    });
    var btn = el("button", "link-btn", "导出PDF");
    if (available === false) {
      btn.disabled = true;
      layout.disabled = true;
      btn.title = "文件已移动或不存在";
    }
    group.appendChild(layout);
    group.appendChild(btn);
    var pdfLink = null, errSpan = null;
    btn.addEventListener("click", function () {
      btn.disabled = true;
      layout.disabled = true;
      btn.textContent = "导出中…";
      apiPost("/api/export_pdf", { run_dir: runDir, per_page: Number(layout.value) }).then(function (data) {
        btn.disabled = false;
        layout.disabled = false;
        btn.textContent = "导出PDF";
        if (!data.ok) {
          if (!errSpan) {
            errSpan = el("span", "field-err");
            btn.parentNode.appendChild(errSpan);
          }
          errSpan.textContent = "导出失败：" + (data.error || "未知错误");
          return;
        }
        if (errSpan) errSpan.textContent = "";
        if (!pdfLink) {
          pdfLink = el("a", null, "关键帧.pdf");
          pdfLink.target = "_blank";
          btn.parentNode.insertBefore(pdfLink, btn.nextSibling);
        }
        pdfLink.href = "/api/files?path=" + encodeURIComponent(data.pdf) + "&v=" + Date.now();
        pdfLink.textContent = "查看 PDF（" + data.pages + " 页）";
        toast("PDF 已生成，每页 " + data.per_page + " 帧");
      }).catch(function (e) {
        btn.disabled = false;
        layout.disabled = false;
        btn.textContent = "导出PDF";
        toast("导出失败: " + e, true);
      });
    });
    return group;
  }

  function makeKeywordDetails(r) {
    var details = el("details", "keyword-details");
    details.appendChild(el("summary", null, "查看关键字命中明细"));
    var content = el("div", "keyword-content");
    details.appendChild(content);
    var loaded = false;
    details.addEventListener("toggle", function () {
      if (!details.open || loaded) return;
      loaded = true;
      content.textContent = "读取明细…";
      fetch("/api/files?path=" + encodeURIComponent(r.keyword_report)).then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      }).then(function (report) {
        content.textContent = "";
        content.appendChild(el("p", "hint", report.total_matched + " 句命中，取样 " + report.sampled_count +
          " 处。定位采用句子中点；同句重复词和多词不重复取帧。"));
        var reasons = { too_close: "与上一命中相距不足 1 秒", sample_limit: "超过 60 处，均匀取样未选中",
          invalid_time: "句子时间无效", no_video: "无可用视频", extraction_failed: "补帧失败",
          added: "新增帧", reused: "复用已有帧", frame_unavailable: "未取到可用帧" };
        if (!report.hits.length) content.appendChild(el("p", "hint", "文字稿中没有匹配句子，请检查关键字拼写或转写内容。"));
        var shown = 0;
        var list = el("div"); content.appendChild(list);
        var more = el("button", "link-btn", "继续显示 60 条");
        function appendPage() {
          report.hits.slice(shown, shown + 60).forEach(function (hit) {
            var row = el("div", "keyword-hit");
            row.appendChild(el("span", "hint", (hit.t === null ? "时间未知" : fmtT(hit.t)) +
              " · " + hit.keywords.join(" / ") + " · " + (reasons[hit.reason] || hit.reason)));
            row.appendChild(el("p", null, hit.sentence));
            if (hit.context_before || hit.context_after) row.appendChild(el("p", "hint",
              "前后文：" + [hit.context_before, hit.context_after].filter(Boolean).join(" / ")));
            if (hit.frame) {
              var image = el("a", null, "查看对应帧");
              image.href = "/api/files?path=" + encodeURIComponent(r.run_dir + "/frames/" + hit.frame);
              image.target = "_blank"; row.appendChild(image);
            }
            list.appendChild(row);
          });
          shown += 60; more.hidden = shown >= report.hits.length;
        }
        more.addEventListener("click", appendPage); content.appendChild(more); appendPage();
      }).catch(function (err) { loaded = false; content.textContent = "读取失败，收起后可重试：" + err.message; });
    });
    return details;
  }

  // ================= 复制 =================
  function copyText(text) {
    function ok() { toast("已复制"); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(ok, function () { fallbackCopy(text); ok(); });
    } else { fallbackCopy(text); ok(); }
  }
  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) { /* 忽略 */ }
    document.body.removeChild(ta);
  }

  // ================= 通用浮层（U11：Escape/焦点约束/焦点还原） =================
  function makeDialog(overlayId, dialogId, closeId, escapeHook) {
    var overlay = $(overlayId), dialog = $(dialogId);
    var lastTrigger = null;
    function onKeys(e) {
      if (e.key === "Escape") {
        e.preventDefault();
        // U08：Escape 逐级退出（如先看图大图 → 网格 → 关闭）
        if (escapeHook && escapeHook() === "handled") return;
        api.close();
        return;
      }
      if (e.key !== "Tab") return;
      var focusables = dialog.querySelectorAll("button, a[href], input, [tabindex='-1']");
      var list = Array.prototype.filter.call(focusables, function (n) {
        return n.offsetParent !== null || n === dialog;
      });
      if (!list.length) return;
      var first = list[0], last = list[list.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
    var api = {
      open: function (trigger) {
        lastTrigger = trigger || null;
        overlay.classList.add("show");
        document.addEventListener("keydown", onKeys, true);
        dialog.focus();
      },
      close: function () {
        overlay.classList.remove("show");
        document.removeEventListener("keydown", onKeys, true);
        if (lastTrigger) { lastTrigger.focus(); lastTrigger = null; }
      },
      isOpen: function () { return overlay.classList.contains("show"); }
    };
    $(closeId).addEventListener("click", api.close);
    overlay.addEventListener("click", function (e) { if (e.target === overlay) api.close(); });
    return api;
  }

  var framesDialog = makeDialog("frames-overlay", "frames-dialog", "frames-close",
    function () {
      // Escape 逐级：大图 → 网格；网格 → 关闭（返回 undefined 让默认 close 接管）
      if (!bigBox.hidden) { backToGrid(); return "handled"; }
      return undefined;
    });
  var readerDialog = makeDialog("reader-overlay", "reader-dialog", "reader-close");

  // ================= U08 帧图查看器（网格/大图/筛选/恢复） =================
  var viewer = { entries: null, dir: null, runDir: null, showHidden: false, bigIdx: -1 };
  var grid = $("frames-grid"), bigBox = $("frames-big"), bigImg = $("big-img");

  function visibleEntries() {
    if (!viewer.entries) return [];
    return viewer.entries.filter(function (e) { return viewer.showHidden || !e.dropped; });
  }
  function renderViewerGrid() {
    grid.innerHTML = "";
    bigBox.hidden = true;
    grid.hidden = false;
    var entries = viewer.entries || [];
    var kept = entries.filter(function (e) { return !e.dropped; }).length;
    $("frames-title").textContent = "浏览关键帧（" + kept + " 张保留" +
      (viewer.showHidden ? "，含已隐藏 " + (entries.length - kept) + " 张）" : "）");
    $("frames-filter").textContent = viewer.showHidden ? "仅看保留" : "查看已隐藏";
    $("frames-filter").style.display =
      kept === entries.length ? "none" : "";
    if (!entries.length) { grid.appendChild(el("div", "empty", "没有帧图")); return; }
    var shown = 0;
    entries.forEach(function (entry) {
      if (entry.dropped && !viewer.showHidden) return;
      shown++;
      var t = (typeof entry.actual_t === "number") ? entry.actual_t : entry.t;
      var cell = el("div", "thumb" + (entry.dropped ? " dropped" : ""));
      var ph = el("div", "ph");
      var img = document.createElement("img");
      img.loading = "lazy";
      img.src = "/api/files?path=" + encodeURIComponent(viewer.dir + "/" + entry.file);
      img.alt = entry.file;
      ph.appendChild(img);
      cell.appendChild(ph);
      var cap = entry.file + (typeof t === "number" ? "  t=" + fmtT(t) : "");
      cell.appendChild(el("div", "fname", cap));
      cell.addEventListener("click", function () { openBig(entry); });
      grid.appendChild(cell);
    });
    if (!shown) grid.appendChild(el("div", "empty", "没有可展示的帧（可能全部被去重剔除）"));
  }
  function openBig(entry) {
    grid.hidden = true;
    bigBox.hidden = false;
    viewer.bigIdx = visibleEntries().indexOf(entry);
    showBig();
  }
  function showBig() {
    var list = visibleEntries();
    if (viewer.bigIdx < 0) viewer.bigIdx = 0;
    if (viewer.bigIdx >= list.length) viewer.bigIdx = list.length - 1;
    if (!list.length) { backToGrid(); return; }
    var entry = list[viewer.bigIdx];
    var t = (typeof entry.actual_t === "number") ? entry.actual_t : entry.t;
    bigImg.src = "/api/files?path=" + encodeURIComponent(viewer.dir + "/" + entry.file);
    $("big-cap").textContent = entry.file +
      (typeof t === "number" ? "  t=" + fmtT(t) : "") +
      "（" + (viewer.bigIdx + 1) + "/" + list.length + "）" +
      (entry.dropped ? "（已隐藏）" : "");
    $("big-prev").disabled = viewer.bigIdx <= 0;
    $("big-next").disabled = viewer.bigIdx >= list.length - 1;
    $("big-restore").hidden = !entry.dropped;
    $("big-copy").onclick = function () {
      copyText("t=" + fmtT(t));
    };
    $("big-restore").onclick = function () { restoreFrame(entry); };
  }
  function backToGrid() {
    viewer.bigIdx = -1;
    bigBox.hidden = true;
    grid.hidden = false;
    renderViewerGrid();
  }
  $("big-prev").addEventListener("click", function () { viewer.bigIdx--; showBig(); });
  $("big-next").addEventListener("click", function () { viewer.bigIdx++; showBig(); });
  $("big-back").addEventListener("click", backToGrid);
  $("frames-filter").addEventListener("click", function () {
    viewer.showHidden = !viewer.showHidden;
    if (bigBox.hidden) renderViewerGrid(); else showBig();
  });
  // 大图方向键导航 + Escape 逐级退出（网格态 Escape 由 dialog 处理关闭）
  document.addEventListener("keydown", function (e) {
    if (!framesDialog.isOpen() || bigBox.hidden) return;
    if (e.key === "ArrowLeft") { viewer.bigIdx--; showBig(); }
    else if (e.key === "ArrowRight") { viewer.bigIdx++; showBig(); }
  });
  function restoreFrame(entry) {
    apiPost("/api/dedup_restore", { run_dir: viewer.runDir, file: entry.file })
      .then(function (d) {
        if (!d.ok) { toast(d.error || "恢复失败", true); return; }
        delete entry.dropped;
        delete entry.dedup_similarity;
        toast("已恢复此帧");
        showBig();
        syncDedupCounts(viewer.runDir, d);
      })
      .catch(function (e) { toast("恢复请求失败: " + e, true); });
  }
  function syncDedupCounts(runDir, d) {
    // 卡片/索引/PDF 口径一致更新
    resultsFeed.forEach(function (feed) {
      (feed.results || []).forEach(function (r) {
        if (r.run_dir === runDir && r.dedup) {
          r.dedup.kept_count = d.kept_count;
          r.dedup.dropped_count = d.dropped_count;
          r.dedup.total_count = d.total_count;
          if (r.friendly) r.friendly.frame_count = d.kept_count;
        }
      });
    });
    renderResults();
  }
  function openFrames(dir, framesJsonPath, runDir, triggerBtn) {
    viewer.runDir = runDir || null;
    viewer.entries = null;   // 清空旧会话，跳转定位等异步加载完成后再设置
    viewer.dir = null;
    viewer.showHidden = false;
    viewer.bigIdx = -1;
    grid.innerHTML = "";
    grid.hidden = false;
    bigBox.hidden = true;
    framesDialog.open(triggerBtn);
    grid.appendChild(el("div", "empty", "加载中…"));
    var jsonPath = framesJsonPath || (dir.replace(/[\\/]+$/, "") + "/frames.json");
    fetch("/api/files?path=" + encodeURIComponent(jsonPath))
      .then(function (r) {
        if (!r.ok) return { __missing: true };   // 404 → 允许回退目录清单
        return r.json();
      })
      .then(function (data) {
        if (data && data.__missing) { listFramesDir(dir); return; }
        if (!Array.isArray(data)) {
          // U08：索引损坏不无提示回退（防止已隐藏帧莫名重现）
          grid.innerHTML = "";
          grid.appendChild(el("div", "empty",
            "frames.json 内容损坏，无法按索引展示（未回退全量目录，避免已隐藏帧重现）"));
          return;
        }
        viewer.entries = data;
        viewer.dir = jsonPath.replace(/[\\/][^\\/]*$/, "");
        renderViewerGrid();
      })
      .catch(function () {
        grid.innerHTML = "";
        grid.appendChild(el("div", "empty",
          "frames.json 读取失败（可能已损坏）；未回退全量目录"));
      });
  }
  function listFramesDir(dir) {
    fetch("/api/files?path=" + encodeURIComponent(dir))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        grid.innerHTML = "";
        if (!data.ok) { grid.appendChild(el("div", "empty", data.error || "加载失败")); return; }
        viewer.entries = (data.files || []).filter(function (f) {
          return /\.(jpe?g|png)$/i.test(f.name);
        }).map(function (f) { return { file: f.name }; });
        viewer.dir = data.dir.replace(/[\\/]+$/, "");
        if (!viewer.entries.length) {
          grid.appendChild(el("div", "empty", "该目录下没有帧图"));
          return;
        }
        renderViewerGrid();
      })
      .catch(function (e) {
        grid.innerHTML = "";
        grid.appendChild(el("div", "empty", "加载失败: " + e));
      });
  }

  // ================= U07 文字稿阅读区 =================
  var reader = { segs: [], shown: 0, query: "", matches: [], matchIdx: -1,
                 result: null, frames: null };
  var READER_PAGE = 100;
  $("reader-search").addEventListener("input", debounce(function () {
    reader.query = $("reader-search").value.trim();
    renderSegments(true);
  }, 250));
  $("reader-more").addEventListener("click", function () { renderSegments(false); });
  $("reader-prev").addEventListener("click", function () { jumpMatch(-1); });
  $("reader-next").addEventListener("click", function () { jumpMatch(1); });
  $("reader-copy-ts").addEventListener("click", function () {
    copyText(reader.segs.map(function (s) {
      return "[" + fmtT(s.start) + "] " + s.text;
    }).join("\n"));
  });
  $("reader-copy").addEventListener("click", function () {
    copyText(reader.segs.map(function (s) { return s.text; }).join("\n"));
  });

  function openReader(r, triggerBtn) {
    reader.result = r;
    reader.segs = [];
    reader.shown = 0;
    reader.query = "";
    $("reader-search").value = "";
    $("reader-title").textContent = "阅读文字稿：" + (r.title || "未命名");
    var runDir = r.run_dir;
    var txtPath = (r.friendly && r.friendly.transcript_txt) || r.transcript_txt;
    var base = txtPath.replace(/[\\/][^\\/]*$/, "");
    $("reader-dl-txt").href = "/api/files?path=" + encodeURIComponent(txtPath);
    var srt = txtPath.replace(/\.txt$/i, ".srt");
    $("reader-dl-srt").href = "/api/files?path=" + encodeURIComponent(srt);
    readerDialog.open(triggerBtn);
    var segsBox = $("reader-segs");
    segsBox.innerHTML = "";
    $("reader-empty").hidden = true;
    segsBox.appendChild(el("div", "empty", "加载中…"));
    fetch("/api/files?path=" + encodeURIComponent(base + "/transcript.json"))
      .then(function (resp) {
        if (!resp.ok) throw new Error("no transcript.json");
        return resp.json();
      })
      .then(function (data) {
        if (!Array.isArray(data)) throw new Error("bad transcript.json");
        reader.segs = data.filter(function (s) {
          return s && typeof s.start === "number" && s.text;
        });
        if (!reader.segs.length) throw new Error("empty");
        segsBox.innerHTML = "";
        renderSegments(true);
      })
      .catch(function () {
        // U07：无结构化文字稿时解释原因（产物状态 reason）
        segsBox.innerHTML = "";
        var art = (r.artifacts || {}).transcript || {};
        $("reader-empty").hidden = false;
        $("reader-empty").textContent = "无法加载结构化文字稿" +
          (art.reason ? "（" + art.reason + "）" : "（transcript.json 缺失或已移动）");
      });
    // 预取帧索引，供时间戳联动
    if (r.frames_json) {
      fetch("/api/files?path=" + encodeURIComponent(r.frames_json))
        .then(function (resp) { return resp.json(); })
        .then(function (data) { if (Array.isArray(data)) reader.frames = data; })
        .catch(function () { reader.frames = null; });
    } else {
      reader.frames = null;
    }
  }

  function renderSegments(reset) {
    var segsBox = $("reader-segs");
    if (reset) { reader.shown = 0; segsBox.innerHTML = ""; }
    var end = Math.min(reader.shown + READER_PAGE, reader.segs.length);
    for (var i = reader.shown; i < end; i++) {
      segsBox.appendChild(renderSeg(reader.segs[i], i));
    }
    reader.shown = end;
    $("reader-more").hidden = reader.shown >= reader.segs.length;
    applySearch();
  }
  function renderSeg(seg, idx) {
    var row = el("div", "seg");
    row.dataset.idx = idx;
    var t = el("span", "seg-t", fmtT(seg.start));
    t.title = "点击定位到最近关键帧";
    t.addEventListener("click", function () { jumpToFrame(seg.start); });
    row.appendChild(t);
    var text = el("span", "seg-text");
    text.textContent = seg.text;   // 一律 textContent 防注入
    row.appendChild(text);
    return row;
  }
  function applySearch() {
    var q = reader.query;
    reader.matches = [];
    reader.matchIdx = -1;
    document.querySelectorAll("#reader-segs .seg-text").forEach(function (node) {
      var text = node.textContent;
      node.textContent = text;   // 先清旧 mark（重建文本节点）
      node.innerHTML = "";
      if (!q) { node.textContent = text; return; }
      var lower = text.toLowerCase(), ql = q.toLowerCase();
      var pos = 0, hit = false;
      while (true) {
        var at = lower.indexOf(ql, pos);
        if (at < 0) break;
        hit = true;
        node.appendChild(document.createTextNode(text.slice(pos, at)));
        var mark = document.createElement("mark");
        mark.textContent = text.slice(at, at + q.length);
        node.appendChild(mark);
        pos = at + q.length;
      }
      node.appendChild(document.createTextNode(text.slice(pos)));
      if (hit) reader.matches.push(node.closest(".seg"));
    });
    $("reader-count").textContent = q ? reader.matches.length + " 处匹配" : "";
  }
  function jumpMatch(step) {
    if (!reader.matches.length) return;
    reader.matchIdx = (reader.matchIdx + step + reader.matches.length) % reader.matches.length;
    var row = reader.matches[reader.matchIdx];
    row.scrollIntoView({ block: "center" });
  }
  function jumpToFrame(t) {
    if (!reader.frames || !reader.frames.length || !reader.result) {
      toast("该任务没有可用关键帧", true);
      return;
    }
    // 最近帧（actual_t 回退 t）
    var best = null, bestDist = Infinity;
    reader.frames.forEach(function (f) {
      if (f.dropped) return;
      var ft = (typeof f.actual_t === "number") ? f.actual_t : f.t;
      if (typeof ft !== "number") return;
      var d = Math.abs(ft - t);
      if (d < bestDist) { bestDist = d; best = f; }
    });
    if (!best) { toast("没有可定位的关键帧", true); return; }
    var framesJsonPath = reader.result.frames_json;
    openFrames((reader.result.friendly && reader.result.friendly.frames_dir)
               || reader.result.frames_dir, framesJsonPath, reader.result.run_dir, null);
    // 打开后直接进大图定位
    var check = setInterval(function () {
      if (!viewer.entries) return;
      clearInterval(check);
      var entry = viewer.entries.filter(function (e) {
        return e.file === best.file;
      })[0];
      if (entry) openBig(entry);
    }, 100);
    setTimeout(function () { clearInterval(check); }, 3000);
  }

  // ================= 设置栏折叠（U01 <1100px） =================
  $("settings-toggle").addEventListener("click", function () {
    var body = $("settings-body");
    body.classList.toggle("open");
    $("settings-toggle").setAttribute("aria-expanded",
      body.classList.contains("open") ? "true" : "false");
  });

  // ================= 启动 =================
  loadPrefs();
  updateSummary();
  updateButtons();
  loadHistory();
})();
