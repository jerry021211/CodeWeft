import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtempSync, rmdirSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "codeagent-verification-"));
const bundle = join(directory, "verification.cjs");
await build({ entryPoints: [resolve("src/lib/verificationStatus.ts")], tsconfig: "tsconfig.app.json", bundle: true, platform: "node", format: "cjs", outfile: bundle });
const { verificationStatus } = createRequire(import.meta.url)(bundle);
after(() => { unlinkSync(bundle); rmdirSync(directory); });
const event = (seq, type, payload, extra = {}) => ({ seq, type, payload, agent_id: "root", ...extra });

test("off and legacy runs show no invented verification status", () => {
  assert.equal(verificationStatus([]), undefined);
  assert.equal(verificationStatus([event(1, "run.completed", {})]), undefined);
});

test("replay orders root status and distinguishes candidate delivery followup", () => {
  const old = event(1, "cvm.verification_status", { checks: [{ status: "passed" }] });
  const latest = event(3, "cvm.verification_status", { checks: [{ status: "stale" }] });
  const child = event(9, "cvm.verification_status", { checks: [{ status: "passed" }] }, { parent_agent_id: "root" });
  const events = [latest, old, child, latest];
  assert.equal(verificationStatus(events).checks[0].status, "stale");
  for (const [index, phase] of ["candidate", "delivered", "followup"].entries()) {
    events.push(event(10 + index, "cvm.verification_advice", { phase }));
    assert.equal(verificationStatus(events).advice.payload.phase, phase);
  }
});

test("unknown payload stays unknown rather than a successful completion", () => {
  const view = verificationStatus([event(1, "cvm.verification_status", { checks: [null, 4, { status: "unknown" }] })]);
  assert.deepEqual(view.checks, [{ status: "unknown" }]);
});
