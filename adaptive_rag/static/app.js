const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = {
  token: "",
  connected: false,
  capabilities: null,
  stages: [],
  trace: null,
  result: null,
  traceId: "",
  selected: null,
  running: false,
  controller: null,
  runs: [],
  tab: "flow",
  started: 0,
  timer: null,
  sessionEpoch: 0,
  viewEpoch: 0,
  sourceEpoch: 0,
  connecting: false,
};

const labels = {
  "decision.preflight": ["前置决策", "根据当前策略决定是否进入检索。"],
  planner: ["问题规划", "理解问题，拆分需要检索的子问题。"],
  rewrite: ["查询改写", "为检索生成更明确的查询表达。"],
  multi_query: ["多查询扩展", "生成不同角度的查询变体。"],
  hyde: ["HyDE 扩展", "用假设文档辅助召回。"],
  snapshot: ["固定知识快照", "为本次检索选择一个不可变知识版本。"],
  authorized_chunks: [
    "权限与范围过滤",
    "按当前身份、权限和 SKU 筛选可检索的块。",
  ],
  lexical: ["BM25 关键词检索", "根据词项匹配召回候选内容。"],
  dense: ["向量语义检索", "按语义相似度召回授权候选内容。"],
  fusion: ["RRF 结果融合", "融合各检索通道的排名，合并重复命中。"],
  reranker: ["相关性重排", "对融合后的候选执行已配置的排序适配器。"],
  parent_backfill: ["父块回填", "将检索到的子块回填为更完整的上下文。"],
  judge: ["证据充分性检查", "判断证据是否覆盖各个子问题。"],
  "decision.gap": ["补检索决策", "根据缺项和剩余预算判断是否补充检索。"],
  gap: ["缺项查询生成", "针对未覆盖的问题生成定向补检查询。"],
  "decision.generate": ["生成前决策", "判断是否继续进入答案生成阶段。"],
  generator: [
    "生成引用回答",
    "根据当前证据生成回答草稿与引用。草稿需要后续核验。",
  ],
  verifier: ["回答与引用核验", "执行当前配置的引用、支持性及完整性检查。"],
  final_access_check: [
    "最终权限复核",
    "在返回结果前重新确认当前证据读取权限。",
  ],
};
const roleNames = {
  parser: "文档解析",
  cleaner: "文本清洗",
  chunker: "父子切块",
  store: "知识存储",
  objects: "原文归档",
  trace: "链路记录",
  decision: "决策策略",
  planner: "问题规划",
  rewrite: "查询改写",
  multi_query: "多查询",
  hyde: "HyDE",
  dense: "语义检索",
  lexical: "关键词检索",
  fusion: "结果融合",
  reranker: "相关性重排",
  judge: "证据检查",
  gap: "定向补检",
  generator: "答案生成",
  verifier: "回答核验",
};
const statuses = {
  ok: "已完成",
  running: "执行中",
  disabled: "未启用",
  skipped: "已跳过",
  error: "失败",
  timeout: "超时",
  cancelled: "已停止",
  answered: "已回答",
  partial: "部分回答",
  insufficient: "证据不足",
  evidence_preview: "证据预览",
  no_index: "暂无知识索引",
  empty: "无可用证据",
  clarify: "需要补充信息",
  capability_unavailable: "能力暂不可用",
  policy_stopped: "策略已停止",
};
const nameFor = (name) =>
  labels[name]?.[0] || labels[name.split(".")[0]]?.[0] || name;
const descriptionFor = (name) =>
  labels[name]?.[1] ||
  labels[name.split(".")[0]]?.[1] ||
  "查看该适配器实际执行的结构化结果。";
const number = (value) =>
  typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })
    : "—";
const ms = (value) => (typeof value === "number" ? `${number(value)} ms` : "—");
function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = String(text);
  return node;
}
function json(value) {
  return JSON.stringify(value, null, 2);
}
function empty(target, text) {
  target.replaceChildren(el("div", "plain-empty", text));
}
function notice(message = "", type = "") {
  const node = $("#notice");
  node.textContent = message;
  node.className = `run-notice ${type}`;
  node.hidden = !message;
}
const failedStatuses = ["error", "timeout", "cancelled"];
function failureDetails(stage, trace = state.trace) {
  if (stage?.error?.message) return stage.error;
  const call = trace?.usage?.calls?.findLast((item) =>
    ["network_error", "timeout", "http_error", "invalid_response"].includes(
      item.status,
    ),
  );
  if (call?.error?.message) return call.error;
  if (call?.status === "network_error")
    return {
      code: "cloud_network_error",
      message:
        "云模型连接中断，本次请求未获得完整响应。已保留此前完成的步骤，可手动重试。",
    };
  if (call?.status === "timeout" || stage?.status === "timeout")
    return {
      code: "request_timeout",
      message: "此步骤已超时，未获得有效返回。请稍后手动重试。",
    };
  if (call?.status === "http_error")
    return {
      code: "cloud_http_error",
      message: `云平台返回请求错误${call.http_status ? `（HTTP ${call.http_status}）` : ""}，请检查模型服务状态与配置。`,
    };
  if (call?.status === "invalid_response")
    return {
      code: "cloud_invalid_response",
      message: "云平台未返回符合协议的数据，本步骤没有有效返回。",
    };
  if (stage?.status === "cancelled")
    return { code: "query_cancelled", message: "查询已停止，此步骤没有完成。" };
  if (stage?.reason === "output_validation_failed")
    return {
      code: "output_validation_failed",
      message: "插件返回未通过数据校验，已停止后续执行。",
    };
  return {
    code: "step_failed",
    message: "此步骤执行失败，没有产生有效返回。已完成步骤可在执行链路中查看。",
  };
}
function failureMessage(trace = state.trace) {
  const stage = trace?.stages?.findLast((item) =>
    failedStatuses.includes(item.status),
  );
  const detail = failureDetails(stage, trace);
  return `${stage ? `${nameFor(stage.name)}：` : ""}${detail.message}`;
}
let toastTimer;
function toast(message) {
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    $("#toast").hidden = true;
  }, 3000);
}

