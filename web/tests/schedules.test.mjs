import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtempSync, rmdirSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";
import { build } from "esbuild";

const directory = mkdtempSync(join(tmpdir(), "codeagent-schedules-"));
const bundle = join(directory, "schedules.cjs");
await build({ entryPoints: [resolve("src/lib/schedules.ts")], tsconfig: "tsconfig.app.json", bundle: true, platform: "node", format: "cjs", outfile: bundle });
const { localScheduleTime, scheduleTimeToISO, intervalLabel } = createRequire(import.meta.url)(bundle);
after(() => { unlinkSync(bundle); rmdirSync(directory); });

test("local input round trips to an offset-aware instant without shifting the selected time", () => {
  for (const value of ["2030-01-01T09:30", "2030-07-10T23:55"]) {
    const iso = scheduleTimeToISO(value);
    assert.ok(iso.endsWith("Z"));
    assert.equal(localScheduleTime(iso), value);
    assert.equal(new Date(iso).getHours(), Number(value.slice(11, 13)));
  }
});

test("invalid calendar input is rejected instead of normalizing into a different day", () => {
  for (const value of ["", "bad date", "2030-02-30T10:00", "2030-13-01T10:00"]) {
    assert.throws(() => scheduleTimeToISO(value));
  }
});

test("interval labels preserve exact elapsed duration", () => {
  assert.equal(intervalLabel(60), "每 1 分钟");
  assert.equal(intervalLabel(3600), "每 1 小时");
  assert.equal(intervalLabel(86400), "每 1 天");
  assert.equal(intervalLabel(90), "每 90 秒");
});
