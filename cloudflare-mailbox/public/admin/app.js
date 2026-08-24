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

function showEntry(message = "") {
  stopPolling();
  elements.view.hidden = true;
  elements.entry.hidden = false;
  elements.token.value = "";
  elements.status.textContent = message;
  window.setTimeout(() => elements.token.focus(), 0);
}

function showView() {
  const shouldFocus = !elements.entry.hidden;
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
  return JSON.stringify(
    items.map((item) => [
      item.id,
      item.email,
      item.sender,
      item.subject,
      item.body,
      item.code,
      item.receivedAt,
      item.permanent,
    ]),
  );
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
  article.append(
    makeElement(
      "pre",
      "message-body",
      message.body || "这封邮件没有可显示的纯文本正文。",
    ),
  );
  elements.reader.append(article);
}

function renderMessages(nextMessages, options = {}) {
  const incoming = Array.isArray(nextMessages) ? nextMessages : [];
  const nextSignature = signature(incoming);
  if (nextSignature === renderedSignature && !options.force) return false;
  const previousIds = new Set(messages.map((item) => item.id));
  messages = incoming;
  renderedSignature = nextSignature;
  if (!messages.some((item) => item.id === selectedId)) {
    selectedId = messages[0] ? messages[0].id : "";
  }
  elements.count.textContent = String(messages.length) + " 封";
  elements.list.replaceChildren();
  elements.empty.hidden = messages.length !== 0;

  for (const message of messages) {
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
  renderReader(options.animate !== false);
  return messages.some((item) => !previousIds.has(item.id));
}

async function refresh(options = {}) {
  if (refreshInFlight) return;
  refreshInFlight = true;
  if (!options.quiet) elements.refreshState.textContent = "正在刷新";
  elements.refreshButton.disabled = true;
  try {
    const payload = await api("/api/operator/messages");
    showView();
    const hasNew = renderMessages(payload.messages, {
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

elements.refreshButton.addEventListener("click", () => refresh());
elements.logoutButton.addEventListener("click", async () => {
  elements.logoutButton.disabled = true;
  try {
    await api("/api/operator/logout", { method: "POST" });
  } catch {
    // Local view can still be cleared safely.
  } finally {
    messages = [];
    selectedId = "";
    renderedSignature = "";
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
