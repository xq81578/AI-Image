const test = require("node:test");
const assert = require("node:assert/strict");

const TimeEchoCore = require("../batch-core.js");

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

async function waitFor(predicate, timeout = 1500) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    if (predicate()) return;
    await wait(2);
  }
  assert.fail("等待批处理状态超时");
}

function makeResult(job, stage) {
  return stage === "save"
    ? { path: `output/${job.id}.png`, filename: `${job.id}.png` }
    : `${job.id}:${stage}`;
}

test("createJobs 生成固定形状、无重复主题和合法年份", () => {
  const jobs = TimeEchoCore.createJobs({
    count: 11,
    currentYear: 2026,
    yearMode: "current",
    random: () => 0.01,
    idPrefix: "echo"
  });

  assert.equal(jobs.length, 11);
  assert.equal(new Set(jobs.map(job => job.theme)).size, 11);
  assert.deepEqual(jobs.map(job => job.id), Array.from({ length: 11 }, (_, i) => `echo-${i + 1}`));
  for (const job of jobs) {
    assert.equal(job.newYear, 2026);
    assert.ok(job.oldYear >= 2000 && job.oldYear < job.newYear);
    assert.equal(job.span, job.newYear - job.oldYear);
    assert.ok(["color", "bw"].includes(job.photoStyle));
    assert.equal(job.steps.length, TimeEchoCore.STEPS.length);
    assert.deepEqual(job.steps[0], {
      status: "waiting",
      attempts: 0,
      error: null,
      elapsedMs: 0,
      retryAt: 0,
      startedAt: 0
    });
    assert.deepEqual(job.results, {});
  }

  const previous = TimeEchoCore.createJobs({ count: 1, currentYear: 2026, yearMode: "previous", random: () => 0.99 })[0];
  assert.equal(previous.newYear, 2025);
  const boundary = TimeEchoCore.createJobs({ count: 1, currentYear: 2001, yearMode: "current", random: () => 0.99 })[0];
  assert.equal(boundary.newYear, 2001);
  assert.equal(boundary.oldYear, 2000);
  assert.equal(boundary.span, 1);
});

test("每个阶段完成后立即进入下一个阶段", async () => {
  const jobs = TimeEchoCore.createJobs({ count: 1, currentYear: 2026, random: () => 0.5 });
  const calls = [];
  const runner = TimeEchoCore.createRunner({
    jobs,
    limits: { text: 6, image: 2, local: 2 },
    execute: async (job, stage) => {
      calls.push(stage);
      return makeResult(job, stage);
    }
  });

  assert.equal(runner.start(), true);
  await waitFor(() => runner.getSummary().saved === 1);
  assert.deepEqual(calls, TimeEchoCore.STEPS);
  assert.equal(jobs[0].current, 6);
  assert.equal(runner.isBusy(), false);
  assert.deepEqual(runner.getSummary(), {
    saved: 1,
    failed: 0,
    paused: 0,
    abandoned: 0,
    active: 0,
    pending: 0,
    total: 1,
    pausedAll: false
  });
});

test("文本、图片、本地阶段分别遵守并发上限", async () => {
  const jobs = TimeEchoCore.createJobs({ count: 5, currentYear: 2026, random: () => 0.4 });
  const active = { text: 0, image: 0, local: 0 };
  const maximum = { text: 0, image: 0, local: 0 };
  const resourceFor = stage => TimeEchoCore.STEPS.indexOf(stage) <= 1
    ? "text"
    : TimeEchoCore.STEPS.indexOf(stage) <= 3 ? "image" : "local";

  const runner = TimeEchoCore.createRunner({
    jobs,
    limits: { text: 2, image: 1, local: 1 },
    execute: (job, stage) => new Promise(resolve => {
      const resource = resourceFor(stage);
      active[resource] += 1;
      maximum[resource] = Math.max(maximum[resource], active[resource]);
      setTimeout(() => {
        active[resource] -= 1;
        resolve(makeResult(job, stage));
      }, 3);
    })
  });
  runner.start();
  await waitFor(() => runner.getSummary().saved === jobs.length, 4000);
  assert.ok(maximum.text <= 2);
  assert.ok(maximum.image <= 1);
  assert.ok(maximum.local <= 1);
});

