const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
const inlineScript = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)]
  .map(match => match[1])
  .find(script => script.includes("window.TimeEchoModels"));
assert.ok(inlineScript, "未找到模型调用 inline script");

function makeNode() {
  return {
    value: "",
    textContent: "",
    title: "",
    dataset: {},
    style: {},
    options: [],
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    matches() { return false; },
    querySelectorAll() { return []; },
    add() {},
    addEventListener() {},
    remove() {},
    setAttribute() {},
    select() {},
    close() {},
    showModal() {},
    getBoundingClientRect() { return { left: 0, right: 0, top: 0, bottom: 0 }; }
  };
}

function makeModels(fetchMock) {
  const nodes = new Map();
  const nodeFor = selector => {
    if (!nodes.has(selector)) nodes.set(selector, makeNode());
    return nodes.get(selector);
  };
  const document = {
    querySelector: nodeFor,
    querySelectorAll() { return []; },
    createElement: makeNode,
    body: { append() {} },
    execCommand() { return true; },
    addEventListener() {}
  };
  const context = {
    window: {},
    document,
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    navigator: {},
    fetch: fetchMock,
    Blob,
    FormData,
    AbortSignal,
    atob,
    btoa,
    setTimeout,
    clearTimeout,
    console
  };
  vm.runInNewContext(inlineScript, context, { filename: "index-inline-models.js" });
  return context.window.TimeEchoModels;
}

function response(payload, status = 200, extraHeaders = {}) {
  const headers = Object.fromEntries(Object.entries(extraHeaders).map(([key, value]) => [key.toLowerCase(), value]));
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get(name) { return headers[name.toLowerCase()] || null; } },
    async text() { return JSON.stringify(payload); }
  };
}

function b64(value = "generated-image") {
  return Buffer.from(value).toString("base64");
}

test("文本模型使用传入的 config 快照", async () => {
  const calls = [];
  const models = makeModels(async (url, options) => {
    calls.push({ url, options });
    return response({ choices: [{ message: { content: "{\"ok\":true}" } }] });
  });
  const snapshot = {
    textProvider: "openai",
    openaiTextKey: "test-snapshot-key",
    openaiTextModel: "snapshot-model"
  };
  const text = await models.callTextModel(
    [{ role: "system", content: "system instruction" }, { role: "user", content: "user prompt" }],
    0.2,
    snapshot
  );

  assert.equal(text, "{\"ok\":true}");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://api.openai.com/v1/chat/completions");
  assert.equal(calls[0].options.headers.authorization, "Bearer test-snapshot-key");
  const body = JSON.parse(calls[0].options.body);
  assert.equal(body.model, "snapshot-model");
  assert.equal(body.temperature, 0.2);
  assert.deepEqual(body.messages, [
    { role: "system", content: "system instruction" },
    { role: "user", content: "user prompt" }
  ]);
});

test("OpenAI 和北海旧图走 generations，现在图用 FormData 引用缓存旧照", async () => {
  for (const provider of ["openai", "beihai"]) {
    const calls = [];
    const models = makeModels(async (url, options) => {
      calls.push({ url, options });
      return response({ data: [{ b64_json: b64(`${provider}-${calls.length}`) }] });
    });
    const config = {
      provider,
      name: provider,
      key: `test-${provider}-key`,
      model: "test-image-model",
      baseUrl: `https://image.${provider}.test/v1`,
      size: "1024x1024",
      quality: "medium"
    };
    const reference = "data:image/png;base64," + b64("cached-old-photo");

    await models.imageFromOpenAI("old-photo prompt", config);
    await models.imageEditFromOpenAI("now-photo prompt", reference, config);

    assert.equal(calls.length, 2);
    assert.equal(calls[0].url, `${config.baseUrl}/images/generations`);
    assert.equal(calls[0].options.method, "POST");
    assert.equal(calls[0].options.headers["content-type"], "application/json");
    const generationBody = JSON.parse(calls[0].options.body);
    assert.equal(generationBody.prompt, "old-photo prompt");
    assert.equal(generationBody.n, 1);

    assert.equal(calls[1].url, `${config.baseUrl}/images/edits`);
    assert.equal(calls[1].options.method, "POST");
    assert.ok(calls[1].options.body instanceof FormData);
    assert.equal(calls[1].options.body.get("model"), "test-image-model");
    assert.equal(calls[1].options.body.get("prompt"), "now-photo prompt");
    const uploaded = calls[1].options.body.get("image");
    assert.equal(uploaded.name, "reference.png");
    assert.equal(uploaded.type, "image/png");
    assert.equal(await uploaded.text(), "cached-old-photo");
  }
});

