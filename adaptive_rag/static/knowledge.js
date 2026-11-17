"use strict";

const knowledgeById = id => document.getElementById(id);
let knowledgeGeneration = null;
let knowledgeBusy = false;

function knowledgeElement(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

async function knowledgeApi(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {credentials: "same-origin", cache: "no-store", ...options,
      headers: {"Content-Type": "application/json", ...options.headers}});
  } catch { throw Error("无法连接服务，请检查网络或服务状态后重试。"); }
  if (response.status === 401) { location.href = "/"; throw Error("请先登录工作台。"); }
  let value;
  try { value = response.status === 204 ? null : await response.json(); }
  catch { throw Error(`服务返回格式错误（HTTP ${response.status}），请稍后重试。`); }
  if (!response.ok) throw Error(response.status === 403 ? "只有管理员可以管理知识和数据分类。" : value?.error?.message || `请求失败（HTTP ${response.status}）`);
  return value;
}

function knowledgeNotice(message, error = false) {
  const node = knowledgeById("knowledge-notice");
  node.textContent = message;
  node.hidden = !message;
  node.classList.toggle("error", error);
}

function resetKnowledgeForm() {
  knowledgeById("knowledge-ingest-form").reset();
  knowledgeById("knowledge-id").value = `doc-${crypto.randomUUID()}`;
  RagDomains.render(knowledgeById("knowledge-data-domains"), []);
}

function documentAction(label, callback, errorNode) {
  const button = knowledgeElement("button", label);
  button.type = "button";
  button.addEventListener("click", async () => {
    if (knowledgeBusy) { errorNode.textContent = "另一项资料变更正在执行，请稍候再试。"; return; }
    knowledgeBusy = true;
    button.disabled = true;
    errorNode.textContent = "";
    try { await callback(); }
    catch (error) { errorNode.textContent = error.message; knowledgeNotice(error.message, true); }
    finally { knowledgeBusy = false; button.disabled = false; }
  });
  return button;
}

function renderKnowledgeDocuments(items) {
  const root = knowledgeById("knowledge-documents");
  root.replaceChildren();
  knowledgeById("knowledge-count").textContent = `${items.length} 份资料`;
  if (!items.length) { root.append(knowledgeElement("div", "暂无资料。先在左侧暂存一份资料，再发布进入知识库。", "plain-empty")); return; }
  for (const item of items) {
    const card = knowledgeElement("article", undefined, "knowledge-document");
    const header = knowledgeElement("div", undefined, "knowledge-document-header");
    const title = knowledgeElement("div");
    title.append(knowledgeElement("h3", item.title || item.document_id),
      knowledgeElement("p", `ID：${item.document_id} · 版本：${item.version || "—"}`, "knowledge-document-meta"));
    const pending = Array.isArray(item.staged_jobs) ? item.staged_jobs : [];
    const state = item.revoked ? "访问已撤销"
      : item.published ? (pending.length ? `已发布 · ${pending.length} 个新版本待发布` : "已发布") : "待发布";
    header.append(title, knowledgeElement("span", state, `knowledge-publish-state${item.revoked ? " revoked" : ""}`));
    card.append(header, knowledgeElement("p", `数据分类：${RagDomains.text(item.data_domains)}`, "knowledge-document-meta"));
    if (item.allowed_subjects?.length) card.append(knowledgeElement("p", "本资料还设有额外的账号/分组权限，分类调整会保留这些限制。", "knowledge-document-meta"));
    const choices = knowledgeElement("div");
    choices.setAttribute("role", "group");
    choices.setAttribute("aria-label", `${item.title || item.document_id} 的数据分类`);
    RagDomains.render(choices, item.data_domains || []);
    const actions = knowledgeElement("div", undefined, "knowledge-document-actions");
    const error = knowledgeElement("div", "", "knowledge-document-error");
    error.setAttribute("role", "alert");
    actions.append(documentAction("保存分类", async () => {
      const dataDomains = RagDomains.values(choices);
      if (!dataDomains.length) throw Error("请至少选择一个数据分类。未分类的历史资料会继续仅管理员可见。");
      await knowledgeApi("/documents/access", {method: "POST", body: JSON.stringify({document_id: item.document_id, data_domains: dataDomains})});
      await refreshKnowledgeDocuments();
      knowledgeNotice(`“${item.title || item.document_id}”的分类已更新，后续检索和引用将按新分类鉴权。`);
    }, error));
    // The backend lists every unpublished revision in `staged_jobs` (newest first), so a
    // document can have several versions waiting; each one gets its own publish action.
    for (const job of pending) {
      const versionLabel = job.version || job.revision_id.slice(0, 8);
      const label = item.published || pending.length > 1 ? `发布版本 ${versionLabel}` : "发布资料";
      actions.append(documentAction(label, async () => {
        await knowledgeApi("/publish", {method: "POST", body: JSON.stringify({job_id: job.job_id, expected_generation: knowledgeGeneration})});
        await refreshKnowledgeDocuments();
        knowledgeNotice(`“${job.title || item.title || item.document_id}”版本 ${versionLabel} 已发布。若需调整分类，可直接在本卡片保存。`);
      }, error));
    }
    if (item.revoked) card.append(knowledgeElement("p", "此资料当前已撤销访问；保存分类不会自动恢复访问。", "knowledge-document-meta"));
    card.append(choices, actions, error);
    root.append(card);
  }
}