test("单组暂停只阻止下游，其他组继续运行", async () => {
  const jobs = TimeEchoCore.createJobs({ count: 2, currentYear: 2026, random: () => 0.4 });
  let releaseFirstIdea;
  const calls = [];
  const runner = TimeEchoCore.createRunner({
    jobs,
    limits: { text: 2, image: 2, local: 2 },
    execute: (job, stage) => {
      calls.push(`${job.id}:${stage}`);
      if (job === jobs[0] && stage === "idea") {
        return new Promise(resolve => { releaseFirstIdea = () => resolve(makeResult(job, stage)); });
      }
      return Promise.resolve(makeResult(job, stage));
    }
  });
  runner.start();
  await waitFor(() => typeof releaseFirstIdea === "function");
  assert.equal(runner.pause(jobs[0].id), true);
  releaseFirstIdea();
  await waitFor(() => runner.getSummary().saved === 1);
  assert.equal(jobs[0].steps[1].status, "waiting");
  assert.equal(runner.getSummary().paused, 1);
  assert.equal(runner.resume(jobs[0].id), true);
  await waitFor(() => runner.getSummary().saved === 2);
  assert.equal(calls.filter(call => call === `${jobs[0].id}:idea`).length, 1);
});

test("整批暂停不会解除单组暂停", async () => {
  const jobs = TimeEchoCore.createJobs({ count: 2, currentYear: 2026, random: () => 0.4 });
  let releaseFirst;
  const runner = TimeEchoCore.createRunner({
    jobs,
    limits: { text: 2, image: 2, local: 2 },
    execute: (job, stage) => {
      if (job === jobs[0] && stage === "idea") {
        return new Promise(resolve => { releaseFirst = () => resolve(makeResult(job, stage)); });
      }
      return Promise.resolve(makeResult(job, stage));
    }
  });
  runner.start();
  await waitFor(() => typeof releaseFirst === "function");
  runner.pause(jobs[0].id);
  runner.pauseAll();
  releaseFirst();
  await wait(20);
  runner.resumeAll();
  assert.equal(jobs[0].paused, true);
  assert.equal(jobs[0].steps[1].status, "waiting");
  assert.equal(runner.getSummary().saved, 1);
  runner.resume(jobs[0].id);
  await waitFor(() => runner.getSummary().saved === 2);
});

test("失败重试只调用失败阶段并保留前序结果", async () => {
  const jobs = TimeEchoCore.createJobs({ count: 1, currentYear: 2026, random: () => 0.4 });
  const calls = [];
  let promptAttempts = 0;
  const runner = TimeEchoCore.createRunner({
    jobs,
    maxRetries: 0,
    execute: async (job, stage) => {
      calls.push(stage);
      if (stage === "prompt" && ++promptAttempts === 1) {
        const error = new Error("校验失败");
        error.code = "VALIDATION_ERROR";
        throw error;
      }
      return makeResult(job, stage);
    }
  });
  runner.start();
  await waitFor(() => jobs[0].steps[1].status === "error");
  assert.equal(jobs[0].results.idea, `${jobs[0].id}:idea`);
  assert.equal(runner.retry(jobs[0].id), true);
  await waitFor(() => runner.getSummary().saved === 1);
  assert.deepEqual(calls, ["idea", "prompt", "prompt", "old", "now", "compose", "save"]);
  assert.equal(jobs[0].steps[0].attempts, 1);
});

test("保存失败只重试保存阶段", async () => {
  const jobs = TimeEchoCore.createJobs({ count: 1, currentYear: 2026, random: () => 0.4 });
  const calls = [];
  let saveAttempts = 0;
  const runner = TimeEchoCore.createRunner({
    jobs,
    maxRetries: 0,
    execute: async (job, stage) => {
      calls.push(stage);
      if (stage === "save" && ++saveAttempts === 1) throw new Error("保存失败");
      return makeResult(job, stage);
    }
  });
  runner.start();
  await waitFor(() => jobs[0].steps[5].status === "error");
  assert.equal(runner.retry(jobs[0].id), true);
  await waitFor(() => runner.getSummary().saved === 1);
  assert.deepEqual(calls, ["idea", "prompt", "old", "now", "compose", "save", "save"]);
});

