#!/usr/bin/env python3
"""v0.5.0 tests — Stop button, queue Send-now, in-progress state, conversations list.

Covers:
  - POST /api/stop (idle -> stopped:false; lock held -> stopped:true + event set)
  - driver.stop() terminates a real in-flight subprocess run
  - POST /api/queue/<id>/send-now (404 unknown; 200 streams fake run; empties queue;
    stops in-flight run first; budget refusal re-queues)
  - GET /api/conversations (lists both conversations, current flag, previews)
  - POST /api/conversations/<id>/open (switches, returns transcript,
    per-conversation CLI session ids)
  - frontend JS contains the new UI (working indicator, Stop/Queue buttons,
    Send now, conversations list, busy poll)

Run: HOME=/tmp/mptest-v050-home python3 test_musepilot_v050.py
"""
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

sys.path.insert(0, "/home/hatch/workspace/musepilot")
import musepilot as mp  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


check("version is 0.5.0", mp.VERSION == "0.5.0")

app = mp.App()
server = ThreadingHTTPServer(("127.0.0.1", 0), mp.Handler)
server.app = app
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
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


# ------------------------------------------------- 1. /api/stop idle
st, body = post("/api/stop", {})
j = json.loads(body)
check("POST /api/stop idle -> 200", st == 200, f"{st}")
check("stop idle: stopped=false busy=false",
      j.get("stopped") is False and j.get("busy") is False, str(j))

# ------------------------------------------------- 2. stop with lock held (simulated run)
app.driver._run_lock.acquire()
try:
    st, body = post("/api/stop", {})
    j = json.loads(body)
    check("stop with lock held -> stopped=true", j.get("stopped") is True, str(j))
    check("stop sets the stop event", app.driver._stop_requested.is_set())
finally:
    app.driver._run_lock.release()
app.driver._stop_requested.clear()

# ------------------------------------------------- 3. driver.stop() kills a real subprocess
# Use a fake CLI whose exec sleeps: build_command needs a real binary, so
# monkeypatch build_command to run `sleep 30` and watch stop() kill it.
orig_build = app.driver.build_command
app.driver.build_command = lambda *a, **k: ["sleep", "30"]
app.driver.cli.binary = "/bin/sleep"  # satisfy the cli.available gate
run_result = {}


def slow_run():
    try:
        run_result["res"] = app.driver.run("slow prompt", session_id="x" * 8)
    except Exception as e:  # noqa: BLE001
        run_result["err"] = str(e)


t = threading.Thread(target=slow_run, daemon=True)
t.start()
time.sleep(1.0)  # let the subprocess spawn
check("slow run holds the lock", app.driver._run_lock.locked())
check("slow run has an active proc",
      app.driver.active_proc is not None and app.driver.active_proc.poll() is None)
st, body = post("/api/stop", {})
t.join(timeout=15)
res = run_result.get("res", {})
check("stopped run finishes promptly", not t.is_alive(), "thread still alive")
check("stopped run reports user stop", res.get("error") == "Run stopped by user.",
      str(res.get("error")))
check("lock released after stop", not app.driver._run_lock.locked())
check("active_proc cleared after stop", app.driver.active_proc is None)
app.driver.build_command = orig_build
app.driver._stop_requested.clear()

# ------------------------------------------------- 4. conversations list + open
# conversation 1 already exists (id 1); add an exchange, start a new one.
app.append_conversation("meta", "first prompt about charts", "chart answer here",
                        model="muse-spark-1.3", effort="low")
sid1 = app.get_muse_session_id()
app.new_conversation()
app.append_conversation("meta", "second prompt about tables", "table answer",
                        model="muse-spark-1.3", effort="low")
sid2 = app.get_muse_session_id()
check("per-conversation muse session ids differ", sid1 != sid2, f"{sid1} vs {sid2}")

convs = get("/api/conversations")["conversations"]
check("GET /api/conversations lists 2", len(convs) == 2, str(len(convs)))
c1 = next(c for c in convs if c["id"] == 1)
c2 = next(c for c in convs if c["id"] == 2)
check("conversation 1 preview", c1["preview"] == "first prompt about charts", c1["preview"])
check("conversation 1 not current", c1["current"] is False)
check("conversation 2 current", c2["current"] is True and c2["messages"] == 1)

