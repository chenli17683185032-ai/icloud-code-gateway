(() => {
  "use strict";

  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const modal = document.querySelector("#key-modal");
  const modalMessage = document.querySelector("#key-modal-message");
  const issuedList = document.querySelector("#issued-key-list");
  let modalReturnFocus = null;

  async function api(url, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("X-CSRF-Token", csrf);
    if (options.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    const response = await fetch(url, {
      ...options,
      headers,
      credentials: "same-origin",
      cache: "no-store",
    });
    let data = {};
    try {
      data = await response.json();
    } catch (_error) {
      data = { status: "error" };
    }
    if (response.status === 401 || response.status === 403) {
      // The 8-hour session expired mid-page; a generic failure message here
      // reads as "the action broke" rather than "you were signed out".
      window.location.assign("/admin/login");
      throw new Error("unauthenticated");
    }
    if (!response.ok) throw new Error(data.status || "error");
    return data;
  }

  function openModal(items, message) {
    modalReturnFocus = document.activeElement;
    modalMessage.textContent = message;
    issuedList.replaceChildren();
    items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "issued-key-row";

      const identity = document.createElement("div");
      identity.className = "issued-key-identity";
      const label = document.createElement("strong");
      label.textContent = item.label;
      const email = document.createElement("span");
      email.textContent = item.email;
      identity.append(label, email);

      const key = document.createElement("code");
      key.textContent = item.access_key;

      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "secondary-button small-button";
      const icon = document.createElement("img");
      icon.src = "/static/icons/copy.svg";
      icon.alt = "";
      icon.setAttribute("aria-hidden", "true");
      const copyText = document.createElement("span");
      copyText.textContent = "复制";
      copy.append(icon, copyText);
      copy.addEventListener("click", async () => {
        await navigator.clipboard.writeText(item.access_key);
        copyText.textContent = "已复制";
        window.setTimeout(() => {
          copyText.textContent = "复制";
        }, 1600);
      });

      row.append(identity, key, copy);
      issuedList.append(row);
    });
    modal.hidden = false;
    document.body.classList.add("modal-open");
    modal.querySelector(".modal-dialog").focus?.();
    modal.querySelector("[data-close-modal]")?.focus();
  }

  function closeModal() {
    if (!modal || modal.hidden) return;
    modal.hidden = true;
    document.body.classList.remove("modal-open");
    issuedList.replaceChildren();
    modalReturnFocus?.focus?.();
    modalReturnFocus = null;
    window.location.reload();
  }

  modal?.querySelectorAll("[data-close-modal]").forEach((button) => {
    button.addEventListener("click", closeModal);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && modal && !modal.hidden) closeModal();
  });

  const createForm = document.querySelector("#create-alias-form");
  const createButton = document.querySelector("#create-submit");
  const createMessage = document.querySelector("#create-message");
  createForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(createForm);
    const payload = {
      count: Number(form.get("count") || 1),
      label_prefix: String(form.get("label_prefix") || "").trim(),
      sender_filter: String(form.get("sender_filter") || "").trim(),
      note: String(form.get("note") || "").trim(),
    };
    if (!payload.label_prefix) {
      createMessage.textContent = "请填写标签。";
      return;
    }
    createMessage.textContent = "";
    createButton.disabled = true;
    createButton.classList.add("is-busy");
    try {
      const data = await api("/admin/api/aliases", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      openModal(
        data.created,
        data.status === "partial"
          ? `已创建 ${data.created.length} 个，批次已停止。`
          : `已创建 ${data.created.length} 个。密钥关闭后不再显示。`,
      );
    } catch (_error) {
      createMessage.textContent = "创建失败，未完成的 Alias 可通过对账恢复。";
    } finally {
      createButton.disabled = false;
      createButton.classList.remove("is-busy");
    }
  });

  document.querySelectorAll(".issue-key-button").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const data = await api(`/admin/api/aliases/${button.dataset.aliasId}/key`, {
          method: "POST",
        });
        openModal([data], "密钥已签发。关闭后不再显示。");
      } catch (_error) {
        window.alert("密钥签发失败。");
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll(".revoke-key-button").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!window.confirm("确认撤销该密钥？")) return;
      button.disabled = true;
      try {
        await api(`/admin/api/aliases/${button.dataset.aliasId}/key`, {
          method: "DELETE",
        });
        window.location.reload();
      } catch (_error) {
        window.alert("密钥撤销失败。");
        button.disabled = false;
      }
    });
  });

  const filter = document.querySelector("#alias-filter");
  const empty = document.querySelector("#alias-filter-empty");
  filter?.addEventListener("input", () => {
    const query = filter.value.trim().toLocaleLowerCase();
    let visible = 0;
    document.querySelectorAll(".alias-row").forEach((row) => {
      const matches = !query || row.dataset.aliasSearch.toLocaleLowerCase().includes(query);
      row.hidden = !matches;
      if (matches) visible += 1;
    });
    if (empty) empty.hidden = visible !== 0;
  });

  const capture = document.querySelector("#capture-status");
  async function refreshCapture() {
    if (!capture) return;
    try {
      const response = await fetch("/admin/api/capture/status", {
        cache: "no-store",
        credentials: "same-origin",
      });
      if (!response.ok) return;
      const data = await response.json();
      capture.querySelector("[data-capture-state]").textContent =
        data.state_label || data.state;
      capture.querySelector("[data-capture-message]").textContent =
        data.message_label || data.message;
      capture.dataset.active = data.active ? "true" : "false";
      if (data.active) {
        window.setTimeout(refreshCapture, 1500);
      } else if (["captured", "failed", "cancelled"].includes(data.state)) {
        window.setTimeout(() => window.location.reload(), 900);
      }
    } catch (_error) {
      window.setTimeout(refreshCapture, 3000);
    }
  }
  if (capture?.dataset.active === "true") refreshCapture();
})();
