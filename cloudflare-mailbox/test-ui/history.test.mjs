import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(
  new URL("../public/admin/app.js", import.meta.url),
  "utf8",
);

function message(number, overrides = {}) {
  return {
    id: String(number),
    email: "test@icloud.com",
    sender: "sender@example.com",
    subject: `邮件 ${number}`,
    body: `邮件正文 ${number}`,
    receivedAt: new Date(Date.UTC(2026, 0, 1) + number * 1000).toISOString(),
    expiresAt: "2099-01-01T00:00:00.000Z",
    permanent: false,
    attachments: [],
    ...overrides,
  };
}

function page(messages, nextCursor = "") {
  return {
    messages,
    has_more: Boolean(nextCursor),
    next_cursor: nextCursor || null,
  };
}

function response(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

class Element {
  constructor() {
    this.children = [];
    this.dataset = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.textContent = "";
    this.attributes = {};
    this.listeners = new Map();
    this.selectors = new Map();
    this.classList = { add() {}, remove() {} };
  }
  append(...children) {
    this.children.push(...children);
  }
  replaceChildren(...children) {
    this.children = children;
  }
  querySelector(selector) {
    if (!this.selectors.has(selector))
      this.selectors.set(selector, new Element());
    return this.selectors.get(selector);
  }
  setAttribute(name, value) {
    this.attributes[name] = value;
  }
  addEventListener(event, listener) {
    this.listeners.set(event, listener);
  }
  focus() {}
}

function inbox(handler, { unified = false } = {}) {
  const nodes = new Map();
  const timers = new Map();
  const calls = [];
  const redirects = [];
  let timerId = 0;
  const document = {
    hidden: false,
    body: new Element(),
    createElement: () => new Element(),
    addEventListener() {},
    querySelector(selector) {
      if (!nodes.has(selector)) nodes.set(selector, new Element());
      return nodes.get(selector);
    },
  };
  document.querySelector("#operator-view").hidden = true;
  const context = vm.createContext({
    document,
    URLSearchParams,
    AbortController,
    navigator: {},
    history: { replaceState() {} },
    window: {
      location: {
        hostname: unified ? "icloud.yunbay.xyz" : "mail.example.com",
        pathname: unified ? "/admin/mail/" : "/admin/",
        hash: "",
        search: "",
        replace: (path) => redirects.push(path),
      },
      setTimeout(callback, delay) {
        const id = ++timerId;
        timers.set(id, { callback, delay });
        return id;
      },
      clearTimeout: (id) => timers.delete(id),
      addEventListener() {},
    },
    fetch: async (path) => {
      // Keep the real startup/restore path, without making it load a mailbox.
      if (path === "/api/operator/session")
        return response({ authenticated: false });
      const url = new URL(path, "https://mail.example.com");
      assert.equal(url.pathname, "/api/operator/messages");
      assert.equal(url.searchParams.get("limit"), "50");
      const cursor = url.searchParams.get("cursor") || "";
      calls.push(cursor);
      return handler(cursor, calls.length);
    },
  });
  vm.runInContext(source, context, { filename: "admin/app.js" });
  const api = vm.runInContext(
    `({ refresh, loadAllMessages, showEntry,
      select(id) { selectedId = id; renderMessages(messages, { force: true }); },
      search(value) {
        elements.search.value = value;
        searchQuery = normalizeSearch(value);
        renderMessages(messages, { force: true });
      },
      snapshot() {
        return JSON.stringify({
          ids: messages.map(message => message.id),
          messages, selectedId, searchQuery, nextCursor, hasMore,
          refreshInFlight, archiveLoadInFlight,
          count: elements.count.textContent,
          progress: elements.searchState.textContent,
          error: elements.error.hidden ? null : elements.error.textContent,
          viewHidden: elements.view.hidden,
          refreshDisabled: elements.refreshButton.disabled,
          listLength: elements.list.children.length,
          readerLength: elements.reader.children.length
        });
      }
    })`,
    context,
  );
  return {
    api,
    calls,
    redirects,
    timers,
    state: () => JSON.parse(api.snapshot()),
    async turn() {
      await new Promise(setImmediate);
    },
    async settle() {
      for (let pass = 0; pass < 100; pass += 1) {
        await new Promise(setImmediate);
        // Run focus, animation, and retry timers, not polling or API timeouts.
        for (const [id, timer] of [...timers]) {
          if (timer.delay <= 800) {
            timers.delete(id);
            timer.callback();
          }
        }
        await new Promise(setImmediate);
        const state = this.state();
        if (!state.refreshInFlight && !state.archiveLoadInFlight) return;
      }
      assert.fail("Mailbox did not finish its bounded operation");
    },
  };
}

test("manual refresh replaces a failed cursor and keeps search and selection", async () => {
  let repaired = false;
  const app = inbox((cursor) => {
    if (!cursor)
      return response(
        page([message(5), message(4)], repaired ? "fresh" : "bad"),
      );
    if (cursor === "bad")
      return response({ message: "邮件分页位置无效。" }, 400);
    assert.equal(cursor, "fresh");
    return response(page([message(3)]));
  });
  await app.api.refresh();
  app.api.select("4");
  app.api.search("邮件");
  await app.api.loadAllMessages();
  assert.equal(app.state().hasMore, true);
  assert.match(app.state().error, /刷新/);
  assert.deepEqual(app.calls, ["", "bad"]);
  repaired = true;
  await app.api.refresh();
  await app.settle();
  assert.deepEqual(app.calls, ["", "bad", "", "fresh"]);
  assert.deepEqual(app.state().ids, ["5", "4", "3"]);
  assert.equal(app.state().selectedId, "4");
  assert.equal(app.state().searchQuery, "邮件");
  assert.equal(app.state().hasMore, false);
  assert.equal(app.state().error, null);
});

test("51 new arrivals after a complete 50-message inbox leave no hidden gap", async () => {
  let data = Array.from({ length: 50 }, (_, index) => message(50 - index));
  const app = inbox((cursor) => {
    const offset = cursor
      ? data.findIndex((item) => item.id === cursor) + 1
      : 0;
    const items = data.slice(offset, offset + 50);
    return response(
      page(items, offset + 50 < data.length ? items.at(-1).id : ""),
    );
  });
  await app.api.refresh();
  assert.equal(app.state().hasMore, false);
  data = Array.from({ length: 101 }, (_, index) => message(101 - index));
  await app.api.refresh({ quiet: true });
  await app.settle();
  assert.deepEqual(
    app.state().ids,
    data.map((item) => item.id),
  );
  assert.equal(app.state().hasMore, false);
  assert.equal(app.state().count, "101 封");
});

test("each completed archive page is rendered while the next page is pending", async () => {
  const lastPage = deferred();
  const app = inbox((cursor) => {
    if (!cursor) return response(page([message(9), message(8)], "8"));
    if (cursor === "8") return response(page([message(7), message(6)], "6"));
    assert.equal(cursor, "6");
    return lastPage.promise;
  });
  await app.api.refresh();
  const loading = app.api.loadAllMessages();
  await app.turn();
  assert.deepEqual(app.state().ids, ["9", "8", "7", "6"]);
  assert.equal(app.state().count, "4+ 封");
  assert.equal(app.state().listLength, 5);
  assert.equal(app.state().archiveLoadInFlight, true);
  assert.equal(app.state().refreshDisabled, true);
  assert.match(app.state().progress, /正在加载/);
  lastPage.resolve(response(page([message(5)])));
  await loading;
  assert.equal(app.state().count, "5 封");
  assert.equal(app.state().archiveLoadInFlight, false);
});

test("transient archive errors retry the same cursor and recover on attempt three", async () => {
  let attempts = 0;
  const app = inbox((cursor) => {
    if (!cursor) return response(page([message(3)], "3"));
    attempts += 1;
    return attempts < 3
      ? response({ message: "服务暂时不可用。" }, 503)
      : response(page([message(2)]));
  });
  await app.api.refresh();
  const loading = app.api.loadAllMessages();
  await app.settle();
  await loading;
  assert.equal(attempts, 3);
  assert.deepEqual(app.calls, ["", "3", "3", "3"]);
  assert.equal(app.state().hasMore, false);
  assert.equal(app.state().error, null);
});

test("persistent archive errors stop after three attempts without losing the loaded page", async () => {
  const app = inbox((cursor) =>
    cursor
      ? response({ message: "服务暂时不可用。" }, 503)
      : response(page([message(3)], "3")),
  );
  await app.api.refresh();
  const loading = app.api.loadAllMessages();
  await app.settle();
  await loading;
  assert.deepEqual(app.calls, ["", "3", "3", "3"]);
  assert.deepEqual(app.state().ids, ["3"]);
  assert.equal(app.state().hasMore, true);
  assert.equal(app.state().nextCursor, "3");
  assert.equal(app.state().archiveLoadInFlight, false);
  assert.equal(app.state().refreshDisabled, false);
  assert.match(app.state().error, /已保留/);
});

test("refresh and polling cannot race an active archive load", async () => {
  const older = deferred();
  let heads = 0;
  const app = inbox((cursor) => {
    if (cursor) return older.promise;
    heads += 1;
    return response(
      heads === 1
        ? page([message(5), message(4)], "4")
        : page([message(5), message(4), message(3)]),
    );
  });
  await app.api.refresh();
  const loading = app.api.loadAllMessages();
  await app.api.refresh({ quiet: true });
  await app.api.refresh();
  assert.deepEqual(app.calls, ["", "4"]);
  assert.equal(
    [...app.timers.values()].some((timer) => timer.delay === 3000),
    false,
  );
  older.resolve(response(page([message(3)])));
  await loading;
  await app.settle();
  assert.deepEqual(app.calls, ["", "4", ""]);
  assert.deepEqual(app.state().ids, ["5", "4", "3"]);
});

test("archive request during refresh waits for the newly returned cursor", async () => {
  const freshHead = deferred();
  let heads = 0;
  const app = inbox((cursor) => {
    if (cursor) {
      assert.equal(cursor, "fresh");
      return response(page([message(3)]));
    }
    heads += 1;
    return heads === 1
      ? response(page([message(5)], "old"))
      : freshHead.promise;
  });
  await app.api.refresh();
  const refreshing = app.api.refresh();
  await app.api.loadAllMessages();
  assert.deepEqual(app.calls, ["", ""]);
  freshHead.resolve(response(page([message(5), message(4)], "fresh")));
  await refreshing;
  await app.settle();
  assert.deepEqual(app.calls, ["", "", "fresh"]);
  assert.deepEqual(app.state().ids, ["5", "4", "3"]);
});

test("quiet refresh keeps permanent history despite stale expiry and applies fresh content", async () => {
  let refreshed = false;
  const oldExpiry = "2000-01-01T00:00:00.000Z";
  const app = inbox((cursor) => {
    if (cursor)
      return response(
        page([
          message(2, { permanent: true, expiresAt: oldExpiry }),
          message(1, { expiresAt: oldExpiry }),
        ]),
      );
    return response(
      refreshed
        ? page([message(5), message(4, { subject: "更新后的标题" })], "4")
        : page([message(4), message(3)], "3"),
    );
  });
  await app.api.refresh();
  await app.api.loadAllMessages();
  app.api.select("2");
  refreshed = true;
  await app.api.refresh({ quiet: true });
  assert.deepEqual(app.state().ids, ["5", "4", "3", "2"]);
  assert.equal(app.state().selectedId, "2");
  assert.equal(
    app.state().messages.find((item) => item.id === "4").subject,
    "更新后的标题",
  );
  assert.equal(app.state().hasMore, false);
});

for (const unified of [false, true]) {
  test(`401 clears sensitive content without finally restoring it (unified=${unified})`, async () => {
    const app = inbox(
      (cursor) =>
        cursor
          ? response({ message: "请重新登录。" }, 401)
          : response(page([message(3)], "3")),
      { unified },
    );
    await app.api.refresh();
    await app.api.loadAllMessages();
    assert.deepEqual(app.calls, ["", "3"]);
    assert.deepEqual(app.state().ids, []);
    assert.equal(app.state().viewHidden, true);
    assert.equal(app.state().listLength, 0);
    assert.equal(app.state().readerLength, 0);
    assert.equal(app.state().archiveLoadInFlight, false);
    assert.equal(
      [...app.timers.values()].some((timer) => timer.delay === 3000),
      false,
    );
    if (unified) assert.deepEqual(app.redirects, ["/admin/operator-session"]);
  });
}

test("a response from before logout cannot reopen or repopulate the mailbox", async () => {
  const pending = deferred();
  const app = inbox(() => pending.promise);
  const refreshing = app.api.refresh();
  app.api.showEntry("已退出。");
  pending.resolve(response(page([message(5)])));
  await refreshing;
  assert.deepEqual(app.state().ids, []);
  assert.equal(app.state().viewHidden, true);
  assert.equal(app.state().listLength, 0);
  assert.equal(app.state().readerLength, 0);
});

test("repeated cursor errors can be repaired by a manual refresh", async () => {
  let repaired = false;
  const app = inbox((cursor) => {
    if (!cursor)
      return response(page([message(5)], repaired ? "fresh" : "same"));
    return response(
      cursor === "fresh"
        ? page([message(4), message(3)])
        : page([message(4)], "same"),
    );
  });
  await app.api.refresh();
  await app.api.loadAllMessages();
  assert.deepEqual(app.state().ids, ["5", "4"]);
  assert.match(app.state().error, /分页位置异常/);
  repaired = true;
  await app.api.refresh();
  await app.settle();
  assert.equal(app.state().hasMore, false);
  assert.deepEqual(app.state().ids, ["5", "4", "3"]);
});
