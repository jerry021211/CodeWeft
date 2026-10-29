import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtempSync, rmdirSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "codeagent-message-actions-"));
const bundle = join(directory, "actions.cjs");
await build({
  stdin: { contents: `export { quoteMessage, copyMessage } from './src/lib/messageActions';
    export { ChatMessage } from './src/components/ChatWorkspace';
    export { api } from './src/lib/api';
    export { createElement } from 'react';
    export { renderToStaticMarkup } from 'react-dom/server';`, resolveDir: resolve(".") },
  tsconfig: "tsconfig.app.json", bundle: true, platform: "node", format: "cjs", outfile: bundle,
});
const { quoteMessage, copyMessage, ChatMessage, api, createElement, renderToStaticMarkup } = createRequire(import.meta.url)(bundle);
after(() => { unlinkSync(bundle); rmdirSync(directory); });
const message = { id: "m", conversation_id: "c", role: "assistant", content: "## Reply\n\nBody", created_at: "2026-10-27T01:00:00Z", status: "complete" };

test("quote preserves drafts, Markdown and blank lines", () => {
  assert.equal(quoteMessage("My draft", "hello\r\n\r\n**world**"), "My draft\n\n> hello\n> \n> **world**\n\n");
  assert.equal(quoteMessage("", "hello"), "> hello\n\n");
});

test("copy writes original Markdown and reports permission failure", async () => {
  const descriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  try {
    let copied;
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async value => { copied = value; } } });
    await copyMessage(message.content);
    assert.equal(copied, message.content);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async () => { throw new Error("denied"); } } });
    await assert.rejects(copyMessage("text"), /复制失败/);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    await assert.rejects(copyMessage("text"), /不支持复制/);
  } finally {
    if (descriptor) Object.defineProperty(navigator, "clipboard", descriptor);
    else delete navigator.clipboard;
  }
});

test("complete replies expose four labeled actions; busy state only blocks branch operations", () => {
  const html = renderToStaticMarkup(createElement(ChatMessage, { message, onQuote() {}, onFork() {}, actionBusy: true }));
  for (const label of ["引用到输入框", "复制回复", "在新分支重新生成", "从此处创建分支"]) assert.ok(html.includes(label));
  assert.equal((html.match(/disabled=""/g) ?? []).length, 2);
  assert.match(html, /<h2>Reply<\/h2>/);
});

test("streaming, user and empty messages do not show reply actions; archive is copy-only", () => {
  for (const patch of [{ status: "streaming" }, { role: "user" }, { content: " " }]) {
    const html = renderToStaticMarkup(createElement(ChatMessage, { message: { ...message, ...patch }, onQuote() {}, onFork() {} }));
    assert.doesNotMatch(html, /aria-label="回复操作"/);
  }
  const html = renderToStaticMarkup(createElement(ChatMessage, { message }));
  assert.match(html, /复制回复/);
  assert.doesNotMatch(html, /引用到输入框|在新分支重新生成|从此处创建分支/);
});

test("fork API sends the selected message and regeneration mode", async () => {
  const original = globalThis.fetch;
  try {
    let call;
    globalThis.fetch = async (url, options) => {
      call = { url, options };
      return new Response(JSON.stringify({ conversation: { id: "fork" }, retry: null }), { status: 201, headers: { "Content-Type": "application/json" } });
    };
    assert.equal((await api.forkConversation("c/a", "m1", true)).conversation.id, "fork");
    assert.equal(call.url, "/api/conversations/c%2Fa/fork");
    assert.deepEqual(JSON.parse(call.options.body), { message_id: "m1", regenerate: true });
  } finally { globalThis.fetch = original; }
});
