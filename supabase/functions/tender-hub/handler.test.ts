import { handler } from "./handler.ts";

const url = "https://prfluuygnxunaitzmttf.supabase.co/functions/v1/tender-hub";
function assert(value: unknown, message: string) {
  if (!value) throw new Error(message);
}

Deno.test("publishable key alone cannot authenticate a user", async () => {
  const response = await handler(new Request(url, {
    headers: { apikey: "sb_publishable_example", origin: "https://www.serenecreations.org" },
  }));
  assert(response.status === 401, "Expected 401 without user JWT");
  assert(response.headers.get("Cache-Control") === "no-store", "Identity must not be cached");
  const body = await response.json();
  assert(!("user" in body), "Must not disclose a user");
});

Deno.test("production browser preflight succeeds without authentication", async () => {
  const response = await handler(new Request(url, {
    method: "OPTIONS", headers: { origin: "https://www.serenecreations.org" },
  }));
  assert(response.status === 204, "Expected preflight 204");
  assert(response.headers.get("Access-Control-Allow-Origin") === "https://www.serenecreations.org", "Exact origin required");
});

Deno.test("untrusted origins cannot read the identity endpoint", async () => {
  const response = await handler(new Request(url, { headers: { origin: "https://example.com" } }));
  assert(response.status === 403, "Expected origin rejection");
  assert(!response.headers.has("Access-Control-Allow-Origin"), "Must not reflect arbitrary origins");
});

Deno.test("unsupported methods cannot reach authentication or business logic", async () => {
  const response = await handler(new Request(url, { method: "DELETE" }));
  assert(response.status === 405, "Expected method rejection");
});
