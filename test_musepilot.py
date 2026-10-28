#!/usr/bin/env python3
"""End-to-end tests for musepilot.py against the fake `muse` CLI.

Run:  HOME=/tmp/mptest-home PATH=/tmp/fakebin:$PATH python3 test_musepilot.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.expanduser("~/workspace/musepilot"))
import musepilot as mp  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{PASS if cond else FAIL}] {name}" + (f" — {detail}" if detail and not cond else ""))


FAKE_HOME = os.environ["HOME"]
BIN = os.path.join(FAKE_HOME, ".musepilot")  # noqa: F841

# ---------------------------------------------------------------- 1. discovery
cli = mp.MuseCLI()
check("fake muse binary found", cli.available, cli.binary)
check("version detected", (cli.version() or "").startswith("muse-bin"))

opts = cli.discover_options(force=True)
check("models discovered from schema",
      [m["id"] for m in opts["models"]] == ["muse-spark-1.3", "muse-wonder-1.0", "muse-wonder-full-1.0"],
      str([m["id"] for m in opts["models"]]))
check("model source is live", opts["model_source"] == "muse schema (live)", opts["model_source"])
check("efforts discovered (8 tiers)",
      opts["reasoning_efforts"] == ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
      str(opts["reasoning_efforts"]))
check("effort source is live", opts["effort_source"] == "muse schema (live)")
check("supports --model", opts["supports_model_flag"])
check("supports --reasoning-effort", opts["supports_effort_flag"])
check("context flag detected", opts["context_flag"] == "--context-window", str(opts["context_flag"]))
check("supports --prompt-file", opts["supports_prompt_file"])
check("supports --session-id", opts["supports_session_id"])
check("supports --max-model-steps", opts["supports_max_steps"])
check("supports --yolo", opts["supports_yolo"])

# ------------------------------------------------- 2. command construction
driver = mp.ExecDriver(cli, mp.UsageLedger())
cmd = driver.build_command("hello", model="muse-wonder-1.0", effort="ultra",
                           context_window=524288, max_steps=25,
                           session_id="abc123", auto_approve=True,
                           prompt_file="/tmp/p.md")
for flag in ["--model", "muse-wonder-1.0", "--reasoning-effort", "ultra",
             "--context-window", "524288", "--max-model-steps", "25",
             "--session-id", "abc123", "--yolo", "--prompt-file", "/tmp/p.md", "--json"]:
    check(f"argv contains {flag}", flag in cmd, str(cmd))
check("no shell used (argv list)", isinstance(cmd, list) and cmd[0] == cli.binary)
check("prompt not passed positionally when prompt-file used",
      "hello" not in cmd, str(cmd))

# ------------------------------------------------- 3. run + streaming + ledger
seen = []
res = driver.run("write a haiku about tests", model="muse-wonder-1.0",
                 effort="medium", max_steps=10, session_id="sess1",
                 on_event=lambda k, p: seen.append((k, p)))
check("run ok", res["ok"], str(res.get("error")))
check("text streamed", "Fake reply to:" in res["text"], res["text"][:80])
check("usage parsed", res["usage"]["total_tokens"] == 1500, str(res["usage"]))
check("saw text events", any(k == "text" for k, _ in seen))
check("saw tool event", any(k == "tool" for k, _ in seen))
check("saw usage event", any(k == "usage" for k, _ in seen))
check("saw done event", any(k == "done" for k, _ in seen))

ledger = mp.UsageLedger()
usage = ledger.window_usage()
check("ledger recorded 1 prompt", usage["prompts"] == 1, str(usage))
check("ledger tokens", usage["total_tokens"] == 1500, str(usage))

# ------------------------------------------------- 4. rolling window math
# add an entry 6h ago (should be outside the window) and one 1h ago
ledger.record({"ts": time.time() - 6 * 3600, "total_tokens": 9999})
ledger.record({"ts": time.time() - 1 * 3600, "total_tokens": 100})
usage = ledger.window_usage()
check("6h-old entry aged out", usage["prompts"] == 2 and usage["total_tokens"] == 1600, str(usage))
check("next_capacity_at set", usage["next_capacity_at"] is not None)
ok, _ = ledger.can_invoke(2)
check("can_invoke false at budget", not ok)
ok, _ = ledger.can_invoke(3)
check("can_invoke true under budget", ok)

# ------------------------------------------------- 5. queue gating
q = mp.PromptQueue()
q.add({"prompt": "queued task one", "model": "muse-spark-1.3", "effort": "low"})
check("queue has 1 item", len(q.list()) == 1)

app = mp.App()
app.config["prompt_budget"] = 2  # window currently has 2 prompts -> exhausted
worker = mp.QueueWorker(app, interval=999)
worker.tick()
check("exhausted window: item stays queued",
      len(app.queue.list()) == 1 and app.queue.list()[0]["status"] == "queued",
      str(app.queue.list()))

# free the window by rewriting the ledger with only old entries
with open(mp.USAGE_FILE, "w") as fh:
    fh.write(json.dumps({"ts": time.time() - 6 * 3600, "total_tokens": 5}) + "\n")
worker.tick()
time.sleep(0.5)
check("freed window: queue drained", len(app.queue.list()) == 0, str(app.queue.list()))
runs = mp.list_run_records()
check("queued run produced a record", len(runs) >= 1, str(len(runs)))
check("queued run done", runs[0]["status"] == "done", str(runs[0].get("status")))

# ------------------------------------------------- 5b. auth
saved_env_key = os.environ.pop("META_API_KEY", None)
auth = mp.AuthManager(cli)
st = auth.status()
check("auth status missing when no credential", not st["ok"] and not st["env_key"]
      and not st["stored_credential"] and not st["keychain_key"], str(st))
check("keychain unavailable on linux", not auth.keychain_available())
ok, msg = auth.keychain_set("secret")
check("keychain_set degrades gracefully", not ok, msg)
check("keychain_has_key false", not auth.keychain_has_key())
check("keychain_get none", auth.keychain_get() is None)

# stored credential file -> detected; CLI probe reports account
cfgdir = os.path.join(FAKE_HOME, ".config", "muse")
os.makedirs(cfgdir, exist_ok=True)
with open(os.path.join(cfgdir, "auth.json"), "w") as fh:
    json.dump({"user": {"email": "fileuser@example.com"}, "token": "sekrit"}, fh)
st = auth.status()
check("stored credential detected", st["ok"] and st["stored_credential"], str(st))
acc = auth.account_info()
check("account email from CLI probe", acc["email"] == "dev@example.com", str(acc))
check("account tier from CLI probe", acc["tier"] == "power", str(acc))
check("account source labeled", "Stored credential" in (acc["source"] or ""), str(acc["source"]))
os.unlink(os.path.join(cfgdir, "auth.json"))

# credential-file fallback for email (no CLI probe hit)
with open(os.path.join(cfgdir, "auth.json"), "w") as fh:
    json.dump({"nested": {"accounts": [{"contactEmail": "fallback@example.org"}]}}, fh)
# hide the account command from probing by monkeypatching _top_commands
auth._top_commands = lambda: []
acc = auth.account_info()
check("account email from credential file fallback", acc["email"] == "fallback@example.org", str(acc))
os.unlink(os.path.join(cfgdir, "auth.json"))

# env key precedence
os.environ["META_API_KEY"] = "test-env-key"
st = auth.status()
check("env key detected", st["ok"] and st["env_key"], str(st))
acc = auth.account_info()
check("env key account source", "environment" in (acc["source"] or ""), str(acc))
env = auth.child_env()
check("child_env keeps env key", env.get("META_API_KEY") == "test-env-key")
del os.environ["META_API_KEY"]
env = auth.child_env()
check("child_env without key", "META_API_KEY" not in env)
if saved_env_key is not None:
    os.environ["META_API_KEY"] = saved_env_key

# driver injects META_API_KEY into the child process env
os.environ["META_API_KEY"] = "test-env-key"
open(os.environ.get("FAKE_MUSE_LOG", "/tmp/fake-muse.log"), "w").close()
driver2 = mp.ExecDriver(cli, mp.UsageLedger(), mp.AuthManager(cli))
driver2.run("env injection check", max_steps=5)
last = open(os.environ.get("FAKE_MUSE_LOG", "/tmp/fake-muse.log")).read().strip().splitlines()[-1]
check("muse child saw META_API_KEY", json.loads(last).get("meta_api_key_set") is True, last)
del os.environ["META_API_KEY"]
if saved_env_key is not None:
    os.environ["META_API_KEY"] = saved_env_key

# ------------------------------------------------- 6. HTTP API
port = 18471
server = mp.ThreadingHTTPServer(("127.0.0.1", port), mp.Handler)
server.app = app
import threading
t = threading.Thread(target=server.serve_forever, daemon=True)
t.start()
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


o = get("/api/options")
check("GET /api/options models", len(o["models"]) == 3)
w = get("/api/window")
check("GET /api/window", w["budget"] == 2 and "usage" in w, str(w.get("budget")))

# budget exhausted (2 prompts? currently ledger has 1 old -> window has 0; set budget 0 to force 429)
post_cfg = post("/api/config", {"prompt_budget": 0})
check("POST /api/config", post_cfg[0] == 200)
st, body = post("/api/chat", {"prompt": "should be refused"})
check("window exhausted -> 429", st == 429, f"status={st} body={body[:120]}")
st, body = post("/api/chat", {"prompt": "forced run", "force": True})
check("force=true bypasses gate", st == 200, f"status={st}")
text = body.decode()
check("SSE stream has text", "Fake reply to:" in text, text[:200])
check("SSE stream ends with done", '"kind": "done"' in text)

# queue via API
st, body = post("/api/queue", {"prompt": "api queued task"})
check("POST /api/queue", st == 200 and b"queued" in body)
qitems = get("/api/queue")
check("GET /api/queue lists item", len(qitems["items"]) == 1)
qid = qitems["items"][0]["id"]
req = urllib.request.Request(BASE + "/api/queue/" + qid, method="DELETE")
with urllib.request.urlopen(req) as r:
    check("DELETE /api/queue/:id", r.status == 200)
check("queue empty after delete", len(get("/api/queue")["items"]) == 0)

# auth endpoints
a = get("/api/auth")
check("GET /api/auth shape", "auth" in a and "account" in a, str(sorted(a.keys())))
check("GET /api/auth not signed in", not a["auth"]["ok"], str(a["auth"]))
st, body = post("/api/auth/key", {"key": "abc"})
check("POST /api/auth/key saves via file on linux (v0.3.0)", st == 200, f"status={st}")
check("POST /api/auth/key response never echoes key", b"abc" not in body, body[:200])
req = urllib.request.Request(BASE + "/api/auth/key", method="DELETE")
try:
    with urllib.request.urlopen(req) as r:
        st = r.status
except urllib.error.HTTPError as e:
    st = e.code
check("DELETE /api/auth/key removes file on linux (v0.3.0)", st == 200, f"status={st}")
st, body = post("/api/auth/logout", {})
check("POST /api/auth/logout", st == 200 and b"logged_out" in body, f"status={st} body={body[:100]}")

server.shutdown()

print()
failed = [n for n, ok, _ in results if not ok]
print(f"{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
