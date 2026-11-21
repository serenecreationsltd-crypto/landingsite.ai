import { createSupabaseContext } from "@supabase/server";

const allowedOrigins = new Set([
  "https://www.serenecreations.org",
  "https://serenecreations.org",
]);

// This endpoint verifies identity only. It does not grant roles or expose admin clients.
export async function handler(request: Request): Promise<Response> {
  const origin = request.headers.get("origin");
  const headers = new Headers({
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
    "Vary": "Origin",
    "X-Content-Type-Options": "nosniff",
  });
  const json = (body: unknown, status: number) =>
    new Response(JSON.stringify(body), { status, headers });

  if (origin && !allowedOrigins.has(origin)) {
    return json({ error: "Origin not allowed" }, 403);
  }
  if (origin) headers.set("Access-Control-Allow-Origin", origin);
  headers.set("Access-Control-Allow-Headers", "authorization, apikey, content-type, x-client-info");
  headers.set("Access-Control-Allow-Methods", "GET, POST, OPTIONS");

  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers });
  if (request.method !== "GET" && request.method !== "POST") {
    headers.set("Allow", "GET, POST, OPTIONS");
    return json({ error: "Method not allowed" }, 405);
  }
  if (!/^Bearer\s+\S+$/i.test(request.headers.get("authorization") ?? "")) {
    return json({ error: "Valid user session required" }, 401);
  }

  try {
    const { data: context, error } = await createSupabaseContext(request, { auth: "user" });
    if (error || !context) {
      const status = error?.status && error.status >= 500 ? 503 : 401;
      return json({ error: status === 401 ? "Valid user session required" : "Authentication service unavailable" }, status);
    }
    // Recheck with Auth so a deleted or invalid user is not accepted on claims alone.
    const { data, error: userError } = await context.supabase.auth.getUser();
    if (userError || !data.user) return json({ error: "Valid user session required" }, 401);
    return json({
      service: "tender-hub",
      authenticated: true,
      user: { id: data.user.id, email: data.user.email ?? null },
    }, 200);
  } catch {
    // Never return tokens, environment variables or upstream error details.
    return json({ error: "Authentication service unavailable" }, 503);
  }
}
