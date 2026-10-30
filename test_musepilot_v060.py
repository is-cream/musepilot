#!/usr/bin/env python3
"""v0.6.0 tests — provider redesign: dropdown, custom providers, key modal.

Covers:
  - GET /api/providers (builtin meta + baseten listed, has_key, current)
  - POST /api/providers (create custom; 400 on bad input)
  - POST /api/providers/<id> (update; 404 unknown)
  - DELETE /api/providers/<id> (delete; 400 builtin; 404 unknown)
  - POST /api/providers/<id>/key (save; per-provider slot)
  - DELETE /api/providers/<id>/key (remove)
  - resolve_provider (builtin specs, custom lookup, unknown -> meta fallback)
  - legacy "claude" provider migration to a custom provider
  - harness dispatch: run_provider_chat routes custom ids to the right
    harness with the provider's base_url/model/key
  - budget gate: muse harness gated, custom harnesses bypass
  - frontend JS: provider dropdown, key button, modals, no settings panel

Run: HOME=/tmp/mptest-v060-home python3 test_musepilot_v060.py
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


check("version is 0.6.8", mp.VERSION == "0.6.8", mp.VERSION)

app = mp.App()
server = ThreadingHTTPServer(("127.0.0.1", 0), mp.Handler)
server.app = app
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{port}"


def get(path):
    with urllib.request.urlopen(BASE + path) as r:
        return json.loads(r.read())


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def delete(path):
    req = urllib.request.Request(BASE + path, method="DELETE")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# --- providers list -------------------------------------------------------
prov = get("/api/providers")
ids = {p["id"]: p for p in prov["providers"]}
check("meta builtin listed", ids.get("meta", {}).get("harness") == "muse")
check("baseten builtin listed", ids.get("baseten", {}).get("harness") == "baseten")
check("baseten is builtin", ids.get("baseten", {}).get("builtin") is True)
check("baseten model fixed", ids.get("baseten", {}).get("model") == mp.BASETEN_MODEL)
check("current defaults to meta", prov["current"] == "meta")
check("no key values leaked", all("key" not in k.lower() or k == "has_key" or k == "key_kind" or k == "key_slot"
                                  for p in prov["providers"] for k in p))

# --- resolve_provider ------------------------------------------------------
m = app.resolve_provider("meta")
check("resolve meta", m["harness"] == "muse" and m["builtin"])
b = app.resolve_provider("baseten")
check("resolve baseten", b["harness"] == "baseten" and b["model"] == mp.BASETEN_MODEL)
u = app.resolve_provider("no-such-id")
check("unknown id falls back to meta", u["id"] == "meta")

# --- create / update / delete ------------------------------------------------
st, j = post("/api/providers", {"name": "T", "harness": "nope",
                                "base_url": "https://x.example", "model": "m"})
check("bad harness rejected", st == 400, str(j))
st, j = post("/api/providers", {"name": "T", "harness": "claude",
                                "base_url": "notaurl", "model": "m"})
check("bad base_url rejected", st == 400, str(j))
st, j = post("/api/providers", {"name": "", "harness": "claude",
                                "base_url": "https://x.example", "model": "m"})
check("empty name rejected", st == 400, str(j))
st, j = post("/api/providers", {"name": "My Claude", "harness": "claude",
                                "base_url": "https://x.example/", "model": "m1"})
check("create ok", st == 200 and j["provider"]["name"] == "My Claude", str(j))
pid = j["provider"]["id"]
check("create strips trailing slash", j["provider"]["base_url"] == "https://x.example", j["provider"]["base_url"])
check("custom not builtin", j["provider"]["builtin"] is False)

spec = app.resolve_provider(pid)
check("resolve custom", spec["harness"] == "claude" and spec["model"] == "m1")

st, j = post(f"/api/providers/{pid}", {"name": "My Claude 2", "harness": "claude",
                                       "base_url": "https://y.example", "model": "m2"})
check("update ok", st == 200 and j["provider"]["name"] == "My Claude 2"
      and j["provider"]["model"] == "m2", str(j))
st, j = post("/api/providers/nope", {"name": "x", "harness": "claude",
                                     "base_url": "https://y.example", "model": "m2"})
check("update unknown 404", st == 404, str(j))

# --- key save / presence / remove --------------------------------------------
st, j = post(f"/api/providers/{pid}/key", {"key": "sekret"})
check("save key ok", st == 200 and j.get("ok"), str(j))
check("has_key true after save",
      any(p["id"] == pid and p["has_key"] for p in get("/api/providers")["providers"]))
check("slot is per-provider", app.auth.stored_key_present(pid))
check("builtin baseten slot untouched", not app.auth.stored_key_present("baseten"))
st, j = delete(f"/api/providers/{pid}/key")
check("delete key ok", st == 200 and j.get("ok"), str(j))
check("has_key false after delete",
      not any(p["id"] == pid and p["has_key"] for p in get("/api/providers")["providers"]))

# key for builtin baseten still works via provider endpoint
st, j = post("/api/providers/baseten/key", {"key": "b10_test"})
check("baseten key save ok", st == 200 and j.get("ok"), str(j))
check("baseten key lands in baseten slot", app.auth.stored_key_present("baseten"))
delete("/api/providers/baseten/key")

st, j = delete("/api/providers/baseten")
check("delete builtin 400", st == 400, str(j))
st, j = delete("/api/providers/nope")
check("delete unknown 404", st == 404, str(j))
st, j = delete(f"/api/providers/{pid}")
check("delete custom ok", st == 200 and j.get("deleted") == pid, str(j))
check("gone from list", not any(p["id"] == pid for p in get("/api/providers")["providers"]))

# --- legacy claude migration --------------------------------------------------
app2 = mp.App()
app2.config["provider"] = "claude"
mp.save_config(app2.config)
app3 = mp.App()  # init triggers migration
migrated = [cp for cp in app3.config.get("custom_providers", []) if cp.get("harness") == "claude"]
check("legacy claude migrated to custom", len(migrated) == 1, str(app3.config.get("custom_providers")))
if migrated:
    check("migration keeps baseten key slot", migrated[0].get("key_slot") == "baseten")
    check("migration sets provider to new id", app3.config.get("provider") == migrated[0]["id"])
    rs = app3.resolve_provider(app3.config.get("provider"))
    check("migrated resolves to claude harness", rs["harness"] == "claude")

# --- harness dispatch ---------------------------------------------------------
calls = {}


def fake_claude(prompt, **kw):
    calls["claude"] = kw
    return {"ok": True, "text": "c", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            "error": None, "model": kw.get("model"), "effort": kw.get("effort")}


def fake_baseten(prompt, **kw):
    calls["baseten"] = kw
    return {"ok": True, "text": "b", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            "error": None, "model": kw.get("model"), "effort": kw.get("effort")}


app.run_claude_chat = fake_claude
app.run_baseten_chat = fake_baseten

st, j = post("/api/providers", {"name": "Direct X", "harness": "baseten",
                                "base_url": "https://api.x.example/v1", "model": "mx"})
dx = j["provider"]["id"]
post(f"/api/providers/{dx}/key", {"key": "k1"})
spec = app.resolve_provider(dx)
res = app.run_provider_chat(spec, "hi", effort="low")
check("baseten harness dispatched", "baseten" in calls and calls["baseten"].get("model") == "mx",
      str(calls.get("baseten")))
check("baseten base_url passed", calls["baseten"].get("base_url") == "https://api.x.example/v1")
check("baseten key passed", calls["baseten"].get("api_key") == "k1")

st, j = post("/api/providers", {"name": "Claude Y", "harness": "claude",
                                "base_url": "https://claude.y.example", "model": "my"})
cy = j["provider"]["id"]
post(f"/api/providers/{cy}/key", {"key": "k2"})
spec = app.resolve_provider(cy)
res = app.run_provider_chat(spec, "hi", effort="high")
check("claude harness dispatched", "claude" in calls and calls["claude"].get("model") == "my",
      str(calls.get("claude")))
check("claude base_url passed", calls["claude"].get("base_url") == "https://claude.y.example")

# missing key -> RuntimeError naming the provider
delete(f"/api/providers/{cy}/key")
try:
    app.run_provider_chat(app.resolve_provider(cy), "hi")
    check("missing key raises", False, "no raise")
except RuntimeError as e:
    check("missing key raises", "Claude Y" in str(e), str(e)[:80])

# --- budget gate by harness ----------------------------------------------------
# muse harness gated; custom harnesses bypass (tested via resolve, not ledger)
check("muse harness is the gated one", app.resolve_provider("meta")["harness"] == mp.HARNESS_MUSE)
check("custom baseten not gated", app.resolve_provider(dx)["harness"] != mp.HARNESS_MUSE)

# --- query_models provider-aware -----------------------------------------------
qm = app.query_models(provider_id="baseten")
check("baseten models single fixed", len(qm["models"]) == 1 and qm["models"][0]["id"] == mp.BASETEN_MODEL,
      str(qm["models"]))
qmx = app.query_models(provider_id=dx)
check("custom models single fixed", len(qmx["models"]) == 1 and qmx["models"][0]["id"] == "mx",
      str(qmx["models"]))

# --- frontend ------------------------------------------------------------------
src = open("/home/hatch/workspace/musepilot/musepilot.py").read()
js = src[src.index("<script>"):src.index("</script>")]
check("no settings panel", 'id="settings"' not in src)
check("no settings button", 'id="settingsbtn"' not in src)
check("provider dropdown", 'id="provider"' in src)
check("key button", 'id="keybtn"' in src)
check("provider modal", 'id="provmodal"' in src)
check("key modal", 'id="keymodal"' in src)
check("conversations section", 'id="convlist"' in src)
check("runs accordion", 'id="runsacc"' in src)
check("JS refreshProviders", "function refreshProviders" in js)
check("JS onProviderChange", "function onProviderChange" in js)
check("JS openKeyModal", "function openKeyModal" in js)
check("JS openProviderDialog", "function openProviderDialog" in js)
check("JS saveCustomProvider", "function saveCustomProvider" in js)
check("JS manage option", "__manage__" in js)
check("no settings- refs in JS", "settings-" not in js)
check("no provider radio refs", 'name=\\"provider\\"' not in js and "name=\"provider\"" not in js)
check("modal CSS", ".modalbox" in src)

# --- version bump ----------------------------------------------------------------
check("README has v0.6.0", "v0.6.0" in open("/home/hatch/workspace/musepilot/README.md").read())

server.shutdown()
fails = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
sys.exit(1 if fails else 0)
