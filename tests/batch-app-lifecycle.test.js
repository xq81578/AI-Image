const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const appSource = fs.readFileSync(path.join(__dirname, "..", "batch-app.js"), "utf8");

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function makeNode() {
  return {
    value: "",
    checked: false,
    disabled: false,
    hidden: false,
    textContent: "",
    innerHTML: "",
    src: "",
    dataset: {},
    style: {},
    classList: {
      toggle() {},
      add() {},
      remove() {},
      contains() { return false; }
    },
    addEventListener() {},
    removeEventListener() {},
    querySelectorAll() { return []; },
    querySelector() { return makeNode(); },
    matches() { return false; },
    closest() { return null; },
    setAttribute(name, value) { this[name] = value; },
    removeAttribute(name) { delete this[name]; },
    close() { this.closed = true; },
    showModal() { this.open = true; },
    getBoundingClientRect() { return { left: 0, right: 0, top: 0, bottom: 0 }; }
  };
}

function makeDb() {
  const records = { jobs: new Map(), meta: new Map() };
  const transactions = [];
  return {
    records,
    transactions,
    transaction(storeNames, mode) {
      const names = Array.isArray(storeNames) ? storeNames : [storeNames];
      const tx = {
        mode,
        names,
        error: null,
        oncomplete: null,
        onerror: null,
        onabort: null,
        objectStore(name) {
          return {
            put(value, key) {
              const store = records[name] || (records[name] = new Map());
              const actualKey = name === "jobs" ? value.id : key;
              store.set(actualKey, clone(value));
            }
          };
        }
      };
      transactions.push(tx);
      queueMicrotask(() => {
        if (typeof tx.oncomplete === "function") tx.oncomplete();
      });
      return tx;
    }
  };
}

function makeJob(id, options = {}) {
  const current = options.current == null ? 6 : options.current;
  const statuses = Array.from({ length: 6 }, (_, index) => ({
    status: index < current ? "done" : "waiting",
    attempts: 1,
    error: null,
    elapsedMs: 1,
    retryAt: 0,
    startedAt: 0
  }));
  if (options.errorStage != null) statuses[options.errorStage].status = "error";
  return {
    id,
    index: 0,
    theme: "老家院子",
    oldYear: 2000,
    newYear: 2026,
    span: 26,
    photoStyle: "color",
    current,
    steps: statuses,
    paused: Boolean(options.paused),
    abandoned: Boolean(options.abandoned),
    results: {
      idea: { title: "旧日院子", story: "熟悉的地方渐渐安静。" },
      prompt: { decadePrompt: "old", nowPrompt: "now", caption: "caption", hashtags: [] },
      old: "data:old",
      now: "data:now",
      compose: "data:compose",
      save: { path: `output/${id}.png`, filename: `${id}.png` }
    },
    pendingImages: { old: "pending-old", now: "pending-now", compose: "pending-compose" },
    ...(options.imagesCleared ? { imagesCleared: true } : {})
  };
}

function makeBatch(id, jobs, saveImages = false) {
  return {
    id,
    createdAt: 123,
    settings: {
      count: jobs.length,
      brief: "",
      platform: "douyin",
      yearMode: "random",
      provider: "mock",
      textLimit: 6,
      imageLimit: 2,
      saveImages,
      config: {}
    },
    jobs,
    videoMode: true,
    video: { batchId: id, status: "done", progress: 100, filename: `${id}.mp4` },
    imagesCleared: false
  };
}