function dialog(id) {
  if (!$(id).open) $(id).showModal();
}
$$("[data-close]").forEach((button) =>
  button.addEventListener("click", () =>
    document.getElementById(button.dataset.close).close(),
  ),
);
$$("dialog").forEach((node) =>
  node.addEventListener("click", (event) => {
    if (event.target === node) {
      const r = node.getBoundingClientRect();
      if (
        event.clientX < r.left ||
        event.clientX > r.right ||
        event.clientY < r.top ||
        event.clientY > r.bottom
      )
        node.close();
    }
  }),
);
$("#connect-open").addEventListener("click", () => {
  dialog("#connection-dialog");
  $("#api-token").focus();
});
$("#plugins-open").addEventListener("click", () => dialog("#plugins-dialog"));

async function api(path, options = {}, token = state.token) {
  const response = await fetch(path, {
    ...options,
    cache: "no-store",
    credentials: "omit",
    headers: { ...options.headers, Authorization: `Bearer ${token}` },
  });
  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）`;
    try {
      const body = await response.json();
      message = body.error?.message || body.detail || message;
    } catch {}
    if (response.status === 401)
      message = "工作台令牌无效或已失效，请重新连接。";
    if (response.status === 403)
      message = "当前身份没有管理员权限，无法查看执行链路。";
    if (response.status === 429) message = "请求过于频繁，请稍后重试。";
    throw new Error(message);
  }
  return response;
}

$("#connect-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.running || state.connecting) return;
  const token = $("#api-token").value.trim();
  if (!token) return;
  const previous = state.token;
  const epoch = state.sessionEpoch;
  state.connecting = true;
  $("#connect-submit").disabled = true;
  $("#connection-error").textContent = "";
  try {
    const session = await (await api("/console/session", {}, token)).json();
    if (epoch !== state.sessionEpoch) return;
    if (!session.admin) throw new Error("此工作台需要管理员身份。");
    // A new principal must never inherit the previous principal's page data.
    if (previous !== token) {
      state.sessionEpoch++;
      clearData();
    }
    state.token = token;
    state.connected = true;
    state.capabilities = session.capabilities;
    $("#connection-label").textContent = "工作台已连接";
    $("#connection-dot").classList.add("connected");
    $("#disconnect-button").hidden = false;
    $("#api-token").value = "";
    renderPlugins();
    $("#connection-dialog").close();
    toast("已连接，读取到实际链路配置。");
  } catch (error) {
    if (epoch === state.sessionEpoch)
      $("#connection-error").textContent = error.message;
  } finally {
    state.connecting = false;
    $("#connect-submit").disabled = false;
  }
});

function disconnect() {
  state.sessionEpoch++;
  if (state.controller) state.controller.abort();
  state.controller = null;
  setRunning(false);
  state.token = "";
  state.connected = false;
  state.capabilities = null;
  clearData();
  $("#connection-label").textContent = "连接工作台";
  $("#connection-dot").classList.remove("connected");
  $("#disconnect-button").hidden = true;
  $("#api-token").value = "";
  $("#connection-dialog").close();
  renderPlugins();
  toast("已断开连接，页面数据已清空。");
}
$("#disconnect-button").addEventListener("click", disconnect);

function renderPlugins() {
  const plugins = state.capabilities?.plugins;
  $("#plugin-summary").replaceChildren();
  $("#plugins-list").replaceChildren();
  if (!plugins) {
    $("#mode-label").textContent = "未连接";
    $("#plugin-summary").append(el("p", "muted", "连接后读取实际插件配置。"));
    $("#plugins-list").textContent = "连接后查看实际配置。";
    return;
  }
  $("#mode-label").textContent =
    state.capabilities.mode === "cloud" ? "云端已接入" : "无模型模式";
  for (const role of [
    "planner",
    "lexical",
    "dense",
    "reranker",
    "generator",
    "verifier",
  ]) {
    const plugin = plugins[role];
    if (!plugin) continue;
    const row = el("div", "plugin-line");
    row.append(el("span", "", roleNames[role]));
    const value = el("span", "plugin-value");
    const dot = el("span", "status-dot");
    if (!plugin.enabled) dot.style.background = "#b9c0ad";
    value.append(
      dot,
      document.createTextNode(
        plugin.plugin || plugin.name || plugin.selector || "—",
      ),
    );
    row.append(value);
    $("#plugin-summary").append(row);
  }
  for (const [role, plugin] of Object.entries(plugins)) {
    const tile = el("div", "plugin-tile");
    tile.append(
      el("strong", "", roleNames[role] || role),
      el(
        "code",
        "",
        `${role} / ${plugin.plugin || plugin.name || plugin.selector || "—"}`,
      ),
      el(
        "span",
        "",
        `${plugin.enabled ? "已启用" : "未启用"}${plugin.requires_model ? " · 云端模型" : ""}`,
      ),
    );
    $("#plugins-list").append(tile);
  }
}

function setRunning(value) {
  state.running = value;
  $("#run-button").disabled = value;
  $("#run-button span").textContent = value ? "正在检索…" : "开始检索";
  $("#cancel-button").hidden = !value;
  $("#connect-submit").disabled = value;
  $("#history-clear").disabled = value;
  $("#trace-form button").disabled = value;
  $("#connect-open").disabled = value;
  $("#timeline-dot").classList.toggle("running-dot", value);
  clearInterval(state.timer);
  if (value)
    state.timer = setInterval(() => {
      if (state.running) {
        $("#metric-duration").replaceChildren(
          document.createTextNode(number(performance.now() - state.started)),
          el("small", "", " ms"),
        );
        $("#duration-caption").textContent = "页面等待耗时 · 实时";
      }
    }, 150);
}
function resetCurrent() {
  state.viewEpoch++;
  state.sourceEpoch++;
  state.stages = [];
  state.trace = null;
  state.result = null;
  state.traceId = "";
  state.selected = null;
  $("#timeline").replaceChildren();
  $("#inspector-body").replaceChildren(
    el("div", "plain-empty", "步骤开始后，点击查看详情。"),
  );
  $("#inspector-heading").textContent = "步骤详情";
  $("#inspector-subtitle").textContent = "细节，比一个答案更多。";
  $("#raw-data").textContent = "等待服务器返回实际运行数据…";
  $("#trace-label").textContent = "正在建立连接，等待服务器生成编号";
  $("#trace-copy").disabled = true;
  $("#export-button").disabled = true;
  $("#metric-duration").replaceChildren(
    document.createTextNode("—"),
    el("small", "", " ms"),
  );
  $("#metric-stages").textContent = "0";
  $("#metric-lanes").textContent = "—";
  $("#metric-evidence").textContent = "—";
  $("#duration-caption").textContent = "等待服务器执行";
  $("#stages-caption").textContent = "按实际运行记录";
  $("#stage-count").textContent = "0";
  $("#evidence-count").textContent = "0";
  $("#run-status").textContent = "连接中";
  $("#timeline-heading").textContent = "等待服务器开始执行";
  empty($("#panel-answer"), "查询执行中，核验完成后展示回答。");
  empty($("#panel-evidence"), "查询执行中，权限复核后展示证据。");
  empty($("#panel-models"), "查询完成后展示实际模型调用。");
  $$(".pipeline-map>div").forEach((node) =>
    node.classList.remove("done", "running"),
  );
  notice();
  switchTab("flow");
}
function clearData() {
  resetCurrent();
  empty($("#panel-answer"), "开始检索后，查看核验完成的回答。");
  empty($("#panel-evidence"), "开始检索后，查看授权引用证据。");
  empty($("#panel-models"), "开始检索后，查看实际模型调用。");
  state.runs = [];
  $("#recent-runs").replaceChildren(
    el("p", "muted", "运行记录仅保留在当前页面。"),
  );
  $("#source-content").replaceChildren();
  if ($("#source-dialog").open) $("#source-dialog").close();
  $("#question").value = "";
  $("#history").value = "";
  $("#sku").value = "";
  $("#trace-id").value = "";
  $("#char-count").textContent = "0 / 4000";
  $("#run-status").textContent = "待运行";
  $("#timeline-heading").textContent = "准备就绪，等待一个好问题";
  $("#trace-label").textContent = "尚未开始，提交问题后自动生成";
  $("#timeline").replaceChildren(
    el("div", "plain-empty", "输入问题，开始追踪一次真实检索。"),
  );
}

$("#question").addEventListener("input", () => {
  $("#char-count").textContent = `${$("#question").value.length} / 4000`;
});
$("#question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    $("#query-form").requestSubmit();
  }
});
$("#query-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.running) return;
  if (state.connecting) {
    toast("正在验证连接，请稍候。");
    return;
  }
  if (!state.connected) {
    dialog("#connection-dialog");
    return;
  }
  const question = $("#question").value.trim();
  if (!question) {
    toast("请输入一个非空问题。");
    return;
  }
  const history = $("#history")
    .value.split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  if (history.length > 6 || history.some((line) => line.length > 4000)) {
    toast("历史上下文最多 6 轮，每轮不超过 4000 字。");
    return;
  }
  const sku = $("#sku").value.trim();
  if (sku && !/^[\p{L}\p{N}_.:-]{1,128}$/u.test(sku)) {
    toast("SKU 仅支持字母、数字、下划线、点、冒号和连字符。");
    return;
  }
  resetCurrent();
  state.started = performance.now();
  setRunning(true);
  const controller = new AbortController();
  state.controller = controller;
  const epoch = state.viewEpoch;
  const session = state.sessionEpoch;
  const current = () =>
    epoch === state.viewEpoch && session === state.sessionEpoch;
  let terminal = false;
  try {
    const response = await api("/query/stream", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify({
        question,
        top_k: Number($("#top-k").value),
        history,
        ...(sku ? { sku } : {}),
      }),
      signal: controller.signal,
    });
    if (!response.headers.get("content-type")?.includes("text/event-stream"))
      throw new Error("服务器没有返回实时链路，请检查服务版本。");
    await readStream(response, (type, data) => {
      if (!current()) return;
      if (type === "trace") {
        state.traceId = data.trace_id;
        $("#trace-label").textContent = state.traceId;
        $("#trace-copy").disabled = false;
      } else if (type === "stage") {
        acceptStage(data);
      } else if (type === "result") {
        terminal = true;
        applyRun(data.result, data.trace);
        saveRun(question);
      } else if (type === "error") {
        terminal = true;
        if (data.trace_id) {
          state.traceId = data.trace_id;
          $("#trace-label").textContent = state.traceId;
          $("#trace-copy").disabled = false;
        }
        if (data.trace) {
          applyRun(data.trace.final_result || null, data.trace);
          saveRun(question);
        }
        throw new Error(data.message || "查询执行失败。");
      }
    });
    if (current() && !terminal)
      throw new Error(
        "连接提前结束，尚未收到最终结果。可使用 Trace ID 检查执行记录。",
      );
  } catch (error) {
    if (!current()) return;
    let aborted = error.name === "AbortError" || controller.signal.aborted;
    let diagnosticLoadError = "";
    // Read-only recovery also supports older servers and an interrupted SSE
    // connection. It never repeats the paid query.
    if (!aborted && state.traceId && !state.trace) {
      try {
        const trace = await (
          await api(`/traces/${encodeURIComponent(state.traceId)}`, {
            signal: controller.signal,
          })
        ).json();
        if (!current()) return;
        applyRun(trace.final_result || null, trace);
        saveRun(question);
      } catch (loadError) {
        if (!current()) return;
        aborted = loadError.name === "AbortError" || controller.signal.aborted;
        if (!aborted)
          diagnosticLoadError = ` 执行记录暂不可读取：${loadError.message}`;
      }
    }
    if (!current()) return;
    if (state.trace) {
      const failed = failedStatuses.includes(state.trace.status);
      if (failed) notice(`${failureMessage()}${diagnosticLoadError}`, "error");
      else
        notice(
          "实时连接中断后，已按当前权限恢复完整执行记录。请查看实际结果。",
        );
      return;
    }
    $("#run-status").textContent = aborted ? "已停止" : "执行失败";
    $("#timeline-heading").textContent = aborted ? "查询已停止" : "执行未完成";
    notice(
      aborted
        ? "已停止等待并取消查询。已完成的步骤保留在时间线中。"
        : `${error.message}${state.traceId ? ` 本次 Trace ID：${state.traceId}。` : ""}${diagnosticLoadError}`,
      aborted ? "" : "error",
    );
    for (const stage of state.stages) {
      if (stage.status === "running")
        stage.status = aborted ? "cancelled" : "error";
    }
    renderTimeline();
    state.selected = state.stages.findLastIndex((stage) =>
      failedStatuses.includes(stage.status),
    );
    renderInspector(state.selected);
    empty(
      $("#panel-answer"),
      aborted
        ? "查询已停止，未获得核验完成的回答。"
        : "查询执行失败，未返回最终回答。请查看失败步骤。",
    );
    empty(
      $("#panel-evidence"),
      "本次未获得最终授权证据。已完成步骤的记录可通过 Trace 查看。",
    );
    empty(
      $("#panel-models"),
      "未能读取完整调用记录。可按本次 Trace ID 重新加载。",
    );
    $("#duration-caption").textContent = "页面等待耗时";
  } finally {
    if (state.controller === controller) {
      setRunning(false);
      state.controller = null;
    }
  }
});
$("#cancel-button").addEventListener("click", () => state.controller?.abort());

async function readStream(response, consume) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      buffer += done
        ? decoder.decode()
        : decoder.decode(value, { stream: true });
      if (buffer.length > 8_000_000)
        throw new Error("单次事件过大，已停止展示。");
      let match;
      while ((match = /\r?\n\r?\n/.exec(buffer))) {
        const block = buffer.slice(0, match.index);
        buffer = buffer.slice(match.index + match[0].length);
        let type = "message";
        const parts = [];
        for (const line of block.split(/\r?\n/)) {
          if (line.startsWith("event:")) type = line.slice(6).trim();
          else if (line.startsWith("data:"))
            parts.push(line.slice(5).replace(/^ /, ""));
        }
        if (parts.length) consume(type, JSON.parse(parts.join("\n")));
      }
      if (done) {
        if (buffer.trim() && !buffer.trim().startsWith(":"))
          throw new Error("实时事件不完整。");
        break;
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

function acceptStage(data) {
  const index = Number.isInteger(data.index) ? data.index : state.stages.length;
  state.stages[index] = { ...state.stages[index], ...data };
  if (state.selected === null) state.selected = index;
  $("#run-status").textContent = "执行中";
  $("#timeline-heading").textContent =
    data.status === "running"
      ? `正在${nameFor(data.name)}`
      : `${nameFor(data.name)} · ${statuses[data.status] || data.status}`;
  renderTimeline();
  renderMetrics();
  if (state.selected === index) renderInspector(index);
}
function phase(name) {
  if (/^(planner|rewrite|multi_query|hyde|decision.preflight)/.test(name))
    return "plan";
  if (/^(snapshot|authorized_chunks|lexical|dense|gap|decision.gap)/.test(name))
    return "retrieve";
  if (/^(fusion|reranker|parent_backfill|judge)/.test(name)) return "rank";
  if (/^(generator|decision.generate)/.test(name)) return "generate";
  return "verify";
}
function renderTimeline() {
  const target = $("#timeline");
  const scroll = target.scrollTop;
  target.replaceChildren();
  state.stages.forEach((stage, index) => {
    if (!stage) return;
    if (
      !$("#show-disabled").checked &&
      ["disabled", "skipped"].includes(stage.status)
    )
      return;
    const row = el("div", "timeline-row");
    row.dataset.status = stage.status;
    row.append(
      el(
        "span",
        "stage-node",
        stage.status === "ok"
          ? "✓"
          : ["disabled", "skipped"].includes(stage.status)
            ? "–"
            : String(index + 1).padStart(2, "0"),
      ),
    );
    const button = el(
      "button",
      `stage-button${state.selected === index ? " selected" : ""}`,
    );
    button.type = "button";
    button.setAttribute(
      "aria-label",
      `${index + 1}. ${nameFor(stage.name)} ${statuses[stage.status] || stage.status} ${ms(stage.duration_ms)}`,
    );
    const main = el("span", "stage-main");
    main.append(
      el("span", "stage-name", nameFor(stage.name)),
      el(
        "span",
        "stage-code",
        `${stage.name} · ${statuses[stage.status] || stage.status}`,
      ),
    );
    button.append(
      main,
      el(
        "span",
        "stage-time",
        stage.status === "running" ? "运行中" : ms(stage.duration_ms),
      ),
      el("span", "stage-arrow", "↗"),
    );
    button.addEventListener("click", () => {
      state.selected = index;
      renderTimeline();
      renderInspector(index);
    });
    row.append(button);
    target.append(row);
  });
  if (!target.childElementCount)
    target.append(
      el(
        "div",
        "plain-empty",
        state.stages.length ? "当前筛选下没有步骤。" : "等待实际步骤…",
      ),
    );
  target.scrollTop = scroll;
  $$(".pipeline-map>div").forEach((node) => {
    const rows = state.stages.filter(
      (stage) =>
        stage &&
        !stage.name.startsWith("decision.") &&
        phase(stage.name) === node.dataset.phase,
    );
    node.classList.toggle(
      "running",
      rows.some((stage) => stage.status === "running"),
    );
    node.classList.toggle(
      "done",
      rows.some((stage) => stage.status === "ok") &&
        !rows.some((stage) => stage.status === "running"),
    );
  });
}
$("#show-disabled").addEventListener("change", renderTimeline);
function renderMetrics() {
  const complete = state.stages.filter(
    (stage) => stage && stage.status !== "running",
  ).length;
  $("#metric-stages").textContent = String(state.stages.length);
  $("#stages-caption").textContent = `${complete} 步已结束`;
  $("#stage-count").textContent = String(state.stages.length);
  if (state.trace) {
    $("#metric-duration").replaceChildren(
      document.createTextNode(number(state.trace.duration_ms)),
      el("small", "", " ms"),
    );
    $("#duration-caption").textContent = "服务端实测 · 含链路处理";
    $("#metric-lanes").textContent = number(
      state.trace.lane_count ?? state.trace.retrieval_lanes?.length ?? 0,
    );
    $("#metric-evidence").textContent = number(
      state.result?.evidence?.length ?? state.trace.evidence?.length ?? 0,
    );
  } else {
    $("#metric-lanes").textContent = String(
      state.stages.filter((stage) =>
        /^(lexical|dense)\./.test(stage?.name || ""),
      ).length,
    );
  }
}

function applyRun(result, trace) {
  clearInterval(state.timer);
  state.result = result || null;
  state.trace = trace || null;
  state.stages = trace?.stages || state.stages;
  state.traceId = result?.trace_id || trace?.trace_id || state.traceId;
  $("#trace-label").textContent = state.traceId || "未提供";
  $("#trace-copy").disabled = !state.traceId;
  $("#export-button").disabled = false;
  const status = result?.status || trace?.status || "ok";
  $("#run-status").textContent = statuses[status] || status;
  $("#timeline-heading").textContent =
    `执行结束 · ${statuses[status] || status}`;
  state.selected = state.stages.findLastIndex(
    (stage) => !["disabled", "skipped"].includes(stage.status),
  );
  const failedIndex = state.stages.findLastIndex((stage) =>
    failedStatuses.includes(stage.status),
  );
  if (failedIndex >= 0) state.selected = failedIndex;
  renderTimeline();
  renderMetrics();
  renderInspector(state.selected);
  renderAnswer();
  renderEvidence();
  renderModels();
  $("#raw-data").textContent = json({
    result: state.result,
    trace: state.trace,
  });
  if (
    [
      "partial",
      "insufficient",
      "no_index",
      "empty",
      "clarify",
      "capability_unavailable",
    ].includes(status)
  )
    notice(result?.message || `本次结果：${statuses[status]}`);
  else if (failedStatuses.includes(status))
    notice(failureMessage(trace), "error");
  else notice();
}

function renderInspector(index) {
  const stage = state.stages[index];
  if (!stage) return;
  $("#inspector-heading").textContent = nameFor(stage.name);
  $("#inspector-subtitle").textContent =
    `步骤 ${String(index + 1).padStart(2, "0")} / ${statuses[stage.status] || stage.status}`;
  const target = $("#inspector-body");
  target.replaceChildren(
    el("p", "inspector-code", stage.name),
    el("p", "inspector-description", descriptionFor(stage.name)),
  );
  const meta = el("div", "inspector-meta");
  for (const [label, value] of [
    ["实际耗时", ms(stage.duration_ms)],
    ["开始偏移", ms(stage.started_ms)],
    ["执行状态", statuses[stage.status] || stage.status],
    ["结果来源", "服务器 Trace"],
  ]) {
    const item = el("div");
    item.append(el("span", "", label), el("strong", "", value));
    meta.append(item);
  }
  target.append(meta);
  if (stage.error_type)
    target.append(el("p", "output-summary", `异常类型：${stage.error_type}`));
  if (failedStatuses.includes(stage.status)) {
    const detail = failureDetails(stage);
    target.append(el("p", "run-notice error", detail.message));
    target.append(el("p", "stage-code", `诊断代码：${detail.code}`));
  }
  if (stage.reason)
    target.append(
      el(
        "p",
        "output-summary",
        `${["disabled", "skipped"].includes(stage.status) ? "跳过原因" : "处理原因"}：${stage.reason}`,
      ),
    );
  const section = el("div", "inspector-section");
  section.append(el("h3", "", "结构化返回结果"));
  if (stage.output !== undefined) {
    renderStageOutput(section, stage.output);
  } else
    section.append(
      el(
        "p",
        "output-summary",
        stage.status === "running"
          ? "步骤正在执行，服务端计时中。"
          : failedStatuses.includes(stage.status)
            ? "本步骤未正常完成，没有有效返回。此前已完成步骤的返回内容保留在执行链路中。"
            : ["disabled", "skipped"].includes(stage.status)
              ? "本次没有调用此步骤。"
              : state.trace
                ? "此 Trace 未记录该步骤的返回内容。"
                : "查询完成并通过权限复核后，展示返回内容。",
      ),
    );
  target.append(section);
}

function renderStageOutput(target, output) {
  const items = output?.hits || output?.parents || output?.queries;
  if (output?.query) target.append(el("p", "output-query", output.query));
  if (Array.isArray(items)) {
    target.append(el("p", "output-summary", `实际返回 ${items.length} 条记录`));
    const list = el("div", "output-items");
    items.forEach((item, index) => {
      const card = el("div", "output-item");
      const caption = el("div", "output-item-top");
      caption.append(
        el("span", "", item.query_id || `#${item.rank ?? index + 1}`),
        el(
          "span",
          "",
          item.score !== undefined ? `score ${number(item.score)}` : "",
        ),
      );
      card.append(caption);
      if (item.document_id)
        card.append(el("p", "output-item-title", item.document_id));
      if (item.text_preview || item.text)
        card.append(
          el("p", "output-item-text", item.text_preview || item.text),
        );
      if (item.preview_truncated)
        card.append(
          el(
            "p",
            "output-item-note",
            "预览已截取；完整授权证据见「证据」视图。",
          ),
        );
      if (item.chunk_id || item.parent_id)
        card.append(el("p", "stage-code", item.chunk_id || item.parent_id));
      list.append(card);
    });
    target.append(list);
    const details = el("details", "output-details");
    details.append(
      el("summary", "", "查看完整结构化数据"),
      el("pre", "json-view", json(output)),
    );
    target.append(details);
  } else target.append(el("pre", "json-view", json(output)));
}

