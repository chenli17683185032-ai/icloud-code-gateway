(() => {
  "use strict";

  const form = document.querySelector("#lookup-form");
  if (!form) return;

  const keyInput = document.querySelector("#access-key");
  const toggleKey = document.querySelector("#toggle-key");
  const toggleIcon = toggleKey.querySelector("img");
  const submitButton = document.querySelector("#lookup-button");
  const error = document.querySelector("#key-error");
  const idle = document.querySelector("#result-idle");
  const loading = document.querySelector("#result-loading");
  const message = document.querySelector("#result-message");
  const messageTitle = document.querySelector("#result-title");
  const messageDetail = document.querySelector("#result-detail");
  const codePanel = document.querySelector("#result-code");
  const digits = document.querySelector("#code-digits");
  const expiry = document.querySelector("#code-expiry");
  const copyButton = document.querySelector("#copy-code");

  // The server holds each request open until the code arrives, so a round trip
  // returns the moment mail lands instead of on a fixed poll tick. These are
  // only the outer bounds for how long we keep re-opening that connection.
  const MAX_WAIT_MS = 3 * 60 * 1000;
  const BUSY_RETRY_SECONDS = 2;

  let activeKey = "";
  let activeCode = "";
  let pollTimer = null;
  let expiryTimer = null;
  let waitDeadline = 0;
  let waitStartedAt = 0;

  function setPanel(panel) {
    [idle, loading, message, codePanel].forEach((item) => {
      item.hidden = item !== panel;
    });
  }

  function stopTimers() {
    if (pollTimer) window.clearTimeout(pollTimer);
    if (expiryTimer) window.clearTimeout(expiryTimer);
    pollTimer = null;
    expiryTimer = null;
  }

  function setBusy(busy) {
    submitButton.disabled = busy;
    submitButton.classList.toggle("is-busy", busy);
  }

  function showMessage(title, detail = "") {
    messageTitle.textContent = title;
    messageDetail.textContent = detail;
    setPanel(message);
  }

  function schedulePoll(seconds) {
    if (!activeKey || Date.now() >= waitDeadline) {
      setBusy(false);
      showMessage("暂未收到验证码", "可以再次查询。");
      return;
    }
    pollTimer = window.setTimeout(() => queryCode(false), Math.max(0, seconds) * 1000);
  }

  function waitedSeconds() {
    return Math.max(0, Math.round((Date.now() - waitStartedAt) / 1000));
  }

  function showCode(code, expiresAt) {
    activeCode = code;
    digits.replaceChildren();
    [...code].forEach((digit) => {
      const item = document.createElement("span");
      item.className = "code-digit";
      item.textContent = digit;
      digits.append(item);
    });
    const expires = new Date(expiresAt).getTime();
    const remaining = Math.max(0, Math.ceil((expires - Date.now()) / 1000));
    expiry.textContent = `剩余 ${remaining} 秒`;
    setPanel(codePanel);
    setBusy(false);
    expiryTimer = window.setInterval(() => {
      const seconds = Math.max(0, Math.ceil((expires - Date.now()) / 1000));
      expiry.textContent = seconds > 0 ? `剩余 ${seconds} 秒` : "验证码已过期";
      if (seconds <= 0) {
        window.clearInterval(expiryTimer);
        expiryTimer = null;
        activeCode = "";
        showMessage("验证码已过期", "请重新查询。");
      }
    }, 1000);
  }

  async function queryCode(reset) {
    if (reset) {
      stopTimers();
      activeKey = keyInput.value.trim();
      error.textContent = "";
      if (!activeKey) {
        error.textContent = "请输入访问密钥。";
        keyInput.focus();
        return;
      }
      waitStartedAt = Date.now();
      waitDeadline = waitStartedAt + MAX_WAIT_MS;
    }
    setBusy(true);
    setPanel(loading);
    try {
      const response = await fetch("/api/code", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ access_key: activeKey }),
        cache: "no-store",
        credentials: "same-origin",
      });
      const data = await response.json();
      if (data.status === "found" && data.code && data.expires_at) {
        showCode(data.code, data.expires_at);
        return;
      }
      if (data.status === "waiting") {
        // The request already waited server-side; reopen it straight away.
        showMessage("正在等待验证码", `已等待 ${waitedSeconds()} 秒，收到后自动显示`);
        schedulePoll(0);
        return;
      }
      if (data.status === "invalid_key") {
        activeKey = "";
        setBusy(false);
        showMessage("密钥无效", "请核对后重试。");
        return;
      }
      if (data.status === "rate_limited") {
        setBusy(false);
        showMessage("查询过于频繁", `请在 ${data.retry_after || 60} 秒后重试。`);
        return;
      }
      if (data.status === "busy") {
        showMessage("收件箱正忙", "稍后自动重试。");
        schedulePoll(data.retry_after || BUSY_RETRY_SECONDS);
        return;
      }
      setBusy(false);
      showMessage("暂时无法查询", "请稍后重试。");
    } catch (_error) {
      setBusy(false);
      showMessage("网络连接失败", "请稍后重试。");
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    queryCode(true);
  });

  toggleKey.addEventListener("click", () => {
    const showing = keyInput.type === "text";
    keyInput.type = showing ? "password" : "text";
    toggleKey.setAttribute("aria-label", showing ? "显示密钥" : "隐藏密钥");
    toggleKey.title = showing ? "显示密钥" : "隐藏密钥";
    toggleIcon.src = showing ? "/static/icons/eye.svg" : "/static/icons/eye-off.svg";
  });

  async function writeClipboard(value) {
    const text = String(value ?? "");
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch (_error) {
        // Fall through for older or non-secure contexts.
      }
    }
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.readOnly = true;
    textarea.setAttribute("aria-hidden", "true");
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    document.body.append(textarea);
    textarea.select();
    textarea.setSelectionRange(0, textarea.value.length);
    const copied = document.execCommand("copy");
    textarea.remove();
    if (!copied) throw new Error("copy_failed");
  }

  copyButton.addEventListener("click", async () => {
    if (!activeCode) return;
    const label = copyButton.querySelector("span");
    try {
      await writeClipboard(activeCode);
      label.textContent = "已复制";
    } catch (_error) {
      // Never swap the panel away: that used to hide the digits the user was
      // about to type in by hand.
      label.textContent = "复制失败";
    }
    window.setTimeout(() => {
      label.textContent = "复制";
    }, 1600);
  });

  // A delivered link carries the key so the buyer never types anything. The key
  // travels in the URL *fragment*, which browsers never send to the server, so
  // it stays out of Caddy and Cloudflare access logs. A `?key=` query is also
  // accepted for links pasted by hand, but it is rewritten to a fragment right
  // away so it is not carried into any later request.
  function consumeKeyFromUrl() {
    const fromHash = new URLSearchParams(
      (window.location.hash || "").replace(/^#/, ""),
    ).get("key");
    const fromQuery = new URLSearchParams(window.location.search).get("key");
    const key = (fromHash || fromQuery || "").trim();
    if (!key) return "";
    // Drop it from the address bar so a screenshot or a forwarded tab does not
    // hand the key to someone else.
    window.history.replaceState(null, "", window.location.pathname);
    return key;
  }

  const deliveredKey = consumeKeyFromUrl();
  if (deliveredKey) {
    keyInput.value = deliveredKey;
    queryCode(true);
  }

  window.addEventListener("pagehide", stopTimers, { once: true });
})();
