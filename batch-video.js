/*
 * 时光对照视频阶段控制器。
 *
 * 这个文件不依赖 DOM，既可以作为浏览器脚本使用，也可以在 Node 中
 * require。视频的 prepare/frame 工作由上层负责，本模块只负责服务端
 * FFmpeg 任务的启动、状态恢复、轮询、取消和完成后的清理。
 */
(function exposeTimeEchoVideo(root, factory) {
  const api = factory();
  if (typeof module === "object" && module && module.exports) module.exports = api;
  if (root) root.TimeEchoVideo = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createVideoApi() {
  "use strict";

  const STATUSES = Object.freeze(["waiting", "running", "done", "error", "cancelled"]);
  const DEFAULT_POLL_INTERVAL_MS = 1000;

  function isObject(value) {
    return value !== null && typeof value === "object";
  }

  function asStatus(value) {
    return STATUSES.includes(value) ? value : null;
  }

  function cloneValue(value) {
    if (Array.isArray(value)) return value.slice();
    if (!isObject(value)) return value;
    const result = {};
    for (const key of Object.keys(value)) result[key] = value[key];
    return result;
  }

  function normalizeError(error, fallbackCode) {
    if (isObject(error) && error.error && isObject(error.error)) error = error.error;
    if (error instanceof Error) {
      const result = { message: error.message || "请求失败" };
      if (error.code != null) result.code = error.code;
      if (error.status != null) result.status = error.status;
      if (error.name && error.name !== "Error") result.name = error.name;
      if (fallbackCode && !result.code) result.code = fallbackCode;
      return result;
    }
    if (isObject(error)) {
      const result = {};
      for (const key of ["message", "code", "status", "name", "detail"]) {
        if (error[key] != null) result[key] = error[key];
      }
      if (!result.message) result.message = String(error.message || error);
      if (fallbackCode && !result.code) result.code = fallbackCode;
      return result;
    }
    return { message: error == null ? "请求失败" : String(error), ...(fallbackCode ? { code: fallbackCode } : {}) };
  }

  function errorStatus(error) {
    const values = [
      error && error.status,
      error && error.statusCode,
      error && error.response && error.response.status,
      error && error.error && error.error.status
    ];
    for (const value of values) {
      const number = Number(value);
      if (Number.isFinite(number)) return number;
    }
    return null;
  }

  function isRecoverableError(error) {
    const status = errorStatus(error);
    // 409 在视频接口里通常表示业务占用/状态冲突，留在 error 让用户手动重试，
    // 避免 waiting + 自动轮询形成重复启动；真正的网络和服务端暂时故障仍可恢复。
    if (status === 408 || status === 425 || status === 429 || status >= 500) return true;
    if (status != null && status >= 400 && status < 500) return false;
    const code = String(error && (error.code || error.name || "")).toLowerCase();
    const message = String(error && error.message || error || "").toLowerCase();
    if (/validation|invalid|unauthorized|forbidden|校验|验证/.test(code + " " + message)) return false;
    return Boolean(
      !status ||
      code.includes("network") ||
      code.includes("timeout") ||
      code === "aborterror" ||
      code === "etimedout" ||
      code === "econnreset" ||
      /network|fetch|timeout|timed?\s*out|连接.*失败|连接.*超时/.test(message)
    );
  }

  function payloadState(payload) {
    if (isObject(payload) && isObject(payload.state)) return payload.state;
    if (isObject(payload) && isObject(payload.video)) return payload.video;
    if (isObject(payload) && isObject(payload.result) && isObject(payload.result.state)) return payload.result.state;
    return isObject(payload) ? payload : {};
  }

  function createController(options) {
    const input = options || {};
    if (input.batchId == null || String(input.batchId) === "") throw new TypeError("createController 需要 batchId。");
    if (typeof input.api !== "function") throw new TypeError("createController 需要 api(path, data) 函数。");

    const batchId = String(input.batchId);
    const api = input.api;
    const persist = typeof input.persist === "function" ? input.persist : async function noopPersist() {};
    const onChange = typeof input.onChange === "function" ? input.onChange : function noopChange() {};
    const onDone = typeof input.onDone === "function" ? input.onDone : async function noopDone() {};
    const pollIntervalMs = Math.max(1, Number.isFinite(Number(input.pollIntervalMs))
      ? Number(input.pollIntervalMs)
      : DEFAULT_POLL_INTERVAL_MS);

    let state = {
      batchId,
      status: "waiting",
      progress: 0,
      bgm: null,
      filename: "",
      saveImages: false,
      missingJobIds: []
    };
    const initial = payloadState(input.initial);
    if (input.initial != null) state = mergeServerState(state, initial, false);

    let disposed = false;
    let epoch = 0;
    let pollTimer = null;
    let startInFlight = null;
    let refreshInFlight = null;
    let cancelInFlight = null;
    let doneInFlight = null;
    let persistChain = Promise.resolve();
    let doneFinalized = false;
    let lastPersistError = null;

    function snapshot() {
      const result = {};
      for (const key of Object.keys(state)) result[key] = cloneValue(state[key]);
      return result;
    }

    function alive(expectedEpoch) {
      return !disposed && expectedEpoch === epoch;
    }

    function safeChange() {
      if (disposed) return;
      try {
        const result = onChange(snapshot());
        if (result && typeof result.then === "function") result.catch(function ignoreChangeError() {});
      } catch (ignored) {
        // UI 观察回调不能影响状态机和轮询。
      }
    }

    function comparableState(value) {
      try {
        return JSON.stringify(value, function omitTransient(key, item) {
          if (key === "connectionError") return undefined;
          return item;
        });
      } catch (ignored) {
        return "";
      }
    }

    function mergeServerState(previous, payload, clearConnection) {
      const next = { ...previous };
      if (payload && Object.prototype.hasOwnProperty.call(payload, "status")) {
        const status = asStatus(payload.status);
        if (status) next.status = status;
      }
      if (payload && Object.prototype.hasOwnProperty.call(payload, "progress")) {
        const progress = Number(payload.progress);
        if (Number.isFinite(progress)) next.progress = progress;
      }
      for (const key of ["bgm", "filename", "path", "saveImages", "missingJobIds", "outputDir"]) {
        if (payload && Object.prototype.hasOwnProperty.call(payload, key)) next[key] = cloneValue(payload[key]);
      }
      if (payload && Object.prototype.hasOwnProperty.call(payload, "error")) {
        next.error = payload.error == null ? undefined : cloneValue(payload.error);
      }
      // 服务端确认重新进入活动态或完成态时，清除上一次启动/请求留下的错误；
      // 完成后的本地清理错误会在 finalizeDone 成功后清除，刷新期间仍可重试。
      if (clearConnection && !Object.prototype.hasOwnProperty.call(payload || {}, "error") &&
        (next.status === "waiting" || next.status === "running" || next.status === "cancelled")) delete next.error;
      if (clearConnection) delete next.connectionError;
      return next;
    }

    function clearPoll() {
      if (pollTimer !== null) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
    }

    function isTerminal() {
      return state.status === "done" || state.status === "error" || state.status === "cancelled";
    }

    function shouldPoll() {
      return !disposed && !isTerminal() && state.status !== "done";
    }

    function schedulePoll(delay) {
      if (!shouldPoll() || pollTimer !== null || refreshInFlight !== null) return;
      pollTimer = setTimeout(() => {
        pollTimer = null;
        if (disposed) return;
        refresh();
      }, delay == null ? pollIntervalMs : Math.max(0, Number(delay) || 0));
    }

    function queuePersist(value, expectedEpoch, awaitResult) {
      if (typeof persist !== "function") return Promise.resolve();
      const task = persistChain.then(async () => {
        if (!alive(expectedEpoch)) return;
        const result = await persist(cloneValue(value));
        if (result === false) throw new Error("视频状态缓存失败。");
        return result;
      });
      // 后续状态仍要有机会写入；每一项自身的错误由调用方处理。
      persistChain = task.catch(function swallowPersistError() {});
      if (awaitResult) return task;
      task.catch(error => {
        if (!alive(expectedEpoch)) return;
        lastPersistError = normalizeError(error, "PERSIST");
        state.error = cloneValue(lastPersistError);
        safeChange();
      });
      return task;
    }

    function applyState(payload, expectedEpoch, optionsForApply) {
      if (!alive(expectedEpoch)) return false;
      const optionsValue = optionsForApply || {};
      const previous = state;
      const next = mergeServerState(previous, payloadState(payload), optionsValue.clearConnection !== false);
      if (optionsValue.forceStatus) next.status = optionsValue.forceStatus;
      if (next.status === "done") {
        // 服务端完成后由 finalizeDone 负责唯一一次终态 persist/onDone。
        clearPoll();
      }
      if (next.status === "error" || next.status === "cancelled") clearPoll();
      state = next;
      const changed = comparableState(previous) !== comparableState(next) ||
        Boolean(previous.connectionError) !== Boolean(next.connectionError);
      if (changed) {
        safeChange();
        if (next.status !== "done" && optionsValue.persist !== false) queuePersist(snapshot(), expectedEpoch, false);
      }
      return changed;
    }

    function setConnectionError(error, expectedEpoch) {
      if (!alive(expectedEpoch)) return;
      const nextError = normalizeError(error, "NETWORK");
      state = { ...state, connectionError: nextError };
      safeChange();
    }

    function setOperationalError(error, expectedEpoch) {
      if (!alive(expectedEpoch)) return;
      const nextError = normalizeError(error, "VIDEO");
      state = { ...state, status: "error", error: nextError };
      clearPoll();
      safeChange();
      queuePersist(snapshot(), expectedEpoch, false);
    }

    function finalizeDone(expectedEpoch) {
      if (!alive(expectedEpoch)) return Promise.resolve(snapshot());
      if (doneFinalized) return Promise.resolve(snapshot());
      if (doneInFlight) return doneInFlight;

      doneInFlight = (async () => {
        try {
          // 终态缓存必须成功后才能清理合图；之前排队的 meta 写入也按顺序完成。
          await queuePersist(snapshot(), expectedEpoch, true);
          if (!alive(expectedEpoch)) return snapshot();
          lastPersistError = null;
        } catch (error) {
          if (alive(expectedEpoch)) {
            lastPersistError = normalizeError(error, "PERSIST");
            state = { ...state, error: cloneValue(lastPersistError) };
            safeChange();
          }
          return snapshot();
        }

        if (!alive(expectedEpoch) || doneFinalized) return snapshot();
        try {
          const result = await onDone(snapshot());
          if (result === false) throw new Error("视频完成后的图片清理失败。");
          if (!alive(expectedEpoch)) return snapshot();
          doneFinalized = true;
          state = { ...state };
          delete state.connectionError;
          delete state.error;
          safeChange();
        } catch (error) {
          if (alive(expectedEpoch)) {
            state = { ...state, error: normalizeError(error, "CLEANUP") };
            safeChange();
          }
        }
        return snapshot();
      })().finally(() => {
        doneInFlight = null;
      });
      return doneInFlight;
    }

    async function performRefresh(expectedEpoch) {
      try {
        const result = await api(`/api/video/status?batchId=${encodeURIComponent(batchId)}`);
        if (!alive(expectedEpoch)) return snapshot();
        applyState(result, expectedEpoch, { clearConnection: true });
        if (state.status === "done") return finalizeDone(expectedEpoch);
        if (state.status === "waiting" || state.status === "running") schedulePoll();
        return snapshot();
      } catch (error) {
        if (!alive(expectedEpoch)) return snapshot();
        if (isRecoverableError(error)) {
          setConnectionError(error, expectedEpoch);
          schedulePoll();
        } else {
          setOperationalError(error, expectedEpoch);
        }
        return snapshot();
      }
    }

    function refresh() {
      if (disposed) return Promise.resolve(snapshot());
      if (refreshInFlight) return refreshInFlight;
      if (cancelInFlight) return cancelInFlight;
      const expectedEpoch = epoch;
      refreshInFlight = performRefresh(expectedEpoch).finally(() => {
        refreshInFlight = null;
        if (alive(expectedEpoch) && (state.status === "waiting" || state.status === "running")) schedulePoll();
      });
      return refreshInFlight;
    }

    async function performStart(expectedEpoch) {
      if (!alive(expectedEpoch)) return snapshot();
      if (state.status === "done") return finalizeDone(expectedEpoch);
      if (state.status === "running") {
        schedulePoll(0);
        return snapshot();
      }

      try {
        const result = await api("/api/video/start", { batchId });
        if (!alive(expectedEpoch)) return snapshot();
        const payload = payloadState(result);
        // start 合同通常会返回 waiting/running；只有确认没有状态时才按 accepted 视作 running。
        if (!asStatus(payload.status)) payload.status = "running";
        applyState(payload, expectedEpoch, { clearConnection: true });
        if (state.status === "done") return finalizeDone(expectedEpoch);
        if (state.status === "waiting" || state.status === "running") schedulePoll();
        return snapshot();
      } catch (error) {
        if (!alive(expectedEpoch)) return snapshot();
        if (isRecoverableError(error)) {
          // POST 的结果未知时只查状态，不重新 POST，防止重复计费/重复编码。
          setConnectionError(error, expectedEpoch);
          return performRefresh(expectedEpoch);
        }
        setOperationalError(error, expectedEpoch);
        return snapshot();
      }
    }

    function start() {
      if (disposed) return Promise.resolve(snapshot());
      if (startInFlight) return startInFlight;
      if (cancelInFlight) return cancelInFlight.then(() => start());
      const expectedEpoch = epoch;
      startInFlight = (refreshInFlight ? refreshInFlight.then(() => performStart(expectedEpoch)) : performStart(expectedEpoch))
        .finally(() => {
          startInFlight = null;
          if (alive(expectedEpoch) && (state.status === "waiting" || state.status === "running")) schedulePoll();
        });
      return startInFlight;
    }

    async function performCancel(expectedEpoch) {
      try {
        const result = await api("/api/video/cancel", { batchId });
        if (!alive(expectedEpoch)) return snapshot();
        const payload = payloadState(result);
        const responseStatus = asStatus(payload.status);
        // 取消和完成可能同时到达，服务端明确返回 done 时必须走完成清理，
        // 不能用本地 cancelled 覆盖已经生成好的视频。
        applyState(payload, expectedEpoch, { clearConnection: true, persist: Boolean(responseStatus) });
        if (state.status === "done") return finalizeDone(expectedEpoch);
        if (!responseStatus) {
          state = { ...state, status: "cancelled" };
          clearPoll();
          safeChange();
          queuePersist(snapshot(), expectedEpoch, false);
        } else if (state.status === "waiting" || state.status === "running") {
          schedulePoll();
        }
        return snapshot();
      } catch (error) {
        if (!alive(expectedEpoch)) return snapshot();
        if (isRecoverableError(error)) {
          setConnectionError(error, expectedEpoch);
          if (state.status === "waiting" || state.status === "running") schedulePoll();
        } else {
          state = { ...state, error: normalizeError(error, "CANCEL") };
          safeChange();
          if (state.status === "waiting" || state.status === "running") schedulePoll();
        }
        return snapshot();
      }
    }

    function cancel() {
      if (disposed || state.status === "done" || state.status === "cancelled") return Promise.resolve(snapshot());
      if (cancelInFlight) return cancelInFlight;
      const expectedEpoch = ++epoch;
      clearPoll();
      // 使尚未返回的 start/status 回调失效；取消请求本身立即发出，避免等待长轮询。
      cancelInFlight = performCancel(expectedEpoch).finally(() => {
        cancelInFlight = null;
      });
      return cancelInFlight;
    }

    function dispose() {
      if (disposed) return false;
      disposed = true;
      epoch += 1;
      clearPoll();
      return true;
    }

    function isBusy() {
      if (disposed) return false;
      return Boolean(startInFlight || refreshInFlight || cancelInFlight || doneInFlight || state.status === "running");
    }

    return {
      getState: snapshot,
      start,
      refresh,
      cancel,
      dispose,
      isBusy
    };
  }

  return Object.freeze({ createController, STATUSES });
});
