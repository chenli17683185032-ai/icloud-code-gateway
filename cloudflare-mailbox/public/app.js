class ApiError extends Error {
  constructor(status, payload) {
    super(
      payload && payload.message ? payload.message : "请求失败，请稍后重试。",
    );
    this.status = status;
  }
}

const elements = {
  entryView: document.querySelector("#entry-view"),
  mailboxView: document.querySelector("#mailbox-view"),
  form: document.querySelector("#lookup-form"),
  email: document.querySelector("#email"),
  token: document.querySelector("#token"),
  lookupButton: document.querySelector("#lookup-button"),
  lookupStatus: document.querySelector("#lookup-status"),
  mailboxEmail: document.querySelector("#mailbox-email"),
  refreshState: document.querySelector("#refresh-state"),
  refreshButton: document.querySelector("#refresh-button"),
  logoutButton: document.querySelector("#logout-button"),
  messageList: document.querySelector("#message-list"),
  messageCount: document.querySelector("#message-count"),
  indexLabel: document.querySelector("#index-label"),
  emptyMailbox: document.querySelector("#empty-mailbox"),
  reader: document.querySelector("#message-reader"),
  mailboxError: document.querySelector("#mailbox-error"),
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
let pollingTimer = 0;
let refreshInFlight = false;
let renderedSignature = "";

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
      throw new ApiError(408, { message: "连接超时，请检查网络后重试。" });
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

function setLookupStatus(message, kind = "error") {
  elements.lookupStatus.textContent = message;
  elements.lookupStatus.dataset.kind = kind;
}

function showEntry(message = "", kind = "error") {
  stopPolling();
  elements.mailboxView.hidden = true;
  elements.entryView.hidden = false;
  elements.token.value = "";
  if (message) setLookupStatus(message, kind);
  window.setTimeout(() => elements.email.focus(), 0);
}

function showMailbox(email, mode = "code_only") {
  const shouldMoveFocus = !elements.entryView.hidden;
  elements.entryView.hidden = true;
  elements.mailboxView.hidden = false;
  elements.mailboxEmail.textContent = email;
  elements.mailboxView.dataset.mode = mode;
  elements.mailboxError.hidden = true;
  if (shouldMoveFocus) {
    window.setTimeout(
      () =>
        document.querySelector("#mailbox-title").focus({ preventScroll: true }),
      0,
    );
  }
}

function formatTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "时间未知" : dateFormatter.format(date);
}