opened = post("/api/conversations/1/open", {})
check("POST /api/conversations/1/open -> 200", opened[0] == 200, str(opened[0]))
oj = json.loads(opened[1])
check("open returns conversation 1 transcript",
      oj["conversation_id"] == 1 and len(oj["messages"]) == 1
      and oj["messages"][0]["prompt"] == "first prompt about charts",
      str(oj)[:200])
check("open restores conversation 1 session id", app.get_muse_session_id() == sid1,
      app.get_muse_session_id())
check("current conversation is now 1",
      next(c for c in get("/api/conversations")["conversations"] if c["id"] == 1)["current"] is True)

st, _ = post("/api/conversations/999/open", {})
check("open unknown conversation -> 404", st == 404, f"{st}")

# ------------------------------------------------- 5. send-now
def fake_run(prompt, **kwargs):
    on_event = kwargs.get("on_event")
    result = {"run_id": "fake123", "ok": True, "error": None, "text": "Fake send-now reply",
              "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
              "model": "muse-spark-1.3", "effort": "low", "elapsed": 0.1}
    if on_event:
        on_event("event", {"run_id": "fake123"})
        on_event("text", {"text": "Fake send-now reply"})
        on_event("done", result)
    return result


app.driver.run = fake_run
app.config["provider"] = "meta"
# budget: make the window exhausted to test the re-queue path first
app.ledger.can_invoke = lambda budget: (False, {"prompts": budget, "total_tokens": 0,
                                                "next_capacity_at": None})
qitem = app.queue.add({"prompt": "queued send-now task", "provider": "meta",
                       "model": "muse-spark-1.3", "effort": "low"})
st, body = post(f"/api/queue/{qitem['id']}/send-now", {})
check("send-now with exhausted budget -> 429", st == 429, f"{st}")
check("budget refusal re-queues the item", len(app.queue.list()) == 1,
      str(app.queue.list()))
# remove the re-queued copy (it got a new id), re-add fresh
for it in app.queue.list():
    app.queue.remove(it["id"])
qitem = app.queue.add({"prompt": "queued send-now task", "provider": "meta",
                       "model": "muse-spark-1.3", "effort": "low"})

st, body = post(f"/api/queue/{qitem['id']}/send-now", {"force": True})
text = body.decode()
check("send-now forced -> 200", st == 200, f"{st} {text[:150]}")
check("send-now streams fake text", "Fake send-now reply" in text, text[:200])
check("send-now stream ends with done", '"kind": "done"' in text)
check("send-now empties the queue", len(app.queue.list()) == 0, str(app.queue.list()))
conv = app.load_conversation()
check("send-now appended to current conversation",
      conv["messages"] and conv["messages"][-1]["prompt"] == "queued send-now task",
      str(conv["messages"][-1]["prompt"] if conv["messages"] else None))

st, _ = post("/api/queue/deadbeef1234/send-now", {})
check("send-now unknown id -> 404", st == 404, f"{st}")

# send-now while a run is in flight: hold the lock, release after 1s,
# send-now must stop, wait, then run.
app.driver._run_lock.acquire()
def release_soon():
    time.sleep(1.0)
    app.driver._stop_requested.clear()  # not actually needed; release is what matters
    app.driver._run_lock.release()
threading.Thread(target=release_soon, daemon=True).start()
qitem2 = app.queue.add({"prompt": "send-now during run", "provider": "meta", "effort": "low"})
t0 = time.time()
st, body = post(f"/api/queue/{qitem2['id']}/send-now", {"force": True})
elapsed = time.time() - t0
check("send-now during run waits for stop then runs", st == 200 and elapsed < 20,
      f"{st} elapsed={elapsed:.1f}s")

# ------------------------------------------------- 6. frontend JS contains the new UI
js_src = open("/home/hatch/workspace/musepilot/musepilot.py").read()
for name, needle in [
    ("working indicator", "\\u25cf working\\u2026"),  # JS escape for "● working…"
    ("Stop button wiring", "/api/stop"),
    ("Queue-while-running button", "queuePromptText(prompt, sel)"),
    ("Send now button", "Send now"),
    ("send-now endpoint call", "/send-now"),
    ("conversations list UI", "refreshConversations"),
    ("open conversation", "openConversation"),
    ("busy poll", "pollBusy"),
    ("busypill", "busypill"),
    ("run controls CSS", ".runctrl"),
]:
    check(f"frontend has {name}", needle in js_src, needle)

server.shutdown()

print()
failed = [n for n, ok, _ in results if not ok]
print(f"{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