function loadApp() {
  const nodes = new Map();
  const nodeFor = selector => {
    if (!nodes.has(selector)) nodes.set(selector, makeNode());
    return nodes.get(selector);
  };
  const videoInstances = [];
  const videoApi = {
    createController(options) {
      const controller = {
        options,
        disposed: false,
        start() { return Promise.resolve({ status: "running" }); },
        refresh() { return Promise.resolve(options.initial || { status: "waiting" }); },
        cancel() { return Promise.resolve({ status: "cancelled" }); },
        dispose() { this.disposed = true; },
        isBusy() { return false; }
      };
      videoInstances.push(controller);
      return controller;
    }
  };
  const revoked = [];
  const document = {
    querySelector: nodeFor,
    querySelectorAll() { return []; },
    addEventListener() {},
    createElement: makeNode,
    body: { append() {} }
  };
  const window = {
    TimeEchoCore: null,
    TimeEchoPrompts: null,
    TimeEchoModels: {
      escapeHtml(value) { return String(value); },
      defaults: {},
      friendlyError(error) { return error.message || String(error); }
    },
    TimeEchoVideo: videoApi,
    addEventListener() {}
  };
  const context = {
    window,
    document,
    URL: {
      createObjectURL() { return "blob:preview"; },
      revokeObjectURL(url) { revoked.push(url); }
    },
    setTimeout() { return 1; },
    clearTimeout() {},
    queueMicrotask,
    console
  };
  const marker = "\n})();";
  const markerIndex = appSource.lastIndexOf(marker);
  assert.notEqual(markerIndex, -1, "batch-app.js 外层闭包结尾已变化，需更新测试 hook 注入点");
  const instrumented = `${appSource.slice(0, markerIndex)}
  window.__batchAppLifecycleTest = {
    serializable,
    clearBatchImages,
    metaSnapshot,
    releasePreviews,
    allFramesReady,
    maybeStartVideo,
    attachVideo,
    setBatch(value) { batch = value; },
    setDb(value) { db = value; },
    setVideoRefs(value) {
      if (Object.prototype.hasOwnProperty.call(value, "controller")) videoController = value.controller;
      if (Object.prototype.hasOwnProperty.call(value, "ready")) videoReady = value.ready;
      if (Object.prototype.hasOwnProperty.call(value, "action")) videoAction = value.action;
      if (Object.prototype.hasOwnProperty.call(value, "runner")) runner = value.runner;
    },
    imageCache
  };
})();`;
  vm.runInNewContext(instrumented, context, { filename: "batch-app.lifecycle.vm.js" });
  return { hook: context.window.__batchAppLifecycleTest, nodes, context, videoInstances, revoked };
}

test("imagesCleared 快照保留已完成 current，不因结果被清空而回退", () => {
  const { hook } = loadApp();
  const job = makeJob("cleared-1", { imagesCleared: true });
  job.results = {};
  job.pendingImages = {};
  job.current = 6;
  const snapshot = hook.serializable(job);
  assert.equal(snapshot.current, 6);
  assert.equal(snapshot.imagesCleared, true);
});

test("视频完成后原图、合图和 pendingImages 一次性清理并写入同一批元数据", async () => {
  const { hook, nodes, revoked } = loadApp();
  const db = makeDb();
  const job = makeJob("clear-1");
  const batch = makeBatch("batch-clear", [job], false);
  hook.setBatch(batch);
  hook.setDb(db);
  hook.imageCache.set("data:old", "blob:old");

  await hook.clearBatchImages(batch);

  const savedJob = db.records.jobs.get(job.id);
  assert.ok(savedJob);
  assert.equal(savedJob.current, 6);
  assert.equal(savedJob.results.old, undefined);
  assert.equal(savedJob.results.now, undefined);
  assert.equal(savedJob.results.compose, undefined);
  assert.ok(savedJob.results.save);
  assert.equal(savedJob.pendingImages, undefined);
  assert.equal(savedJob.imagesCleared, true);
  assert.equal(db.records.meta.get("latest").imagesCleared, true);
  assert.equal(batch.imagesCleared, true);
  assert.equal(job.results.old, undefined);
  assert.equal(job.results.now, undefined);
  assert.equal(job.results.compose, undefined);
  assert.equal(job.pendingImages, undefined);
  assert.equal(job.imagesCleared, true);
  assert.deepEqual(revoked, ["blob:old"]);
  assert.equal(nodes.get("#pictureFull").src, undefined);
  assert.equal(nodes.get("#pictureDialog").closed, true);
});

