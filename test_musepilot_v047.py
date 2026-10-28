#!/usr/bin/env python3
"""v0.4.7 tests — Claude + Muse session memory, saved conversation files.

Run: HOME=/tmp/mptest-v047-home python3 test_musepilot_v047.py
Fake `claude` emits stream-json lines and logs its argv; no real keys used.
"""
import json
import os
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, "/home/hatch/workspace/musepilot")
import musepilot as mp  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


os.environ.pop("META_API_KEY", None)
os.environ.pop("BASETEN_API_KEY", None)
os.environ["PATH"] = "/usr/bin:/bin"
check("version is 0.4.7", mp.VERSION == "0.4.7")
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "musepilot.py")).read()
check("UI restores saved conversation", "restoreConversation" in src and "/api/conversation" in src)
check("UI New conversation calls server", "/api/conversation/new" in src)
check("UI shows saved file", "convinfo" in src)
check("rotation limit is 1 MiB", mp.MAX_CONVERSATION_FILE_BYTES == 1048576)

home = os.environ["HOME"]
bin_dir = os.path.join(home, ".local", "bin")
os.makedirs(bin_dir, exist_ok=True)
argv_log = os.path.join(home, "claude-argv.jsonl")
fake = os.path.join(bin_dir, "claude")
normal_script = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_ARGV_LOG"], "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
print(json.dumps({"type": "system", "subtype": "init", "model": "meta-models/muse-spark-open-1026"}))
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello memory"}]}}))
print(json.dumps({"type": "result", "result": "Hello memory", "usage": {"input_tokens": 10, "cache_read_input_tokens": 0, "output_tokens": 5}}))
"""
os.environ["FAKE_ARGV_LOG"] = argv_log
open(fake, "w").write(normal_script)
os.chmod(fake, 0o755)


def read_argv():
    # find_cli_binary also runs `claude --version` for verification; only
    # the real -p runs count as chat invocations.
    if not os.path.exists(argv_log):
        return []
    return [json.loads(line) for line in open(argv_log) if line.strip() and json.loads(line)[:1] == ["-p"]]


app = mp.App()
app.auth.get_baseten_key = lambda: "fake-baseten-key"

# First Claude run: --session-id UUID, no --resume
r1 = app.run_claude_chat("first prompt", effort="low", workdir=home, auto_approve=False)
check("first Claude run ok", r1["ok"] and r1["text"] == "Hello memory", str(r1))
argv1 = read_argv()
check("first Claude run has argv", len(argv1) == 1, str(argv1))
sid1 = app.config.get("claude_session_id")
check("first run stores a valid UUID session", mp.is_valid_uuid(sid1), str(sid1))
check("first run uses --session-id, not --resume",
      "--session-id" in argv1[0] and argv1[0][argv1[0].index("--session-id") + 1] == sid1 and "--resume" not in argv1[0], str(argv1[0]))
check("allowedTools stays last", argv1[0][-2] == "--allowedTools", str(argv1[0]))
check("session started flag set", app.config.get("claude_session_started") is True)

# Second Claude run: --resume same UUID
r2 = app.run_claude_chat("are you sure?", effort="low", workdir=home, auto_approve=False)
check("second Claude run ok", r2["ok"], str(r2))
argv2 = read_argv()
check("second run uses --resume same UUID",
      len(argv2) == 2 and "--resume" in argv2[1] and argv2[1][argv2[1].index("--resume") + 1] == sid1 and "--session-id" not in argv2[1], str(argv2[1] if len(argv2) > 1 else argv2))

# Transcript saved by the handler path: App-level append used by server/queue.
# run_claude_chat itself does not append; simulate the server append here
# exactly as the handler does, then verify the file.
app.append_conversation("claude", "first prompt", r1["text"], model=r1.get("model"), effort="low")
app.append_conversation("claude", "are you sure?", r2["text"], model=r2.get("model"), effort="low")
conv = app.load_conversation()
check("transcript file exists", os.path.exists(conv["file"]), conv["file"])
check("transcript has 2 exchanges", len(conv["messages"]) == 2, str(conv["messages"]))
check("transcript prompts preserved", [m["prompt"] for m in conv["messages"]] == ["first prompt", "are you sure?"])

# Muse session: stable UUID, changes on new conversation
muse_sid = app.get_muse_session_id()
check("Muse session is a valid UUID", mp.is_valid_uuid(muse_sid), str(muse_sid))
check("Muse session stable", app.get_muse_session_id() == muse_sid)

# New conversation resets both sessions
old_cid = app.config["conversation_id"]
newc = app.new_conversation()
check("new conversation increments id", newc["conversation_id"] == old_cid + 1, str(newc))
check("new conversation clears Claude session", app.config.get("claude_session_id") is None and app.config.get("claude_session_started") is False)
check("new conversation clears Muse session", app.config.get("muse_session_id") is None)
r3 = app.run_claude_chat("fresh start", effort="low", workdir=home, auto_approve=False)
argv3 = read_argv()
sid3 = app.config.get("claude_session_id")
check("after new conversation, fresh --session-id UUID",
      mp.is_valid_uuid(sid3) and sid3 != sid1 and "--session-id" in argv3[-1] and "--resume" not in argv3[-1], str(argv3[-1] if argv3 else []))

# Resume failure fallback: fake that fails on --resume, succeeds otherwise
fail_script = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_ARGV_LOG"], "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
if "--resume" in sys.argv:
    print(json.dumps({"type": "result", "result": "session not found", "is_error": True, "usage": {}}))
else:
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Recovered"}]}}))
    print(json.dumps({"type": "result", "result": "Recovered", "usage": {"input_tokens": 1, "output_tokens": 1}}))
"""
open(fake, "w").write(fail_script)
os.chmod(fake, 0o755)
# Force a started session with a known id, then run: resume must fail, retry must succeed
app.config["claude_session_id"] = "11111111-2222-3333-4444-555555555555"
app.config["claude_session_started"] = True
mp.save_config(app.config)
before = len(read_argv())
r4 = app.run_claude_chat("after storage cleared", effort="low", workdir=home, auto_approve=False)
after_argv = read_argv()[before:]
check("resume failure recovers with fresh session", r4["ok"] and r4["text"] == "Recovered", str(r4))
check("resume failure made 2 calls (resume, then fresh session-id)",
      len(after_argv) == 2 and "--resume" in after_argv[0] and "--session-id" in after_argv[1], str(after_argv))