async function refreshKnowledgeDocuments() {
  const value = await knowledgeApi("/documents");
  knowledgeGeneration = value.generation ?? null;
  knowledgeById("knowledge-generation").textContent = knowledgeGeneration ? `当前快照：${knowledgeGeneration}` : "尚无已发布知识快照";
  const documents = value.documents || [];
  renderKnowledgeDocuments(documents);
  return documents;
}

knowledgeById("knowledge-file").addEventListener("change", async () => {
  const file = knowledgeById("knowledge-file").files[0];
  if (!file) return;
  const errorNode = knowledgeById("knowledge-form-error");
  errorNode.textContent = "";
  try {
    if (file.size > 2_000_000 || !/\.(md|markdown|txt|html|htm)$/i.test(file.name)) throw Error("请选择不超过 2MB 的 .md、.txt 或 .html 文件。PDF 请先通过现有解析入口处理。");
    knowledgeById("knowledge-content").value = await file.text();
    if (!knowledgeById("knowledge-title").value.trim()) knowledgeById("knowledge-title").value = file.name.slice(0, 300);
    knowledgeById("knowledge-format").value = /\.html?$/i.test(file.name) ? "text/html" : /\.txt$/i.test(file.name) ? "text/plain" : "text/markdown";
  } catch (error) { errorNode.textContent = error.message; knowledgeById("knowledge-file").value = ""; }
});

knowledgeById("knowledge-ingest-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (knowledgeBusy) { knowledgeById("knowledge-form-error").textContent = "另一项资料变更正在执行，请稍候再试。"; return; }
  const button = knowledgeById("knowledge-ingest");
  knowledgeBusy = true; button.disabled = true;
  knowledgeById("knowledge-form-error").textContent = "";
  try {
    const dataDomains = RagDomains.values(knowledgeById("knowledge-data-domains"));
    if (!dataDomains.length) throw Error("请选择资料的数据分类，再进行暂存。");
    const title = knowledgeById("knowledge-title").value.trim();
    const documentId = knowledgeById("knowledge-id").value.trim();
    const value = await knowledgeApi("/documents", {method: "POST", body: JSON.stringify({
      document_id: documentId, version: knowledgeById("knowledge-version").value.trim(),
      title, content: knowledgeById("knowledge-content").value,
      media_type: knowledgeById("knowledge-format").value, data_domains: dataDomains,
    })});
    resetKnowledgeForm();
    const documents = await refreshKnowledgeDocuments();
    // Classification belongs to the document, not to a version. When this id already existed the
    // server keeps its current classification, so say so instead of letting the choice vanish.
    const live = documents.find(item => item.document_id === documentId);
    const sameDomains = (left, right) => [...left].sort().join(",") === [...right].sort().join(",");
    if (live && !sameDomains(live.data_domains || [], dataDomains)) {
      knowledgeNotice(`“${title}”已暂存，尚未发布。注意：这份资料已存在，数据分类属于资料本身、不随版本变化，仍为“${RagDomains.text(live.data_domains)}”，本次勾选的分类没有生效。如需调整，请在下方资料卡片修改并保存分类。任务 ID：${value.job_id}`, true);
    } else {
      knowledgeNotice(`“${title}”已暂存，尚未发布。请在资料卡片上点击发布按钮。任务 ID：${value.job_id}`);
    }
  } catch (error) { knowledgeById("knowledge-form-error").textContent = error.message; }
  finally { knowledgeBusy = false; button.disabled = false; }
});

knowledgeById("knowledge-refresh").addEventListener("click", async () => {
  const button = knowledgeById("knowledge-refresh");
  button.disabled = true;
  try { await refreshKnowledgeDocuments(); knowledgeNotice("已读取最新资料和分类。"); }
  catch (error) { knowledgeNotice(error.message, true); }
  finally { button.disabled = false; }
});

(async () => {
  try {
    const session = await knowledgeApi("/console/session");
    knowledgeById("knowledge-account").textContent = session.username;
    if (!session.admin) throw Error("只有管理员可以管理知识入库与分类。请返回知识库对话，按当前权限检索资料。");
    await RagDomains.load();
    knowledgeById("knowledge-domains").textContent = `数据权限：${RagDomains.text(session.data_domains)}`;
    resetKnowledgeForm();
    await refreshKnowledgeDocuments();
    knowledgeById("knowledge-workspace").hidden = false;
  } catch (error) { knowledgeNotice(error.message, true); }
})();
