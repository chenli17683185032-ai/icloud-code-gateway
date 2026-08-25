class ApiError extends Error {
  constructor(status, payload) {
    super(
      payload && payload.message ? payload.message : "请求失败，请稍后重试。",
    );
    this.status = status;
  }
}

const elements = {
  entry: document.querySelector("#operator-entry"),
  view: document.querySelector("#operator-view"),
  form: document.querySelector("#operator-form"),
  token: document.querySelector("#operator-token"),
  loginButton: document.querySelector("#operator-login-button"),
  status: document.querySelector("#operator-status"),
  refreshState: document.querySelector("#operator-refresh-state"),
  refreshButton: document.querySelector("#operator-refresh-button"),
  logoutButton: document.querySelector("#operator-logout-button"),
  count: document.querySelector("#operator-message-count"),
  search: document.querySelector("#operator-search-input"),
  searchState: document.querySelector("#operator-search-state"),
  list: document.querySelector("#operator-message-list"),
  empty: document.querySelector("#operator-empty"),
  reader: document.querySelector("#operator-reader"),
  error: document.querySelector("#operator-error"),
};

const dateFormatter = new Intl.DateTimeFormat("zh-CN", {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

let messages = [];
let selectedId = "";
let renderedSignature = "";
let pollingTimer = 0;
let refreshInFlight = false;
let archiveLoadInFlight = false;
let nextCursor = "";
let hasMore = false;
let loadedOlderPages = false;
let searchQuery = "";
let searchTimer = 0;
let searchGeneration = 0;
const readerModes = new Map();

async function api(path, options = {}) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(path, {
      ...options,
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
      headers: {
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
      },
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new ApiError(response.status, payload);
    return payload;
  } catch (error) {
    if (error && error.name === "AbortError") {
      throw new ApiError(408, { message: "连接超时，请稍后重试。" });
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

function makeElement(name, className, text) {
  const element = document.createElement(name);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function formatTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "时间未知" : dateFormatter.format(date);
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "大小未知";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function messagePath(messageId, suffix) {
  return `/api/operator/messages/${encodeURIComponent(messageId)}/${suffix}`;
}

function normalizeSearch(value) {
  return String(value || "")
    .normalize("NFKC")
    .trim()
    .toLocaleLowerCase();
}

function messageSearchText(message) {
  return normalizeSearch(
    [
      message.email,
      message.sender,
      message.subject,
      message.body,
      message.code,
      ...(message.attachments || []).map((attachment) => attachment.filename),
    ].join("\n"),
  );
}

function visibleMessages() {
  if (!searchQuery) return messages;
  return messages.filter((message) =>
    messageSearchText(message).includes(searchQuery),
  );
}

function mergeMessages(...groups) {
  const unique = new Map();
  for (const group of groups) {
    for (const message of group || []) unique.set(message.id, message);
  }
  return [...unique.values()].sort(
    (left, right) =>
      new Date(right.receivedAt).getTime() -
        new Date(left.receivedAt).getTime() ||
      String(right.id).localeCompare(String(left.id)),
  );
}

async function fetchMessagePage(cursor = "") {
  const query = new URLSearchParams({ limit: "50" });
  if (cursor) query.set("cursor", cursor);
  return api(`/api/operator/messages?${query.toString()}`);
}

function updateSearchState(message = "") {
  if (message) {
    elements.searchState.textContent = message;
    return;
  }
  if (searchQuery) {
    elements.searchState.textContent = hasMore
      ? `已搜索当前加载的 ${messages.length} 封`
      : `已搜索全部 ${messages.length} 封`;
  } else {
    elements.searchState.textContent = hasMore
      ? `已加载最新 ${messages.length} 封`
      : `已加载全部 ${messages.length} 封`;
  }
}

function resetArchiveState() {
  messages = [];
  selectedId = "";
  renderedSignature = "";
  nextCursor = "";
  hasMore = false;
  loadedOlderPages = false;
  archiveLoadInFlight = false;
  searchQuery = "";
  searchGeneration += 1;
  readerModes.clear();
  elements.count.textContent = "0 封";
  elements.list.replaceChildren();
  elements.reader.replaceChildren();
  elements.empty.hidden = true;
}

function showEntry(message = "") {
  stopPolling();
  if (searchTimer) window.clearTimeout(searchTimer);
  resetArchiveState();
  document.body.classList.remove("operator-active");
  elements.view.hidden = true;
  elements.entry.hidden = false;
  elements.token.value = "";
  elements.search.value = "";
  elements.searchState.textContent = "";
  elements.status.textContent = message;
  window.setTimeout(() => elements.token.focus(), 0);
}

function showView() {
  const shouldFocus = !elements.entry.hidden;
  document.body.classList.add("operator-active");
  elements.entry.hidden = true;
  elements.view.hidden = false;
  elements.error.hidden = true;
  if (shouldFocus) {
    window.setTimeout(
      () =>
        document
          .querySelector("#operator-title")
          .focus({ preventScroll: true }),
      0,
    );
  }
}

async function copyText(value, button) {
  const textarea = document.createElement("textarea");
  textarea.value = String(value || "");
  textarea.readOnly = true;
  textarea.style.position = "fixed";
  textarea.style.insetInlineStart = "-9999px";
  document.body.append(textarea);
  textarea.select();
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } finally {
    textarea.remove();
  }
  if (!copied && navigator.clipboard && navigator.clipboard.writeText) {
    await navigator.clipboard.writeText(String(value || ""));
  } else if (!copied) {
    throw new Error("copy_failed");
  }
  const original = button.textContent;
  button.textContent = "已复制";
  window.setTimeout(() => {
    button.textContent = original;
  }, 1400);
}

function signature(items) {
  return JSON.stringify([
    searchQuery,
    hasMore,
    archiveLoadInFlight,
    items.map((item) => [
      item.id,
      item.email,
      item.sender,
      item.subject,
      item.body,
      item.code,
      item.receivedAt,
      item.permanent,
      item.hasHtml,
      (item.attachments || []).map((attachment) => [
        attachment.id,
        attachment.filename,
        attachment.mimeType,
        attachment.size,
        attachment.inline,
      ]),
    ]),
  ]);
}

function appendMessageContent(article, message) {
  const bodyHost = makeElement("div", "message-content");
  const mode =
    readerModes.get(message.id) || (message.hasHtml ? "html" : "text");

  if (message.hasHtml) {
    const toolbar = makeElement("div", "reader-format-toolbar");
    const controls = makeElement("div", "reader-format-controls");
    const htmlButton = makeElement("button", "format-button", "原始排版");
    const textButton = makeElement("button", "format-button", "纯文本");
    htmlButton.type = "button";
    textButton.type = "button";
    controls.append(htmlButton, textButton);
    toolbar.append(
      controls,
      makeElement("span", "reader-security-note", "远程图片与脚本已阻止"),
    );
    article.append(toolbar);

    const renderMode = (nextMode) => {
      readerModes.set(message.id, nextMode);
      htmlButton.setAttribute("aria-pressed", String(nextMode === "html"));
      textButton.setAttribute("aria-pressed", String(nextMode === "text"));
      bodyHost.replaceChildren();
      if (nextMode === "html") {
        const frame = document.createElement("iframe");
        frame.className = "email-frame";
        frame.title = `${message.subject || "无主题"} 的原始邮件排版`;
        frame.loading = "lazy";
        frame.referrerPolicy = "no-referrer";
        frame.setAttribute("sandbox", "");
        frame.src = messagePath(message.id, "html");
        bodyHost.append(frame);
      } else {
        bodyHost.append(
          makeElement(
            "pre",
            "message-body",
            message.body || "这封邮件没有可显示的纯文本正文。",
          ),
        );
      }
    };
    htmlButton.addEventListener("click", () => renderMode("html"));
    textButton.addEventListener("click", () => renderMode("text"));
    renderMode(mode === "text" ? "text" : "html");
  } else {
    bodyHost.append(
      makeElement(
        "pre",
        "message-body",
        message.body || "这封邮件没有可显示的纯文本正文。",
      ),
    );
  }
  article.append(bodyHost);
}

function appendAttachments(article, message) {
  const attachments = Array.isArray(message.attachments)
    ? message.attachments
    : [];
  if (!attachments.length) return;
  const section = makeElement("section", "attachments-section");
  const heading = makeElement("div", "attachments-heading");
  heading.append(
    makeElement("h3", "", `附件 ${attachments.length}`),
    makeElement("span", "", "私有加密存储"),
  );
  const list = makeElement("div", "attachment-list");
  for (const attachment of attachments) {
    const row = makeElement("div", "attachment-row");
    const identity = makeElement("div", "attachment-identity");
    identity.append(
      makeElement("strong", "", attachment.filename || "未命名附件"),
      makeElement(
        "span",
        "",
        `${attachment.mimeType || "application/octet-stream"} · ${formatBytes(attachment.size)}`,
      ),
    );
    const download = makeElement("a", "attachment-download", "下载到本地");
    download.href = messagePath(
      message.id,
      `attachments/${encodeURIComponent(attachment.id)}`,
    );
    download.download = attachment.filename || "attachment";
    row.append(identity, download);
    list.append(row);
  }
  section.append(heading, list);
  article.append(section);
}

function renderReader(animate = true) {
  const message = messages.find((item) => item.id === selectedId);
  elements.reader.replaceChildren();
  if (!message) {
    elements.reader.dataset.empty = "true";
    const placeholder = makeElement("div", "reader-placeholder");
    placeholder.append(
      makeElement("span", "", "↙"),
      makeElement("p", "", "选择邮件查看完整正文。"),
    );
    elements.reader.append(placeholder);
    return;
  }
  delete elements.reader.dataset.empty;
  const article = makeElement("div", "reader-article");
  if (animate) article.classList.add("is-animated");
  const kicker = makeElement("div", "reader-kicker");
  kicker.append(
    makeElement("span", "", message.email + " · " + message.sender),
    makeElement("time", "", formatTime(message.receivedAt)),
  );
  article.append(
    kicker,
    makeElement("h2", "", message.subject || "（无主题）"),
  );

  const retention = makeElement(
    "p",
    "operator-retention",
    message.permanent ? "长期保存" : "验证码邮件 · 24 小时后清理",
  );
  article.append(retention);

  if (message.code) {
    const block = makeElement("div", "verification-block");
    const value = makeElement("div", "");
    value.append(
      makeElement("span", "verification-label", "识别到的验证码"),
      makeElement("code", "verification-code", message.code),
    );
    const copy = makeElement("button", "copy-button", "复制验证码");
    copy.type = "button";
    copy.addEventListener("click", () => copyText(message.code, copy));
    block.append(value, copy);
    article.append(block);
  }
  appendMessageContent(article, message);
  appendAttachments(article, message);
  elements.reader.append(article);
}

function renderMessages(nextMessages, options = {}) {
  const incoming = Array.isArray(nextMessages) ? nextMessages : [];
  const nextSignature = signature(incoming);
  if (nextSignature === renderedSignature && !options.force) return false;
  const previousIds = new Set(messages.map((item) => item.id));
  messages = incoming;
  renderedSignature = nextSignature;
  const visible = visibleMessages();
  if (!visible.some((item) => item.id === selectedId)) {
    selectedId = visible[0] ? visible[0].id : "";
  }
  elements.count.textContent = searchQuery
    ? `${visible.length} / ${messages.length} 封`
    : `${messages.length}${hasMore ? "+" : ""} 封`;
  elements.list.replaceChildren();
  elements.empty.hidden = visible.length !== 0;
  elements.empty.querySelector("strong").textContent = searchQuery
    ? "没有匹配邮件"
    : "还没有收到邮件";
  elements.empty.querySelector("p").textContent = searchQuery
    ? "换一个邮箱、发件人、标题或正文关键词。"
    : "后台会静默刷新，新邮件到达后会立即出现。";

  for (const message of visible) {
    const row = makeElement("button", "message-row");
    row.type = "button";
    row.setAttribute(
      "aria-current",
      message.id === selectedId ? "true" : "false",
    );
    const meta = makeElement("span", "message-meta");
    meta.append(
      makeElement("span", "message-sender", message.email),
      makeElement("time", "message-time", formatTime(message.receivedAt)),
    );
    row.append(
      meta,
      makeElement("span", "message-subject", message.subject || "（无主题）"),
      makeElement("span", "message-preview", message.body || "无纯文本正文"),
    );
    const badges = makeElement("span", "operator-badges");
    badges.append(
      makeElement(
        "span",
        "code-chip",
        message.permanent ? "长期" : message.code || "24h",
      ),
    );
    row.append(badges);
    row.addEventListener("click", () => {
      selectedId = message.id;
      renderMessages(messages, { force: true, animate: true });
    });
    elements.list.append(row);
  }
  if (hasMore) {
    const loadAll = makeElement(
      "button",
      "load-all-messages",
      archiveLoadInFlight
        ? "正在加载全部邮件…"
        : searchQuery
          ? "加载全部邮件以完成搜索"
          : "加载全部历史邮件",
    );
    loadAll.type = "button";
    loadAll.disabled = archiveLoadInFlight;
    loadAll.addEventListener("click", () => {
      void loadAllMessages();
    });
    elements.list.append(loadAll);
  }
  updateSearchState();
  renderReader(options.animate !== false);
  return messages.some((item) => !previousIds.has(item.id));
}

async function loadAllMessages() {
  if (archiveLoadInFlight || !hasMore) return;
  archiveLoadInFlight = true;
  loadedOlderPages = true;
  stopPolling();
  renderMessages(messages, { force: true, animate: false });
  const seenCursors = new Set();
  let pageCount = 0;
  try {
    while (hasMore) {
      if (!nextCursor || seenCursors.has(nextCursor)) {
        throw new ApiError(500, { message: "邮件分页位置异常，请刷新重试。" });
      }
      seenCursors.add(nextCursor);
      pageCount += 1;
      updateSearchState(`正在加载全部邮件 · 第 ${pageCount + 1} 页`);
      const payload = await fetchMessagePage(nextCursor);
      messages = mergeMessages(messages, payload.messages);
      nextCursor = String(payload.next_cursor || "");
      hasMore = Boolean(payload.has_more);
    }
    renderMessages(messages, { force: true, animate: false });
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      showEntry(error.message);
      return;
    }
    elements.error.textContent =
      error && error.message ? error.message : "加载全部邮件失败。";
    elements.error.hidden = false;
  } finally {
    archiveLoadInFlight = false;
    renderMessages(messages, { force: true, animate: false });
    schedulePolling();
  }
}

async function refresh(options = {}) {
  if (refreshInFlight) return;
  refreshInFlight = true;
  if (!options.quiet) elements.refreshState.textContent = "正在刷新";
  elements.refreshButton.disabled = true;
  try {
    const payload = await fetchMessagePage();
    let nextMessages;
    if (loadedOlderPages) {
      const now = Date.now();
      nextMessages = mergeMessages(
        payload.messages,
        messages.filter(
          (message) => new Date(message.expiresAt).getTime() > now,
        ),
      );
    } else {
      nextMessages = Array.isArray(payload.messages) ? payload.messages : [];
      nextCursor = String(payload.next_cursor || "");
      hasMore = Boolean(payload.has_more);
    }
    showView();
    const hasNew = renderMessages(nextMessages, {
      force: !options.quiet,
      animate: !options.quiet,
    });
    if (!options.quiet) {
      elements.view.classList.remove("is-manual-refresh");
      void elements.view.offsetWidth;
      elements.view.classList.add("is-manual-refresh");
      window.setTimeout(
        () => elements.view.classList.remove("is-manual-refresh"),
        260,
      );
    }
    elements.refreshState.textContent = hasNew
      ? "刚收到新邮件"
      : "已同步 " + formatTime(new Date().toISOString());
    schedulePolling();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      showEntry(error.message);
      return;
    }
    elements.error.textContent =
      error && error.message ? error.message : "后台刷新失败。";
    elements.error.hidden = false;
    elements.refreshState.textContent = "同步失败";
    schedulePolling(5000);
  } finally {
    refreshInFlight = false;
    elements.refreshButton.disabled = false;
  }
}

function stopPolling() {
  if (pollingTimer) window.clearTimeout(pollingTimer);
  pollingTimer = 0;
}

function schedulePolling(delay = 3000) {
  stopPolling();
  if (elements.view.hidden || document.hidden) return;
  pollingTimer = window.setTimeout(() => refresh({ quiet: true }), delay);
}

async function login(token) {
  elements.status.textContent = "正在验证操作员身份…";
  elements.loginButton.disabled = true;
  try {
    await api("/api/operator/session", {
      method: "POST",
      body: JSON.stringify({ token }),
    });
    elements.token.value = "";
    showView();
    await refresh();
  } catch (error) {
    elements.status.textContent =
      error && error.message ? error.message : "无法进入后台。";
  } finally {
    elements.loginButton.disabled = false;
  }
}

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!elements.form.reportValidity()) return;
  await login(elements.token.value.trim());
});