function renderAnswer() {
  const target = $("#panel-answer");
  target.replaceChildren();
  const result = state.result;
  if (!result) {
    empty(
      target,
      failedStatuses.includes(state.trace?.status)
        ? "本次执行未完成，没有生成核验通过的最终回答。请查看失败步骤；此前完成的检索结果仍可逐步查看。"
        : "此历史 Trace 未记录最终回答，详情请查看步骤和证据。",
    );
    return;
  }
  target.append(
    el(
      "div",
      "answer-status",
      `${statuses[result.status] || result.status} · ${result.message || ""}`,
    ),
  );
  if (result.answer) target.append(el("div", "answer-text", result.answer));
  else
    target.append(
      el(
        "p",
        "muted",
        result.status === "evidence_preview"
          ? "当前模式返回证据预览，尚未启用答案生成。"
          : "本次没有返回可确认的最终答案。",
      ),
    );
  if (result.claims?.length) {
    const list = el("div", "claim-list");
    list.append(el("h3", "", "逐条结论与出处"));
    for (const claim of result.claims) {
      const item = el("div", "claim");
      item.append(el("p", "", claim.text));
      for (const id of claim.evidence_ids || []) {
        const position = (result.evidence || []).findIndex(
          (e) => e.evidence_id === id,
        );
        const button = el(
          "button",
          "citation-button",
          position >= 0 ? `证据 ${position + 1} ↗` : id,
        );
        button.type = "button";
        button.title = id;
        button.addEventListener("click", () => {
          switchTab("evidence");
          const card = document.getElementById(`evidence-${position}`);
          card?.scrollIntoView({ behavior: "smooth", block: "nearest" });
          card?.focus({ preventScroll: true });
        });
        item.append(button);
      }
      list.append(item);
    }
    target.append(list);
  }
  if (result.missing_query_ids?.length)
    target.append(
      el(
        "div",
        "missing-box",
        `仍需补充的子问题：${result.missing_query_ids.join("、")}`,
      ),
    );
}