test("saveImages 开启时清理回调跳过删除图片", async () => {
  const { hook } = loadApp();
  const db = makeDb();
  const job = makeJob("keep-1");
  const batch = makeBatch("batch-keep", [job], true);
  hook.setBatch(batch);
  hook.setDb(db);

  await hook.clearBatchImages(batch);

  assert.equal(db.transactions.length, 0);
  assert.equal(job.results.compose, "data:compose");
  assert.equal(job.pendingImages.now, "pending-now");
  assert.equal(batch.imagesCleared, false);
});

test("过期批次、已清理批次的回调不触碰当前缓存", async () => {
  const { hook } = loadApp();
  const db = makeDb();
  const owner = makeBatch("old-batch", [makeJob("old-job")], false);
  const current = makeBatch("current-batch", [makeJob("current-job")], false);
  hook.setBatch(current);
  hook.setDb(db);
  await hook.clearBatchImages(owner);
  assert.equal(db.transactions.length, 0);
  assert.equal(owner.jobs[0].results.compose, "data:compose");

  owner.imagesCleared = true;
  hook.setBatch(owner);
  await hook.clearBatchImages(owner);
  assert.equal(db.transactions.length, 0);
  assert.equal(owner.jobs[0].results.compose, "data:compose");
});

test("暂停、失败组阻止自动开始视频，放弃组从就绪判断中排除", () => {
  const { hook } = loadApp();
  const controller = {
    starts: 0,
    start() { this.starts += 1; return Promise.resolve(); },
    isBusy() { return false; }
  };
  const ready = makeJob("ready");
  const paused = makeJob("paused", { current: 5, paused: true });
  const batch = makeBatch("batch-ready", [ready, paused], false);
  batch.video.status = "waiting";
  hook.setBatch(batch);
  hook.setVideoRefs({ controller, ready: true, action: false, runner: { getSummary() { return { pausedAll: false }; } } });
  assert.equal(hook.allFramesReady(), false);
  hook.maybeStartVideo();
  assert.equal(controller.starts, 0);

  paused.abandoned = true;
  assert.equal(hook.allFramesReady(), true);
  hook.maybeStartVideo();
  assert.equal(controller.starts, 1);

  const failed = makeJob("failed", { current: 5, errorStage: 5 });
  failed.abandoned = false;
  const failedBatch = makeBatch("batch-failed", [ready, failed], false);
  failedBatch.video.status = "waiting";
  hook.setBatch(failedBatch);
  hook.setVideoRefs({ controller, ready: true, action: false, runner: { getSummary() { return { pausedAll: false }; } } });
  assert.equal(hook.allFramesReady(), false);
  hook.maybeStartVideo();
  assert.equal(controller.starts, 1);
});

test("整批暂停阻止视频自动开始，恢复后才允许开始", () => {
  const { hook } = loadApp();
  const controller = {
    starts: 0,
    start() { this.starts += 1; return Promise.resolve(); },
    isBusy() { return false; }
  };
  const batch = makeBatch("batch-global-pause", [makeJob("ready")], false);
  batch.video.status = "waiting";
  hook.setBatch(batch);
  const runner = { pausedAll: true, getSummary() { return { pausedAll: this.pausedAll }; } };
  hook.setVideoRefs({ controller, ready: true, action: false, runner });
  hook.maybeStartVideo();
  assert.equal(controller.starts, 0);
  runner.pausedAll = false;
  hook.maybeStartVideo();
  assert.equal(controller.starts, 1);
});

test("切换批次后旧视频 controller 的观察、持久化和完成回调全部失效", async () => {
  const { hook, videoInstances } = loadApp();
  const db = makeDb();
  const owner = makeBatch("owner", [makeJob("owner-job")], false);
  const other = makeBatch("other", [makeJob("other-job")], false);
  hook.setDb(db);
  hook.setBatch(owner);
  hook.attachVideo();
  const controller = videoInstances.at(-1);
  const callbacks = controller.options;
  hook.setBatch(other);

  callbacks.onChange({ status: "running", progress: 30 });
  await callbacks.persist({ status: "running", progress: 30 });
  await callbacks.onDone();

  assert.deepEqual(owner.video, { batchId: "owner", status: "done", progress: 100, filename: "owner.mp4" });
  assert.equal(owner.jobs[0].results.compose, "data:compose");
  assert.equal(db.transactions.length, 0);
});