function makeElement(name, className, text) {
  const element = document.createElement(name);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
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

function renderReader(animate = true) {
  const message = messages.find((item) => item.id === selectedId);
  elements.reader.replaceChildren();
  if (!message) {
    elements.reader.dataset.empty = "true";
    const placeholder = makeElement("div", "reader-placeholder");
    placeholder.append(
      makeElement("span", "", "↙"),
      makeElement("p", "", "选择邮件查看验证码和正文。"),
    );
    elements.reader.append(placeholder);
    return;
  }
  delete elements.reader.dataset.empty;

  const article = makeElement("div", "reader-article");
  if (animate) article.classList.add("is-animated");
  const kicker = makeElement("div", "reader-kicker");
  kicker.append(
    makeElement("span", "", message.sender || "未知发件人"),
    makeElement("time", "", formatTime(message.receivedAt)),
  );
  const title = makeElement("h2", "", message.subject || "（无主题）");
  article.append(kicker, title);

  if (message.code) {
    const verification = makeElement("div", "verification-block");
    const value = makeElement("div", "");
    value.append(
      makeElement("span", "verification-label", "识别到的验证码"),
      makeElement("code", "verification-code", message.code),
    );
    const copy = makeElement("button", "copy-button", "复制验证码");
    copy.type = "button";
    copy.addEventListener("click", () => {
      copyText(message.code, copy).catch(() => {
        copy.textContent = "复制失败";
      });
    });
    verification.append(value, copy);
    article.append(verification);
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

function messageSignature(items) {
  return JSON.stringify(
    items.map((item) => [
      item.id,
      item.code,
      item.receivedAt,
      item.sender,
      item.subject,
      item.body,
    ]),
  );
}

function renderMessages(nextMessages, options = {}) {
  const previousIds = new Set(messages.map((message) => message.id));
  const incoming = Array.isArray(nextMessages) ? nextMessages : [];
  const signature = messageSignature(incoming);
  const changed = signature !== renderedSignature;
  if (!changed && !options.force) return false;
  messages = incoming;
  renderedSignature = signature;
  if (!messages.some((message) => message.id === selectedId)) {
    selectedId = messages[0] ? messages[0].id : "";
  }
  elements.messageCount.textContent = String(messages.length) + " 封";
  elements.messageList.replaceChildren();
  elements.emptyMailbox.hidden = messages.length !== 0;

  for (const message of messages) {
    if (elements.mailboxView.dataset.mode === "code_only") {
      const row = makeElement("div", "message-row code-only-row");
      const value = makeElement("span", "code-only-value", message.code);
      const meta = makeElement("span", "");
      meta.append(
        makeElement("span", "code-only-hint", formatTime(message.receivedAt)),
      );
      const copy = makeElement("button", "copy-button", "复制验证码");
      copy.type = "button";
      copy.addEventListener("click", () => {
        copyText(message.code, copy).catch(() => {
          copy.textContent = "复制失败";
        });
      });
      meta.append(copy);
      row.append(value, meta);
      elements.messageList.append(row);
      continue;
    }
    const row = makeElement("button", "message-row");
    row.type = "button";
    row.setAttribute(
      "aria-current",
      message.id === selectedId ? "true" : "false",
    );
    const meta = makeElement("span", "message-meta");
    meta.append(
      makeElement("span", "message-sender", message.sender || "未知发件人"),
      makeElement("time", "message-time", formatTime(message.receivedAt)),
    );
    row.append(
      meta,
      makeElement("span", "message-subject", message.subject || "（无主题）"),
      makeElement("span", "message-preview", message.body || "无纯文本正文"),
    );
    if (message.code)
      row.append(makeElement("code", "code-chip", message.code));
    row.addEventListener("click", () => {
      selectedId = message.id;
      renderMessages(messages, { force: true, animate: true });
      renderReader(true);
      if (window.matchMedia("(max-width: 760px)").matches) {
        elements.reader.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
    elements.messageList.append(row);
  }
  renderReader(options.animate !== false);
  return messages.some((message) => !previousIds.has(message.id));
}

async function refreshMailbox(options = {}) {
  if (refreshInFlight) return;
  refreshInFlight = true;
  if (!options.quiet) elements.refreshState.textContent = "正在刷新";
  elements.refreshButton.disabled = true;
  try {
    const payload = await api("/api/messages");
    showMailbox(payload.mailbox.email, payload.mailbox.mode);
    const hasNew = renderMessages(payload.messages, {
      force: !options.quiet,
      animate: !options.quiet,
    });
    if (!options.quiet) {
      elements.mailboxView.classList.remove("is-manual-refresh");
      void elements.mailboxView.offsetWidth;
      elements.mailboxView.classList.add("is-manual-refresh");
      window.setTimeout(
        () => elements.mailboxView.classList.remove("is-manual-refresh"),
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
    elements.mailboxError.textContent =
      error && error.message ? error.message : "邮件刷新失败，请稍后重试。";
    elements.mailboxError.hidden = false;
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
  if (elements.mailboxView.hidden || document.hidden) return;
  pollingTimer = window.setTimeout(
    () => refreshMailbox({ quiet: true }),
    delay,
  );
}

async function establishSession(credentials) {
  setLookupStatus("正在验证并打开收件箱…", "success");
  elements.lookupButton.disabled = true;
  try {
    const payload = await api("/api/session", {
      method: "POST",
      body: JSON.stringify(credentials),
    });
    elements.token.value = "";
    showMailbox(payload.mailbox.email, payload.mailbox.mode);
    await refreshMailbox();
  } catch (error) {
    setLookupStatus(
      error && error.message ? error.message : "无法打开收件箱，请稍后重试。",
    );
  } finally {
    elements.lookupButton.disabled = false;
  }
}

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!elements.form.reportValidity()) return;
  await establishSession({
    email: elements.email.value.trim(),
    token: elements.token.value.trim(),
  });
});

elements.refreshButton.addEventListener("click", () => refreshMailbox());

elements.logoutButton.addEventListener("click", async () => {
  elements.logoutButton.disabled = true;
  try {
    await api("/api/logout", { method: "POST" });
  } catch {
    // Clearing the local view is still safe if the network request fails.
  } finally {
    messages = [];
    selectedId = "";
    elements.logoutButton.disabled = false;
    showEntry("已退出收件箱。", "success");
  }
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopPolling();
  else if (!elements.mailboxView.hidden) refreshMailbox({ quiet: true });
});

window.addEventListener("pagehide", stopPolling);

const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ""));
const fragmentEmail = fragment.get("email");
const fragmentToken = fragment.get("token") || fragment.get("key");
if (fragmentEmail) elements.email.value = fragmentEmail;
if (fragmentToken) elements.token.value = fragmentToken;
if (window.location.hash)
  history.replaceState(
    null,
    "",
    window.location.pathname + window.location.search,
  );

async function restoreMailbox() {
  try {
    const session = await api("/api/session");
    if (!session.authenticated) return;
    showMailbox(session.mailbox.email, session.mailbox.mode);
    await refreshMailbox({ quiet: true });
  } catch {
    showEntry();
  }
}

if (fragmentToken) {
  if (fragmentEmail) {
    window.setTimeout(() => elements.form.requestSubmit(), 0);
  } else {
    window.setTimeout(() => establishSession({ token: fragmentToken }), 0);
  }
} else {
  restoreMailbox();
}
