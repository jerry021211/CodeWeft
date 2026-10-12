import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtempSync, unlinkSync, rmdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "mcp-settings-"));
const bundle = join(directory, "panel.cjs");
await build({
  stdin: { contents: `export { McpConfigModal } from './src/components/McpConfigModal'; export { createElement } from 'react'; export { renderToStaticMarkup } from 'react-dom/server';`, resolveDir: resolve(".") },
  tsconfig: "tsconfig.app.json", bundle: true, platform: "node", format: "cjs", outfile: bundle,
});
const { McpConfigModal, createElement: h, renderToStaticMarkup: render } = createRequire(import.meta.url)(bundle);
after(() => { unlinkSync(bundle); rmdirSync(directory); });
const server = (name, status, trusted = true) => ({ name, status, trusted, transport: 'http',
  url: 'https://example.invalid/mcp', args: [], command: '', env_keys: [], header_keys: [],
  fingerprint: 'a'.repeat(64), startup_timeout: 15, call_timeout: 60, tool_count: status === 'connected' ? 4 : 0 });
const props = { embedded: true, open: true, workspace: '/project', onSave() {}, onDelete() {}, onClose() {}, onAction() {} };

test('empty state offers a clear first action without exposing a large form', () => {
  const html = render(h(McpConfigModal, { ...props, config: { servers: [] } }));
  assert.match(html, /添加第一个服务/);
  assert.match(html, /为你的 Agent 连接更多工具/);
  assert.doesNotMatch(html, /<form/);
  assert.doesNotMatch(html, /role="dialog"/);
});

test('saved, trusted and connected are distinct states with correct actions', () => {
  const html = render(h(McpConfigModal, { ...props, config: { servers: [
    server('pending', 'awaiting_trust', false), server('idle', 'not_connected'), server('ready', 'connected'),
    { ...server('broken', 'error'), error: '连接失败，请检查地址。' },
  ] } }));
  for (const text of ['待信任', '未连接', '已连接', '连接异常', '4 个工具', '检查配置', '重试', '搜索 MCP 服务']) {
    assert.ok(html.includes(text), text);
  }
  assert.match(html, /role="alert"/);
  assert.match(html, /aria-expanded="false"/);
  assert.match(html, /删除 pending/);
});

test('loading and unavailable management actions are represented accessibly', () => {
  const loading = render(h(McpConfigModal, { ...props, loading: true }));
  assert.match(loading, /role="status"/);
  assert.match(loading, /正在读取服务/);
  const noActions = render(h(McpConfigModal, { ...props, onAction: undefined, config: { servers: [server('idle', 'not_connected')] } }));
  assert.match(noActions, /disabled=""[^>]*>连接<\/button>/);
  assert.equal(render(h(McpConfigModal, { ...props, open: false })), '');
});
