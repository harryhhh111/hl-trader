import assert from "node:assert/strict";
import test from "node:test";
import { apiFetch, ApiError } from "./client";

test("client uses same-origin v1 API and unwraps data", async () => {
  const originalFetch = globalThis.fetch;
  let requested: RequestInfo | URL | undefined;
  let options: RequestInit | undefined;
  globalThis.fetch = async (input, init) => {
    requested = input;
    options = init;
    return new Response(JSON.stringify({ data: { ok: true } }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    assert.deepEqual(await apiFetch<{ ok: boolean }>("/api/v1/example"), { ok: true });
    assert.equal(requested, "/api/v1/example");
    assert.equal(options?.credentials, "same-origin");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("client rejects legacy and absolute endpoints before fetch", async () => {
  await assert.rejects(apiFetch("/api/us/watchlist"), /same-origin/);
  await assert.rejects(apiFetch("https://example.invalid/api/v1/news"), /same-origin/);
});

test("client preserves safe HTTP detail", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: "未登录" }), {
    status: 401,
    headers: { "Content-Type": "application/json" },
  });
  try {
    await assert.rejects(apiFetch("/api/v1/private"), (error: unknown) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, 401);
      assert.equal(error.message, "未登录");
      return true;
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("write requests send the CSRF cookie in the header", async () => {
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  let headers: Headers | undefined;
  Object.assign(globalThis, { document: { cookie: "hl_csrf=test-csrf" } });
  globalThis.fetch = async (_input, init) => {
    headers = new Headers(init?.headers);
    return new Response(JSON.stringify({ data: { authenticated: false } }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    await apiFetch("/api/v1/session/logout", { method: "POST" });
    assert.equal(headers?.get("X-CSRF-Token"), "test-csrf");
  } finally {
    globalThis.fetch = originalFetch;
    Object.assign(globalThis, { document: originalDocument });
  }
});
