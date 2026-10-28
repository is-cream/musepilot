#!/usr/bin/env python3
"""v0.4.1 tests for musepilot.py — Baseten provider radio, fixed model,
selectable reasoning effort, fixed context, and persisted Settings.

Run with an isolated HOME, e.g.:
  HOME=/tmp/mptest-v041-home python3 test_musepilot_v041.py

No real API keys are used anywhere in this file: Baseten chats are
tested against a local fake SSE server, with a fake key.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.expanduser("~/workspace/musepilot"))
import musepilot as mp  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


os.environ.pop("META_API_KEY", None)
os.environ.pop("BASETEN_API_KEY", None)

# --- constants: model hardcoded by owner instruction, key never hardcoded
check("Baseten model constant is the hackathon model",
      mp.BASETEN_MODEL == "meta-models/muse-spark-open-1026")
check("Baseten efforts are the documented Muse Spark Open levels",
      mp.BASETEN_EFFORTS == ["low", "medium", "high", "xhigh"])
check("version is 0.4.1", mp.VERSION == "0.4.1")
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "musepilot.py")).read()
check("source contains no real-looking Baseten key value",
      "b10_" not in src.replace("b10_…", "").replace("b10_...", ""))

# --- App with isolated HOME, provider baseten, NO key yet: context unknown, not guessed
app = mp.App()
app.config["provider"] = "baseten"
mp.save_config(app.config)
check("config persists provider baseten",
      mp.load_config().get("provider") == "baseten")
models = app.query_models(force=True)
check("Baseten model list is exactly one fixed model",
      [m["id"] for m in models["models"]] == [mp.BASETEN_MODEL], str(models["models"]))
check("Baseten model list without key has unknown context, not a guessed constant",
      models["models"][0]["context_window"] is None and models["models_error"] is not None,
      str(models))
check("Baseten model source is not Meta",
      "Meta" not in models["model_source"], models["model_source"])

# --- separate key slots, never mixed, never in status
ok, _ = app.auth.save_api_key("fake-baseten-key-123", provider="baseten")
check("Baseten key saves (file slot, isolated HOME)", ok)
ok2, _ = app.auth.save_api_key("fake-meta-key-456", provider="meta")
check("Meta key saves separately", ok2)
check("get_baseten_key returns Baseten key, not Meta",
      app.auth.get_baseten_key() == "fake-baseten-key-123")
check("get_api_key (meta) returns Meta key, not Baseten",
      app.auth.get_api_key() == "fake-meta-key-456")
st = app.auth.status()
check("auth status never contains a key value",
      "fake-baseten-key-123" not in json.dumps(st) and "fake-meta-key-456" not in json.dumps(st))
check("auth status reports Baseten key present", st.get("baseten_key") is True)
okd, _ = app.auth.delete_api_key(provider="baseten")
check("deleting Baseten key does not delete Meta key",
      okd and app.auth.get_api_key() == "fake-meta-key-456" and app.auth.get_baseten_key() is None)
app.auth.save_api_key("fake-baseten-key-123", provider="baseten")

# --- Baseten query prepopulation: fake /v1/models server (context 999999 proves live query, not constant)
class FakeModels(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            body = {"data": [{"id": mp.BASETEN_MODEL, "context_length": 999999,
                              "max_completion_tokens": 888888,
                              "supported_features": ["tools", "reasoning"]}]}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())
        else:
            self.send_response(404)
            self.end_headers()
msrv = ThreadingHTTPServer(("127.0.0.1", 0), FakeModels)
mport = msrv.server_address[1]
threading.Thread(target=msrv.serve_forever, daemon=True).start()
mp.BASETEN_API_BASE = f"http://127.0.0.1:{mport}/v1"
app._baseten_model_cache = None
models_q = app.query_models(force=True)
check("Baseten model list context comes from live /v1/models query",
      models_q["models"][0]["context_window"] == 999999, str(models_q["models"]))
detail = app.model_detail(mp.BASETEN_MODEL)
check("Baseten detail offers the 4 documented efforts",
      detail["reasoning_efforts"] == mp.BASETEN_EFFORTS, str(detail["reasoning_efforts"]))
check("Baseten detail default effort is xhigh",
      detail["default_reasoning_effort"] == "xhigh")
check("Baseten detail context flag is None (not settable)",
      detail["context_flag"] is None)
check("Baseten detail context comes from live query, not constant",
      detail["context_window"] == 999999, str(detail["context_window"]))
check("Baseten detail note says context is not a setting",
      "NOT a per-request setting" in detail["context_note"])
msrv.shutdown()

# --- config persistence for BOTH providers
app.config["provider"] = "baseten"
app.config["baseten_effort"] = "low"
app.config["default_model"] = "muse-spark-1.3"
app.config["default_effort"] = "high"
app.config["default_context_window"] = 262144
mp.save_config(app.config)
reloaded = mp.load_config()
check("reloaded config keeps Baseten effort",
      reloaded.get("baseten_effort") == "low")
check("reloaded config keeps Meta model and effort",
      reloaded.get("default_model") == "muse-spark-1.3" and reloaded.get("default_effort") == "high")
check("reloaded config keeps context window value",
      reloaded.get("default_context_window") == 262144)
check("reloaded config keeps provider",
      reloaded.get("provider") == "baseten")

# --- fake Baseten SSE server, then run_baseten_chat
seen = {}


class FakeBaseten(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode() or "{}")
        seen["body"] = body
        seen["auth"] = self.headers.get("Authorization")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunks = [
            {"choices": [{"delta": {"content": "Hello "}}]},
            {"choices": [{"delta": {"content": "Baseten"}}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
        ]
        for c in chunks:
            self.wfile.write(("data: " + json.dumps(c) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeBaseten)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
mp.BASETEN_API_BASE = f"http://127.0.0.1:{port}/v1"
events = []
result = app.run_baseten_chat("Say hello", effort="low", on_event=lambda k, p: events.append((k, p)))
srv.shutdown()
check("Baseten chat returns streamed text",
      result["text"] == "Hello Baseten", result["text"])
check("Baseten chat sends fixed model",
      seen["body"].get("model") == mp.BASETEN_MODEL, str(seen["body"].get("model")))
check("Baseten chat sends selected effort",
      seen["body"].get("reasoning_effort") == "low", str(seen["body"].get("reasoning_effort")))
check("Baseten chat does NOT send context_window",
      "context_window" not in seen["body"])
check("Baseten chat sends Bearer key, key not in body",
      seen["auth"] == "Bearer fake-baseten-key-123" and "fake-baseten-key-123" not in json.dumps(seen["body"]))
check("Baseten chat usage from stream",
      result["usage"]["total_tokens"] == 15, str(result["usage"]))
check("Baseten chat emits text events",
      any(k == "text" for k, _ in events))

# --- UI presence
check("Settings has Meta/Baseten radio",
      'name="provider"' in src and 'value="baseten"' in src)
check("Settings has Baseten key field",
      'id="settings-basetenkey"' in src)
check("JS saves effort to config on change",
      "saveEffort" in src and "baseten_effort" in src)
check("JS saves context to config on change",
      "saveContext" in src and "default_context_window" in src)
check("JS currentSelections includes provider",
      "provider:" in src and "currentSelections" in src)

failed = [r for r in results if not r[1]]
print(f"\n{len(results)-len(failed)}/{len(results)} passed")
if failed:
    sys.exit(1)
