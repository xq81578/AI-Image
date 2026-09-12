/*
 * Time Echo 的无 DOM 批处理核心。
 *
 * 这个文件同时作为浏览器脚本和 CommonJS 模块使用：浏览器中暴露
 * window.TimeEchoCore，Node 中则可直接 require("./batch-core.js")。
 */
(function exposeTimeEchoCore(root, factory) {
  const api = factory();

  if (typeof module === "object" && module && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.TimeEchoCore = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function createCore() {
  "use strict";

  const STEPS = Object.freeze(["idea", "prompt", "old", "now", "compose", "save"]);
  const THEMES = Object.freeze([
    "农村老屋",
    "老家院子",
    "村庄变迁",
    "荒废院落",
    "亲情",
    "校园",
    "老街",
    "爱情",
    "宠物",
    "城市变迁",
    "老物件"
  ]);

  const DEFAULT_LIMITS = Object.freeze({ text: 6, image: 2, local: 2 });
  const DEFAULT_MAX_RETRIES = 2;

  function asFiniteNumber(value, fallback) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function normalizeRandom(random) {
    const value = Number(typeof random === "function" ? random() : Math.random());
    if (!Number.isFinite(value)) return 0;
    // Math.random() 的约定是 [0, 1)，但注入测试函数时偶尔会返回边界值。
    return Math.max(0, Math.min(0.9999999999999999, value));
  }

  function randomInt(random, min, max) {
    if (max <= min) return min;
    return min + Math.floor(normalizeRandom(random) * (max - min + 1));
  }

  function shuffle(values, random) {
    const result = values.slice();
    for (let index = result.length - 1; index > 0; index -= 1) {
      const swapIndex = randomInt(random, 0, index);
      const current = result[index];
      result[index] = result[swapIndex];
      result[swapIndex] = current;
    }
    return result;
  }

  function getNewYear(currentYear, yearMode, random) {
    if (yearMode === "current") return currentYear;
    if (yearMode === "previous") return currentYear - 1;
    return normalizeRandom(random) < 0.5 ? currentYear : currentYear - 1;
  }

  function createOldYear(newYear, random) {
    const oldest = 2000;
    const latest = newYear - 1;
    if (latest < oldest) {
      throw new RangeError("newYear 必须大于等于 2001，才能生成 2000 年起的旧照片年份。");
    }

    // 大约 80% 的结果至少相隔十年；剩余结果允许较近的旧照，避免年份完全固定。
    const longSpanLatest = newYear - 10;
    if (longSpanLatest >= oldest && normalizeRandom(random) < 0.8) {
      return randomInt(random, oldest, longSpanLatest);
    }
    return randomInt(random, Math.max(oldest, newYear - 9), latest);
  }

  function createJobs(options) {
    const input = options || {};
    const countNumber = Number(input.count);
    if (!Number.isFinite(countNumber) || countNumber < 0) {
      throw new TypeError("count 必须是非负数。");
    }
    const count = Math.floor(countNumber);
    const currentYear = Math.floor(asFiniteNumber(input.currentYear, new Date().getFullYear()));
    if (currentYear < 2001) {
      throw new RangeError("currentYear 必须大于等于 2001。");
    }

    const yearMode = input.yearMode || "random";
    if (!["random", "current", "previous"].includes(yearMode)) {
      throw new TypeError("yearMode 必须是 random、current 或 previous。");
    }
    const random = typeof input.random === "function" ? input.random : Math.random;
    const idPrefix = input.idPrefix == null || input.idPrefix === "" ? "job" : String(input.idPrefix);
    const themePool = [];
    const jobs = [];

    for (let index = 0; index < count; index += 1) {
      if (themePool.length === 0) themePool.push(...shuffle(THEMES, random));
      const theme = themePool.shift();
      const newYear = getNewYear(currentYear, yearMode, random);
      const oldYear = createOldYear(newYear, random);
      const span = newYear - oldYear;
      // 黑白照片只是较久远照片中的少量形式；近年的旧照始终保持彩色。
      const photoStyle = span >= 15 && normalizeRandom(random) < 0.12 ? "bw" : "color";
      const steps = STEPS.map(() => ({
        status: "waiting",
        attempts: 0,
        error: null,
        elapsedMs: 0,
        retryAt: 0,
        startedAt: 0
      }));

      jobs.push({
        id: `${idPrefix}-${index + 1}`,
        index,
        theme,
        oldYear,
        newYear,
        span,
        photoStyle,
        current: 0,
        steps,
        paused: false,
        abandoned: false,
        results: {}
      });
    }
    return jobs;
  }

  function stageIndex(stage) {
    if (typeof stage === "number" && Number.isInteger(stage)) {
      return stage >= 0 && stage < STEPS.length ? stage : -1;
    }
    if (typeof stage !== "string") return -1;
    return STEPS.indexOf(stage);
  }

  function stageName(stage) {
    const index = stageIndex(stage);
    return index < 0 ? null : STEPS[index];
  }

    function normalizeLimits(limits) {
    const input = limits || {};
    const result = {};
    for (const key of ["text", "image", "local"]) {
      const value = Math.floor(asFiniteNumber(input[key], DEFAULT_LIMITS[key]));
      result[key] = Math.max(1, value);
    }
    return result;
  }

  function normalizeError(error) {
    if (error instanceof Error) return error;
    if (error && typeof error === "object") return error;
    return new Error(error == null ? "未知错误" : String(error));
  }

  function readErrorStatus(error) {
    const candidates = [
      error && error.status,
      error && error.statusCode,
      error && error.response && error.response.status,
      error && error.cause && error.cause.status
    ];
    for (const candidate of candidates) {
      const number = Number(candidate);
      if (Number.isFinite(number)) return number;
    }
    return null;
  }

  function isValidationError(error) {
    const code = String(error && (error.code || error.name || "")).toLowerCase();
    const message = String(error && error.message || error || "").toLowerCase();
    return Boolean(
      error && (error.validation === true || error.isValidationError === true) ||
      /validation|invalid|校验|验证/.test(code) ||
      /validation|invalid|校验|验证/.test(message)
    );
  }

  function isTransientModelError(error) {
    const status = readErrorStatus(error);
    if (status === 401 || status === 403 || isValidationError(error)) return false;
    if (status === 429 || (status >= 500 && status <= 599)) return true;
    const code = String(error && (error.code || error.name || "")).toLowerCase();
    const message = String(error && error.message || error || "").toLowerCase();
    return Boolean(
      code === "networktimeout" ||
      code === "network_timeout" ||
      code === "timeout" ||
      code === "etimedout" ||
      code === "econnreset" ||
      code === "aborterror" ||
      /network\s*(request\s*)?timeout|timed?\s*out|连接.*超时/.test(message)
    );
  }

  function resourceForStage(stage) {
    if (stage <= 1) return "text";
    if (stage <= 3) return "image";
    return "local";
  }

  function createRunner(options) {
    const input = options || {};
    if (!Array.isArray(input.jobs)) throw new TypeError("createRunner 需要 jobs 数组。");
    if (typeof input.execute !== "function") throw new TypeError("createRunner 需要 execute(job, stage) 函数。");

    const jobs = input.jobs;
    const execute = input.execute;
    const onChange = typeof input.onChange === "function" ? input.onChange : function noop() {};
    const checkpoint = typeof input.checkpoint === "function" ? input.checkpoint : async function noopCheckpoint() {};
    const limits = normalizeLimits(input.limits);
    const maxRetries = Math.max(0, Math.floor(asFiniteNumber(input.maxRetries, DEFAULT_MAX_RETRIES)));
    const retryDelayOption = input.retryDelayMs == null ? 0 : input.retryDelayMs;

    const pools = {
      text: { limit: limits.text, active: 0, queue: [] },
      image: { limit: limits.image, active: 0, queue: [] },
      local: { limit: limits.local, active: 0, queue: [] }
    };
    const states = new Map();
    const jobsById = new Map();
    let started = false;
    let disposed = false;
    let globallyPaused = false;
    let scheduling = false;
    let scheduleAgain = false;

    function ensureJob(job, index) {
      if (!job || typeof job !== "object") throw new TypeError(`jobs[${index}] 不是任务对象。`);
      if (typeof job.id !== "string") job.id = String(job.id == null ? `job-${index + 1}` : job.id);
      if (!Array.isArray(job.steps) || job.steps.length !== STEPS.length) {
        throw new TypeError(`任务 ${job.id} 的 steps 必须包含 ${STEPS.length} 个阶段。`);
      }
      if (!job.results || typeof job.results !== "object") job.results = {};
      job.paused = Boolean(job.paused);
      job.abandoned = Boolean(job.abandoned);
      if (!Number.isInteger(job.current) || job.current < 0) job.current = 0;
      if (job.current > STEPS.length) job.current = STEPS.length;
      job.steps.forEach(step => {
        if (!step || typeof step !== "object") throw new TypeError(`任务 ${job.id} 含有无效阶段。`);
        if (!step.status) step.status = "waiting";
        if (!Number.isFinite(Number(step.attempts))) step.attempts = 0;
        if (!Number.isFinite(Number(step.elapsedMs))) step.elapsedMs = 0;
        if (!Number.isFinite(Number(step.retryAt))) step.retryAt = 0;
        if (!Number.isFinite(Number(step.startedAt))) step.startedAt = 0;
        if (!Object.prototype.hasOwnProperty.call(step, "error")) step.error = null;
      });
      if (jobsById.has(job.id)) throw new TypeError(`任务 id 重复：${job.id}`);
      const state = {
        job,
        inflight: null,
        queuedStage: null,
        queueToken: 0,
        retryStage: null,
        retryTimer: null,
        retryGeneration: 0,
        retryRemainingMs: 0,
        persistenceErrorStage: null,
        persistenceRetryQueued: false
      };
      states.set(job, state);
      jobsById.set(job.id, job);
      return state;
    }

    jobs.forEach(ensureJob);

    function isSaveDone(job) {
      return job.steps[STEPS.length - 1].status === "done";
    }

    function hasError(job) {
      return job.steps.some(step => step.status === "error");
    }

    function isTerminal(job) {
      return job.abandoned || isSaveDone(job);
    }

    function safeEmit() {
      try {
        const result = onChange(jobs, getSummary());
        if (result && typeof result.then === "function") result.catch(function ignoreOnChangeError() {});
      } catch (error) {
        // UI 回调属于观察者，不能让它的异常中断队列调度。
      }
    }

    function getSummary() {
      let saved = 0;
      let failed = 0;
      let paused = 0;
      let abandoned = 0;
      let active = 0;
      let pending = 0;

      for (const job of jobs) {
        const state = states.get(job);
        const terminal = isTerminal(job);
        if (job.abandoned) abandoned += 1;
        else if (isSaveDone(job)) saved += 1;
        if (!terminal && hasError(job)) failed += 1;
        if (!terminal && job.paused) paused += 1;
        if (!terminal && state && state.inflight) active += 1;

        if (!terminal && !job.paused && !hasError(job) && !(state && state.inflight)) {
          pending += 1;
        }
      }
      return { saved, failed, paused, abandoned, active, pending, total: jobs.length, pausedAll: globallyPaused };
    }

    function isBusy() {
      return jobs.some(job => !isTerminal(job));
    }

    function findJob(idOrJob) {
      if (idOrJob && typeof idOrJob === "object" && states.has(idOrJob)) return idOrJob;
      return jobsById.get(String(idOrJob));
    }

    function cancelRetryTimer(state, preserveRemaining) {
      if (state.retryTimer !== null) {
        clearTimeout(state.retryTimer);
        state.retryTimer = null;
      }
      if (preserveRemaining && state.retryStage !== null) {
        const step = state.job.steps[state.retryStage];
        state.retryRemainingMs = Math.max(0, Number(step.retryAt || 0) - Date.now());
      }
      state.retryGeneration += 1;
    }

    function retryDelay(attempt, job, stage, error) {
      let value;
      if (typeof retryDelayOption === "function") {
        try {
          // 函数参数以 attempt 开头，额外提供任务、阶段和错误，便于按阶段调整退避。
          value = retryDelayOption(attempt, job, stage, error);
        } catch (ignored) {
          value = 0;
        }
      } else {
        value = retryDelayOption;
      }
      const delay = Number(value);
      return Number.isFinite(delay) ? Math.max(0, delay) : 0;
    }

    function queueStage(state, stage) {
      const job = state.job;
      const step = job.steps[stage];
      if (state.inflight || state.queuedStage !== null || step.status === "done") return false;
      if (stage > 0 && job.steps[stage - 1].status !== "done") return false;

      step.status = "queued";
      step.retryAt = 0;
      state.queueToken += 1;
      state.queuedStage = stage;
      const pool = pools[resourceForStage(stage)];
      pool.queue.push({ state, stage, token: state.queueToken });
      job.current = stage;
      return true;
    }

    function scheduleRetry(state, stage, error, remainingOverride) {
      const step = state.job.steps[stage];
      cancelRetryTimer(state, false);
      const delay = remainingOverride == null
        ? retryDelay(step.attempts, state.job, STEPS[stage], error)
        : Math.max(0, Number(remainingOverride) || 0);
      const due = Date.now() + delay;
      step.status = "retry";
      step.retryAt = due;
      state.retryStage = stage;
      state.retryRemainingMs = delay;
      const generation = state.retryGeneration;

      if (disposed || globallyPaused || state.job.paused || state.job.abandoned) return;
      state.retryTimer = setTimeout(() => {
        state.retryTimer = null;
        if (generation !== state.retryGeneration) return;
        state.retryRemainingMs = 0;
        if (disposed || globallyPaused || state.job.paused || state.job.abandoned) return;
        if (state.retryStage !== stage || state.job.steps[stage].status !== "retry") return;
        state.retryStage = null;
        state.job.steps[stage].retryAt = 0;
        queueStage(state, stage);
        requestSchedule();
        safeEmit();
      }, delay);
    }

    function canDispatch(entry) {
      const state = entry.state;
      const job = state.job;
      const step = job.steps[entry.stage];
      if (entry.token !== state.queueToken || state.queuedStage !== entry.stage) return false;
      if (disposed || globallyPaused || job.paused) return false;
      state.queuedStage = null;
      if (job.abandoned) return false;
      if (state.inflight || step.status !== "queued") return false;
      if (entry.stage > 0 && job.steps[entry.stage - 1].status !== "done") return false;
      return true;
    }

    function requestSchedule() {
      if (disposed || !started || globallyPaused) return;
      if (scheduling) {
        scheduleAgain = true;
        return;
      }
      scheduling = true;
      do {
        scheduleAgain = false;
        for (const resource of ["text", "image", "local"]) {
          const pool = pools[resource];
          let availableEntries = pool.queue.length;
          while (!disposed && !globallyPaused && pool.active < pool.limit && pool.queue.length > 0 && availableEntries > 0) {
            const entry = pool.queue.shift();
            // 暂停组的 entry 不能被消费掉；轮转后仍让其它组使用空闲名额。
            if (entry.token === entry.state.queueToken && entry.state.queuedStage === entry.stage && entry.state.job.paused) {
              pool.queue.push(entry);
              availableEntries -= 1;
              continue;
            }
            if (!canDispatch(entry)) continue;
            dispatch(entry, pool);
            availableEntries = pool.queue.length;
          }
        }
      } while (scheduleAgain);
      scheduling = false;
    }

    function dispatch(entry, pool) {
      const state = entry.state;
      const job = state.job;
      const step = job.steps[entry.stage];
      const startedAt = Date.now();
      state.inflight = { stage: entry.stage, kind: "execute", pool };
      pool.active += 1;
      step.status = "running";
      step.attempts += 1;
      step.error = null;
      step.retryAt = 0;
      step.startedAt = startedAt;
      job.current = entry.stage;
      safeEmit();

      Promise.resolve()
        .then(() => execute(job, STEPS[entry.stage]))
        .then(value => finishExecuteSuccess(state, entry.stage, startedAt, value), error => finishExecuteError(state, entry.stage, startedAt, error))
        .catch(error => {
          // finish* 自身的异常也不能卡住资源计数。
          finishExecuteError(state, entry.stage, startedAt, error);
        });
    }

    function releaseExecute(state, stage, startedAt) {
      const inflight = state.inflight;
      if (inflight && inflight.stage === stage && inflight.kind === "execute") {
        inflight.pool.active = Math.max(0, inflight.pool.active - 1);
      }
      const elapsed = Math.max(0, Date.now() - startedAt);
      state.job.steps[stage].elapsedMs += elapsed;
      state.job.steps[stage].startedAt = 0;
    }

    function finishExecuteSuccess(state, stage, startedAt, value) {
      const job = state.job;
      const step = job.steps[stage];
      if (!state.inflight || state.inflight.stage !== stage) return;
      releaseExecute(state, stage, startedAt);

      // 结果必须先落到内存，再调用 checkpoint；checkpoint 失败时据此只重试持久化。
      job.results[STEPS[stage]] = value;
      state.inflight.kind = "checkpoint";
      safeEmit();
      requestSchedule();

      invokeCheckpoint(job)
        .then(() => finishCheckpointSuccess(state, stage), error => finishCheckpointError(state, stage, error))
        .catch(error => finishCheckpointError(state, stage, error));
    }

    function finishExecuteError(state, stage, startedAt, error) {
      const job = state.job;
      const step = job.steps[stage];
      if (!state.inflight || state.inflight.stage !== stage) return;
      releaseExecute(state, stage, startedAt);
      state.inflight = null;
      step.error = normalizeError(error);
      step.retryAt = 0;
      step.startedAt = 0;

      if (stage < 4 && isTransientModelError(step.error) && step.attempts <= maxRetries) {
        scheduleRetry(state, stage, step.error);
      } else {
        step.status = "error";
      }
      safeEmit();
      requestSchedule();
    }

    function finishCheckpointSuccess(state, stage) {
      if (!state.inflight || state.inflight.stage !== stage || state.inflight.kind !== "checkpoint") return;
      const job = state.job;
      const step = job.steps[stage];
      state.inflight = null;
      state.persistenceErrorStage = null;
      state.persistenceRetryQueued = false;
      step.status = "done";
      step.error = null;
      step.retryAt = 0;
      step.startedAt = 0;
      job.current = stage + 1;

      if (!disposed && !job.paused && !job.abandoned && !globallyPaused && stage + 1 < STEPS.length) {
        queueStage(state, stage + 1);
      }
      safeEmit();
      requestSchedule();
    }

    function finishCheckpointError(state, stage, error) {
      if (!state.inflight || state.inflight.stage !== stage || state.inflight.kind !== "checkpoint") return;
      const step = state.job.steps[stage];
      state.inflight = null;
      state.persistenceErrorStage = stage;
      step.status = "error";
      step.error = normalizeError(error);
      step.retryAt = 0;
      step.startedAt = 0;
      safeEmit();
      requestSchedule();
    }

    function startCheckpointRetry(state, stage) {
      const job = state.job;
      const step = job.steps[stage];
      if (state.inflight || disposed || !started || globallyPaused || job.paused || job.abandoned) return false;
      state.persistenceErrorStage = stage;
      state.inflight = { stage, kind: "checkpoint", pool: null };
      step.status = "running";
      step.error = null;
      step.retryAt = 0;
      step.startedAt = 0;
      safeEmit();
      invokeCheckpoint(job)
        .then(() => finishCheckpointSuccess(state, stage), error => finishCheckpointError(state, stage, error))
        .catch(error => finishCheckpointError(state, stage, error));
      return true;
    }

    function invokeCheckpoint(job) {
      return Promise.resolve()
        .then(() => checkpoint(job))
        .then(result => {
          if (result === false) throw new Error("checkpoint 返回失败。");
          return result;
        });
    }

    function prepareStart() {
      for (const state of states.values()) {
        const job = state.job;
        if (job.abandoned || isSaveDone(job)) {
          job.current = STEPS.length;
          continue;
        }
        // 持久化恢复不应再次悄悄调用模型；未完成的 running 交给用户手动重试。
        job.steps.forEach((step, stage) => {
          if (step.status === "running") {
            step.status = "error";
            step.error = new Error("任务恢复时该阶段仍在运行，请手动重试。");
            step.retryAt = 0;
            step.startedAt = 0;
            if (job.current === stage) state.persistenceErrorStage = null;
          }
        });
        const current = job.steps.findIndex(step => step.status !== "done");
        if (current < 0) {
          job.current = STEPS.length;
          continue;
        }
        job.current = current;
        const step = job.steps[current];
        // 重新载入的任务没有内存中的 persistenceErrorStage；已有结果而阶段为
        // error 时视作 checkpoint 失败，手动 retry 只重试持久化，避免再次调用模型。
        if (step.status === "error" && Object.prototype.hasOwnProperty.call(job.results, STEPS[current])) {
          state.persistenceErrorStage = current;
        }
        if (state.persistenceRetryQueued && state.persistenceErrorStage === current &&
            !job.paused && !globallyPaused && !disposed) {
          startCheckpointRetry(state, current);
          continue;
        }
        if (step.status === "waiting" || step.status === "queued") {
          step.status = "waiting";
          queueStage(state, current);
        } else if (step.status === "retry") {
          const remaining = Math.max(0, Number(step.retryAt || 0) - Date.now());
          state.retryStage = current;
          state.retryRemainingMs = remaining;
          if (!globallyPaused && !job.paused) scheduleRetry(state, current, step.error, remaining);
        }
      }
    }

    function start() {
      if (disposed || started) return false;
      started = true;
      prepareStart();
      requestSchedule();
      safeEmit();
      return true;
    }

    function resetFrom(idOrJob, requestedStage) {
      const job = findJob(idOrJob);
      const stage = stageIndex(requestedStage);
      if (disposed || !job || stage < 0 || job.abandoned || isSaveDone(job)) return false;
      const state = states.get(job);
      if (state.inflight) return false;

      const canReset = job.paused || hasError(job) || job.steps.some(step => step.status === "retry");
      if (!canReset) return false;
      // 每个任务最多有一个有效排队项；如果它在目标阶段之前，说明目标尚未成为断点。
      if (state.queuedStage !== null && state.queuedStage < stage) return false;
      if (state.retryStage !== null && state.retryStage < stage) return false;

      cancelRetryTimer(state, false);
      state.retryStage = null;
      state.retryRemainingMs = 0;
      state.persistenceErrorStage = null;
      state.persistenceRetryQueued = false;
      state.queueToken += 1;
      state.queuedStage = null;
      for (const pool of Object.values(pools)) {
        pool.queue = pool.queue.filter(entry => entry.state !== state);
      }

      for (let index = stage; index < STEPS.length; index += 1) {
        const step = job.steps[index];
        delete job.results[STEPS[index]];
        if (job.pendingImages && typeof job.pendingImages === "object") delete job.pendingImages[STEPS[index]];
        step.status = "waiting";
        step.attempts = 0;
        step.error = null;
        step.elapsedMs = 0;
        step.retryAt = 0;
        step.startedAt = 0;
      }
      delete job.persistenceError;
      job.current = stage;

      // 暂停状态由调用方保留；未暂停的失败组可直接从新的断点继续。
      if (!job.paused && started && !globallyPaused) queueStage(state, stage);
      safeEmit();
      requestSchedule();
      return true;
    }

    function pause(idOrJob) {
      const job = findJob(idOrJob);
      if (disposed || !job || job.abandoned || isSaveDone(job) || job.paused) return false;
      const state = states.get(job);
      job.paused = true;
      cancelRetryTimer(state, true);
      safeEmit();
      return true;
    }

    function resume(idOrJob) {
      const job = findJob(idOrJob);
      if (disposed || !job || job.abandoned || isSaveDone(job) || !job.paused) return false;
      const state = states.get(job);
      job.paused = false;
      const current = job.steps.findIndex(step => step.status !== "done");
      if (current >= 0 && !state.inflight && state.queuedStage === null && !globallyPaused && !disposed) {
        const step = job.steps[current];
        if (step.status === "waiting") queueStage(state, current);
        else if (step.status === "retry") scheduleRetry(state, current, step.error, state.retryRemainingMs);
        else if (step.status === "error" && state.persistenceErrorStage === current) startCheckpointRetry(state, current);
      }
      requestSchedule();
      safeEmit();
      return true;
    }

    function retry(idOrJob, requestedStage) {
      const job = findJob(idOrJob);
      if (disposed || !job || job.abandoned || isSaveDone(job)) return false;
      const state = states.get(job);
      let stage = requestedStage == null ? -1 : stageIndex(requestedStage);
      if (stage < 0) {
        stage = job.steps.findIndex(step => step.status === "error" || step.status === "retry");
      }
      if (stage < 0 || stage >= STEPS.length) return false;
      const step = job.steps[stage];
      if (step.status === "running" || step.status === "queued" || step.status === "waiting") return false;
      if (state.inflight) return false;
      if (state.retryStage === stage) cancelRetryTimer(state, false);

      if ((state.persistenceErrorStage === stage || Object.prototype.hasOwnProperty.call(job.results, STEPS[stage])) &&
          Object.prototype.hasOwnProperty.call(job.results, STEPS[stage])) {
        step.status = "error";
        state.persistenceRetryQueued = true;
        if (started && !globallyPaused && !job.paused && !disposed) startCheckpointRetry(state, stage);
        safeEmit();
        return true;
      }

      step.status = "waiting";
      step.error = null;
      step.retryAt = 0;
      state.retryStage = null;
      state.retryRemainingMs = 0;
      if (!globallyPaused && !job.paused && !disposed) queueStage(state, stage);
      requestSchedule();
      safeEmit();
      return true;
    }

    function abandon(idOrJob) {
      const job = findJob(idOrJob);
      if (disposed || !job || job.abandoned || isSaveDone(job)) return false;
      const state = states.get(job);
      job.abandoned = true;
      job.paused = false;
      cancelRetryTimer(state, false);
      safeEmit();
      // 队列中的旧 entry 会在调度时被跳过；在途 execute/checkpoint 仍会回收结果。
      return true;
    }

    function pauseAll() {
      if (disposed || globallyPaused) return false;
      globallyPaused = true;
      for (const state of states.values()) cancelRetryTimer(state, true);
      safeEmit();
      return true;
    }

    function resumeAll() {
      if (disposed || !globallyPaused) return false;
      globallyPaused = false;
      for (const state of states.values()) {
        const job = state.job;
        if (job.abandoned || job.paused || state.inflight || state.queuedStage !== null) continue;
        const current = job.steps.findIndex(step => step.status !== "done");
        if (current < 0) continue;
        const step = job.steps[current];
        if (step.status === "retry") {
          scheduleRetry(state, current, step.error, state.retryRemainingMs);
        } else if (step.status === "waiting") {
          queueStage(state, current);
        } else if (step.status === "error" && state.persistenceErrorStage === current) {
          startCheckpointRetry(state, current);
        }
      }
      requestSchedule();
      safeEmit();
      return true;
    }

    function dispose() {
      if (disposed) return false;
      disposed = true;
      for (const state of states.values()) cancelRetryTimer(state, false);
      for (const pool of Object.values(pools)) pool.queue.length = 0;
      safeEmit();
      return true;
    }

    return {
      start,
      pause,
      resume,
      retry,
      resetFrom,
      abandon,
      pauseAll,
      resumeAll,
      dispose,
      getSummary,
      isBusy,
      jobs
    };
  }

  return Object.freeze({ STEPS, THEMES, createJobs, createRunner });
});