function renderEvidence() {
  const evidence = state.result?.evidence || state.trace?.evidence || [];
  $("#evidence-count").textContent = String(evidence.length);
  const target = $("#panel-evidence");
  target.replaceChildren();
  if (!evidence.length) {
    empty(target, "本次没有可展示的授权证据。");
    return;
  }
  evidence.forEach((item, index) => {
    const card = el("article", "evidence-card");
    card.id = `evidence-${index}`;
    card.tabIndex = -1;
    const top = el("div", "evidence-top");
    top.append(
      el("h3", "", item.document_id || "未命名文档"),
      el("span", "evidence-number", `E${String(index + 1).padStart(2, "0")}`),
    );
    const bottom = el("div", "evidence-bottom");
    bottom.append(
      el(
        "span",
        "",
        `${item.version || "—"} · ${item.query_ids?.join(", ") || "证据"}${item.truncated ? " · 已按预算截取" : ""}`,
      ),
    );
    const button = el("button", "source-button", "查看原文 ↗");
    button.type = "button";
    button.addEventListener("click", () => showSource(item));
    bottom.append(button);
    card.append(
      top,
      el(
        "p",
        "evidence-path",
        [...(item.heading_path || []), item.locator]
          .filter(Boolean)
          .join(" / "),
      ),
      el("p", "evidence-text", item.text || ""),
      bottom,
    );
    const details = el("details");
    details.append(
      el("summary", "", "引用元数据"),
      el(
        "pre",
        "",
        json(
          Object.fromEntries(
            Object.entries(item).filter(([key]) => key !== "text"),
          ),
        ),
      ),
    );
    card.append(details);
    target.append(card);
  });
}
async function showSource(item) {
  dialog("#source-dialog");
  const target = $("#source-content");
  const epoch = ++state.sourceEpoch;
  const session = state.sessionEpoch;
  target.replaceChildren(el("p", "muted", "正在回源并复核当前权限…"));
  try {
    const generation =
      item.generation || state.result?.generation || state.trace?.generation;
    const response = await api(
      `/evidence/${encodeURIComponent(generation)}/${encodeURIComponent(item.evidence_id)}`,
    );
    const data = await response.json();
    if (epoch !== state.sourceEpoch || session !== state.sessionEpoch) return;
    target.replaceChildren(
      el(
        "p",
        "source-meta",
        `${data.document_id} / ${data.version}\n${(data.heading_path || []).join(" / ")}\n${data.locator || ""}${data.truncated ? "\n按原查询证据预算截取，未包含整个文档。" : ""}`,
      ),
      el("div", "source-body", data.text),
    );
  } catch (error) {
    if (epoch === state.sourceEpoch && session === state.sessionEpoch)
      target.replaceChildren(
        el("div", "form-error", `无法读取证据：${error.message}`),
      );
  }
}

