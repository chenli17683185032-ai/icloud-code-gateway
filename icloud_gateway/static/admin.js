(() => {
  "use strict";

  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const configuredPublicUrl =
    document.querySelector('meta[name="public-base-url"]')?.content || "";
  const publicUrl = configuredPublicUrl || window.location.origin;
  const modal = document.querySelector("#key-modal");
  const modalMessage = document.querySelector("#key-modal-message");
  const issuedList = document.querySelector("#issued-key-list");
  const deleteModal = document.querySelector("#delete-alias-modal");
  const deleteForm = document.querySelector("#delete-alias-form");
  const deleteEmail = document.querySelector("#delete-alias-email");
  const deleteConfirmation = document.querySelector("#delete-alias-confirmation");
  const deleteMessage = document.querySelector("#delete-alias-message");
  const deleteSubmit = document.querySelector("#delete-alias-submit");
  const refreshCodesButton = document.querySelector("#refresh-admin-codes");
  const adminCodesMessage = document.querySelector("#admin-codes-message");
  const adminCodesTable = document.querySelector("#admin-codes-table");
  const adminCodesList = document.querySelector("#admin-codes-list");
  const adminCodesEmpty = document.querySelector("#admin-codes-empty");
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

  function standardParameters(item) {
    const url = item.public_url || publicUrl;
    return `邮箱账号：${item.email}；解码网站：${url}；接码密钥：${item.access_key}`;
  }

  function createCopyButton(value, label = "复制") {
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "secondary-button small-button";
    const icon = document.createElement("img");
    icon.src = "/static/icons/copy.svg";
    icon.alt = "";
    icon.setAttribute("aria-hidden", "true");
    const copyText = document.createElement("span");
    copyText.textContent = label;
    copy.append(icon, copyText);
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(value);
        copyText.textContent = "已复制";
      } catch (_error) {
        copyText.textContent = "复制失败";
      }
      window.setTimeout(() => {
        copyText.textContent = label;
      }, 1600);
    });
    return copy;
  }

  function openModal(items, message) {
    modalReturnFocus = document.activeElement;
    modalMessage.textContent = message;
    issuedList.replaceChildren();
    if (items.length > 1) {
      const all = items.map(standardParameters).join("\n");
      issuedList.append(createCopyButton(all, "复制全部成功项"));
    }
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
      key.textContent = standardParameters(item);

      const copy = createCopyButton(standardParameters(item));

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
    createMessage.textContent = `正在串行创建 ${payload.count} 个 Alias，请勿关闭页面…`;
    createButton.disabled = true;
    createButton.classList.add("is-busy");
    try {
      const data = await api("/admin/api/aliases", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      openModal(
        data.created.map((item) => ({ ...item, public_url: data.public_url })),
        data.status === "partial"
          ? `请求 ${data.requested} 个，成功 ${data.succeeded} 个，失败或状态未知 ${data.failed} 个。批次已安全停止。`
          : `请求 ${data.requested} 个，成功 ${data.succeeded} 个。密钥可在 Alias 列表再次查看。`,
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
        openModal([data], "密钥已签发，可在 Alias 列表再次查看。");
      } catch (error) {
        if (error instanceof AuthenticationRequiredError) return;
        window.alert("密钥签发失败。");
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll(".reveal-key-button").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const data = await api(
          `/admin/api/aliases/${button.dataset.aliasId}/key/reveal`,
          { method: "POST" },
        );
        openModal([data], "当前有效密钥。");
      } catch (error) {
        if (error instanceof AuthenticationRequiredError) return;
        window.alert(
          error.message === "conflict"
            ? "该密钥由旧版本签发，轮换后即可查看。"
            : "密钥读取失败。",
        );
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
  const selectVisible = document.querySelector("#select-visible-aliases");
  const selectedCount = document.querySelector("#selected-count");
  const bulkMessage = document.querySelector("#bulk-message");
  const aliasCheckboxes = [...document.querySelectorAll(".alias-checkbox")];

  function visibleCheckboxes() {
    return aliasCheckboxes.filter((checkbox) => !checkbox.closest(".alias-row").hidden);
  }

  function updateSelection() {
    const selected = aliasCheckboxes.filter((checkbox) => checkbox.checked).length;
    if (selectedCount) selectedCount.textContent = String(selected);
    const visible = visibleCheckboxes();
    const visibleSelected = visible.filter((checkbox) => checkbox.checked).length;
    if (selectVisible) {
      selectVisible.checked = visible.length > 0 && visibleSelected === visible.length;
      selectVisible.indeterminate = visibleSelected > 0 && visibleSelected < visible.length;
    }
  }

  aliasCheckboxes.forEach((checkbox) => checkbox.addEventListener("change", updateSelection));
  selectVisible?.addEventListener("change", () => {
    visibleCheckboxes().forEach((checkbox) => {
      checkbox.checked = selectVisible.checked;
    });
    updateSelection();
  });

  document.querySelectorAll(".bulk-action").forEach((button) => {
    button.addEventListener("click", async () => {
      const action = button.dataset.bulkAction;
      const aliasIds = aliasCheckboxes
        .filter((checkbox) => checkbox.checked)
        .map((checkbox) => checkbox.value);
      if (!aliasIds.length) {
        bulkMessage.textContent = "请先选择 Alias。";
        return;
      }
      let confirmed = false;
      if (action === "issue_keys") {
        confirmed = window.confirm("将为所有选中活动项签发或轮换密钥，旧密钥会立即失效。继续？");
        if (!confirmed) return;
      } else if (action === "deactivate") {
        confirmed = window.confirm("将串行停用所有选中活动项，并逐条等待 Apple 确认。继续？");
        if (!confirmed) return;
      } else if (action === "delete") {
        confirmed = window.confirm("将从 iCloud 永久删除所有选中的失活项。此操作无法恢复，确定继续？");
        if (!confirmed) return;
      }
      document.querySelectorAll(".bulk-action").forEach((item) => (item.disabled = true));
      bulkMessage.textContent = `正在串行处理 ${aliasIds.length} 项，请勿关闭页面…`;
      try {
        const data = await api("/admin/api/aliases/bulk", {
          method: "POST",
          body: JSON.stringify({ action, alias_ids: aliasIds, confirmed }),
        });
        const successfulKeys = data.results
          .filter((item) => item.status === "success" && item.access_key)
          .map((item) => ({ ...item, public_url: data.public_url }));
        if (successfulKeys.length) {
          openModal(
            successfulKeys,
            `请求 ${data.requested} 项，成功 ${data.succeeded} 项，失败 ${data.failed} 项。`,
          );
        } else {
          window.alert(`处理完成：成功 ${data.succeeded} 项，失败 ${data.failed} 项。`);
          window.location.reload();
        }
      } catch (error) {
        if (error instanceof AuthenticationRequiredError) return;
        bulkMessage.textContent = "批量操作失败，请刷新确认每项状态。";
        document.querySelectorAll(".bulk-action").forEach((item) => (item.disabled = false));
      }
    });
  });

  filter?.addEventListener("input", () => {
    const query = filter.value.trim().toLocaleLowerCase();
    let visible = 0;
    document.querySelectorAll(".alias-row").forEach((row) => {
      const matches = !query || row.dataset.aliasSearch.toLocaleLowerCase().includes(query);
      row.hidden = !matches;
      if (matches) visible += 1;
    });
    if (empty) empty.hidden = visible !== 0;
    updateSelection();
  });
  updateSelection();

  function clearAdminCodes() {
    adminCodesList?.replaceChildren();
    if (adminCodesTable) adminCodesTable.hidden = true;
    if (adminCodesEmpty) adminCodesEmpty.hidden = true;
  }

  function renderAdminCodes(items) {
    items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "admin-codes-row";
      row.setAttribute("role", "row");

      const identity = document.createElement("div");
      identity.className = "admin-code-alias";
      identity.setAttribute("role", "cell");
      const label = document.createElement("strong");
      label.textContent = item.label;
      const email = document.createElement("span");
      email.textContent = item.email;
      identity.append(label, email);

      const code = document.createElement("code");
      code.className = "admin-code-value";
      code.setAttribute("role", "cell");
      code.textContent = item.code;

      const receivedAt = document.createElement("time");
      receivedAt.className = "admin-code-time";
      receivedAt.setAttribute("role", "cell");
      receivedAt.dateTime = item.received_at;
      receivedAt.textContent = item.received_at_display;

      const action = document.createElement("div");
      action.className = "admin-code-action";
      action.setAttribute("role", "cell");
      action.append(createCopyButton(item.code));

      row.append(identity, code, receivedAt, action);
      adminCodesList.append(row);
    });
  }

  refreshCodesButton?.addEventListener("click", async () => {
    clearAdminCodes();
    adminCodesMessage.textContent = "正在读取最近验证码…";
    refreshCodesButton.disabled = true;
    refreshCodesButton.classList.add("is-busy");
    try {
      const data = await api("/admin/api/codes/recent", { method: "POST" });
      if (data.codes.length) {
        renderAdminCodes(data.codes);
        adminCodesTable.hidden = false;
      } else {
        adminCodesEmpty.hidden = false;
      }
      const suffix = data.truncated ? "，结果已达到扫描上限" : "";
      adminCodesMessage.textContent = `扫描 ${data.scanned} 封，找到 ${data.codes.length} 条${suffix}。`;
    } catch (error) {
      if (error instanceof AuthenticationRequiredError) return;
      adminCodesMessage.textContent =
        error.message === "not_configured"
          ? "IMAP 尚未配置。"
          : error.message === "busy"
            ? "IMAP 正忙，请稍后重试。"
            : "验证码读取失败，请稍后重试。";
    } finally {
      refreshCodesButton.disabled = false;
      refreshCodesButton.classList.remove("is-busy");
    }
  });

  window.addEventListener("pagehide", clearAdminCodes);

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
