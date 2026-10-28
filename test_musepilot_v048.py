#!/usr/bin/env python3
"""v0.4.8 tests — file/image attachments passed to Claude + Muse.

Run: HOME=/tmp/mptest-v048-home python3 test_musepilot_v048.py
Fake claude/muse binaries log argv; no real keys used.
"""
import base64
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
check("version is 0.4.8", mp.VERSION == "0.4.8")
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "musepilot.py")).read()
check("UI has Attach button", 'id="attachbtn"' in src)
check("UI handles paste", "clipboardData" in src)
check("UI handles drop", "dataTransfer" in src)
check("UI uploads to /api/attachments", "/api/attachments" in src)
_refreshclis = src.split("async function refreshClis")[1].split("async function loadAll")[0]
_loadall = src.split("async function loadAll")[1].split("async function restoreConversation")[0]
check("restore is in loadAll, not Tools button",
      "restoreConversation" not in _refreshclis and "await restoreConversation();" in _loadall)
check("Muse supports --image flag in code", "supports_image_flag" in src)

home = os.environ["HOME"]
# save_attachment + sanitisation
att = mp.save_attachment("../evil.txt", "text/plain", b"hello file")
check("attachment saved under attachments dir", att["path"].startswith(mp.ATTACHMENTS_DIR) and os.path.exists(att["path"]), att["path"])
check("attachment name sanitised", att["name"] == "evil.txt" and ".." not in att["name"], att["name"])
check("attachment kind file", att["kind"] == "file")
img = mp.save_attachment("shot.png", "image/png", b"\x89PNG fake")
check("image kind detected", img["kind"] == "image")

# resolve_attachments drops escapes
bad = mp.resolve_attachments([{"path": "/etc/passwd", "name": "passwd"}])
check("resolve drops paths outside attachments dir", bad == [], str(bad))
good = mp.resolve_attachments([att, img])
check("resolve keeps valid attachments", len(good) == 2, str(good))

# Prompt suffix + Muse --image
suffix = mp.attachments_prompt_suffix(good)
check("prompt suffix lists both paths", att["path"] in suffix and img["path"] in suffix, suffix)
check("prompt suffix tells model to read image", "read the image" in suffix.lower(), suffix)

# Fake muse binary: log argv, check --image and prompt-file content
bin_dir = os.path.join(home, ".local", "bin")
os.makedirs(bin_dir, exist_ok=True)
muse_log = os.path.join(home, "muse-argv.jsonl")
fake_muse = os.path.join(bin_dir, "muse")
muse_script = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_MUSE_LOG"], "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
print(json.dumps({"type": "text", "text": "muse saw attachments"}))
"""
os.environ["FAKE_MUSE_LOG"] = muse_log
open(fake_muse, "w").write(muse_script)
os.chmod(fake_muse, 0o755)

app = mp.App()
# ExecDriver.build_command with a fake CLI that claims --image support
# Use the real driver but monkeypatch discover_options for this check only
orig_discover = app.cli.discover_options
app.cli.discover_options = lambda force=False: {**orig_discover(force=force), "supports_image_flag": True,
                                                "supports_model_flag": False, "supports_effort_flag": False,
                                                "context_flag": None, "supports_max_steps": False,
                                                "supports_session_id": True, "supports_yolo": True,
                                                "supports_prompt_file": True}
cmd = app.driver.build_command("hi", session_id="11111111-2222-3333-4444-555555555555",
                               image_paths=[img["path"]])
check("Muse build_command includes --image for image", "--image" in cmd and img["path"] in cmd, str(cmd))
cmd2 = app.driver.build_command("hi", session_id="11111111-2222-3333-4444-555555555555",
                                image_paths=[])
check("Muse build_command omits --image when none", "--image" not in cmd2, str(cmd2))
app.cli.discover_options = orig_discover

# Fake claude: prompt (argv[1]) must contain attachment paths
claude_log = os.path.join(home, "claude-argv.jsonl")
fake_claude = os.path.join(bin_dir, "claude")
claude_script = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "claude saw attachments"}]}}))
print(json.dumps({"type": "result", "result": "claude saw attachments", "usage": {"input_tokens": 5, "output_tokens": 5}}))
"""
os.environ["FAKE_CLAUDE_LOG"] = claude_log
open(fake_claude, "w").write(claude_script)
os.chmod(fake_claude, 0o755)
app.auth.get_baseten_key = lambda: "fake-baseten-key"
r = app.run_claude_chat("look at these", effort="low", workdir=home, auto_approve=False, attachments=good)
check("Claude run with attachments ok", r["ok"] and r["text"] == "claude saw attachments", str(r))
claude_argv = [json.loads(l) for l in open(claude_log) if l.strip() and json.loads(l)[:1] == ["-p"]]
check("Claude prompt contains attachment paths",
      claude_argv and att["path"] in claude_argv[-1][1] and img["path"] in claude_argv[-1][1], str(claude_argv[-1][1] if claude_argv else []))

# Baseten stream content: text inline + image vision (mock urlopen)
calls = {}


class FakeResp:
    def __enter__(self):
        return [b'data: {"choices": [{"delta": {"content": "baseten saw"}}]}\n\n',
                b'data: {"usage": {"prompt_tokens": 3, "completion_tokens": 2}}\n\n']
    def __exit__(self, *a):
        return False


orig_urlopen = None
import urllib.request as _ur
orig_urlopen = _ur.urlopen


def fake_urlopen(req, timeout=600):
    calls["body"] = json.loads(req.data.decode("utf-8"))
    return FakeResp()


_ur.urlopen = fake_urlopen
try:
    text, usage, error = mp.baseten_chat_stream("fake-key", "describe", "low", None, attachments=good)
finally:
    _ur.urlopen = orig_urlopen
check("Baseten stream ok", error is None, str(error))
body = calls.get("body", {})
content = body.get("messages", [{}])[0].get("content")
check("Baseten content is list with text + image",
      isinstance(content, list) and any(c.get("type") == "image_url" for c in content), str(content)[:300])
check("Baseten text inlines file content",
      isinstance(content, list) and "hello file" in content[0].get("text", ""), str(content)[:300])

# Server smoke: POST /api/attachments, GET /api/conversation, POST new
server = ThreadingHTTPServer(("127.0.0.1", 0), mp.Handler)
server.app = app
port = server.server_address[1]
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    payload = json.dumps({"name": "note.txt", "mime": "text/plain",
                          "data_base64": base64.b64encode(b"server upload").decode()}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/attachments", method="POST", data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        up = json.loads(resp.read())
    check("server upload returns attachment", "attachment" in up and os.path.exists(up["attachment"]["path"]), str(up)[:200])
    # Oversize: shrink limit, expect 413
    mp.MAX_ATTACHMENT_BYTES = 10
    payload2 = json.dumps({"name": "big.txt", "mime": "text/plain",
                           "data_base64": base64.b64encode(b"x" * 100).decode()}).encode()
    req2 = urllib.request.Request(f"http://127.0.0.1:{port}/api/attachments", method="POST", data=payload2,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req2) as resp:
            code2 = resp.status
    except urllib.error.HTTPError as e:
        code2 = e.code
    check("server rejects oversize attachment", code2 == 413, str(code2))
    mp.MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
finally:
    server.shutdown()
    server.server_close()

failed = [r for r in results if not r[1]]
print(f"\n{len(results)-len(failed)}/{len(results)} passed")
if failed:
    sys.exit(1)