elements.search.addEventListener("input", () => {
  searchQuery = normalizeSearch(elements.search.value);
  searchGeneration += 1;
  const generation = searchGeneration;
  if (searchTimer) window.clearTimeout(searchTimer);
  renderMessages(messages, { force: true, animate: false });
  if (!searchQuery || !hasMore) return;
  updateSearchState("正在读取全部邮件以完成搜索…");
  searchTimer = window.setTimeout(async () => {
    if (generation !== searchGeneration || !searchQuery) return;
    await loadAllMessages();
  }, 250);
});

elements.refreshButton.addEventListener("click", () => refresh());
elements.logoutButton.addEventListener("click", async () => {
  elements.logoutButton.disabled = true;
  try {
    await api("/api/operator/logout", { method: "POST" });
  } catch {
    // Local view can still be cleared safely.
  } finally {
    elements.logoutButton.disabled = false;
    showEntry("已退出操作员后台。");
  }
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopPolling();
  else if (!elements.view.hidden) refresh({ quiet: true });
});
window.addEventListener("pagehide", stopPolling);

const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ""));
const fragmentToken = fragment.get("token") || fragment.get("key");
if (window.location.hash) {
  history.replaceState(
    null,
    "",
    window.location.pathname + window.location.search,
  );
}

async function restore() {
  try {
    const session = await api("/api/operator/session");
    if (!session.authenticated) return;
    showView();
    await refresh({ quiet: true });
  } catch {
    showEntry();
  }
}

if (fragmentToken) window.setTimeout(() => login(fragmentToken), 0);
else restore();
