const test = require("node:test");
const assert = require("node:assert/strict");

const TimeEchoVideo = require("../batch-video.js");

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

async function waitFor(predicate, timeout = 1200) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    if (predicate()) return;
    await wait(2);
  }
  assert.fail("等待视频控制器状态超时");
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function error(message, fields) {
  return Object.assign(new Error(message), fields || {});
}

test("start 并发调用共享同一个启动请求", async () => {
  const gate = deferred();
  const calls = [];
  const controller = TimeEchoVideo.createController({
    batchId: "batch/1",
    pollIntervalMs: 5,
    api: async (path, data) => {
      calls.push({ path, data });
      if (path === "/api/video/start") return gate.promise;
      throw error("不应重复查询", { code: "TEST" });
    }
  });

  const first = controller.start();
  const second = controller.start();
  assert.strictEqual(first, second);
  gate.resolve({ status: "waiting", progress: 0, filename: "echo.mp4" });
  await first;

  assert.deepEqual(calls, [{ path: "/api/video/start", data: { batchId: "batch/1" } }]);
  controller.dispose();
});

test("保留服务端的视频附加状态，默认不把 saveImages 当成图片数组", () => {
  const controller = TimeEchoVideo.createController({
    batchId: "state-shape",
    api: async () => ({ status: "waiting" })
  });
  assert.equal(controller.getState().saveImages, false);
  assert.deepEqual(controller.getState().missingJobIds, []);
  controller.dispose();
});

test("服务端返回的视频状态保留 missingJobIds 和 outputDir", async () => {
  const controller = TimeEchoVideo.createController({
    batchId: "state-fields",
    api: async path => path === "/api/video/start"
      ? {
        status: "running",
        progress: 10,
        saveImages: true,
        missingJobIds: ["job-2"],
        outputDir: "D:/exports"
      }
      : { status: "running", progress: 10 }
  });

  const state = await controller.start();
  assert.equal(state.saveImages, true);
  assert.deepEqual(state.missingJobIds, ["job-2"]);
  assert.equal(state.outputDir, "D:/exports");
  controller.dispose();
});

test("启动网络超时只查状态恢复，不重复提交视频任务", async () => {
  let startCalls = 0;
  let statusCalls = 0;
  const changes = [];
  const controller = TimeEchoVideo.createController({
    batchId: "网络恢复批次",
    pollIntervalMs: 5,
    api: async path => {
      if (path === "/api/video/start") {
        startCalls += 1;
        throw error("请求超时", { code: "ETIMEDOUT" });
      }
      statusCalls += 1;
      if (statusCalls === 1) return { status: "running", progress: 18, filename: "echo.mp4" };
      return { status: "done", progress: 100, filename: "echo.mp4", path: "outputs/echo.mp4" };
    },
    onChange: state => changes.push(state),
    onDone: async () => {}
  });

  const started = await controller.start();
  assert.equal(started.status, "running");
  assert.equal(startCalls, 1);
  assert.equal(started.connectionError, undefined);
  await waitFor(() => controller.getState().status === "done");

  assert.equal(startCalls, 1);
  assert.equal(statusCalls, 2);
  assert.equal(controller.getState().connectionError, undefined);
  assert.ok(changes.some(state => state.status === "running"));
  controller.dispose();
});

test("done 先持久化，成功后只清理一次图片缓存", async () => {
  const events = [];
  let statusCalls = 0;
  const controller = TimeEchoVideo.createController({
    batchId: "done-order",
    api: async path => {
      if (path === "/api/video/start") {
        return {
          status: "done",
          progress: 100,
          bgm: "old-days",
          filename: "echo.mp4",
          path: "outputs/echo.mp4",
          saveImages: true
        };
      }
      statusCalls += 1;
      return { status: "done", progress: 100, filename: "echo.mp4", path: "outputs/echo.mp4" };
    },
    persist: async state => {
      events.push(`persist:start:${state.status}`);
      await wait(3);
      events.push(`persist:end:${state.status}`);
    },
    onDone: async state => {
      events.push(`cleanup:${state.status}`);
    }
  });

  await controller.start();
  assert.deepEqual(events, ["persist:start:done", "persist:end:done", "cleanup:done"]);
  await controller.refresh();
  assert.deepEqual(events, ["persist:start:done", "persist:end:done", "cleanup:done"]);
  assert.equal(statusCalls, 1);
  assert.equal(controller.getState().error, undefined);
  controller.dispose();
});

test("持久化失败不会清理图片，后续 refresh 可重试完成处理", async () => {
  let persistCalls = 0;
  let cleanupCalls = 0;
  const controller = TimeEchoVideo.createController({
    batchId: "persist-retry",
    api: async path => path === "/api/video/start" ? { status: "done", progress: 100 } : { status: "done", progress: 100 },
    persist: async () => {
      persistCalls += 1;
      if (persistCalls === 1) throw error("IndexedDB 暂时不可用");
    },
    onDone: async () => {
      cleanupCalls += 1;
    }
  });

  await controller.start();
  assert.equal(persistCalls, 1);
  assert.equal(cleanupCalls, 0);
  assert.equal(controller.getState().status, "done");
  assert.equal(controller.getState().error.code, "PERSIST");

  await controller.refresh();
  assert.equal(persistCalls, 2);
  assert.equal(cleanupCalls, 1);
  assert.equal(controller.getState().status, "done");
  assert.equal(controller.getState().error, undefined);
  controller.dispose();
});