function renderModels() {
  const target = $("#panel-models");
  target.replaceChildren();
  const usage = state.trace?.usage || state.result?.capabilities?.usage;
  if (!usage) {
    empty(target, "此 Trace 未记录模型调用用量。");
    return;
  }
  const metrics = el("div", "usage-metrics");
  for (const [label, value, caption] of [
    ["模型请求", state.trace?.model_calls ?? usage.model_calls, "含失败与重试"],
    ["输入 Token", usage.input_tokens, "按平台返回记录"],
    ["输出 Token", usage.output_tokens, "按平台返回记录"],
  ]) {
    const item = el("div");
    item.append(
      el("span", "", label),
      el("strong", "", number(value)),
      el("span", "", caption),
    );
    metrics.append(item);
  }
  target.append(metrics);
  const calls = usage.calls || [];
  if (!calls.length) {
    target.append(el("div", "plain-empty", "本次没有执行模型调用。"));
    return;
  }
  const wrap = el("div", "table-scroll");
  const table = el("table", "data-table");
  const head = el("thead");
  const row = el("tr");
  for (const label of ["模型 / 接口", "尝试", "状态", "耗时"])
    row.append(el("th", "", label));
  head.append(row);
  const body = el("tbody");
  for (const call of calls) {
    const row = el("tr");
    const model = el("td");
    model.append(
      document.createTextNode(call.model || "—"),
      el("div", "stage-code", call.endpoint || ""),
    );
    row.append(
      model,
      el("td", "", call.attempt ?? "—"),
      el(
        "td",
        "",
        `${call.status || "—"}${call.http_status ? ` / ${call.http_status}` : ""}`,
      ),
      el("td", "", ms(call.duration_ms)),
    );
    if (call.error_code || call.error_type)
      row.children[2].append(
        el(
          "div",
          "output-summary",
          [call.error_code, call.error_type].filter(Boolean).join(" / "),
        ),
      );
    body.append(row);
  }
  table.append(head, body);
  wrap.append(table);
  target.append(wrap);
  const details = el("details");
  details.append(el("summary", "", "完整用量记录"), el("pre", "", json(usage)));
  target.append(details);
}

