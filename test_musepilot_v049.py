#!/usr/bin/env python3
"""v0.4.9 tests — 5-hour budget gate is Muse Code only; Run anyway works.

Run: HOME=/tmp/mptest-v049-home python3 test_musepilot_v049.py
"""
import json
import os
import sys
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

sys.path.insert(0, "/home/hatch/workspace/musepilot")
import musepilot as mp  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


check("version is 0.4.9", mp.VERSION == "0.4.9")
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "musepilot.py")).read()
check("Run anyway passes original prompt",
      "sendChat(true, prompt)" in src and "promptOverride" in src)
check("chat gate is provider-aware", 'chat_provider != "meta"' in src)
check("queue gate is provider-aware", 'qprovider != "meta"' in src)

app = mp.App()
# Exhaust the Muse window: can_invoke always False
app.ledger.can_invoke = lambda budget: (False, {"prompts": budget, "total_tokens": 0, "next_capacity_at": None})


def fake_result(text):
    return {"run_id": "x", "ok": True, "error": None, "text": text,
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            "model": "muse-spark-open-1026", "effort": "low", "elapsed": 0.1}


def fake_claude(prompt, effort=None, workdir=None, auto_approve=False, on_event=None, attachments=None):
    r = fake_result("claude ran despite muse budget")
    if on_event:
        on_event("done", r)
    return r


def fake_baseten(prompt, effort=None, on_event=None, attachments=None):
    r = fake_result("baseten ran despite muse budget")
    if on_event:
        on_event("done", r)
    return r


app.run_claude_chat = fake_claude
app.run_baseten_chat = fake_baseten

server = ThreadingHTTPServer(("127.0.0.1", 0), mp.Handler)
server.app = app
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()


def post_chat(provider, force=False):
    body = json.dumps({"prompt": "hello", "provider": provider, "force": force,
                       "effort": "low", "workdir": os.environ["HOME"]}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/chat", method="POST", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


try:
    status, body = post_chat("claude")
    check("Claude chat bypasses muse budget (not 429)", status == 200, f"{status} {body[:200]}")
    status, body = post_chat("baseten")
    check("Baseten chat bypasses muse budget (not 429)", status == 200, f"{status} {body[:200]}")
    status, body = post_chat("meta")
    check("Muse chat still gated without force (429)", status == 429, f"{status} {body[:200]}")
    check("Muse 429 message mentions budget", "5-hour window budget" in body, body[:200])
    status, body = post_chat("meta", force=True)
    # Muse driver.run is real here; force bypasses the gate, so it must NOT be 429
    # (it may fail later on the missing muse CLI, which is not a gate failure)
    check("Muse chat with force bypasses gate (not 429)", status != 429, f"{status} {body[:200]}")
finally:
    server.shutdown()
    server.server_close()

# QueueWorker: Claude item runs despite exhausted Muse window; Muse item requeues
app.queue.add({"prompt": "claude queued", "provider": "claude", "effort": "low"})
app.queue.add({"prompt": "muse queued", "provider": "meta", "effort": "low"})
worker = mp.QueueWorker(app, interval=9999)
worker.tick()  # Claude item first
items = app.queue.list()
check("queued Claude item ran (not left queued)",
      not any(it["prompt"] == "claude queued" and it["status"] == "queued" for it in items),
      str(items))
worker.tick()  # Muse item next
items = app.queue.list()
check("queued Muse item stays queued while budget exhausted",
      any(it["prompt"] == "muse queued" and it["status"] == "queued" for it in items),
      str(items))

failed = [r for r in results if not r[1]]
print(f"\n{len(results)-len(failed)}/{len(results)} passed")
if failed:
    sys.exit(1)
