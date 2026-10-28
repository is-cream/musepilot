#!/usr/bin/env python3
"""v0.4.6 version-sync guard — Info.plist must match code VERSION.

Run: python3 test_musepilot_v046.py
This is the guard that would have caught the v0.2.0 -> v0.4.5 drift:
code VERSION was bumped every release, Info.plist was not, so Finder
showed 0.2.0 for a 0.4.5 build.
"""
import os
import plistlib
import re
import sys

sys.path.insert(0, "/home/hatch/workspace/musepilot")
import musepilot as mp  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


base = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(base, "MusePilot.app", "Contents", "Info.plist"), "rb") as fh:
    plist = plistlib.load(fh)
check("code VERSION is 0.4.6", mp.VERSION == "0.4.6")
check("Info.plist CFBundleVersion matches code VERSION",
      plist.get("CFBundleVersion") == mp.VERSION,
      f"plist={plist.get('CFBundleVersion')} code={mp.VERSION}")
check("Info.plist CFBundleShortVersionString matches code VERSION",
      plist.get("CFBundleShortVersionString") == mp.VERSION,
      f"plist={plist.get('CFBundleShortVersionString')} code={mp.VERSION}")
check("bundled copy VERSION matches source",
      re.search(r'VERSION = "([^"]+)"', open(os.path.join(
          base, "MusePilot.app", "Contents", "Resources", "musepilot.py")).read()).group(1) == mp.VERSION)
check("UI page uses __VERSION__ placeholder (so top bar follows code)",
      "__VERSION__" in mp.UI_HTML)

failed = [r for r in results if not r[1]]
print(f"\n{len(results)-len(failed)}/{len(results)} passed")
if failed:
    sys.exit(1)