function switchTab(name) {
  state.tab = name;
  $$("[data-tab]").forEach((button) => {
    const active = button.dataset.tab === name;
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  $$(".tab-panel").forEach(
    (panel) => (panel.hidden = panel.id !== `panel-${name}`),
  );
}
$$("[data-tab]").forEach((button) => {
  button.addEventListener("click", () => switchTab(button.dataset.tab));
  button.addEventListener("keydown", (event) => {
    const tabs = $$("[data-tab]");
    const index = tabs.indexOf(button);
    let next;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft")
      next = (index + tabs.length - 1) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else return;
    event.preventDefault();
    switchTab(tabs[next].dataset.tab);
    tabs[next].focus();
  });
});
function saveRun(question) {
  if (!state.traceId) return;
  state.runs = state.runs.filter((run) => run.id !== state.traceId);
  state.runs.unshift({
    id: state.traceId,
    question,
    result: state.result,
    trace: state.trace,
  });
  state.runs = state.runs.slice(0, 8);
  renderRuns();
}
function renderRuns() {
  $("#recent-runs").replaceChildren();
  if (!state.runs.length) {
    $("#recent-runs").append(el("p", "muted", "运行记录仅保留在当前页面。"));
    return;
  }
  state.runs.forEach((run, index) => {
    const button = el("button", "recent-run");
    button.type = "button";
    button.append(
      el("span", "", String(index + 1).padStart(2, "0")),
      el("span", "recent-title", run.question),
      el("time", "", ms(run.trace?.duration_ms)),
    );
    button.title = run.question;
    button.addEventListener("click", () => {
      if (state.running) {
        toast("请先等待当前查询结束。");
        return;
      }
      loadTrace(run.id);
    });
    $("#recent-runs").append(button);
  });
}
$("#history-clear").addEventListener("click", () => {
  if (state.running) return;
  clearData();
  toast("本次会话记录已清空。");
});
async function loadTrace(id) {
  if (!state.connected) {
    dialog("#connection-dialog");
    return;
  }
  if (state.running) return;
  const epoch = ++state.viewEpoch;
  const session = state.sessionEpoch;
  $("#trace-form button").disabled = true;
  try {
    const trace = await (await api(`/traces/${encodeURIComponent(id)}`)).json();
    if (epoch !== state.viewEpoch || session !== state.sessionEpoch) return;
    resetCurrent();
    applyRun(trace.final_result || null, trace);
    $("#question").value = trace.question || "";
    $("#char-count").textContent = `${$("#question").value.length} / 4000`;
    toast("已按当前权限加载 Trace。");
  } catch (error) {
    if (epoch === state.viewEpoch && session === state.sessionEpoch)
      notice(error.message, "error");
  } finally {
    if (!state.running) $("#trace-form button").disabled = false;
  }
}
$("#trace-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const id = $("#trace-id").value.trim();
  if (id) loadTrace(id);
});
$("#trace-copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(state.traceId);
    toast("Trace ID 已复制。");
  } catch {
    toast("无法访问剪贴板，可从「本次 Trace ID」区域手动复制。");
  }
});
$("#export-button").addEventListener("click", () => {
  const blob = new Blob([json({ result: state.result, trace: state.trace })], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const link = el("a");
  link.href = url;
  link.download = `rag-trace-${state.traceId || "run"}.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    if (!state.running) {
      dialog("#connection-dialog");
      $("#api-token").focus();
    }
  }
});
