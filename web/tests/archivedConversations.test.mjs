import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtempSync, rmdirSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "codeagent-archives-"));
const bundle = join(directory, "api.cjs");
await build({ entryPoints: [resolve("src/lib/api.ts")], tsconfig: "tsconfig.app.json", bundle: true, platform: "node", format: "cjs", outfile: bundle });
const { api } = createRequire(import.meta.url)(bundle);
after(() => { unlinkSync(bundle); rmdirSync(directory); });

const page = Array.from({ length: 100 }, (_, index) => ({ id: `archive-${index}`, archived_at: "2026-10-05T00:00:00Z" }));
test("archive listing loads beyond 100 records and forwards cancellation", async t => {
  const urls = [];
  const controller = new AbortController();
  t.mock.method(globalThis, "fetch", async (url, options) => {
    urls.push(url);
    assert.equal(options.signal, controller.signal);
    return Response.json(urls.length === 1 ? page : [{ id: "archive-100", archived_at: "2026-10-04T00:00:00Z" }]);
  });
  const records = await api.listArchivedConversations(controller.signal);
  assert.equal(records.length, 101);
  assert.deepEqual(urls, ["/api/conversations?archived_only=true&limit=100&offset=0", "/api/conversations?archived_only=true&limit=100&offset=100"]);
});

test("an empty archive stops without extra requests", async t => {
  const fetch = t.mock.method(globalThis, "fetch", async () => Response.json([]));
  assert.deepEqual(await api.listArchivedConversations(), []);
  assert.equal(fetch.mock.callCount(), 1);
});

test("an older backend cannot expose active chats as archived", async t => {
  t.mock.method(globalThis, "fetch", async () => Response.json([{ id: "active", archived_at: null }]));
  await assert.rejects(api.listArchivedConversations(), /重启 CodeAgent/);
});

test("an older backend ignoring pagination cannot cause an infinite request loop", async t => {
  const fetch = t.mock.method(globalThis, "fetch", async () => Response.json(page));
  await assert.rejects(api.listArchivedConversations(), /重启 CodeAgent/);
  assert.equal(fetch.mock.callCount(), 2);
});
