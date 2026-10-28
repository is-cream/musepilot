#!/usr/bin/env python3
"""v0.4.3 tests for musepilot.py — markdown + mermaid rendering in chat.

Run: HOME=/tmp/mptest-v043-home python3 test_musepilot_v043.py
Checks the JS renderer (extracted from the served page) and that the
page no longer renders assistant text via textContent alone.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, "/home/hatch/workspace/musepilot")
import musepilot as mp  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


check("version is 0.4.3", mp.VERSION == "0.4.3")
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "musepilot.py")).read()
check("page has renderMarkdown", "function renderMarkdown" in src)
check("page has renderMermaid", "function renderMermaid" in src)
check("chat renders markdown on text events, not textContent",
      "body.innerHTML = renderMarkdown(body.dataset.raw)" in src)
check("top bar no longer hardcodes muse exec for all providers",
      'id="verlabel"' in src)
check("assistant meta labels Baseten direct with no tools",
      "Baseten direct · no tools" in src)
check("mermaid loads from CDN with offline fallback",
      "cdn.jsdelivr.net/npm/mermaid" in src and "offline: code block stays visible" in src)

# Extract renderer and run it in node
js = re.search(r"<script>(.*?)</script>", src, re.S).group(1)
start = js.find("function esc")
end = js.find("function renderMermaid")
code = js[start:end]
test_js = code + r'''
const sample = "### Where to check\n\n**Bold** and `code`\n\n- a\n- b\n\n```python\nx = 1\n```\n\n```mermaid\ngraph TD\nA-->B\n```\n\n<script>alert(1)</script>";
const out = renderMarkdown(sample);
const checks = [
  out.includes("<h3>Where to check</h3>"),
  out.includes("<strong>Bold</strong>"),
  out.includes("<code>code</code>"),
  out.includes("<ul>") && out.includes("<li>a</li>"),
  out.includes("<pre><code"),
  out.includes("mermaid-block"),
  !out.includes("<script>alert") && out.includes("&lt;script&gt;"),
];
console.log(out);
if (checks.some(c => !c)) { console.error("FAILED", checks); process.exit(1); }
console.log("RENDERER OK");
'''
open("/tmp/md_test_v043.js", "w").write(test_js)
rc = subprocess.run(["node", "/tmp/md_test_v043.js"], capture_output=True, text=True)
check("node renderer passes all checks", rc.returncode == 0, rc.stdout[-500:] + rc.stderr[-500:])
# JS syntax for full page
open("/tmp/musepilot_ui043_full.js", "w").write(js)
rc2 = subprocess.run(["node", "--check", "/tmp/musepilot_ui043_full.js"], capture_output=True, text=True)
check("full page JS syntax OK", rc2.returncode == 0, rc2.stderr)

failed = [r for r in results if not r[1]]
print(f"\n{len(results)-len(failed)}/{len(results)} passed")
if failed:
    sys.exit(1)
