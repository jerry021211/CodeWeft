import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtempSync, rmdirSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "codeagent-settings-"));
const bundle = join(directory, "settings.cjs");
await build({ entryPoints: [resolve("src/lib/modelSettings.ts")], tsconfig: "tsconfig.app.json", bundle: true, platform: "node", format: "cjs", outfile: bundle });
const { modelSettingsDraft, modelSettingsError } = createRequire(import.meta.url)(bundle);
after(() => { unlinkSync(bundle); rmdirSync(directory); });

function draft() {
  const service = { model: "", base_url: "", has_api_key: false, enabled: false };
  return modelSettingsDraft({ revision: "environment", configured: true, services: {
    chat: { model: "chat", protocol: "openai_chat", base_url: "http://localhost:1234/v1", has_api_key: true, max_tokens: 1000 },
    speech: { ...service }, embedding: { ...service, dimensions: 0 },
  } });
}

test("draft preserves stored keys with null and strips response-only flags", () => {
  const value = draft();
  for (const service of Object.values(value.services)) {
    assert.equal(service.api_key, null);
    assert.equal("has_api_key" in service, false);
  }
  assert.equal("configured" in value, false);
  assert.equal(modelSettingsError(value), undefined);
});

test("enabled services require their own model and endpoint", () => {
  const value = draft();
  value.services.speech.enabled = true;
  assert.match(modelSettingsError(value), /语音识别.*模型名称/);
  value.services.speech.model = "transcriber";
  assert.match(modelSettingsError(value), /语音识别.*服务地址/);
  value.services.speech.base_url = "https://speech.example/v1";
  assert.equal(modelSettingsError(value), undefined);
});

test("embedding requires actual dimensions and credential URLs are rejected", () => {
  const value = draft();
  Object.assign(value.services.embedding, { enabled: true, model: "vector", base_url: "https://vectors.example/v1" });
  assert.match(modelSettingsError(value), /实际输出维度/);
  value.services.embedding.dimensions = 768;
  assert.equal(modelSettingsError(value), undefined);
  value.services.chat.base_url = "https://user:password@example.com/v1";
  assert.match(modelSettingsError(value), /服务地址/);
});
