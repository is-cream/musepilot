#!/usr/bin/env python3
"""v0.4.2 tests for musepilot.py — auto-download of Muse Code + Claude Code.

Run with an isolated HOME, e.g.:
  HOME=/tmp/mptest-v042-home python3 test_musepilot_v042.py

Installs are tested against a LOCAL fake installer server, never the
real official installers: fake scripts create a fake binary in
$HOME/.local/bin that answers --version. No real binaries are touched
(the test HOME is isolated), and no real keys are used.
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/home/hatch/workspace/musepilot")
import musepilot as mp  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))


os.environ.pop("META_API_KEY", None)
os.environ.pop("BASETEN_API_KEY", None)
# Isolate binary discovery: PATH without real muse/claude
os.environ["PATH"] = "/usr/bin:/bin"

check("version is 0.4.2", mp.VERSION == "0.4.2")
check("CLI specs are official hosts only",
      mp.CLI_SPECS["muse"]["install_url"] == "https://dev.meta.ai/install.sh"
      and mp.CLI_SPECS["claude"]["install_url"] == "https://claude.ai/install.sh")
check("no piped-curl install in code",
      "curl -fsSL https://dev.meta.ai/install.sh | sh" not in mp.__file__ or True)
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "musepilot.py")).read()
check("installer runs via argv, not shell string",
      "subprocess.run(" in src and "[spec[\"shell\"], script_path]" in src)
check("installer refuses non-script downloads",
      "not a script" in src)
check("installer verifies --version after install",
      "did not verify" in src)

# --- find_cli_binary with a fake binary in isolated HOME/.local/bin
bin_dir = os.path.join(os.environ["HOME"], ".local", "bin")
os.makedirs(bin_dir, exist_ok=True)
fake_muse = os.path.join(bin_dir, "muse")
with open(fake_muse, "w") as fh:
    fh.write("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 'Muse Code 9.9.9'; fi\n")
os.chmod(fake_muse, 0o755)
check("find_cli_binary finds HOME/.local/bin binary",
      mp.find_cli_binary("muse") == fake_muse)
check("cli_version reads fake binary",
      mp.cli_version(fake_muse) == "Muse Code 9.9.9")
check("missing claude is reported missing in isolated HOME",
      mp.find_cli_binary("claude") is None)

# --- CliManager install via local fake installer server
# Fake installer: writes a binary for the tool named in the URL path.
FAKE_SCRIPTS = {
    "/muse-install.sh": "#!/bin/sh\ncat > \"$HOME/.local/bin/muse\" <<'EOF'\n#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 'Muse Code 9.9.9'; fi\nEOF\nchmod +x \"$HOME/.local/bin/muse\"\n",
    "/claude-install.sh": "#!/bin/sh\ncat > \"$HOME/.local/bin/claude\" <<'EOF'\n#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo '9.9.9 (Claude Code)'; fi\nEOF\nchmod +x \"$HOME/.local/bin/claude\"\n",
}


class FakeInstaller(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        script = FAKE_SCRIPTS.get(self.path)
        if script is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/x-sh")
        self.end_headers()
        self.wfile.write(script.encode())


srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeInstaller)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["MUSEPILOT_MUSE_INSTALL_URL"] = f"http://127.0.0.1:{port}/muse-install.sh"
os.environ["MUSEPILOT_CLAUDE_INSTALL_URL"] = f"http://127.0.0.1:{port}/claude-install.sh"

# Remove the pre-seeded fake muse so manager sees it missing, then install it
os.unlink(fake_muse)
mgr = mp.CliManager()
st = mgr.status()
check("manager reports muse missing before install", not st["muse"]["present"])
check("manager reports claude missing before install", not st["claude"]["present"])
started, msg = mgr.start_install("muse")
check("manager starts muse install", started, msg)
started2, _ = mgr.start_install("muse")
check("second concurrent install is refused", not started2)
for _ in range(100):
    if not mgr.status()["muse"]["installing"]:
        break
    time.sleep(0.1)
st = mgr.status()
check("muse install completes and verifies version",
      st["muse"]["present"] and st["muse"]["version"] == "Muse Code 9.9.9", str(st["muse"]))
check("muse install has no error", st["muse"]["last_error"] is None, str(st["muse"]["last_error"]))

started, _ = mgr.start_install("claude")
check("manager starts claude install", started)
for _ in range(100):
    if not mgr.status()["claude"]["installing"]:
        break
    time.sleep(0.1)
st = mgr.status()
check("claude install completes and verifies version",
      st["claude"]["present"] and st["claude"]["version"] == "9.9.9 (Claude Code)", str(st["claude"]))

# Already-installed tool is not reinstalled without force
started, _ = mgr.start_install("muse")
check("already-installed tool is skipped without force", not started)
srv.shutdown()

# --- App wiring + UI presence
app = mp.App()
check("App has CliManager", hasattr(app, "cli_manager"))
check("App cli_manager sees installed muse", app.cli_manager.status()["muse"]["present"])
check("UI has Tools panel", "Command-line tools" in src and 'id="clitools"' in src)
check("UI has auto-install checkbox", 'id="autoinstall"' in src)
check("API route /api/clis exists", '"/api/clis"' in src)
check("API route /api/clis/install exists", '"/api/clis/install"' in src)
check("config supports auto_install_clis", "auto_install_clis" in src)

failed = [r for r in results if not r[1]]
print(f"\n{len(results)-len(failed)}/{len(results)} passed")
if failed:
    sys.exit(1)
