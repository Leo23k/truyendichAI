// Read-only CDN for the private leotruyen-storage bucket.
// Deploy: npx wrangler deploy cloudflare/b2-cdn.mjs --name leotruyen-b2-cdn
// Set B2_KEY_ID and B2_APPLICATION_KEY with `wrangler secret put`.
// Set B2_ENDPOINT=s3.ca-east-006.backblazeb2.com as a Worker variable.

const encoder = new TextEncoder();
const bucket = "leotruyen-storage";
const ttlSeconds = 300;
const emptyHash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

function hex(bytes) {
  return Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, "0")).join("");
}

async function sha256(value) {
  return hex(await crypto.subtle.digest("SHA-256", encoder.encode(value)));
}

async function hmac(key, value) {
  const imported = await crypto.subtle.importKey("raw", key, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return new Uint8Array(await crypto.subtle.sign("HMAC", imported, encoder.encode(value)));
}

function objectPath(url) {
  if (url.search || url.hash) return null;
  let parts;
  try {
    parts = url.pathname.slice(1).split("/").map(part => decodeURIComponent(part));
  } catch {
    return null;
  }
  if (parts.length !== 3 || parts[0] !== "chapters" || !parts[2].endsWith(".json")) return null;
  if (parts.some(part => !part || part === "." || part === ".." || !/^[a-zA-Z0-9._-]+$/.test(part))) return null;
  return parts.map(part => encodeURIComponent(part).replace(/[!'()*]/g, c => `%${c.charCodeAt(0).toString(16).toUpperCase()}`)).join("/");
}

async function signedOriginRequest(path, env) {
  const endpoint = env.B2_ENDPOINT.replace(/^https?:\/\//, "").replace(/\/$/, "");
  if (!/^s3\.[a-z0-9-]+\.backblazeb2\.com$/.test(endpoint)) throw new Error("Invalid B2 endpoint");
  const region = endpoint.slice(3, -".backblazeb2.com".length);
  const uri = `/${bucket}/${path}`;
  const date = new Date().toISOString().replace(/[:-]|\.\d{3}/g, "");
  const day = date.slice(0, 8);
  const scope = `${day}/${region}/s3/aws4_request`;
  const signedHeaders = "host;x-amz-content-sha256;x-amz-date";
  const canonical = `GET\n${uri}\n\nhost:${endpoint}\nx-amz-content-sha256:${emptyHash}\nx-amz-date:${date}\n\n${signedHeaders}\n${emptyHash}`;
  const stringToSign = `AWS4-HMAC-SHA256\n${date}\n${scope}\n${await sha256(canonical)}`;
  let key = encoder.encode(`AWS4${env.B2_APPLICATION_KEY}`);
  for (const value of [day, region, "s3", "aws4_request"]) key = await hmac(key, value);
  const signature = hex(await hmac(key, stringToSign));
  return new Request(`https://${endpoint}${uri}`, {
    headers: {
      "x-amz-date": date,
      "x-amz-content-sha256": emptyHash,
      Authorization: `AWS4-HMAC-SHA256 Credential=${env.B2_KEY_ID}/${scope}, SignedHeaders=${signedHeaders}, Signature=${signature}`,
    },
  });
}

function reply(response, status) {
  const headers = new Headers(response.headers);
  headers.set("X-Cache-Status", status);
  return new Response(response.body, { status: response.status, headers });
}

export default {
  async fetch(request, env) {
    if (request.method !== "GET" && request.method !== "HEAD") return new Response("Method not allowed", { status: 405 });
    const url = new URL(request.url);
    const path = objectPath(url);
    if (!path) return new Response("Not found", { status: 404 });
    if (!env.B2_KEY_ID || !env.B2_APPLICATION_KEY || !env.B2_ENDPOINT) {
      return new Response("CDN origin is not configured", { status: 503 });
    }

    const cache = caches.default;
    const cacheKey = new Request(`${url.origin}/${path}`, { method: "GET" });
    const hit = await cache.match(cacheKey);
    if (hit) return request.method === "HEAD" ? new Response(null, { status: 200, headers: reply(hit, "HIT").headers }) : reply(hit, "HIT");

    let origin;
    try {
      origin = await fetch(await signedOriginRequest(path, env));
    } catch {
      return new Response("Origin unavailable", { status: 502, headers: { "Cache-Control": "no-store" } });
    }
    if (origin.status === 404) return new Response("Not found", { status: 404, headers: { "Cache-Control": "no-store" } });
    if (!origin.ok) return new Response("Origin unavailable", { status: 502, headers: { "Cache-Control": "no-store" } });

    const headers = new Headers();
    headers.set("Content-Type", origin.headers.get("Content-Type") || "application/json");
    headers.set("Cache-Control", `public, max-age=${ttlSeconds}`);
    for (const name of ["Content-Encoding", "ETag", "Last-Modified"]) {
      if (origin.headers.has(name)) headers.set(name, origin.headers.get(name));
    }
    const response = new Response(origin.body, { status: 200, headers });
    try {
      await cache.put(cacheKey, response.clone());
    } catch {
      // A cache write failure must not make an otherwise valid chapter unreadable.
    }
    return request.method === "HEAD" ? new Response(null, { status: 200, headers: reply(response, "MISS").headers }) : reply(response, "MISS");
  },
};
