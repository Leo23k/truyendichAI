"""The CDN read path must stay stable and never leak origin credentials."""

import subprocess
from pathlib import Path

import httpx
import pytest

from app import cdn, storage
from sync import service


def test_cdn_urls_and_local_fallback(monkeypatch):
    monkeypatch.setenv("CDN_BASE_URL", "https://cdn.example.test/")
    monkeypatch.setenv("CDN_READ_TOKEN", "private-test-token")
    monkeypatch.setattr(cdn, "b2_configured", lambda: True)
    assert storage.cdn_object_url("chapters/book/001.json") == "https://cdn.example.test/chapters/book/001.json"
    assert cdn.storage_url("chapters", "book/001.json") == "https://cdn.example.test/chapters/book/001.json"
    with pytest.raises(ValueError):
        storage.cdn_object_url("../private.json")
    monkeypatch.delenv("CDN_READ_TOKEN")
    monkeypatch.setattr(cdn, "presigned_download_url", lambda path: f"b2://{path}")
    assert cdn.storage_url("chapters", "book/001.json") == "b2://chapters/book/001.json"
    monkeypatch.delenv("CDN_BASE_URL")
    assert storage.cdn_object_url("chapters/book/001.json") is None


def test_sync_download_uses_cdn_without_supabase_or_direct_b2_headers(monkeypatch):
    seen = []

    def fetch(request):
        seen.append(request)
        return httpx.Response(200, json={"content": "chapter"})

    monkeypatch.setenv("CDN_BASE_URL", "https://cdn.example.test")
    monkeypatch.setenv("CDN_READ_TOKEN", "private-test-token")
    monkeypatch.setattr(service, "b2_configured", lambda: True)
    monkeypatch.setattr(service, "b2_download", lambda *_: pytest.fail("B2 GET bypassed CDN"))
    original_client = httpx.Client
    monkeypatch.setattr(service.httpx, "Client", lambda **_: original_client(transport=httpx.MockTransport(fetch)))
    remote = object.__new__(service.SupabaseSync)
    assert remote.download_json("chapters", "book/001.json") == {"content": "chapter"}
    assert seen[0].url == "https://cdn.example.test/chapters/book/001.json"
    assert "authorization" not in seen[0].headers
    assert "apikey" not in seen[0].headers
    assert seen[0].headers["x-cdn-read-token"] == "private-test-token"


