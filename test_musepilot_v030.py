#!/usr/bin/env python3
"""v0.3.0 tests for musepilot.py — Settings/Connect, live model query,
per-model detail, streaming timing, voice UI presence, no hardcoding.

Run:
  HOME=/tmp/mptest-v030-home PATH=/tmp/fakebin-v030:$PATH \
    FAKE_MUSE_LOG=/tmp/fake-muse-v030.log python3 test_musepilot_v030.py

The fake `muse` in /tmp/fakebin-v030 is NOT part of the repo; it is built
by the test runner (see the build notes in the repo README). The tests
also probe the real CLI at ~/.local/bin/muse when present, but only to
assert it does NOT return the v0.2.0 "javascript" model bug.
"""
import http.client
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.expanduser("~/workspace/musepilot"))
import musepilot as mp  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{PASS if cond else FAIL}] {name}" + (f" — {detail}" if detail and not cond else ""))


FAKE_HOME = os.environ["HOME"]
os.environ.pop("META_API_KEY", None)

# ---------------------------------------------------------- 1. discovery
cli = mp.MuseCLI()
check("fake muse binary found", cli.available, cli.binary)
opts = cli.discover_options(force=True)
check("models from exact Model enum only",
      [m["id"] for m in opts["models"]] == ["muse-test-a", "muse-test-b"],
      str(opts["models"]))
check("javascript NOT presented as a model",
      "javascript" not in [m["id"] for m in opts["models"]])
check("model source is live", opts["model_source"] == "muse schema (live)", opts["model_source"])
check("efforts from ReasoningEffort enum",
      opts["reasoning_efforts"] == ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
      str(opts["reasoning_efforts"]))
check("effort source is live", opts["effort_source"] == "muse schema (live)")
check("context flag absent on fake CLI (1.4.10 parity)", opts["context_flag"] is None)

# No hardcoded populated list: with only the trap enum, models must be empty.
orig_schema = cli._schema_enums
cli._schema_enums = lambda: {"CodeModeLanguage": {"values": ["javascript"], "description": ""}}
cli._options_cache = None
opts2 = cli.discover_options(force=True)
check("trap-only schema yields NO models (no fallback)",
      opts2["models"] == [], str(opts2["models"]))
check("trap-only schema yields empty-model note",
      "no model list" in opts2["model_source"].lower(), opts2["model_source"])
cli._schema_enums = orig_schema
cli._options_cache = None
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "musepilot.py")).read()
check("no FALLBACK_MODELS constant", "FALLBACK_MODELS" not in src)
check("no EFFORT_VALUES constant", "EFFORT_VALUES" not in src)
check("no builtin-fallback source string", "builtin-fallback" not in src)

# ---------------------------------------------------------- 2. model profile
prof = cli.model_profile("muse-test-a")
check("model-profile parsed", prof is not None and prof.get("default_reasoning_effort") == "medium", str(prof))
check("model-profile rejects bad ids", cli.model_profile("bad/id;rm") is None)
check("valid_model_id accepts", mp.valid_model_id("muse-spark-1.3"))
check("valid_model_id rejects spaces/paths", not mp.valid_model_id("a b/c"))

