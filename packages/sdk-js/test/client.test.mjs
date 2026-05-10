import { test } from "node:test";
import assert from "node:assert/strict";
import { PlenoAnonymize, PlenoAnonymizeError } from "../dist/index.js";

function mockFetch(handler) {
  return async (url, init) => {
    const body = init && init.body ? JSON.parse(init.body) : undefined;
    return handler(String(url), init?.method ?? "GET", body);
  };
}

function jsonResponse(data, init = {}) {
  return new Response(JSON.stringify(data), {
    status: init.status ?? 200,
    headers: { "content-type": "application/json" },
  });
}

test("analyze posts to /api/analyze with language default", async () => {
  let captured;
  const client = new PlenoAnonymize({
    endpoint: "https://example.test",
    fetch: mockFetch((url, method, body) => {
      captured = { url, method, body };
      return jsonResponse([
        { entity_type: "EMAIL_ADDRESS", start: 0, end: 16, score: 1, text: "a@b.co" },
      ]);
    }),
  });
  const findings = await client.analyze("a@b.co");
  assert.equal(captured.url, "https://example.test/api/analyze");
  assert.equal(captured.method, "POST");
  assert.equal(captured.body.language, "ja");
  assert.equal(findings.length, 1);
  assert.equal(findings[0].entity_type, "EMAIL_ADDRESS");
});

test("redact accepts string shorthand", async () => {
  const client = new PlenoAnonymize({
    endpoint: "https://example.test",
    fetch: mockFetch((url, _method, body) => {
      assert.equal(body.text, "hello");
      return jsonResponse({ text: "<X>", items: [] });
    }),
  });
  const out = await client.redact("hello");
  assert.equal(out.text, "<X>");
});

test("non-2xx becomes PlenoAnonymizeError with body", async () => {
  const client = new PlenoAnonymize({
    endpoint: "https://example.test",
    fetch: mockFetch(() => jsonResponse({ detail: "bad" }, { status: 422 })),
  });
  await assert.rejects(
    () => client.analyze("x"),
    (err) => {
      assert.ok(err instanceof PlenoAnonymizeError);
      assert.equal(err.status, 422);
      assert.deepEqual(err.body, { detail: "bad" });
      return true;
    },
  );
});

test("endpoint trailing slash is normalized", async () => {
  const client = new PlenoAnonymize({
    endpoint: "https://example.test///",
    fetch: mockFetch((url) => {
      assert.equal(url, "https://example.test/api/analyze");
      return jsonResponse([]);
    }),
  });
  await client.analyze("hi");
});
