import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtempSync, unlinkSync, rmdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "codeagent-approval-modes-"));
const bundle = join(directory, "modes.cjs");
await build({ stdin: { contents: `export { ComposerToolbar } from './src/components/ComposerToolbar';
export { api } from './src/lib/api';
export { createElement } from 'react'; export { renderToStaticMarkup } from 'react-dom/server';`, resolveDir: resolve(".") },
  tsconfig: "tsconfig.app.json", bundle: true, platform: "node", format: "cjs", outfile: bundle });
const { ComposerToolbar, api, createElement: h, renderToStaticMarkup: render } = createRequire(import.meta.url)(bundle);
after(() => { unlinkSync(bundle); rmdirSync(directory); });
const noop = () => {};

test("approval mode defaults to auto and remains separate from read-only work", () => {
  const html = render(h(ComposerToolbar, { busy: false, readOnly: true, onReadOnlyChange: noop, onApprovalModeChange: noop }));
  assert.match(html, /审批模式：自动审批/);
  assert.match(html, /只读/);
  assert.match(html, /有风险或无法判断时询问/);
});

test("running and saving lock approval selection without claiming unrestricted file access", () => {
  for (const locked of [{ busy: true }, { approvalSaving: true }, { teamLeadActive: true }, { planLocked: true }]) {
    const html = render(h(ComposerToolbar, { approvalMode: "full", onReadOnlyChange: noop, onApprovalModeChange: noop, ...locked }));
    assert.match(html, /aria-label="审批模式：完全访问"[^>]*disabled/);
    assert.match(html, /仍遵守只读及项目限制/);
    assert.doesNotMatch(html, /任何文件/);
  }
});

test("API sends saved and run modes explicitly but leaves legacy callers unchanged", async () => {
  const previous = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, method: init.method, body: JSON.parse(init.body) });
    return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
  };
  try {
    await api.updateConversation("one", { approval_mode: "ask" });
    await api.createRun("one", "hello", false, false, false, undefined, [], undefined, "auto");
    await api.createRun("two", "legacy");
    assert.deepEqual(calls[0].body, { approval_mode: "ask" });
    assert.equal(calls[1].body.approvalMode, "auto");
    assert.equal(Object.hasOwn(calls[2].body, "approvalMode"), false);
  } finally { globalThis.fetch = previous; }
});
