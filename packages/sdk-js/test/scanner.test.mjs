import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, writeFile, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PlenoAnonymize, scanPaths } from "../dist/index.js";

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), "pleno-scan-"));
  await writeFile(join(root, "a.md"), "Contact john@example.com");
  await writeFile(join(root, "b.md"), "no pii here");
  await mkdir(join(root, "node_modules"));
  await writeFile(join(root, "node_modules", "skip.md"), "ignored@example.com");
  await mkdir(join(root, "nested"));
  await writeFile(join(root, "nested", "c.txt"), "Email: alice@example.com");
  return root;
}

test("scanPaths walks dirs, skips ignored, aggregates findings", async () => {
  const root = await fixture();
  const calls = [];
  const fakeFetch = async (url, init) => {
    const body = JSON.parse(init.body);
    calls.push(body.text);
    const findings = [];
    const re = /[\w.+-]+@[\w-]+\.[\w.-]+/g;
    let match;
    while ((match = re.exec(body.text)) !== null) {
      findings.push({
        entity_type: "EMAIL_ADDRESS",
        start: match.index,
        end: match.index + match[0].length,
        score: 1,
        text: match[0],
      });
    }
    return new Response(JSON.stringify(findings), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  const client = new PlenoAnonymize({ endpoint: "https://example.test", fetch: fakeFetch });
  const summary = await scanPaths(client, [root]);
  const scannedNames = summary.files.filter((f) => !f.skipped).map((f) => f.path);
  assert.ok(scannedNames.some((p) => p.endsWith("a.md")));
  assert.ok(scannedNames.some((p) => p.endsWith("c.txt")));
  assert.ok(!scannedNames.some((p) => p.includes("node_modules")));
  assert.equal(summary.totalFindings, 2);
  assert.equal(summary.byEntity.EMAIL_ADDRESS, 2);
});
