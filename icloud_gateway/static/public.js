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

  let activeKey = "";
  let activeCode = "";
  let pollTimer = null;
  let expiryTimer = null;
  let attempts = 0;

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
    if (attempts >= 12 || !activeKey) {
      setBusy(false);
      showMessage("暂未收到验证码", "可以再次查询。");
      return;
    }
    pollTimer = window.setTimeout(() => queryCode(false), Math.max(1, seconds) * 1000);
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
      attempts = 0;
      error.textContent = "";
      if (!activeKey) {
        error.textContent = "请输入访问密钥。";
        keyInput.focus();
        return;
      }
    }
    attempts += 1;
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
        showMessage("正在等待验证码", `第 ${attempts} 次查询`);
        schedulePoll(data.retry_after || 5);
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
        schedulePoll(data.retry_after || 3);
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

  copyButton.addEventListener("click", async () => {
    if (!activeCode) return;
    try {
      await navigator.clipboard.writeText(activeCode);
      copyButton.querySelector("span").textContent = "已复制";
      window.setTimeout(() => {
        copyButton.querySelector("span").textContent = "复制";
      }, 1600);
    } catch (_error) {
      showMessage("无法复制", "请手动记录验证码。");
    }
  });

  window.addEventListener("pagehide", stopTimers, { once: true });
})();
