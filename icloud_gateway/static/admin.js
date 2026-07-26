(() => {
  "use strict";

  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const modal = document.querySelector("#key-modal");
  const modalMessage = document.querySelector("#key-modal-message");
  const issuedList = document.querySelector("#issued-key-list");
  const deleteModal = document.querySelector("#delete-alias-modal");
  const deleteForm = document.querySelector("#delete-alias-form");
  const deleteEmail = document.querySelector("#delete-alias-email");
  const deleteConfirmation = document.querySelector("#delete-alias-confirmation");
  const deleteMessage = document.querySelector("#delete-alias-message");
  const deleteSubmit = document.querySelector("#delete-alias-submit");
  let modalReturnFocus = null;
  let deleteTarget = null;

  class AuthenticationRequiredError extends Error {}

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
      throw new AuthenticationRequiredError();
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

  function openDeleteModal(button) {
    deleteTarget = {
      id: button.dataset.aliasId,
      email: button.dataset.aliasEmail,
      button,
    };
    deleteEmail.textContent = deleteTarget.email;
    deleteConfirmation.value = "";
    deleteMessage.textContent = "";
    deleteSubmit.disabled = false;
    deleteModal.hidden = false;
    document.body.classList.add("modal-open");
    deleteConfirmation.focus();
  }

  function closeDeleteModal() {
    if (!deleteModal || deleteModal.hidden) return;
    deleteModal.hidden = true;
    document.body.classList.remove("modal-open");
    deleteConfirmation.value = "";
    deleteMessage.textContent = "";
    deleteTarget?.button?.focus();
    deleteTarget = null;
  }

  deleteModal?.querySelectorAll("[data-close-delete-modal]").forEach((button) => {
    button.addEventListener("click", closeDeleteModal);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (deleteModal && !deleteModal.hidden) {
      closeDeleteModal();
    } else if (modal && !modal.hidden) {
      closeModal();
    }
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
    } catch (error) {
      if (error instanceof AuthenticationRequiredError) return;
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
      } catch (error) {
        if (error instanceof AuthenticationRequiredError) return;
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
      } catch (error) {
        if (error instanceof AuthenticationRequiredError) return;
        window.alert("密钥撤销失败。");
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll(".deactivate-alias-button").forEach((button) => {
    button.addEventListener("click", async () => {
      const email = button.dataset.aliasEmail;
      if (
        !window.confirm(
          `确认在 iCloud 停用 ${email}？Apple 确认后，该 Alias 的访问密钥会立即撤销。`,
        )
      ) {
        return;
      }
      button.disabled = true;
      try {
        await api(`/admin/api/aliases/${button.dataset.aliasId}/deactivate`, {
          method: "POST",
          body: JSON.stringify({ confirmed: true }),
        });
        window.location.reload();
      } catch (error) {
        if (error instanceof AuthenticationRequiredError) return;
        window.alert("Alias 停用失败，本地状态未改变。请刷新后重试。");
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll(".reactivate-alias-button").forEach((button) => {
    button.addEventListener("click", async () => {
      const email = button.dataset.aliasEmail;
      if (!window.confirm(`确认在 iCloud 恢复 ${email}？`)) return;
      button.disabled = true;
      try {
        await api(`/admin/api/aliases/${button.dataset.aliasId}/reactivate`, {
          method: "POST",
          body: JSON.stringify({ confirmed: true }),
        });
        window.location.reload();
      } catch (error) {
        if (error instanceof AuthenticationRequiredError) return;
        window.alert("Alias 恢复失败，本地状态未改变。请刷新后重试。");
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll(".delete-alias-button").forEach((button) => {
    button.addEventListener("click", () => openDeleteModal(button));
  });

  deleteForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!deleteTarget) return;
    const confirmation = deleteConfirmation.value.trim();
    if (confirmation.toLocaleLowerCase() !== deleteTarget.email.toLocaleLowerCase()) {
      deleteMessage.textContent = "输入的邮箱不匹配。";
      deleteConfirmation.focus();
      return;
    }
    deleteMessage.textContent = "";
    deleteSubmit.disabled = true;
    try {
      await api(`/admin/api/aliases/${deleteTarget.id}`, {
        method: "DELETE",
        body: JSON.stringify({ confirmation }),
      });
      window.location.reload();
    } catch (error) {
      if (error instanceof AuthenticationRequiredError) return;
      deleteMessage.textContent = "永久删除失败，本地记录未删除。请刷新后重试。";
      deleteSubmit.disabled = false;
    }
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
      if (response.status === 401 || response.status === 403) {
        window.location.assign("/admin/login");
        return;
      }
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
