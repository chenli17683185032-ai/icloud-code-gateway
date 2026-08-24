(() => {
  "use strict";

  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const configuredPublicUrl =
    document.querySelector('meta[name="public-base-url"]')?.content || "";
  const publicUrl = configuredPublicUrl || window.location.origin;
  const configuredAliasBatchLimit = Number.parseInt(
    document.querySelector('meta[name="alias-batch-limit"]')?.content || "",
    10,
  );
  const aliasBatchLimit =
    Number.isSafeInteger(configuredAliasBatchLimit) && configuredAliasBatchLimit > 0
      ? configuredAliasBatchLimit
      : 100;
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
  const autoRefreshCodes = document.querySelector("#auto-refresh-codes");
  const adminCodesMessage = document.querySelector("#admin-codes-message");
  const adminCodesTable = document.querySelector("#admin-codes-table");
  const adminCodesList = document.querySelector("#admin-codes-list");
  const adminCodesEmpty = document.querySelector("#admin-codes-empty");
  let modalReturnFocus = null;
  let deleteTarget = null;
  let codesRefreshTimer = null;
  let codesRefreshInFlight = false;
  // A refresh is an in-memory read of the mailbox index the watcher maintains,
  // not an IMAP login, so the whole list can stay live at this interval.
  const CODES_REFRESH_MS = 3000;

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

  // Row actions have no message slot next to them, and window.alert() blocks
  // the page and reads like a browser fault. One live region reports the
  // outcome of every JSON action instead.
  let toastTimer = null;
  function toast(message, kind = "success") {
    let host = document.querySelector("#action-toast");
    if (!host) {
      host = document.createElement("div");
      host.id = "action-toast";
      host.className = "action-toast";
      host.setAttribute("role", "status");
      host.setAttribute("aria-live", "polite");
      document.body.append(host);
    }
    host.textContent = message;
    host.dataset.kind = kind;
    host.classList.add("is-visible");
    if (toastTimer) window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => host.classList.remove("is-visible"), 4200);
  }

  function reloadAfterToast(message) {
    toast(message, "success");
    window.setTimeout(() => window.location.reload(), 900);
  }

  function emailList(items) {
    return items.map((item) => item.email).join("\n");
  }

  // The buyer opens this and the code appears with nothing to type. The key
  // rides in the fragment so it never reaches the server as a logged query.
  function deliveryLink(item) {
    const base = String(item.public_url || publicUrl).replace(/\/+$/, "");
    return `${base}/#key=${encodeURIComponent(item.access_key)}`;
  }

  function deliveryLinkList(items) {
    return items.map(deliveryLink).join("\n");
  }

  function standardParameters(item) {
    const url = item.public_url || publicUrl;
    return `邮箱：${item.email}；网站：${url}；密钥：${item.access_key}；取码链接：${deliveryLink(item)}`;
  }

  function standardParameterList(items) {
    return items.map(standardParameters).join("\n");
  }

  async function writeClipboard(value) {
    const text = String(value ?? "");
    // Run the synchronous path inside the original click gesture. Some browser
    // clipboard permission checks take seconds even for plain text; execCommand
    // completes immediately and keeps copying an existing email fully local.
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.readOnly = true;
    textarea.setAttribute("aria-hidden", "true");
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    document.body.append(textarea);
    textarea.select();
    textarea.setSelectionRange(0, textarea.value.length);
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } finally {
      textarea.remove();
    }
    if (copied) return;
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
    throw new Error("copy_failed");
  }

  const CANONICAL_USAGE_TOKENS = {
    gpt: "gpt",
    grok: "grok",
    封号: "封号",
    活跃: "活跃",
    已使用: "已使用",
    banned: "封号",
    ban: "封号",
    active: "活跃",
    used: "已使用",
  };

  function splitUsageTokens(value) {
    const tokens = [];
    const leftover = [];
    String(value || "")
      .replaceAll(",", " ")
      .replaceAll("|", " ")
      .split(/\s+/)
      .forEach((piece) => {
        const raw = String(piece || "").trim();
        if (!raw) return;
        const token = CANONICAL_USAGE_TOKENS[raw.toLocaleLowerCase()];
        if (token) {
          if (!tokens.includes(token)) tokens.push(token);
          return;
        }
        leftover.push(raw);
      });
    const extra = leftover.join(" ").trim();
    if (extra && !tokens.includes(extra)) tokens.push(extra);
    return tokens;
  }

  function usageKind(value) {
    const tokens = splitUsageTokens(value);
    if (!tokens.length) return "empty";
    if (tokens.length === 1 && (tokens[0] === "gpt" || tokens[0] === "grok")) {
      return tokens[0];
    }
    return "custom";
  }

  function composeUsage(existing, nextFixedToken) {
    const tokens = splitUsageTokens(existing).filter(
      (token) =>
        token === "gpt" ||
        token === "grok" ||
        token === "封号" ||
        token === "活跃" ||
        token === "已使用",
    );
    if (tokens.includes(nextFixedToken)) {
      return tokens.filter((token) => token !== nextFixedToken).join(" ");
    }
    tokens.push(nextFixedToken);
    return tokens.join(" ");
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
        await writeClipboard(value);
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
    issuedList.append(createCopyButton(deliveryLinkList(items), "复制取码链接"));
    issuedList.append(createCopyButton(emailList(items), "一键复制"));
    issuedList.append(createCopyButton(standardParameterList(items), "导出信息"));
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

      const copy = createCopyButton(deliveryLink(item), "取码链接");

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
  const terminalJobStatuses = new Set([
    "completed",
    "partial",
    "failed",
    "needs_reconcile",
    "cancelled",
  ]);

  function jobCounts(job) {
    const counts = { success: 0, failed: 0, unknown: 0, queued: 0, running: 0, cancelled: 0 };
    (job.results || []).forEach((item) => {
      if (Object.hasOwn(counts, item.status)) counts[item.status] += 1;
    });
    return counts;
  }

  function jobSummary(job) {
    const counts = jobCounts(job);
    const parts = [`请求 ${job.requested} 项`, `成功 ${counts.success} 项`];
    if (counts.failed) parts.push(`明确失败 ${counts.failed} 项`);
    if (counts.unknown) parts.push(`远端结果不确定 ${counts.unknown} 项，需人工对账`);
    if (counts.queued) parts.push(`尚未开始 ${counts.queued} 项`);
    if (counts.running) parts.push(`处理中 ${counts.running} 项`);
    if (counts.cancelled) parts.push(`已取消 ${counts.cancelled} 项`);
    return `${parts.join("；")}。`;
  }

  function formatCountdown(totalSeconds) {
    const seconds = Math.max(0, Number(totalSeconds) || 0);
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const rest = seconds % 60;
    if (hours > 0) {
      return `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
    }
    return `${minutes}:${String(rest).padStart(2, "0")}`;
  }

  function jobProgressText(job) {
    if (job.wait_reason === "rate_limited") {
      const retryAfter = Number(job.retry_after_seconds || 0);
      const code = job.cooldown_code || "-41015";
      if (retryAfter > 0) {
        return (
          `Apple 限流 ${code}，已成功 ${job.succeeded}/${job.requested}。` +
          `冷却中，约 ${formatCountdown(retryAfter)} 后自动继续` +
          (job.resume_at ? `（至 ${job.resume_at}）` : "") +
          "。"
        );
      }
      return (
        `Apple 限流 ${code}，已成功 ${job.succeeded}/${job.requested}。` +
        `已关闭额外冷却，正在按串行间隔立即重试。`
      );
    }
    if (String(job.error || "").includes("rate limited")) {
      return (
        `Apple 限流中：已成功 ${job.succeeded}/${job.requested}，` +
        `正在继续重试剩余任务。`
      );
    }
    return `任务进度 ${job.current}/${job.requested}，成功 ${job.succeeded}，失败 ${job.failed}。`;
  }

  async function pollJob(jobId, messageElement) {
    while (true) {
      const job = await api(`/admin/api/jobs/${encodeURIComponent(jobId)}`);
      messageElement.textContent = terminalJobStatuses.has(job.status)
        ? jobSummary(job)
        : jobProgressText(job);
      if (terminalJobStatuses.has(job.status)) return job;
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
  }

  async function revealCompletedJob(job) {
    return api(`/admin/api/jobs/${encodeURIComponent(job.job_id)}/results`, {
      method: "POST",
    });
  }

  function showCompletedJob(job, messageElement) {
    const successfulKeys = job.results
      .filter((item) => item.status === "success" && item.access_key)
      .map((item) => ({ ...item, public_url: job.public_url }));
    const summary = jobSummary(job);
    if (successfulKeys.length) {
      openModal(successfulKeys, summary);
      return;
    }
    messageElement.textContent = summary;
    if (job.status !== "needs_reconcile") {
      reloadAfterToast(summary);
      return;
    }
    // A reconcile job deliberately does not reload, so the controls have to be
    // handed back or the toolbar stays dead until a manual refresh.
    toast(summary, "error");
    document.querySelectorAll(".bulk-action").forEach((item) => (item.disabled = false));
  }

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
    createMessage.textContent = `正在创建 ${payload.count} 项持久任务…`;
    createButton.disabled = true;
    createButton.classList.add("is-busy");
    try {
      const data = await api("/admin/api/aliases", {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify(payload),
      });
      const job = await pollJob(data.job_id, createMessage);
      showCompletedJob(await revealCompletedJob(job), createMessage);
    } catch (error) {
      if (error instanceof AuthenticationRequiredError) return;
      createMessage.textContent = "创建失败，未完成的 Alias 可通过对账恢复。";
    } finally {
      createButton.disabled = false;
      createButton.classList.remove("is-busy");
    }
  });

  document.querySelectorAll(".alias-email-copy").forEach((button) => {
    button.addEventListener("click", async () => {
      const feedback = button.parentElement.querySelector(".alias-copy-feedback");
      const email = button.dataset.copyEmail || "";
      // Clipboard only. Copying used to also pin the page to this one alias and
      // kick off an IMAP scan, which made it feel like a network round-trip and
      // left no way back to watching the whole list.
      try {
        await writeClipboard(email);
        feedback.textContent = "已复制";
      } catch (_error) {
        feedback.textContent = "复制失败";
      }
      window.setTimeout(() => {
        feedback.textContent = "";
      }, 1600);
    });
  });

  function applyUsageState(control, usageLabel) {
    const value = String(usageLabel || "").trim();
    const tokens = splitUsageTokens(value);
    const custom = tokens
      .filter((token) => !["gpt", "grok", "封号", "活跃", "已使用"].includes(token))
      .join(" ");
    control.dataset.usageLabel = value;
    control.querySelectorAll(".usage-choice").forEach((choice) => {
      const selected = choice.hasAttribute("data-usage-custom")
        ? Boolean(custom)
        : tokens.includes(choice.dataset.usageValue || "");
      choice.setAttribute("aria-pressed", selected ? "true" : "false");
    });
    const usageCurrent = control.querySelector(".usage-current");
    usageCurrent.textContent = custom;
    usageCurrent.hidden = !custom;
    const customInput = control.querySelector(".usage-custom-input");
    customInput.value = custom;
    control.querySelector(".usage-clear").disabled = !value;
    const row = control.closest(".alias-row");
    if (row) {
      row.dataset.searchUsage = value;
      composeRowSearch(row);
    }
  }

  function setUsageBusy(control, busy) {
    control.querySelectorAll("button, input").forEach((element) => {
      element.disabled = busy;
    });
    if (!busy) {
      control.querySelector(".usage-clear").disabled = !control.dataset.usageLabel;
    }
  }

  async function persistUsage(control, value, successMessage) {
    const feedback = control.querySelector(".usage-feedback");
    setUsageBusy(control, true);
    try {
      const data = await api(
        `/admin/api/aliases/${encodeURIComponent(control.dataset.aliasId)}/usage`,
        {
          method: "POST",
          body: JSON.stringify({ usage_label: value }),
        },
      );
      applyUsageState(control, data.usage_label);
      feedback.textContent = successMessage;
      window.setTimeout(() => {
        feedback.textContent = "";
      }, 1800);
      return true;
    } catch (error) {
      if (error instanceof AuthenticationRequiredError) return false;
      feedback.textContent = "用途保存失败。";
      return false;
    } finally {
      setUsageBusy(control, false);
    }
  }

  document.querySelectorAll(".alias-usage").forEach((control) => {
    applyUsageState(control, control.dataset.usageLabel || "");
    const customForm = control.querySelector(".usage-custom-form");
    const customInput = control.querySelector(".usage-custom-input");
    const feedback = control.querySelector(".usage-feedback");

    control.querySelectorAll(".usage-choice[data-usage-value]").forEach((button) => {
      button.addEventListener("click", async () => {
        const token = button.dataset.usageValue;
        const nextValue = composeUsage(control.dataset.usageLabel || "", token);
        customForm.hidden = true;
        const added = splitUsageTokens(nextValue).includes(token);
        await persistUsage(
          control,
          nextValue,
          added ? `已标记 ${button.textContent.trim()}。` : `已取消 ${button.textContent.trim()}。`,
        );
      });
    });

    control.querySelector("[data-usage-custom]").addEventListener("click", () => {
      customForm.hidden = false;
      feedback.textContent = "";
      customInput.focus();
      customInput.select();
    });

    control.querySelector(".usage-custom-cancel").addEventListener("click", () => {
      customForm.hidden = true;
      applyUsageState(control, control.dataset.usageLabel || "");
      feedback.textContent = "";
    });

    customForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const customValue = customInput.value.trim();
      if (!customValue || customValue.length > 80 || /[\r\n\u0000]/u.test(customValue)) {
        feedback.textContent = "请输入 1–80 个字符且不含换行的用途。";
        customInput.focus();
        return;
      }
      const tokens = splitUsageTokens(control.dataset.usageLabel || "").filter((token) =>
        ["gpt", "grok", "封号", "活跃", "已使用"].includes(token),
      );
      tokens.push(customValue);
      if (await persistUsage(control, tokens.join(" "), "自定义用途已保存。")) {
        customForm.hidden = true;
      }
    });

    control.querySelector(".usage-clear").addEventListener("click", async () => {
      customForm.hidden = true;
      await persistUsage(control, "", "用途已清除。");
    });
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
        toast(
          error.message === "edge_sync_error"
            ? "密钥已在本地签发，但同步云端失败，请稍后用「同步到云端」重试。"
            : "密钥签发失败。",
          "error",
        );
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
        toast(
          error.message === "conflict"
            ? "该密钥由旧版本签发，轮换后即可查看。"
            : "密钥读取失败。",
          "error",
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
        reloadAfterToast("密钥已撤销，旧密钥立即失效。");
      } catch (error) {
        if (error instanceof AuthenticationRequiredError) return;
        toast("密钥撤销失败。", "error");
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
        reloadAfterToast(`${email} 已在 iCloud 停用，密钥已撤销。`);
      } catch (error) {
        if (error instanceof AuthenticationRequiredError) return;
        toast("Alias 停用失败，本地状态未改变。请刷新后重试。", "error");
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
        reloadAfterToast(`${email} 已在 iCloud 恢复。`);
      } catch (error) {
        if (error instanceof AuthenticationRequiredError) return;
        toast("Alias 恢复失败，本地状态未改变。请刷新后重试。", "error");
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
      const email = deleteTarget.email;
      await api(`/admin/api/aliases/${deleteTarget.id}`, {
        method: "DELETE",
        body: JSON.stringify({ confirmation }),
      });
      closeDeleteModal();
      reloadAfterToast(`${email} 已从 iCloud 永久删除。`);
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

  function limitedVisibleSelectionCount(alreadySelected, visibleCount, limit) {
    return Math.min(visibleCount, Math.max(0, limit - alreadySelected));
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

  aliasCheckboxes.forEach((checkbox) =>
    checkbox.addEventListener("change", () => {
      const selected = aliasCheckboxes.filter((item) => item.checked);
      if (selected.length > aliasBatchLimit) {
        checkbox.checked = false;
        if (bulkMessage) bulkMessage.textContent = `单次最多选择 ${aliasBatchLimit} 项。`;
      }
      updateSelection();
    }),
  );
  selectVisible?.addEventListener("change", () => {
    const visible = visibleCheckboxes();
    if (!selectVisible.checked) {
      visible.forEach((checkbox) => {
        checkbox.checked = false;
      });
      updateSelection();
      return;
    }
    const alreadySelected = aliasCheckboxes.filter(
      (checkbox) => checkbox.checked && !visible.includes(checkbox),
    ).length;
    const available = limitedVisibleSelectionCount(
      alreadySelected,
      visible.length,
      aliasBatchLimit,
    );
    let selectedHere = 0;
    visible.forEach((checkbox) => {
      checkbox.checked = selectedHere < available;
      if (checkbox.checked) selectedHere += 1;
    });
    if (selectedHere < visible.length && bulkMessage) {
      bulkMessage.textContent = `单次最多选择 ${aliasBatchLimit} 项，已选择当前可见项中的前 ${selectedHere} 项。`;
    }
    updateSelection();
  });

  document.querySelectorAll(".bulk-action").forEach((button) => {
    button.addEventListener("click", async () => {
      const action = button.dataset.bulkAction;
      const selectedAliases = aliasCheckboxes.filter((checkbox) => checkbox.checked);
      const aliasIds = selectedAliases.map((checkbox) => checkbox.value);
      if (!aliasIds.length) {
        bulkMessage.textContent = "请先选择 Alias。";
        return;
      }
      if (aliasIds.length > aliasBatchLimit) {
        bulkMessage.textContent = `单次最多处理 ${aliasBatchLimit} 项，请缩小选择范围。`;
        updateSelection();
        return;
      }
      if (
        action === "deactivate" &&
        selectedAliases.some((checkbox) => checkbox.dataset.aliasState !== "active")
      ) {
        bulkMessage.textContent = "批量停用只能处理活动项，请取消选择失活项后重试。";
        return;
      }
      if (
        action === "delete" &&
        selectedAliases.some((checkbox) => checkbox.dataset.aliasState !== "inactive")
      ) {
        bulkMessage.textContent = "永久删除只能处理失活项，请先批量停用选中的活动项。";
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
      bulkMessage.textContent = `正在创建 ${aliasIds.length} 项持久任务…`;
      try {
        const data = await api("/admin/api/aliases/bulk", {
          method: "POST",
          headers: { "Idempotency-Key": crypto.randomUUID() },
          body: JSON.stringify({ action, alias_ids: aliasIds, confirmed }),
        });
        const job = await pollJob(data.job_id, bulkMessage);
        showCompletedJob(await revealCompletedJob(job), bulkMessage);
      } catch (error) {
        if (error instanceof AuthenticationRequiredError) return;
        bulkMessage.textContent = "批量操作失败，请刷新确认每项状态。";
        document.querySelectorAll(".bulk-action").forEach((item) => (item.disabled = false));
      }
    });
  });

  function firstNonTerminalJob(jobs) {
    return jobs.find((item) => !terminalJobStatuses.has(item.status));
  }

  async function resumeActiveJobs() {
    try {
      const data = await api("/admin/api/jobs");
      const job = firstNonTerminalJob(data.jobs);
      if (!job) return;
      const messageElement = job.kind === "create_aliases" ? createMessage : bulkMessage;
      if (!messageElement) return;
      messageElement.textContent = `正在恢复任务 ${job.current}/${job.requested}…`;
      const completed = await pollJob(job.job_id, messageElement);
      showCompletedJob(await revealCompletedJob(completed), messageElement);
    } catch (error) {
      if (error?.constructor !== AuthenticationRequiredError && createMessage) {
        createMessage.textContent = "恢复后台任务状态失败，请刷新重试。";
      }
    }
  }
  resumeActiveJobs();

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

  function clearAliasRowCodes() {
    document.querySelectorAll(".alias-code").forEach((cell) => {
      cell.dataset.code = "";
      cell.replaceChildren();
      const empty = document.createElement("span");
      empty.className = "alias-code-empty muted-text";
      empty.textContent = "—";
      cell.append(empty);
    });
  }

  // Usage labels and verification codes both feed the row filter. They used to
  // write `data-alias-search` directly and overwrite each other, so a row that
  // received a code silently stopped matching a search for "gpt" or "封号".
  function composeRowSearch(row) {
    const parts = [
      row.dataset.aliasBaseSearch || "",
      row.dataset.searchUsage || "",
      row.dataset.searchCode || "",
    ];
    row.dataset.aliasSearch = parts.join(" ").trim().replace(/\s+/g, " ");
  }

  function setRowSearchCode(cell, code) {
    const row = cell.closest(".alias-row");
    if (!row) return;
    row.dataset.searchCode = String(code || "");
    composeRowSearch(row);
  }

  function renderOneAliasCode(aliasId, item) {
    const cell = document.querySelector(`.alias-code[data-alias-id="${CSS.escape(aliasId)}"]`);
    if (!cell) return;
    // Repainting an unchanged cell would restart the copy button animation and
    // fight the operator's cursor, so only touch cells whose code moved.
    if (cell.dataset.code === (item?.code || "")) return;
    cell.dataset.code = item?.code || "";
    cell.replaceChildren();
    if (!item || !item.code) {
      const empty = document.createElement("span");
      empty.className = "alias-code-empty muted-text";
      empty.textContent = "—";
      cell.append(empty);
      setRowSearchCode(cell, "");
      return;
    }
    const wrap = document.createElement("div");
    wrap.className = "alias-code-content";
    const code = document.createElement("code");
    code.className = "alias-code-value";
    code.textContent = item.code;
    code.title = item.received_at_display
      ? `收到于 ${item.received_at_display}`
      : "最近验证码";
    const copy = createCopyButton(item.code, "复制");
    copy.classList.add("small-button");
    wrap.append(code, copy);
    if (item.received_at_display) {
      const time = document.createElement("time");
      time.className = "alias-code-time";
      time.dateTime = item.received_at || "";
      time.textContent = item.received_at_display;
      wrap.append(time);
    }
    cell.append(wrap);
    setRowSearchCode(cell, item.code);
  }

  function renderAliasRowCodes(byAlias) {
    const map = byAlias && typeof byAlias === "object" ? byAlias : {};
    document.querySelectorAll(".alias-code").forEach((cell) => {
      const aliasId = cell.dataset.aliasId || "";
      renderOneAliasCode(aliasId, map[aliasId] || null);
    });
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

  function imapCodesEnabled() {
    return Boolean(refreshCodesButton) && !(autoRefreshCodes?.disabled);
  }

  async function refreshAdminCodes({ quiet = false } = {}) {
    if (!imapCodesEnabled() || !adminCodesMessage) return;
    if (codesRefreshInFlight) return;
    codesRefreshInFlight = true;
    if (!quiet && refreshCodesButton) {
      adminCodesMessage.textContent = "正在读取最近验证码…";
      refreshCodesButton.disabled = true;
      refreshCodesButton.classList.add("is-busy");
    }
    try {
      const data = await api("/admin/api/codes/recent", {
        method: "POST",
        body: JSON.stringify({}),
      });
      clearAdminCodes();
      if (data.codes?.length) {
        renderAdminCodes(data.codes);
        if (adminCodesTable) adminCodesTable.hidden = false;
        if (adminCodesEmpty) adminCodesEmpty.hidden = true;
      } else if (adminCodesEmpty) {
        adminCodesEmpty.hidden = false;
      }
      renderAliasRowCodes(data.by_alias || {});
      const found = data.codes?.length || 0;
      adminCodesMessage.textContent = found
        ? `${found} 个邮箱有最近验证码。`
        : "最近没有收到验证码。";
    } catch (error) {
      if (error instanceof AuthenticationRequiredError) return;
      adminCodesMessage.textContent =
        error.message === "not_configured"
          ? "IMAP 尚未配置。"
          : error.message === "busy"
            ? "IMAP 正忙，请稍后重试。"
            : "验证码读取失败，请稍后重试。";
    } finally {
      codesRefreshInFlight = false;
      if (refreshCodesButton) {
        refreshCodesButton.disabled = Boolean(autoRefreshCodes?.disabled);
        refreshCodesButton.classList.remove("is-busy");
      }
    }
  }

  function scheduleCodesRefresh() {
    if (codesRefreshTimer) {
      window.clearTimeout(codesRefreshTimer);
      codesRefreshTimer = null;
    }
    if (!autoRefreshCodes?.checked || autoRefreshCodes.disabled) return;
    codesRefreshTimer = window.setTimeout(async () => {
      await refreshAdminCodes({ quiet: true });
      scheduleCodesRefresh();
    }, CODES_REFRESH_MS);
  }

  refreshCodesButton?.addEventListener("click", async () => {
    await refreshAdminCodes({ quiet: false });
    scheduleCodesRefresh();
  });

  autoRefreshCodes?.addEventListener("change", () => {
    if (autoRefreshCodes.checked) {
      refreshAdminCodes({ quiet: true }).finally(scheduleCodesRefresh);
    } else if (codesRefreshTimer) {
      window.clearTimeout(codesRefreshTimer);
      codesRefreshTimer = null;
    }
  });

  // The server answers from the warm mailbox index, so showing codes for every
  // alias costs no IMAP work and needs no pinning by the operator.
  if (imapCodesEnabled()) {
    refreshAdminCodes({ quiet: true }).finally(scheduleCodesRefresh);
  }

  window.addEventListener("pagehide", () => {
    clearAdminCodes();
    clearAliasRowCodes();
    if (codesRefreshTimer) {
      window.clearTimeout(codesRefreshTimer);
      codesRefreshTimer = null;
    }
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

  // Several of these POSTs block for a long time: a tag sweep reads the whole
  // mailbox, an edge sync pushes every alias, an Apple reconcile lists every
  // page. With no pending state the page looks frozen and people click again.
  // JS-driven forms call preventDefault first, so they are skipped here.
  document.addEventListener("submit", (event) => {
    if (event.defaultPrevented) return;
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const submitter =
      event.submitter instanceof HTMLButtonElement
        ? event.submitter
        : form.querySelector('button[type="submit"]');
    if (!submitter || submitter.disabled) return;
    const label = submitter.querySelector("span");
    if (label) label.textContent = "处理中…";
    submitter.classList.add("is-busy");
    // Disable only after the browser has serialised and sent the form.
    window.setTimeout(() => {
      submitter.disabled = true;
    }, 0);
  });

  // The result banner sits at the top of the page while most actions live far
  // below it, so after a redirect the confirmation was easy to miss entirely.
  const noticeBanner = document.querySelector(".notice");
  if (noticeBanner) {
    noticeBanner.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      composeUsage,
      deliveryLink,
      deliveryLinkList,
      emailList,
      firstNonTerminalJob,
      jobCounts,
      jobSummary,
      limitedVisibleSelectionCount,
      splitUsageTokens,
      standardParameterList,
      standardParameters,
      usageKind,
    };
  }
})();