test("图片缓存清理失败保留 done，refresh 只重试清理不重新编码", async () => {
  let startCalls = 0;
  let cleanupCalls = 0;
  const controller = TimeEchoVideo.createController({
    batchId: "cleanup-retry",
    api: async path => {
      if (path === "/api/video/start") {
        startCalls += 1;
        return { status: "done", progress: 100 };
      }
      return { status: "done", progress: 100 };
    },
    onDone: async () => {
      cleanupCalls += 1;
      if (cleanupCalls === 1) throw error("图片缓存清理失败");
    }
  });

  await controller.start();
  assert.equal(startCalls, 1);
  assert.equal(cleanupCalls, 1);
  assert.equal(controller.getState().status, "done");
  assert.equal(controller.getState().error.code, "CLEANUP");

  await controller.refresh();
  assert.equal(startCalls, 1);
  assert.equal(cleanupCalls, 2);
  assert.equal(controller.getState().status, "done");
  assert.equal(controller.getState().error, undefined);

  await controller.refresh();
  assert.equal(startCalls, 1);
  assert.equal(cleanupCalls, 2);
  controller.dispose();
});

test("状态查询网络故障保留原状态并自动恢复轮询", async () => {
  let statusCalls = 0;
  const controller = TimeEchoVideo.createController({
    batchId: "refresh-network",
    initial: { status: "running", progress: 30, filename: "echo.mp4" },
    pollIntervalMs: 5,
    api: async path => {
      assert.equal(path, "/api/video/status?batchId=refresh-network");
      statusCalls += 1;
      if (statusCalls === 1) throw error("网络断开", { code: "NETWORK_ERROR" });
      return { status: "done", progress: 100, filename: "echo.mp4" };
    }
  });

  await controller.refresh();
  assert.equal(controller.getState().status, "running");
  assert.equal(controller.getState().connectionError.code, "NETWORK_ERROR");
  await waitFor(() => controller.getState().status === "done");
  assert.equal(statusCalls, 2);
  assert.equal(controller.getState().connectionError, undefined);
  controller.dispose();
});

test("取消会使在途启动失效，取消请求只发送一次且不派发过期回调", async () => {
  const startGate = deferred();
  const cancelGate = deferred();
  const calls = [];
  const changes = [];
  const controller = TimeEchoVideo.createController({
    batchId: "cancel-race",
    api: async (path, data) => {
      calls.push({ path, data });
      if (path === "/api/video/start") return startGate.promise;
      if (path === "/api/video/cancel") return cancelGate.promise;
      throw error("不应查询状态", { code: "TEST" });
    },
    onChange: state => changes.push(state)
  });

  const startPromise = controller.start();
  await waitFor(() => calls.length === 1);
  const cancelPromise = controller.cancel();
  assert.strictEqual(cancelPromise, controller.cancel());
  assert.equal(calls.length, 2);
  cancelGate.resolve({ status: "cancelled", progress: 21 });
  await cancelPromise;
  assert.equal(controller.getState().status, "cancelled");

  startGate.resolve({ status: "running", progress: 99 });
  await startPromise;
  assert.equal(controller.getState().status, "cancelled");
  assert.equal(calls.filter(call => call.path === "/api/video/start").length, 1);
  assert.equal(calls.filter(call => call.path === "/api/video/cancel").length, 1);
  assert.equal(changes.filter(state => state.status === "running").length, 0);
  controller.dispose();
});

test("取消响应为 done 时优先完成持久化和清理，不覆盖为 cancelled", async () => {
  const events = [];
  const controller = TimeEchoVideo.createController({
    batchId: "cancel-done-race",
    initial: { status: "running", progress: 80 },
    api: async path => {
      assert.equal(path, "/api/video/cancel");
      return { status: "done", progress: 100, path: "outputs/race.mp4" };
    },
    persist: async state => { events.push(`persist:${state.status}`); },
    onDone: async state => { events.push(`cleanup:${state.status}`); }
  });

  const result = await controller.cancel();
  assert.equal(result.status, "done");
  assert.equal(controller.getState().status, "done");
  assert.deepEqual(events, ["persist:done", "cleanup:done"]);
  controller.dispose();
});

test("waiting 空闲时 isBusy 为 false，正在请求或运行时为 true", async () => {
  const gate = deferred();
  const controller = TimeEchoVideo.createController({
    batchId: "busy-shape",
    api: async path => {
      if (path === "/api/video/start") return gate.promise;
      return { status: "running", progress: 1 };
    }
  });

  assert.equal(controller.isBusy(), false);
  const request = controller.start();
  assert.equal(controller.isBusy(), true);
  gate.resolve({ status: "running", progress: 2 });
  await request;
  assert.equal(controller.isBusy(), true);
  controller.dispose();
});

test("dispose 后忽略过期 done 回调，不执行持久化和图片清理", async () => {
  const startGate = deferred();
  let changes = 0;
  let persistCalls = 0;
  let cleanupCalls = 0;
  const controller = TimeEchoVideo.createController({
    batchId: "dispose-race",
    api: async path => {
      assert.equal(path, "/api/video/start");
      return startGate.promise;
    },
    onChange: () => { changes += 1; },
    persist: async () => { persistCalls += 1; },
    onDone: async () => { cleanupCalls += 1; }
  });

  const request = controller.start();
  controller.dispose();
  startGate.resolve({ status: "done", progress: 100 });
  const result = await request;

  assert.equal(result.status, "waiting");
  assert.equal(controller.getState().status, "waiting");
  assert.equal(changes, 0);
  assert.equal(persistCalls, 0);
  assert.equal(cleanupCalls, 0);
  assert.equal(controller.isBusy(), false);
});
