#!/usr/bin/env python3
"""v0.4.5 tests — Claude Code provider + v0.4.4 label fix.

Run: HOME=/tmp/mptest-v045-home python3 test_musepilot_v045.py
Fake `claude` emits stream-json lines; no real keys or binaries used.
"""
import json
import os
import sys

sys.path.insert(0, "/home/hatch/workspace/musepilot")
import musepilot as mp  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


os.environ.pop("META_API_KEY", None)
os.environ.pop("BASETEN_API_KEY", None)
os.environ["PATH"] = "/usr/bin:/bin"
check("version is 0.4.5", mp.VERSION == "0.4.5")
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "musepilot.py")).read()
check("UI has Claude provider radio", 'value="claude"' in src and 'id="provider-claude"' in src)
check("Send button switches to Claude Code", "Send to Claude Code" in src)
check("Send button switches to Baseten", "Send to Baseten" in src)
check("Composer placeholder switches for Claude", "Ask Claude Code to do something" in src)
check("Claude env uses Baseten Anthropic endpoint (no /v1)",
      mp.CLAUDE_BASETEN_BASE_URL == "https://inference.baseten.co")
check("Claude read tools include WebSearch/WebFetch",
      "WebSearch" in mp.CLAUDE_READ_TOOLS and "WebFetch" in mp.CLAUDE_READ_TOOLS)
check("Claude write tools are NOT in read list",
      "Edit" not in mp.CLAUDE_READ_TOOLS and "Bash" not in mp.CLAUDE_READ_TOOLS)

# Fake claude binary in isolated HOME/.local/bin
bin_dir = os.path.join(os.environ["HOME"], ".local", "bin")
os.makedirs(bin_dir, exist_ok=True)
fake = os.path.join(bin_dir, "claude")
script = """#!/usr/bin/env python3
import json, sys
print(json.dumps({"type": "system", "subtype": "init", "model": "meta-models/muse-spark-open-1026"}))
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "WebSearch"}]}}))
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello Claude"}]}}))
print(json.dumps({"type": "result", "result": "Hello Claude", "usage": {"input_tokens": 10, "cache_read_input_tokens": 20, "output_tokens": 5}}))
"""
open(fake, "w").write(script)
os.chmod(fake, 0o755)
# Fake claude must see the argv it was given: record via env file
events = []
text, usage, error = mp.claude_chat_stream(
    fake, "fake-baseten-key", "hi", "low", os.environ["HOME"], False,
    on_event=lambda k, p: events.append((k, p)))
check("fake Claude driver returns text", text == "Hello Claude", text)
check("fake Claude driver reports no error", error is None, str(error))
check("fake Claude driver emits tool event", any(k == "tool" and p.get("name") == "WebSearch" for k, p in events), str(events))
check("fake Claude driver emits text event", any(k == "text" for k, p in events))
check("fake Claude usage totals input+cache+output",
      usage == {"input_tokens": 30, "output_tokens": 5, "total_tokens": 35}, str(usage))

# App routing: provider claude, model list + detail
app = mp.App()
app.config["provider"] = "claude"
mp.save_config(app.config)
check("config persists provider claude", mp.load_config().get("provider") == "claude")
# With no Baseten key, model list still shows fixed model, context unknown
models = app.query_models(force=True)
check("Claude model list is fixed Baseten model",
      [m["id"] for m in models["models"]] == [mp.BASETEN_MODEL], str(models["models"]))
check("Claude model source mentions tools",
      "tools" in models["model_source"].lower() or models["models_error"] is not None, models["model_source"])

failed = [r for r in results if not r[1]]
print(f"\n{len(results)-len(failed)}/{len(results)} passed")
if failed:
    sys.exit(1)
