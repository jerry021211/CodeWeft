import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "codeagent-approvals-"));
const bundle = join(directory, "approvals.cjs");
await build({ stdin: { contents: `export { pendingApprovalOptions } from './src/hooks/usePendingApprovals';
export { QueryClient, QueryObserver } from '@tanstack/react-query';`, resolveDir: resolve(".") },
  tsconfig: "tsconfig.app.json", bundle: true, platform: "node", format: "cjs", outfile: bundle });
// Enable TanStack's browser polling, without needing a DOM or an SSE connection.
globalThis.window = {};
const { pendingApprovalOptions, QueryClient, QueryObserver } = createRequire(import.meta.url)(bundle);
const originalFetch = globalThis.fetch;
after(() => { globalThis.fetch = originalFetch; delete globalThis.window; rmSync(directory, { recursive: true }); });
const response = items => new Response(JSON.stringify(items), { headers: { "Content-Type": "application/json" } });
async function until(predicate) {
  const deadline = Date.now() + 4000;
  while (!predicate()) {
    assert.ok(Date.now() < deadline, "approval state did not update automatically");
    await new Promise(resolve => setTimeout(resolve, 10));
  }
}

test("new worker approvals appear and expire automatically with no planning SSE or page reload", async () => {
  const client = new QueryClient();
  let rows = [], requests = 0;
  globalThis.fetch = async url => {
    assert.equal(url, "/api/conversations/team-chat/approvals");
    requests++;
    return response(rows);
  };
  const observer = new QueryObserver(client, pendingApprovalOptions("team-chat"));
  let result;
  const unsubscribe = observer.subscribe(value => { result = value; });
  try {
    await until(() => result?.isSuccess);
    assert.deepEqual(result.data, []);
    rows = [{ id: "worker-permission", run_id: "completed-planning-run", status: "pending" }];
    await until(() => result.data?.[0]?.id === "worker-permission");
    assert.equal(result.data[0].run_id, "completed-planning-run");
    rows = [];
    await until(() => result.data?.length === 0);
    assert.ok(requests >= 3);
  } finally { unsubscribe(); client.clear(); }
});

test("switching conversations cancels old approval fetches and cannot show another chat's request", async () => {
  const client = new QueryClient();
  let oldSignal;
  globalThis.fetch = (url, init) => {
    if (url.includes("/old/")) {
      oldSignal = init.signal;
      return new Promise((_resolve, reject) => init.signal.addEventListener("abort", () => reject(new Error("aborted"))));
    }
    return Promise.resolve(response([{ id: "new-permission", run_id: "new-run", status: "pending" }]));
  };
  const observer = new QueryObserver(client, pendingApprovalOptions("old"));
  let result;
  const unsubscribe = observer.subscribe(value => { result = value; });
  try {
    await until(() => oldSignal != null);
    observer.setOptions(pendingApprovalOptions("new"));
    await until(() => result.data?.[0]?.id === "new-permission");
    assert.equal(oldSignal.aborted, true);
    assert.equal(result.data.length, 1);
  } finally { unsubscribe(); client.clear(); }
});
