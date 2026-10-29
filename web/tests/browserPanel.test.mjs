import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtempSync, unlinkSync, rmdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "browser-panel-"));
const bundle = join(directory, "panel.cjs");
await build({ stdin: { contents: `export { BrowserSessionCard, browserPageLink } from './src/components/BrowserPanel';
export { api } from './src/lib/api'; export { createElement } from 'react';
export { renderToStaticMarkup } from 'react-dom/server';`, resolveDir: resolve(".") },
  tsconfig: "tsconfig.app.json", bundle: true, platform: "node", format: "cjs", outfile: bundle });
const { BrowserSessionCard, browserPageLink, api, createElement: h, renderToStaticMarkup: render } = createRequire(import.meta.url)(bundle);
after(() => { unlinkSync(bundle); rmdirSync(directory); });
const base = { id: "one", server: "browser", control: "agent", started: true, headless: false, busy: false,
  title: "A page", url: "https://example.com/", updated_at: null, error: "", idle_timeout_seconds: 1800 };

test("pending takeover never reports human control and disables repeat takeover", () => {
  const html = render(h(BrowserSessionCard, { session: { ...base, control: "pausing" }, busy: false, onAction() {} }));
  assert.match(html, /等待当前操作结束/);
  assert.match(html, /请先不要操作窗口/);
  assert.match(html, /disabled=""[^>]*>接管浏览器/);
  assert.doesNotMatch(html, />交还 Agent</);
});
test("human control exposes handback and explains last observed page state", () => {
  const html = render(h(BrowserSessionCard, { session: { ...base, control: "human" }, busy: false, onAction() {} }));
  assert.match(html, /交还 Agent/);
  assert.match(html, /页面信息暂不自动更新/);
  assert.match(html, /noopener noreferrer/);
});
test("external-open accepts only ordinary web URLs without credentials", () => {
  for (const url of ["javascript:alert(1)", "file:///secret", "data:text/html,test", "https://user:pass@example.com/", "bad"]) {
    assert.equal(browserPageLink(url), undefined);
  }
  assert.equal(browserPageLink(base.url), base.url);
  const html = render(h(BrowserSessionCard, { session: { ...base, headless: true }, busy: false, onAction() {} }));
  assert.match(html, /后台浏览器不支持人工接管/);
  assert.match(html, /disabled=""[^>]*>接管浏览器/);
});
test("browser API scopes every command to its conversation and session", async () => {
  const previous = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init) => { calls.push([url, init.method]); return new Response("{}", { headers: { "Content-Type": "application/json" } }); };
  try {
    await api.browserSessions("one");
    await api.browserAction("one", "session", "takeover");
    assert.deepEqual(calls, [["/api/conversations/one/browsers", undefined], ["/api/conversations/one/browsers/session/takeover", "POST"]]);
  } finally { globalThis.fetch = previous; }
});