# ---------------------------------------------------------- 3. API models (local fake server)
class FakeAPI(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        authz = self.headers.get("Authorization") or ""
        if self.path == "/v1/models" and authz.startswith("Bearer ") and authz != "Bearer wrong-key":
            body = json.dumps({"data": [
                {"id": "muse-api-1", "context_window": 1048576},
                {"id": "muse-api-2"},
            ]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        else:
            body = b'{"error":"unauthorized"}'
            self.send_response(401); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

api_srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
api_port = api_srv.server_address[1]
threading.Thread(target=api_srv.serve_forever, daemon=True).start()
models, err = mp.query_api_models("test-api-key", base=f"http://127.0.0.1:{api_port}/v1")
check("API models parsed", [m["id"] for m in models] == ["muse-api-1", "muse-api-2"] and err is None, str(models) + str(err))
check("API context_window parsed", models[0]["context_window"] == 1048576 and models[1]["context_window"] is None)
models2, err2 = mp.query_api_models("wrong-key", base=f"http://127.0.0.1:{api_port}/v1")
check("API wrong key -> error, no models", models2 == [] and err2 is not None, str(err2))

# ---------------------------------------------------------- 4. auth file storage (Linux)
app = mp.App()
check("app cli env_provider wired", app.cli.env_provider is not None)
auth = app.auth
check("auth status not signed in initially (no env/file)",
      not auth.status()["ok"] or auth.status()["env_key"] is False, str(auth.status()))
ok, msg = auth.save_api_key("test-api-key")
check("save_api_key works on Linux (file)", ok, msg)
check("key file exists mode 0600",
      os.path.isfile(mp.KEY_FILE) and (os.stat(mp.KEY_FILE).st_mode & 0o777) == 0o600,
      oct(os.stat(mp.KEY_FILE).st_mode & 0o777) if os.path.isfile(mp.KEY_FILE) else "missing")
check("get_api_key returns saved key", auth.get_api_key() == "test-api-key")
check("status shows stored key, no secret", auth.status()["stored_key"] and "test-api-key" not in json.dumps(auth.status()))
env = auth.child_env()
check("child_env injects file key", env.get("META_API_KEY") == "test-api-key")
# App.query_models with API base pointed at the fake server must prefer API models.
mp.API_BASE = f"http://127.0.0.1:{api_port}/v1"
app._models_cache = None
qm = app.query_models(force=True)
check("query_models prefers API list", [m["id"] for m in qm["models"]] == ["muse-api-1", "muse-api-2"]
      and "Meta Model API" in qm["model_source"], str(qm))
check("query_models response contains no secret", "test-key-123" not in json.dumps(qm))
detail = app.model_detail("muse-test-a")
check("model_detail efforts live", detail["reasoning_efforts"] == opts["reasoning_efforts"], str(detail.get("reasoning_efforts")))
check("model_detail default effort from profile", detail["default_reasoning_effort"] == "medium", str(detail))
check("model_detail context not settable without flag",
      detail["context_flag"] is None and "not settable" in detail["context_note"].lower(), detail["context_note"])
check("model_detail profile note warns about validation",
      "does not validate" in detail["profile_note"] or "does not prove" in detail["profile_note"])
check("model_detail rejects bad id", "error" in app.model_detail("bad/id"))

# ---------------------------------------------------------- 5. HTTP: auth + models + SSE timing
# Use a large budget so /api/chat is not gated.
app.config["prompt_budget"] = 10000
mp.save_config(app.config)
port = 18472
server = mp.ThreadingHTTPServer(("127.0.0.1", port), mp.Handler)
server.app = app
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.3)
BASE = f"http://127.0.0.1:{port}"


def get(path):
    with urllib.request.urlopen(BASE + path) as r:
        return json.loads(r.read())


def post(path, body, expect=200):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


a = get("/api/auth")
check("GET /api/auth shape, no secret", "auth" in a and "test-api-key" not in json.dumps(a))
m = get("/api/models")
check("GET /api/models returns API list", [x["id"] for x in m["models"]] == ["muse-api-1", "muse-api-2"], str(m))
d = get("/api/models/muse-test-a")
check("GET /api/models/<id> detail", d["default_reasoning_effort"] == "medium", str(d))

# Streaming timing: fake CLI emits text at ~0.4s and ~1.2s, total ~1.2s.
conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
conn.request("POST", "/api/chat", body=json.dumps({"prompt": "stream test", "force": True}),
             headers={"Content-Type": "application/json"})
resp = conn.getresponse()
check("SSE content type", resp.getheader("Content-Type") == "text/event-stream", str(resp.getheader("Content-Type")))
t0 = time.time()
first_text_at, done_at, chunks = None, None, ""
while True:
    data = resp.read(64)
    if not data:
        done_at = time.time()
        break
    chunks += data.decode("utf-8", "ignore")
    if first_text_at is None and "Hello " in chunks:
        first_text_at = time.time()
check("SSE first text arrives before stream ends",
      first_text_at is not None and done_at is not None and first_text_at < done_at - 0.15,
      f"first={first_text_at - t0 if first_text_at else None:.2f}s total={done_at - t0:.2f}s" if done_at else "no data")
check("SSE full text present", "Hello " in chunks and "world" in chunks and '"kind": "done"' in chunks)

# Key save via HTTP triggers a model query and never returns the key.
st, body = post("/api/auth/key", {"key": "http-key-456"})
check("POST /api/auth/key succeeds on Linux", st == 200, f"{st} {body[:120]}")
check("POST /api/auth/key response has models, no secret",
      b"muse-api-1" in body and b"http-key-456" not in body, body[:200].decode("utf-8", "ignore"))
req = urllib.request.Request(BASE + "/api/auth/key", method="DELETE")
with urllib.request.urlopen(req) as r:
    check("DELETE /api/auth/key succeeds", r.status == 200)
check("key file removed after DELETE", not os.path.isfile(mp.KEY_FILE))
server.shutdown()
api_srv.shutdown()

# ---------------------------------------------------------- 6. voice + settings UI presence
html = mp.UI_HTML
for needle, name in [
    ('id="mic"', "mic button present"),
    ("SpeechRecognition", "Web Speech API used"),
    ("stopVoiceAndFlush", "Send auto-stops voice"),
    ('id="settings"', "Settings panel present"),
    ('id="settings-model"', "Settings model select present"),
    ('id="settings-apikey"', "Settings API key field present"),
    ("voice-to-text", "voice-to-text wording present"),
]:
    check("UI: " + name, needle in html)
# sendChat must stop voice BEFORE reading the prompt value.
sendchat = html[html.index("async function sendChat"):html.index("async function queuePrompt")]
check("UI: sendChat stops voice before reading prompt",
      sendchat.index("stopVoiceAndFlush") < sendchat.index("$('prompt').value"))
# No textarea-locked transcript: transcript target is the editable prompt box.
check("UI: transcript target is prompt textarea", "$('prompt').value = voiceBase" in html)

# ---------------------------------------------------------- 7. real CLI sanity (if present)
real_bin = os.path.expanduser("~/.local/bin/muse")
if os.path.isfile(real_bin):
    real_cli = mp.MuseCLI()
    # Force the real binary (fakebin is first on PATH in this test).
    real_cli.binary = real_bin
    real_cli._options_cache = None
    ropts = real_cli.discover_options(force=True)
    check("real CLI: javascript is NOT a model",
          "javascript" not in [m["id"] for m in ropts["models"]], str(ropts["models"]))
    check("real CLI: efforts are the 8 live tiers",
          ropts["reasoning_efforts"] == ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
          str(ropts["reasoning_efforts"]))
    check("real CLI: no context flag in 1.4.10", ropts["context_flag"] is None, str(ropts["context_flag"]))

print()
failed = [n for n, ok, _ in results if not ok]
print(f"{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