test("checkpoint 失败保留结果，重试只执行持久化", async () => {
  const jobs = TimeEchoCore.createJobs({ count: 1, currentYear: 2026, random: () => 0.4 });
  let checkpointAttempts = 0;
  const calls = [];
  const firstRunner = TimeEchoCore.createRunner({
    jobs,
    maxRetries: 0,
    execute: async (job, stage) => {
      calls.push(stage);
      return makeResult(job, stage);
    },
    checkpoint: async () => {
      if (++checkpointAttempts === 1) throw new Error("缓存暂时不可用");
    }
  });
  firstRunner.start();
  await waitFor(() => jobs[0].steps[0].status === "error");
  assert.equal(jobs[0].results.idea, `${jobs[0].id}:idea`);
  firstRunner.dispose();

  const resumed = TimeEchoCore.createRunner({
    jobs,
    execute: async (job, stage) => {
      calls.push(`resumed:${stage}`);
      return makeResult(job, stage);
    },
    checkpoint: async () => { checkpointAttempts += 1; }
  });
  resumed.start();
  assert.equal(resumed.retry(jobs[0].id), true);
  await waitFor(() => resumed.getSummary().saved === 1);
  assert.equal(calls.includes("resumed:idea"), false);
  assert.equal(calls.filter(stage => stage === "idea").length, 1);
});

test("自动重试等待期间暂停，恢复后才重新请求", async () => {
  const jobs = TimeEchoCore.createJobs({ count: 1, currentYear: 2026, random: () => 0.4 });
  let attempts = 0;
  const runner = TimeEchoCore.createRunner({
    jobs,
    maxRetries: 2,
    retryDelayMs: 30,
    execute: async (job, stage) => {
      if (stage === "idea" && ++attempts === 1) {
        const error = new Error("busy");
        error.status = 429;
        throw error;
      }
      return makeResult(job, stage);
    }
  });
  runner.start();
  await waitFor(() => jobs[0].steps[0].status === "retry");
  runner.pause(jobs[0].id);
  await wait(50);
  assert.equal(attempts, 1);
  runner.resume(jobs[0].id);
  await waitFor(() => runner.getSummary().saved === 1);
  assert.equal(attempts, 2);
});

test("放弃任务后在途请求可收结果，但不会派发后续阶段", async () => {
  const jobs = TimeEchoCore.createJobs({ count: 1, currentYear: 2026, random: () => 0.4 });
  let releaseIdea;
  const calls = [];
  const runner = TimeEchoCore.createRunner({
    jobs,
    execute: (job, stage) => {
      calls.push(stage);
      if (stage === "idea") return new Promise(resolve => { releaseIdea = () => resolve("idea-result"); });
      return Promise.resolve(makeResult(job, stage));
    }
  });
  runner.start();
  await waitFor(() => typeof releaseIdea === "function");
  assert.equal(runner.abandon(jobs[0].id), true);
  releaseIdea();
  await wait(30);
  assert.equal(jobs[0].results.idea, "idea-result");
  assert.deepEqual(calls, ["idea"]);
  assert.equal(runner.isBusy(), false);
  assert.equal(runner.abandon(jobs[0].id), false);
});

test("暂停组的图片排队项不会被消费，恢复后仍能继续", async () => {
  const jobs = TimeEchoCore.createJobs({ count: 3, currentYear: 2026, random: () => 0.4 });
  let releaseFirstOld;
  const runner = TimeEchoCore.createRunner({
    jobs,
    limits: { text: 6, image: 1, local: 2 },
    execute: (job, stage) => {
      if (job === jobs[0] && stage === "old") {
        return new Promise(resolve => { releaseFirstOld = () => resolve(makeResult(job, stage)); });
      }
      return Promise.resolve(makeResult(job, stage));
    }
  });

  runner.start();
  await waitFor(() => typeof releaseFirstOld === "function" && jobs[1].steps[2].status === "queued");
  runner.pause(jobs[1].id);
  releaseFirstOld();
  await waitFor(() => jobs[2].steps[5].status === "done" && jobs[0].steps[5].status === "done");
  assert.equal(jobs[1].steps[2].status, "queued");
  assert.equal(runner.getSummary().pausedAll, false);
  runner.resume(jobs[1].id);
  await waitFor(() => runner.getSummary().saved === 3);
});

test("全局暂停保留图片排队项，resumeAll 后继续且不改组暂停", async () => {
  const jobs = TimeEchoCore.createJobs({ count: 2, currentYear: 2026, random: () => 0.4 });
  let releaseFirstOld;
  const runner = TimeEchoCore.createRunner({
    jobs,
    limits: { text: 6, image: 1, local: 2 },
    execute: (job, stage) => {
      if (job === jobs[0] && stage === "old") {
        return new Promise(resolve => { releaseFirstOld = () => resolve(makeResult(job, stage)); });
      }
      return Promise.resolve(makeResult(job, stage));
    }
  });
  runner.start();
  await waitFor(() => typeof releaseFirstOld === "function" && jobs[1].steps[2].status === "queued");
  runner.pause(jobs[0].id);
  assert.equal(runner.pauseAll(), true);
  assert.equal(runner.getSummary().pausedAll, true);
  releaseFirstOld();
  await wait(20);
  assert.equal(jobs[1].steps[2].status, "queued");
  assert.equal(jobs[0].paused, true);
  runner.resumeAll();
  assert.equal(runner.getSummary().pausedAll, false);
  assert.equal(jobs[0].paused, true);
  await waitFor(() => runner.getSummary().saved === 1);
  runner.resume(jobs[0].id);
  await waitFor(() => runner.getSummary().saved === 2);
});