def test_web_chapter_read_sends_no_supabase_key_to_cdn(monkeypatch):
    seen = []

    def fetch(request):
        seen.append(request)
        return httpx.Response(200, json={"content": "chapter"})

    monkeypatch.setenv("CDN_BASE_URL", "https://cdn.example.test")
    monkeypatch.setenv("CDN_READ_TOKEN", "private-test-token")
    monkeypatch.setattr(cdn, "SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setattr(cdn, "SUPABASE_ANON_KEY", "test-anon-key")
    monkeypatch.setattr(cdn, "b2_configured", lambda: True)
    original_client = httpx.Client
    monkeypatch.setattr(cdn.httpx, "Client", lambda **_: original_client(transport=httpx.MockTransport(fetch)))
    assert cdn.load_cdn_json("chapters/book/001.json") == {"content": "chapter"}
    assert seen[0].url == "https://cdn.example.test/chapters/book/001.json"
    assert "apikey" not in seen[0].headers
    assert seen[0].headers["x-cdn-read-token"] == "private-test-token"


def test_web_chapter_falls_back_to_supabase_and_logs_primary_failure(monkeypatch, caplog):
    seen = []

    def fetch(request):
        seen.append(request)
        if request.url.host == "cdn.example.test":
            return httpx.Response(502)
        return httpx.Response(200, json={"content": "backup"})

    monkeypatch.setenv("CDN_BASE_URL", "https://cdn.example.test")
    monkeypatch.setenv("CDN_READ_TOKEN", "private-test-token")
    monkeypatch.setattr(cdn, "SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setattr(cdn, "SUPABASE_ANON_KEY", "test-anon-key")
    monkeypatch.setattr(cdn, "b2_configured", lambda: True)
    original_client = httpx.Client
    monkeypatch.setattr(cdn.httpx, "Client", lambda **_: original_client(transport=httpx.MockTransport(fetch)))
    assert cdn.load_cdn_json("chapters/book/001.json") == {"content": "backup"}
    assert len(seen) == 2
    assert "apikey" not in seen[0].headers
    assert seen[1].headers["apikey"] == "test-anon-key"
    assert "x-cdn-read-token" not in seen[1].headers
    assert "trying Supabase Storage" in caplog.text


def test_worker_signs_private_origin_and_caches_only_success():
    worker = (Path(__file__).resolve().parents[1] / "cloudflare" / "b2-cdn.mjs").as_uri()
    script = r"""
import assert from 'node:assert/strict';
import { createHash, createHmac } from 'node:crypto';
import worker from WORKER_URL;
const objects = new Map();
globalThis.caches = { default: {
  match: async key => objects.get(key.url)?.clone(),
  put: async (key, response) => { objects.set(key.url, response.clone()); },
}};
let originCalls = 0;
globalThis.fetch = async request => {
  originCalls++;
  assert.equal(new URL(request.url).pathname, '/leotruyen-storage/chapters/book/001.json');
  const date = request.headers.get('x-amz-date');
  const day = date.slice(0, 8), region = 'ca-east-006';
  const scope = `${day}/${region}/s3/aws4_request`;
  const empty = createHash('sha256').update('').digest('hex');
  const canonical = `GET\n/leotruyen-storage/chapters/book/001.json\n\nhost:s3.ca-east-006.backblazeb2.com\nx-amz-content-sha256:${empty}\nx-amz-date:${date}\n\nhost;x-amz-content-sha256;x-amz-date\n${empty}`;
  const digest = createHash('sha256').update(canonical).digest('hex');
  let key = Buffer.from('AWS4secret');
  for (const part of [day, region, 's3', 'aws4_request']) key = createHmac('sha256', key).update(part).digest();
  const signature = createHmac('sha256', key).update(`AWS4-HMAC-SHA256\n${date}\n${scope}\n${digest}`).digest('hex');
  assert.ok(request.headers.get('authorization').endsWith(signature));
  return new Response('{"content":"kiếm lai"}', { headers: { 'Content-Type': 'application/json' } });
};
const env = { B2_ENDPOINT: 's3.ca-east-006.backblazeb2.com', B2_KEY_ID: 'key', B2_APPLICATION_KEY: 'secret', CDN_READ_TOKEN: 'private-test-token' };
const url = 'https://cdn.example.test/chapters/book/001.json';
assert.equal((await worker.fetch(new Request(url), env)).status, 401);
assert.equal((await worker.fetch(new Request(url, { headers: { 'X-CDN-Read-Token': 'wrong' } }), env)).status, 401);
assert.equal(originCalls, 0);
const authorized = () => new Request(url, { headers: { 'X-CDN-Read-Token': 'private-test-token' } });
let response = await worker.fetch(authorized(), env);
assert.equal(response.headers.get('X-Cache-Status'), 'MISS');
assert.equal(response.headers.get('Cache-Control'), 'private, no-store');
assert.equal(await response.text(), '{"content":"kiếm lai"}');
response = await worker.fetch(authorized(), env);
assert.equal(response.headers.get('X-Cache-Status'), 'HIT');
assert.equal(response.headers.get('Cache-Control'), 'private, no-store');
assert.equal(originCalls, 1);
const concurrent = await Promise.all(Array.from({ length: 8 }, () => worker.fetch(authorized(), env)));
assert.ok(concurrent.every(item => item.headers.get('X-Cache-Status') === 'HIT'));
assert.equal(originCalls, 1);
assert.equal((await worker.fetch(new Request(url), env)).status, 401);
assert.equal(originCalls, 1);
assert.equal((await worker.fetch(new Request(url + '?token=x'), env)).status, 404);
assert.equal((await worker.fetch(new Request('https://cdn.example.test/covers/book.jpg'), env)).status, 404);
assert.equal((await worker.fetch(new Request('https://cdn.example.test/chapters/book%2Fother/001.json'), env)).status, 404);
assert.equal((await worker.fetch(new Request(url, { method: 'POST' }), env)).status, 405);
""".replace("WORKER_URL", repr(worker))
    result = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
