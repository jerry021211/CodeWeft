import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtempSync, unlinkSync, rmdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "personal-todos-"));
const bundle = join(directory, "panel.cjs");
await build({
  stdin: { contents: `export { PersonalTodoPanel } from './src/components/PersonalTodoPanel'; export { createElement } from 'react'; export { renderToStaticMarkup } from 'react-dom/server';`, resolveDir: resolve(".") },
  tsconfig: "tsconfig.app.json", bundle: true, platform: "node", format: "cjs", outfile: bundle,
});
const { PersonalTodoPanel, createElement: h, renderToStaticMarkup: render } = createRequire(import.meta.url)(bundle);
after(() => { unlinkSync(bundle); rmdirSync(directory); });

test("personal progress preserves the supplied sequence and distinguishes all three states", () => {
  const html = render(h(PersonalTodoPanel, { todos: [
    { content: "先调查", status: "completed" },
    { content: "实现功能", status: "in_progress" },
    { content: "验证结果", status: "pending" },
  ] }));
  assert.ok(html.indexOf("先调查") < html.indexOf("实现功能"));
  assert.ok(html.indexOf("实现功能") < html.indexOf("验证结果"));
  assert.match(html, /1 已完成 · 1 进行中 · 1 待处理/);
  assert.match(html, /aria-expanded="true"/);
  for (const status of ["completed", "in_progress", "pending"]) assert.match(html, new RegExp(`data-status="${status}"`));
});

test("cleared personal lists disappear and completed lists remain visible", () => {
  assert.equal(render(h(PersonalTodoPanel, { todos: [] })), "");
  const html = render(h(PersonalTodoPanel, { title: "成员甲 · 个人步骤", todos: [{ content: "完成验证", status: "completed" }] }));
  assert.match(html, /成员甲 · 个人步骤/);
  assert.match(html, /1 已完成/);
  assert.match(html, /完成验证/);
});