check("config session id replaced after fallback", app.config.get("claude_session_id") != "11111111-2222-3333-4444-555555555555")

# Rotation: small limit, old file kept, new file started
mp.MAX_CONVERSATION_FILE_BYTES = 200
# Seed the current file first: rotation only applies to a non-empty file,
# otherwise the first entry would skip a file number with an empty file.
app.append_conversation("claude", "seed", "seed reply")
cid_before = app.config["conversation_id"]
app.append_conversation("claude", "big prompt " + ("x" * 500), "big reply " + ("y" * 500))
cid_after = app.config["conversation_id"]
check("rotation starts a new file above limit", cid_after == cid_before + 1, f"{cid_before} -> {cid_after}")
check("old conversation file kept", os.path.exists(mp.conversation_file_path(cid_before)))
check("new conversation file exists", os.path.exists(mp.conversation_file_path(cid_after)))
mp.MAX_CONVERSATION_FILE_BYTES = 1048576

# Server smoke: GET /api/conversation and POST /api/conversation/new
server = ThreadingHTTPServer(("127.0.0.1", 0), mp.Handler)
server.app = app
port = server.server_address[1]
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/conversation") as resp:
        data = json.loads(resp.read())
    check("server GET /api/conversation", data["conversation_id"] == app.config["conversation_id"] and "messages" in data, str(data)[:200])
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/conversation/new", method="POST", data=b"{}",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        data2 = json.loads(resp.read())
    check("server POST /api/conversation/new", data2["conversation_id"] == app.config["conversation_id"], str(data2))
finally:
    server.shutdown()
    server.server_close()

failed = [r for r in results if not r[1]]
print(f"\n{len(results)-len(failed)}/{len(results)} passed")
if failed:
    sys.exit(1)
