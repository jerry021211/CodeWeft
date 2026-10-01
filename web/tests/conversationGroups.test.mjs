import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtempSync, rmdirSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "codeagent-project-groups-"));
const bundle = join(directory, "groups.cjs");
await build({ entryPoints: [resolve("src/lib/conversationGroups.ts")], tsconfig: "tsconfig.app.json", bundle: true, platform: "node", format: "cjs", outfile: bundle });
const { groupConversations, workspaceKey } = createRequire(import.meta.url)(bundle);
after(() => { unlinkSync(bundle); rmdirSync(directory); });
const conversation = (id, workspace, day) => ({ id, workspace, title: id, updated_at: `2026-09-${day}T00:00:00Z` });

test("groups Windows path variants, never merges different projects with the same folder name", () => {
  const groups = groupConversations([
    conversation("a", "D:\\Projects\\web", "10"),
    conversation("b", "d:/projects/WEB/", "12"),
    conversation("c", "D:\\Other\\web", "11"),
  ]);
  assert.equal(groups.length, 2);
  assert.deepEqual(groups[0].conversations.map(item => item.id), ["b", "a"]);
  assert.equal(groups[1].conversations[0].id, "c");
});

test("sorts both projects and conversations by most recent update without mutating input", () => {
  const input = [conversation("old", "/work/a", "01"), conversation("other", "/work/b", "20"), conversation("new", "/work/a", "25")];
  const groups = groupConversations(input);
  assert.deepEqual(groups.map(group => group.name), ["a", "b"]);
  assert.deepEqual(groups[0].conversations.map(item => item.id), ["new", "old"]);
  assert.deepEqual(input.map(item => item.id), ["old", "other", "new"]);
});

test("preserves case-sensitive POSIX paths and handles empty workspaces", () => {
  assert.notEqual(workspaceKey("/work/App"), workspaceKey("/work/app"));
  assert.equal(workspaceKey("/"), "/");
  assert.deepEqual(groupConversations([]), []);
  assert.equal(groupConversations([conversation("unbound", "", "01")])[0].name, "未绑定工作区");
});