test("onChange 重入暂停也不会丢掉尚未派发的任务", async () => {
  const jobs = TimeEchoCore.createJobs({ count: 2, currentYear: 2026, random: () => 0.4 });
  let runner;
  let pausedOnce = false;
  runner = TimeEchoCore.createRunner({
    jobs,
    execute: async (job, stage) => makeResult(job, stage),
    onChange: () => {
      if (!pausedOnce && jobs[0].steps[0].status === "running") {
        pausedOnce = true;
        runner.pauseAll();
      }
    }
  });
  runner.start();
  assert.equal(runner.getSummary().pausedAll, true);
  runner.resumeAll();
  await waitFor(() => runner.getSummary().saved === 2);
});

test("resetFrom 只清理目标阶段及后续内容并保留暂停状态", async () => {
  const jobs = TimeEchoCore.createJobs({ count: 1, currentYear: 2026, random: () => 0.4 });
  let failNow = true;
  const runner = TimeEchoCore.createRunner({
    jobs,
    maxRetries: 0,
    execute: async (job, stage) => {
      if (stage === "now" && failNow) throw new Error("现在图失败");
      return makeResult(job, stage);
    }
  });
  runner.start();
  await waitFor(() => jobs[0].steps[3].status === "error");
  runner.pause(jobs[0].id);
  jobs[0].persistenceError = new Error("旧缓存错误");
  jobs[0].pendingImages = { now: "old-download", compose: "old-compose-download", save: "old-save-download" };
  assert.equal(runner.resetFrom(jobs[0].id, "now"), true);
  assert.equal(jobs[0].paused, true);
  assert.equal(jobs[0].current, 3);
  assert.equal(jobs[0].steps[0].status, "done");
  assert.equal(jobs[0].steps[2].status, "done");
  failNow = false;
  for (const stage of ["now", "compose", "save"]) {
    const index = TimeEchoCore.STEPS.indexOf(stage);
    assert.equal(jobs[0].steps[index].status, "waiting");
    assert.equal(jobs[0].steps[index].attempts, 0);
    assert.equal(jobs[0].results[stage], undefined);
    assert.equal(jobs[0].pendingImages[stage], undefined);
  }
  assert.equal(jobs[0].persistenceError, undefined);
  runner.resume(jobs[0].id);
  await waitFor(() => runner.getSummary().saved === 1);
});

test("running 阶段有 startedAt，执行回收后归零；恢复以 done 阶段为断点", async () => {
  const jobs = TimeEchoCore.createJobs({ count: 1, currentYear: 2026, random: () => 0.4 });
  let releaseIdea;
  const runner = TimeEchoCore.createRunner({
    jobs,
    execute: (job, stage) => {
      if (stage === "idea") return new Promise(resolve => { releaseIdea = () => resolve(makeResult(job, stage)); });
      return Promise.resolve(makeResult(job, stage));
    }
  });
  runner.start();
  await waitFor(() => jobs[0].steps[0].status === "running");
  assert.ok(jobs[0].steps[0].startedAt > 0);
  releaseIdea();
  await waitFor(() => jobs[0].steps[0].status === "done");
  assert.equal(jobs[0].steps[0].startedAt, 0);
  await waitFor(() => runner.getSummary().saved === 1);

  const resumedJobs = TimeEchoCore.createJobs({ count: 1, currentYear: 2026, random: () => 0.4 });
  resumedJobs[0].steps[0].status = "done";
  resumedJobs[0].results.idea = "persisted-idea";
  resumedJobs[0].current = 0;
  const resumedCalls = [];
  const resumed = TimeEchoCore.createRunner({
    jobs: resumedJobs,
    execute: async (job, stage) => {
      resumedCalls.push(stage);
      return makeResult(job, stage);
    }
  });
  resumed.start();
  await waitFor(() => resumed.getSummary().saved === 1);
  assert.equal(resumedCalls.includes("idea"), false);
  assert.equal(resumedJobs[0].results.idea, "persisted-idea");
});