test("Gemini now 图把动态 prompt 和缓存旧照作为 inlineData 发送", async () => {
  const calls = [];
  const models = makeModels(async (url, options) => {
    calls.push({ url, options });
    return response({ candidates: [{ content: { parts: [{ inlineData: { mimeType: "image/png", data: b64("gemini-now") } }] } }] });
  });
  const reference = "data:image/jpeg;base64," + b64("cached-old-photo");
  const settings = {
    geminiKey: "test-gemini-key",
    geminiModel: "test-gemini-image",
    geminiAspectRatio: "4:3"
  };

  const image = await models.imageFromGemini("dynamic now prompt 2026", reference, settings);
  assert.match(image, /^data:image\/png;base64,/);
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /models\/test-gemini-image:generateContent$/);
  assert.equal(calls[0].options.headers["x-goog-api-key"], "test-gemini-key");
  assert.equal(calls[0].url.includes("key="), false);
  const body = JSON.parse(calls[0].options.body);
  assert.deepEqual(body.contents[0].parts, [
    { text: "dynamic now prompt 2026" },
    { inlineData: { mimeType: "image/jpeg", data: b64("cached-old-photo") } }
  ]);
  assert.deepEqual(body.generationConfig.imageConfig, { aspectRatio: "4:3" });
});

test("Gemini 旧图默认允许无 reference，并使用请求头传递 Key", async () => {
  const calls = [];
  const models = makeModels(async (url, options) => {
    calls.push({ url, options });
    return response({ candidates: [{ content: { parts: [{ inlineData: { mimeType: "image/png", data: b64("gemini-old") } }] } }] });
  });
  const settings = { geminiKey: "test-gemini-key", geminiModel: "test-gemini-image" };

  const image = await models.imageFromGemini("old photo prompt", undefined, settings);
  assert.match(image, /^data:image\/png;base64,/);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.headers["x-goog-api-key"], "test-gemini-key");
  const body = JSON.parse(calls[0].options.body);
  assert.deepEqual(body.contents[0].parts, [{ text: "old photo prompt" }]);
});

test("缺少 Gemini 参考图时拒绝请求，不能退回无引用生图", async () => {
  let calls = 0;
  const models = makeModels(async () => {
    calls += 1;
    // 如果实现错误地继续发起无引用请求，这个成功响应会让 assert.rejects 明确失败。
    return response({ candidates: [{ content: { parts: [{ inlineData: { mimeType: "image/png", data: b64("unexpected-image") } }] } }] });
  });

  await assert.rejects(
    models.imageFromGemini("now prompt without reference", undefined, { geminiKey: "test-gemini-key" }, true),
    /参考图|旧照|引用|reference/i
  );
  assert.equal(calls, 0);
});

test("上游 API 错误会保留 HTTP status，供自动重试判断", async () => {
  const models = makeModels(async () => response({ error: { message: "rate limited" } }, 429));
  await assert.rejects(
    models.imageFromOpenAI("retryable prompt", {
      provider: "openai",
      name: "OpenAI",
      key: "test-openai-key",
      model: "test-image-model",
      baseUrl: "https://image.openai.test/v1",
      size: "1024x1024",
      quality: "medium"
    }),
    error => error.status === 429 && error.message === "rate limited"
  );
});
