#!/usr/bin/env python3
"""
MusePilot — a macOS chat interface that drives the Muse Code CLI (`muse`).

Zero third-party dependencies: Python 3.8+ standard library only.

What it does:
  * Discovers models, reasoning-effort tiers and supported flags dynamically
    from the installed `muse` binary (via `muse schema generate-json-schema`
    and `muse --help` / `muse exec --help`) and, when an API key is saved,
    from the Meta Model API (`GET /v1/models`). There is NO hardcoded
    populated list: if a live source returns no models, efforts or context
    options, the UI shows that honestly (empty list + error) instead of
    inventing one, and offers a manual model-id entry.
  * Settings / Connect in the web UI: save an API key, validate it, and
    refresh the model list from that key. Selecting a model re-queries
    that model's effort options and context information.
  * Chat responses stream over SSE as `muse exec --json` emits them.
  * Voice input in the composer uses the browser's Web Speech API
    (voice-to-text only): live transcription into the editable prompt
    box; Send auto-stops transcription and sends the full text.
  * Chat UI to send prompts to `muse exec --json`, passing the selected
    model, reasoning effort, context window and other options through.
  * v0.4.0 provider switch (owner instruction 2026-10-26): Settings has a
    Meta / Baseten radio. Meta is the v0.3.0 path (queried models, CLI
    efforts). Baseten is a SEPARATE path: it calls Baseten's
    OpenAI-compatible Chat Completions API directly (the `muse` CLI
    cannot be pointed at Baseten — live-tested 2026-10-26: the CLI
    rejects a non-sanctioned base_url with "base_url is off the
    sanctioned front door"). Baseten's model is HARDCODED to
    meta-models/muse-spark-open-1026 ONLY, by owner instruction:
    this provider supports no other model in MusePilot. The Baseten
    API KEY is NEVER hardcoded — the user enters it in Settings,
    stored Keychain / mode-0600 file, never returned by any API.
    Baseten reasoning effort IS selectable (live-tested 2026-10-26:
    minimal/low/medium/high/xhigh/max/none all accepted, low=231
    reasoning tokens vs xhigh=432 on the same prompt; default xhigh).
    Baseten context window is NOT selectable: it is a fixed model
    property, 1,048,576 tokens, read from Baseten GET /v1/models
    (context_length). A `context_window` request parameter is
    silently ignored by Baseten (live-tested). All Settings
    selections (provider, model, effort, context) are saved to
    ~/.musepilot/config.json and restored on next launch, for
    BOTH providers (v0.3.0 saved only the model reliably).
  * Tracks token/prompt usage in a rolling 5-hour window ledger and can queue
    prompts to auto-invoke Muse Code when window capacity frees up.

Usage:
    python3 musepilot.py [--port 8471] [--no-browser]

Data lives in ~/.musepilot/ (usage.jsonl, queue.json, config.json, runs/).
The local web server binds to 127.0.0.1 only.
"""

import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

APP_NAME = "MusePilot"
VERSION = "0.6.2"
WINDOW_SECONDS = 5 * 3600  # the rolling usage window Meta documents for Muse Code

HOME = os.path.expanduser("~")
DATA_DIR = os.path.join(HOME, ".musepilot")
USAGE_FILE = os.path.join(DATA_DIR, "usage.jsonl")
QUEUE_FILE = os.path.join(DATA_DIR, "queue.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
RUNS_DIR = os.path.join(DATA_DIR, "runs")
CONVERSATIONS_DIR = os.path.join(DATA_DIR, "conversations")
ATTACHMENTS_DIR = os.path.join(DATA_DIR, "attachments")
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MiB per file
MAX_BASETEN_TEXT_ATTACHMENT_CHARS = 100000
MAX_CONVERSATION_FILE_BYTES = 1048576  # 1 MiB: rotate to a new file above this
# Non-macOS API-key storage: a mode-0600 file under DATA_DIR. On macOS the
# Keychain is preferred; this file is only used where the Keychain is not
# available. It holds ONLY the key, never anything else, and is never
# returned by any API endpoint.
KEY_FILE = os.path.join(DATA_DIR, "api_key")
# Baseten key storage: same stores as Meta, different slot, so the two
# keys can never be mixed up. Never hardcoded, never returned by any API.
KEY_FILE_BASETEN = os.path.join(DATA_DIR, "api_key_baseten")
BASETEN_API_BASE = "https://inference.baseten.co/v1"
# Owner instruction 2026-10-26: the ONLY Baseten model MusePilot offers.
# This is the sole hardcoded model in v0.4.0, and it is intentional:
# Baseten's hackathon key works only with this model. The API key is
# never hardcoded alongside it.
BASETEN_MODEL = "meta-models/muse-spark-open-1026"
# v0.4.1 correction (owner instruction 2026-10-26: prepopulate by
# QUERYING the model): Baseten's GET /v1/models is the live source for
# context. GET /v1/models/<id> does NOT exist on Baseten (HTTP 405,
# live-tested) — the list must be fetched and filtered by id.
# The list entry for the hackathon model carries context_length
# (1,048,576 on 2026-10-26), max_completion_tokens, supported_features
# (tools, json_mode, reasoning) and pricing — but it does NOT carry a
# reasoning-effort list. The effort list therefore comes from Baseten's
# reasoning documentation, Muse Spark Open row: low, medium, high,
# xhigh (default). Baseten's generic validator also accepts minimal,
# max and none, but for THIS model they are not distinct levels
# (docs: none and minimal run at low; max is not listed for Spark
# Open), so v0.4.0's 7-value list was misleading and is narrowed
# here to the 4 documented, distinct levels. "none" does NOT turn
# reasoning off for Muse Spark Open — it reasons regardless.
BASETEN_EFFORTS = ["low", "medium", "high", "xhigh"]
BASETEN_MAX_TOKENS = 8192  # max output tokens per Baseten chat (reasoning counts toward it)
# v0.6.0: provider model. Every provider has an id, a name, and a harness.
# Harnesses: "muse" (muse CLI, Meta auth), "claude" (claude CLI against an
# Anthropic-compatible base URL), "baseten" (direct OpenAI-compatible HTTP).
# "meta" and "baseten" are builtin; everything else lives in
# config["custom_providers"] as {id, name, harness, base_url, model, key_slot}.
HARNESS_MUSE = "muse"
HARNESS_CLAUDE = "claude"
HARNESS_BASETEN = "baseten"
PROVIDER_META = "meta"
PROVIDER_BASETEN = "baseten"
CUSTOM_HARNESSES = (HARNESS_CLAUDE, HARNESS_BASETEN)
# Meta Model API base, OpenAI-compatible. Overridable for testing only via
# MUSEPILOT_API_BASE; the default is the documented Meta base.
API_BASE = os.environ.get("MUSEPILOT_API_BASE") or "https://api.meta.ai/v1"

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(RUNS_DIR, exist_ok=True)
    os.makedirs(CONVERSATIONS_DIR, exist_ok=True)
    os.makedirs(ATTACHMENTS_DIR, exist_ok=True)


def now_ts():
    return time.time()


def fmt_time(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def run_cmd(argv, timeout=15, cwd=None, input_data=None, env=None):
    """Run a command, return (returncode, stdout, stderr). No shell.

    `env`, when given, is the full child environment (used so discovery
    and model-profile probes run with the saved API key injected, the
    same environment chat runs use).
    """
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, cwd=cwd,
            stdin=subprocess.DEVNULL if input_data is None else subprocess.PIPE,
            input=input_data, env=env,
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except FileNotFoundError:
        return 127, "", "command not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"


def valid_model_id(model_id):
    """A model id is a single token: letters, digits, '.', '_' and '-' only.

    Rejects path separators, spaces and shell metacharacters even though
    the CLI is always invoked via an argv list (no shell).
    """
    if not isinstance(model_id, str):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", model_id or ""))


# ---------------------------------------------------------------------------
# Muse CLI discovery — everything dynamic, nothing hardcoded.
#
# There is deliberately NO fallback model / effort / context list in this
# file. If a live source returns nothing, the caller gets an empty list
# plus a source note saying so, and the UI shows an error + a manual
# model-id entry. Inventing a list here is exactly the bug v0.3.0 fixes:
# v0.2.0's loose schema-enum match returned "javascript" (from
# CodeModeLanguage, whose name merely contains "model") as a model.
# ---------------------------------------------------------------------------

# Plan tiers and default *estimated* prompt budgets per 5h window.
# Meta publishes "10-50 prompts every 5 hours" for Everyday; High/Power/Max are
# documented as 5x/20x/80x multiples of the Everyday allowance. These are
# estimates — the user can override the number in the UI.
PLAN_TIERS = [
    {"id": "everyday", "label": "Everyday — $5/mo", "default_budget": 30},
    {"id": "high", "label": "High — $15/mo", "default_budget": 150},
    {"id": "power", "label": "Power — $50/mo", "default_budget": 600},
    {"id": "max", "label": "Max — $200/mo", "default_budget": 2400},
]


# CLI provisioning (v0.4.2) — auto-download Muse Code + Claude Code if missing.
#
# Official installers only, verified 2026-10-26 by reading the scripts
# before any use (not blind pipes):
#   Muse Code:  curl -fsSL https://dev.meta.ai/install.sh | sh
#               -> installs ~/.local/bin/muse (launcher, downloads from
#               api.meta.ai with a sha256 check in the script).
#   Claude Code: curl -fsSL https://claude.ai/install.sh | bash
#               -> installs ~/.local/bin/claude (from downloads.claude.ai).
# MusePilot downloads the script to a temp file first, runs it with an
# argv list (never a shell string), then verifies the result by
# re-finding the binary and running --version. A failed install never
# pretends success: status carries the error and the UI shows it.
# Env overrides MUSEPILOT_MUSE_INSTALL_URL / MUSEPILOT_CLAUDE_INSTALL_URL
# exist ONLY so tests can point at a local fake installer; they are not
# exposed in the UI and must never be set on a user's machine.
CLI_SPECS = {
    "muse": {
        "label": "Muse Code",
        "command": "muse",
        "install_url": "https://dev.meta.ai/install.sh",
        "install_host": "dev.meta.ai",
        "shell": "bash",
        "env_url_var": "MUSEPILOT_MUSE_INSTALL_URL",
    },
    "claude": {
        "label": "Claude Code",
        "command": "claude",
        "install_url": "https://claude.ai/install.sh",
        "install_host": "claude.ai",
        "shell": "bash",
        "env_url_var": "MUSEPILOT_CLAUDE_INSTALL_URL",
    },
}


def find_cli_binary(command):
    """Find a CLI binary: PATH first, then the standard install dirs.

    The .app double-click path does not inherit a shell PATH, so the
    standard dirs are searched explicitly — that is why a CLI installed
    in a terminal still shows as found when MusePilot is launched
    from Finder.
    """
    found = shutil.which(command)
    if found:
        return found
    for candidate in (
        os.path.join(HOME, ".local", "bin", command),
        "/usr/local/bin/" + command,
        "/opt/homebrew/bin/" + command,
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def cli_version(binary):
    if not binary:
        return None
    rc, out, _ = run_cmd([binary, "--version"], timeout=10)
    return out.strip().splitlines()[0] if rc == 0 and out.strip() else None


class CliManager:
    """Tracks and (auto-)installs the muse + claude CLIs."""

    def __init__(self, on_installed=None):
        self._lock = threading.Lock()
        self._state = {}
        self._threads = {}
        for name, spec in CLI_SPECS.items():
            self._state[name] = {
                "label": spec["label"],
                "present": False,
                "path": None,
                "version": None,
                "installing": False,
                "last_error": None,
                "last_install_at": None,
            }
        self._on_installed = on_installed
        self.refresh()

    def refresh(self):
        with self._lock:
            for name, spec in CLI_SPECS.items():
                if self._state[name]["installing"]:
                    continue
                path = find_cli_binary(spec["command"])
                self._state[name]["present"] = path is not None
                self._state[name]["path"] = path
                self._state[name]["version"] = cli_version(path) if path else None
        return self.status()

    def status(self):
        with self._lock:
            return {name: dict(info) for name, info in self._state.items()}

    def start_install(self, name, force=False):
        """Start a background install. Returns (started, message)."""
        if name not in CLI_SPECS:
            return False, "Unknown tool: %s" % name
        with self._lock:
            st = self._state[name]
            if st["installing"]:
                return False, "%s is already downloading." % st["label"]
            if st["present"] and not force:
                return False, "%s is already installed." % st["label"]
            st["installing"] = True
            st["last_error"] = None
        th = threading.Thread(target=self._install, args=(name,), daemon=True)
        self._threads[name] = th
        th.start()
        return True, "Downloading %s…" % CLI_SPECS[name]["label"]

    def _install(self, name):
        import urllib.parse
        import urllib.request
        spec = CLI_SPECS[name]
        log_tail = ""
        try:
            url = os.environ.get(spec["env_url_var"]) or spec["install_url"]
            parsed = urllib.parse.urlparse(url)
            # Official host only, unless a test override is explicitly set.
            if not os.environ.get(spec["env_url_var"]) and parsed.hostname != spec["install_host"]:
                raise RuntimeError("Refusing non-official installer host: %s" % parsed.hostname)
            req = urllib.request.Request(url, headers={"User-Agent": "MusePilot/" + VERSION})
            with urllib.request.urlopen(req, timeout=60) as resp:
                script = resp.read()
            if not script.strip().startswith(b"#!"):
                raise RuntimeError("Downloaded installer is not a script; refusing to run it.")
            fd, script_path = tempfile.mkstemp(prefix="musepilot-install-", suffix=".sh")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(script)
                os.chmod(script_path, 0o700)
                env = dict(os.environ)
                env["HOME"] = HOME
                proc = subprocess.run(
                    [spec["shell"], script_path],
                    capture_output=True, text=True, timeout=300, env=env)
                log_tail = (proc.stdout + proc.stderr)[-600:]
                if proc.returncode != 0:
                    raise RuntimeError("Installer exited %s: %s" % (proc.returncode, log_tail.strip()[-300:]))
            finally:
                try:
                    os.unlink(script_path)
                except OSError:
                    pass
            # Verify: re-find + --version. No version, no success claim.
            path = find_cli_binary(spec["command"])
            version = cli_version(path) if path else None
            if not path or not version:
                raise RuntimeError("Installer finished but '%s --version' did not verify." % spec["command"])
            with self._lock:
                st = self._state[name]
                st.update({"present": True, "path": path, "version": version,
                           "installing": False, "last_error": None,
                           "last_install_at": now_ts()})
            if self._on_installed:
                try:
                    self._on_installed(name, path)
                except Exception:  # noqa: BLE001 - install success must not die here
                    pass
        except Exception as e:  # noqa: BLE001 - install errors are user-facing
            with self._lock:
                st = self._state[name]
                st.update({"installing": False,
                           "last_error": str(e)[:400],
                           "present": find_cli_binary(spec["command"]) is not None})
                st["path"] = find_cli_binary(spec["command"])
                st["version"] = cli_version(st["path"]) if st["path"] else None


class MuseCLI:
    def __init__(self):
        self.binary = self._find_binary()
        self._options_cache = None
        self._cache_at = 0
        # Set by App after AuthManager exists: a callable returning the
        # full child environment (with the saved API key injected). None
        # means "inherit the process environment" (tests, no auth).
        self.env_provider = None

    def _env(self):
        try:
            return self.env_provider() if self.env_provider else None
        except Exception:  # noqa: BLE001 - discovery must not die on auth
            return None

    # -- binary ----------------------------------------------------------
    def _find_binary(self):
        return find_cli_binary("muse")

    def refresh(self):
        """Re-detect the binary after an install (v0.4.2)."""
        self.binary = self._find_binary()
        self._options_cache = None
        self._cache_at = 0
        return self.available

    @property
    def available(self):
        return self.binary is not None

    def version(self):
        if not self.available:
            return None
        rc, out, _ = run_cmd([self.binary, "--version"], timeout=10, env=self._env())
        return out.strip().splitlines()[0] if rc == 0 and out.strip() else None

    def model_profile(self, model_id):
        """Run `muse model-profile show <model>` and parse its knobs.

        Returns a dict like {"default_reasoning_effort": "high", ...} or
        None when the CLI is missing, the id is invalid, or the command
        fails. IMPORTANT: this command does NOT validate a model id —
        in CLI 1.4.10 it returns plausible knobs even for an unknown id —
        so a non-None result must never be presented as "this model
        exists". It is per-model detail, not a model list or a validity
        check.
        """
        if not self.available or not valid_model_id(model_id):
            return None
        rc, out, err = run_cmd(
            [self.binary, "model-profile", "show", model_id],
            timeout=15, env=self._env())
        text = out if rc == 0 else ""
        if rc != 0 or not text.strip():
            return None
        profile = {}
        for line in text.splitlines():
            stripped = line.strip()
            # The "re-resolution" section repeats knob names with
            # resolution-policy values (e.g. default_reasoning_effort =
            # "startupFixed …") — those must not overwrite the real
            # top-section values, so stop there and keep first wins.
            if stripped.startswith("re-resolution"):
                break
            m = re.match(r"^([a-z_][a-z0-9_]*)\s{2,}(\S.*)$", stripped)
            if m and m.group(1) not in profile:
                profile[m.group(1)] = m.group(2).strip()
            m2 = re.match(r"^([a-z_][a-z0-9_]*):\s+(\S.*)$", stripped)
            if m2 and m2.group(1) not in profile:
                profile[m2.group(1)] = m2.group(2).strip()
        return profile or None

    # -- help parsing ----------------------------------------------------
    def _help_text(self, *args):
        if not self.available:
            return ""
        rc, out, err = run_cmd([self.binary, *args, "--help"], timeout=10, env=self._env())
        text = out if rc == 0 else err
        return text or ""

    @staticmethod
    def _flags_from_help(text):
        """Extract long flags from --help output: {'--model': 'description'}.

        Line-based: a flag starts a line; indented lines that don't look like
        another flag are its description continuation.
        """
        flags = {}
        lines = (text or "").splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            m = re.match(r"^\s*(--[a-zA-Z][\w-]*)(\s+(.*))?$", line)
            if m:
                flag = m.group(1)
                desc = (m.group(3) or "").strip()
                extra = []
                j = i + 1
                while j < len(lines):
                    nxt = lines[j]
                    if not nxt.strip():
                        break
                    if re.match(r"^\s*-{1,2}[a-zA-Z]", nxt):
                        break  # another flag/option line
                    if not nxt[:1].isspace():
                        break  # dedented: new section
                    extra.append(nxt.strip())
                    j += 1
                if extra:
                    desc = (desc + " " + " ".join(extra)).strip()
                flags[flag] = desc
                i = j
                continue
            i += 1
        return flags

    # -- schema parsing --------------------------------------------------
    def _schema_enums(self):
        """Run `muse schema generate-json-schema` and collect string enums."""
        if not self.available:
            return {}
        tmpdir = tempfile.mkdtemp(prefix="musepilot-schema-")
        try:
            rc, out, err = run_cmd(
                [self.binary, "schema", "generate-json-schema", "--out", tmpdir],
                timeout=30, env=self._env(),
            )
            if rc != 0:
                return {}
            enums = {}
            for root, _dirs, files in os.walk(tmpdir):
                for f in files:
                    if not f.endswith(".json"):
                        continue
                    try:
                        with open(os.path.join(root, f), encoding="utf-8") as fh:
                            schema = json.load(fh)
                    except (OSError, ValueError):
                        continue
                    for name, definition in (schema.get("$defs") or {}).items():
                        enum = definition.get("enum")
                        if (isinstance(enum, list) and enum
                                and all(isinstance(v, str) for v in enum)):
                            enums[name] = {
                                "values": enum,
                                "description": definition.get("description", ""),
                            }
            return enums
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # -- public: dynamic options -----------------------------------------
    def discover_options(self, force=False):
        """Return UI options discovered live from the CLI. Cached 60s."""
        if self._options_cache and not force and (now_ts() - self._cache_at) < 60:
            return self._options_cache

        top_help = self._help_text()
        exec_help = self._help_text("exec")
        exec_flags = self._flags_from_help(exec_help)
        top_flags = self._flags_from_help(top_help)
        schema_enums = self._schema_enums()

        # Reasoning effort: prefer the schema enum named exactly
        # ReasoningEffort, else the pipe-separated list in --help text
        # (e.g. "none|minimal|low|medium|high|xhigh|max|ultra"). The
        # tiers are global to the CLI, not per model — per-model detail
        # (default tier, knobs) comes from model_profile(), see
        # /api/models/<id>. No hardcoded tier list anywhere.
        efforts, effort_source = [], "none — no effort list returned by the CLI"
        for name, enum in schema_enums.items():
            if name.lower() == "reasoningeffort":
                efforts, effort_source = list(enum["values"]), "muse schema (live)"
                break
        if not efforts:
            help_lines = (exec_help + "\n" + top_help).splitlines()
            for i, line in enumerate(help_lines):
                if "--reasoning-effort" in line:
                    for following in help_lines[i:i + 4]:
                        pm = re.search(r"([a-z]+(?:\|[a-z]+)+)", following)
                        if pm:
                            vals = [v.strip() for v in pm.group(1).split("|") if v.strip()]
                            if len(vals) >= 2:
                                efforts, effort_source = vals, "muse --help (live)"
                            break
                    if efforts:
                        break

        # Models: ONLY an enum named exactly Model / ModelId / ModelName
        # in the schema counts. The v0.2.0 bug was matching any enum whose
        # name merely contained "model" — CodeModeLanguage matched and
        # "javascript" was presented as a model. Help text in CLI 1.4.10
        # carries no model ids ("Model id for non-echo providers"), so
        # there is no help-based model list to fall back to. Empty here
        # is honest: the API query (see query_api_models) is the other
        # live source, and the UI then offers manual model-id entry.
        models, model_source = [], "none — CLI returned no model list"
        for name, enum in schema_enums.items():
            if name.lower() in ("model", "modelid", "modelname"):
                models = [{"id": v, "label": v, "context_window": None}
                          for v in enum["values"]]
                model_source = "muse schema (live)"
                break

        # Context-window flag: does the CLI accept one?
        context_flag, context_source = None, None
        for flag in ("--context-window", "--max-context-tokens", "--context-tokens",
                     "--max-context", "--context-size"):
            if flag in exec_flags or flag in top_flags:
                context_flag, context_source = flag, "muse --help (live)"
                break

        options = {
            "cli_available": self.available,
            "cli_binary": self.binary,
            "cli_version": self.version(),
            "models": models,
            "model_source": model_source,
            "supports_model_flag": ("--model" in exec_flags) or ("--model" in top_flags),
            "reasoning_efforts": efforts,
            "effort_source": effort_source,
            "supports_effort_flag": "--reasoning-effort" in exec_flags or "--reasoning-effort" in top_flags,
            "context_flag": context_flag,
            "context_source": context_source,
            "supports_prompt_file": "--prompt-file" in exec_flags,
            "supports_session_id": "--session-id" in exec_flags,
            "supports_max_steps": "--max-model-steps" in exec_flags,
            "supports_yolo": "--yolo" in exec_flags,
            "supports_image_flag": "--image" in exec_flags,
            "exec_flags": sorted(exec_flags.keys()),
        }
        self._options_cache = options
        self._cache_at = now_ts()
        return options

# ---------------------------------------------------------------------------
# Meta Model API query (OpenAI-compatible) — the authenticated model list.
#
# With a saved API key, GET <API_BASE>/models with Authorization: Bearer
# <key> returns {"data": [{"id": ..., ...}, ...]} on a compliant server.
# Context fields, when present, are read generically (context_window,
# context_length, max_input_tokens, max_context_tokens) and left as None
# when absent — never guessed, never filled from a builtin table.
# Any non-200, malformed, or empty response is a failure with an error
# string, not a silent fallback: the caller shows the error.
# The key is used ONLY in this server-side call and is never sent to the
# browser or logged.
# ---------------------------------------------------------------------------

def query_api_models(api_key, base=None, timeout=15):
    """Query the Meta Model API for available models.

    Returns (models, error): models is a list of
    {"id", "label", "context_window"} (context_window may be None);
    error is None on success, else a short human-readable reason.
    """
    if not api_key:
        return [], "No API key saved."
    import urllib.error
    import urllib.request
    url = (base or API_BASE).rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + api_key,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return [], "API key rejected by the Meta Model API (HTTP %s)." % e.code
        return [], "Meta Model API returned HTTP %s for /models." % e.code
    except Exception as e:  # noqa: BLE001 - network errors are user-facing
        return [], "Could not reach the Meta Model API: %s" % str(e)[:150]
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        return [], "Meta Model API returned no models."
    models = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not model_id or not valid_model_id(str(model_id)):
            continue
        ctx = None
        for field in ("context_window", "context_length",
                      "max_input_tokens", "max_context_tokens"):
            try:
                if item.get(field) is not None:
                    ctx = int(item[field])
                    break
            except (TypeError, ValueError):
                continue
        models.append({"id": str(model_id), "label": str(model_id),
                       "context_window": ctx})
    if not models:
        return [], "Meta Model API returned no usable model ids."
    return models, None


# ---------------------------------------------------------------------------
# Authentication — delegated to the `muse` CLI; MusePilot never asks the CLI
# for credentials itself. This manager only *detects* which credential source
# is active and can optionally hold a META_API_KEY in the macOS Keychain so
# the double-clickable .app (which doesn't inherit shell env vars) can use it.
#
# v0.3.0: on machines without a Keychain, a saved key goes to a mode-0600
# file under ~/.musepilot/ instead, so the Settings / Connect flow works
# on Linux too (previously Save key silently degraded to an error there).
#
# Documented precedence (matches the CLI's own): META_API_KEY env var >
# key stored via `muse auth set` > stored browser session.
# ---------------------------------------------------------------------------

class AuthManager:
    KEYCHAIN_SERVICE = "ai.muse.pilot"
    KEYCHAIN_ACCOUNT = "META_API_KEY"
    KEYCHAIN_ACCOUNT_BASETEN = "BASETEN_API_KEY"

    @staticmethod
    def _keychain_account(provider="meta"):
        # v0.6.0: custom providers get their own keychain slot.
        if provider == "baseten":
            return AuthManager.KEYCHAIN_ACCOUNT_BASETEN
        if provider == "meta":
            return AuthManager.KEYCHAIN_ACCOUNT
        return "CUSTOM_API_KEY_" + re.sub(r"[^A-Za-z0-9_]", "_", str(provider))[:32]

    @staticmethod
    def _key_file(provider="meta"):
        # v0.6.0: custom providers get their own 0600 key file.
        if provider == "baseten":
            return KEY_FILE_BASETEN
        if provider == "meta":
            return KEY_FILE
        safe = re.sub(r"[^A-Za-z0-9_]", "_", str(provider))[:32]
        return os.path.join(DATA_DIR, "api_key_" + safe)

    def get_baseten_key(self):
        """Baseten key: env BASETEN_API_KEY wins, else the saved Baseten slot. Secret: never log/return via API."""
        env = os.environ.get("BASETEN_API_KEY")
        if env:
            return env
        return self.get_api_key(provider="baseten")

    def __init__(self, cli: MuseCLI):
        self.cli = cli

    # -- detection -----------------------------------------------------
    @staticmethod
    def _muse_config_dir():
        return os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")

    def env_key_present(self):
        return bool(os.environ.get("META_API_KEY"))

    def stored_credential_present(self):
        """A credential cached by `muse login` / the first-run sign-in flow."""
        return os.path.isfile(
            os.path.join(self._muse_config_dir(), "muse", "auth.json"))

    def keychain_available(self):
        return sys.platform == "darwin" and shutil.which("security") is not None

    def keychain_has_key(self, provider="meta"):
        if not self.keychain_available():
            return False
        rc, out, _ = run_cmd(
            ["security", "find-generic-password",
             "-a", self._keychain_account(provider), "-s", self.KEYCHAIN_SERVICE, "-w"],
            timeout=10)
        return rc == 0 and bool(out.strip())

    # -- file-backed key (non-macOS) -----------------------------------
    def file_key_present(self, provider="meta"):
        f = self._key_file(provider)
        try:
            return os.path.isfile(f) and os.path.getsize(f) > 0
        except OSError:
            return False

    def file_get(self, provider="meta"):
        """Return the file-stored key, or None. Secret: never log/return via API."""
        try:
            with open(self._key_file(provider), encoding="utf-8") as fh:
                key = fh.read().strip()
            return key or None
        except OSError:
            return None

    def file_set(self, secret, provider="meta"):
        secret = (secret or "").strip()
        if not secret or len(secret) > 4096:
            return False, "Invalid key."
        try:
            ensure_dirs()
            os.chmod(DATA_DIR, 0o700)
        except OSError:
            pass
        try:
            fd = os.open(self._key_file(provider), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, secret.encode("utf-8"))
            finally:
                os.close(fd)
            os.chmod(self._key_file(provider), 0o600)
            return True, "Saved to a mode-0600 file under ~/.musepilot/ (this machine only)."
        except OSError as e:
            return False, "Could not save the key: %s" % str(e)[:150]

    def file_delete(self, provider="meta"):
        try:
            os.unlink(self._key_file(provider))
            return True, "Removed the saved API key file."
        except FileNotFoundError:
            return False, "No saved API key file."
        except OSError as e:
            return False, "Could not remove the key file: %s" % str(e)[:150]

    # -- unified key store (Keychain preferred, file otherwise) ----------
    def stored_key_present(self, provider="meta"):
        return self.keychain_has_key(provider) or self.file_key_present(provider)

    def key_store(self, provider="meta"):
        if self.keychain_available() and self.keychain_has_key(provider):
            return "keychain"
        if self.file_key_present(provider):
            return "file"
        return "none"

    def get_api_key(self, provider="meta"):
        """Internal only: the saved key (Keychain or file), or None.

        Never put the return value in an API response, a log line, or a
        file other than the storage itself.
        """
        key = self.keychain_get(provider)
        if key:
            return key
        return self.file_get(provider)

    def save_api_key(self, secret, provider="meta"):
        """Save an API key via Keychain (macOS) or the 0600 file (elsewhere)."""
        if self.keychain_available():
            return self.keychain_set(secret, provider)
        return self.file_set(secret, provider)

    def delete_api_key(self, provider="meta"):
        """Remove the saved key from whichever store holds it."""
        removed = []
        if self.keychain_available():
            ok, _ = self.keychain_delete(provider)
            if ok:
                removed.append("Keychain")
        ok, _ = self.file_delete(provider)
        if ok:
            removed.append("file")
        if removed:
            return True, "Removed the saved API key (%s)." % ", ".join(removed)
        return False, "No saved API key to remove."

    def status(self):
        """Presence-based check. Never includes secret values."""
        env = self.env_key_present()
        stored = self.stored_credential_present()
        kc = self.keychain_has_key()
        fk = self.file_key_present()
        if env:
            detail = "Using META_API_KEY from the environment."
        elif kc:
            detail = "Using API key from the macOS Keychain."
        elif fk:
            detail = "Using API key from a mode-0600 file under ~/.musepilot/."
        elif stored:
            detail = "Using the credential cached by `muse login`."
        else:
            detail = ("No credential found. Run `muse login` in a terminal, "
                      "or save an API key in Settings.")
        return {
            "ok": bool(env or stored or kc or fk),
            "env_key": env,
            "stored_credential": stored,
            "keychain_key": kc,
            "file_key": fk,
            "stored_key": bool(kc or fk),
            "key_store": self.key_store(),
            "baseten_key": bool(os.environ.get("BASETEN_API_KEY")) or self.stored_key_present("baseten"),
            "baseten_store": ("env" if os.environ.get("BASETEN_API_KEY") else self.key_store("baseten")),
            "keychain_available": self.keychain_available(),
            "detail": detail,
        }

    # -- keychain ------------------------------------------------------
    def keychain_get(self, provider="meta"):
        """Return the stored key, or None. Callers must treat it as secret:
        never log it, never put it in API responses, never write it to disk."""
        if not self.keychain_available():
            return None
        rc, out, _ = run_cmd(
            ["security", "find-generic-password",
             "-a", self._keychain_account(provider), "-s", self.KEYCHAIN_SERVICE, "-w"],
            timeout=10)
        if rc == 0 and out.strip():
            return out.strip()
        return None

    def keychain_set(self, secret, provider="meta"):
        """Store the key in the login keychain. Returns (ok, message)."""
        if not self.keychain_available():
            return False, "macOS Keychain not available on this machine."
        secret = (secret or "").strip()
        if not secret or len(secret) > 4096:
            return False, "Invalid key."
        # -U updates the item if it already exists. The secret travels as a
        # single argv element (briefly visible in process listings on a
        # multi-user machine); the preferred path remains `muse login`, which
        # never puts a secret through this app at all.
        rc, _out, err = run_cmd(
            ["security", "add-generic-password",
             "-a", self._keychain_account(provider), "-s", self.KEYCHAIN_SERVICE,
             "-U", "-w", secret],
            timeout=15)
        if rc == 0:
            return True, "Saved to the macOS Keychain."
        return False, (err.strip().splitlines() or ["Keychain save failed."])[-1][:200]

    def keychain_delete(self, provider="meta"):
        if not self.keychain_available():
            return False, "macOS Keychain not available on this machine."
        rc, _out, err = run_cmd(
            ["security", "delete-generic-password",
             "-a", self._keychain_account(provider), "-s", self.KEYCHAIN_SERVICE],
            timeout=15)
        if rc == 0:
            return True, "Removed from the Keychain."
        return False, (err.strip().splitlines() or ["Keychain delete failed."])[-1][:200]

    # -- child process env ---------------------------------------------
    def child_env(self):
        """Environment for `muse` subprocesses.

        Mirrors CLI precedence: an exported META_API_KEY wins; otherwise a
        Keychain-stored key is injected; otherwise the CLI falls back to its
        own stored credential.
        """
        env = dict(os.environ)
        if not env.get("META_API_KEY"):
            key = self.get_api_key()
            if key:
                env["META_API_KEY"] = key
        return env

    def logout(self):
        """Run `muse logout` — clears the CLI's stored credential."""
        if not self.cli.available:
            return False, "The `muse` CLI was not found."
        rc, out, err = run_cmd([self.cli.binary, "logout"], timeout=30)
        msg = (out.strip() or err.strip()).splitlines()
        if rc == 0:
            return True, (msg[-1] if msg else "Signed out.")[:200]
        return False, ((msg[-1] if msg else "Logout failed.")[:200])

    # -- account identity --------------------------------------------
    def _top_commands(self):
        """Command names from the CLI's top-level --help (e.g. ['exec', ...])."""
        text = self.cli._help_text()
        m = re.search(r"Commands:\s*\n((?:[ \t]+\S[^\n]*\n?)+)", text)
        cmds = []
        if m:
            for line in m.group(1).splitlines():
                cm = re.match(r"\s*([a-z][\w-]*)", line)
                if cm:
                    cmds.append(cm.group(1))
        return cmds

    @staticmethod
    def _extract_email(text):
        m = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text or "")
        return m.group(0) if m else None

    @staticmethod
    def _extract_tier(text):
        m = re.search(r"(?im)^\s*tier\s*[:=]\s*(.+?)\s*$", text or "")
        return m.group(1)[:60] if m else None

    def _email_from_credential_file(self):
        """Best-effort email from the cached credential file.

        The schema is CLI-version dependent, so we scan defensively for
        email-like values and never return anything else from the file.
        """
        path = os.path.join(self._muse_config_dir(), "muse", "auth.json")
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None

        def scan(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, str) and "email" in k.lower():
                        email = self._extract_email(v)
                        if email:
                            return email
                    found = scan(v)
                    if found:
                        return found
            elif isinstance(obj, list):
                for v in obj:
                    found = scan(v)
                    if found:
                        return found
            return None

        return scan(data)

    def account_info(self):
        """Who is signed in? Best-effort; never includes secrets.

        Strategy: if the active credential is an API key there is usually no
        local identity to show; otherwise probe the CLI for an account/status
        command (only ones advertised in --help), then fall back to scanning
        the cached credential file for an email address.
        """
        info = {"email": None, "tier": None, "source": None, "detail": ""}
        status = self.status()
        if status["env_key"]:
            info["source"] = "API key (META_API_KEY, environment)"
            info["detail"] = ("API keys aren't tied to a visible account locally; "
                              "the key itself selects the billing account.")
            return info
        if status["keychain_key"] and not status["stored_credential"]:
            info["source"] = "API key (macOS Keychain)"
            info["detail"] = ("API keys aren't tied to a visible account locally; "
                              "the key itself selects the billing account.")
            return info
        if status["file_key"] and not status["stored_credential"]:
            info["source"] = "API key (local file, ~/.musepilot/)"
            info["detail"] = ("API keys aren't tied to a visible account locally; "
                              "the key itself selects the billing account.")
            return info
        if not status["stored_credential"]:
            info["detail"] = "Not signed in."
            return info

        info["source"] = "Stored credential (`muse login`)"
        if self.cli.available:
            cmds = set(self._top_commands())
            probes = []
            if "account" in cmds:
                probes.append(["account", "status"])
            if "auth" in cmds:
                probes.append(["auth", "status"])
            if "whoami" in cmds:
                probes.append(["whoami"])
            for argv in probes:
                rc, out, err = run_cmd([self.cli.binary] + argv, timeout=15)
                if rc != 0:
                    continue
                text = out or ""
                email = self._extract_email(text)
                tier = self._extract_tier(text)
                info["email"] = email
                info["tier"] = tier
                info["detail"] = "From `muse %s`." % " ".join(argv)
                if email or tier:
                    return info
        email = self._email_from_credential_file()
        if email:
            info["email"] = email
            info["detail"] = "From the cached credential file."
        else:
            info["detail"] = ("Signed in, but this CLI version doesn't expose "
                              "the account email.")
        return info


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "plan_tier": "everyday",
    "prompt_budget": 30,          # estimated prompts allowed per 5h window
    "default_effort": "high",
    "default_model": None,        # None => CLI default
    "default_max_steps": 40,
    "default_workdir": HOME,
    "auto_approve": False,        # maps to --yolo; off by default (safer)
    "auto_install_clis": True,   # v0.4.2: auto-download muse/claude if missing on launch
    # v0.6.0: "provider" is "meta", "baseten", or a custom provider id.
    # Custom providers live in "custom_providers" (see resolve_provider).
    "provider": "meta",
    "custom_providers": [],
    "baseten_effort": "xhigh",
    "baseten_model": BASETEN_MODEL,
    "default_context_window": None,
    # v0.4.7: conversation memory. Session ids are real UUIDs (Claude and
    # Muse both require UUID session ids; MusePilot previously sent a
    # 12-hex UI id that changed on every page load, so memory was lost).
    # They live in config so a reload or server restart keeps the same
    # conversation; New conversation resets them (see new_conversation).
    "conversation_id": 1,
    "claude_session_id": None,
    "claude_session_started": False,
    "muse_session_id": None,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        if isinstance(saved, dict):
            cfg.update({k: v for k, v in saved.items() if k in DEFAULT_CONFIG})
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
            json.dump({k: cfg.get(k) for k in DEFAULT_CONFIG}, fh, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Usage ledger — rolling 5-hour window
# ---------------------------------------------------------------------------

class UsageLedger:
    """Append-only JSONL ledger of Muse Code invocations.

    Each entry: {ts, model, effort, input_tokens, output_tokens, total_tokens,
                 steps, prompt_chars}. The 5-hour window is computed as a
    trailing window over `ts` — usage ages out exactly 5h after it happened.
    """

    def __init__(self, path=USAGE_FILE):
        self.path = path
        self._lock = threading.Lock()

    def record(self, entry):
        entry = dict(entry)
        entry.setdefault("ts", now_ts())
        line = json.dumps(entry)
        with self._lock:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return entry

    def _read_all(self):
        entries = []
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue
        except FileNotFoundError:
            pass
        return entries

    def window_usage(self, window_seconds=WINDOW_SECONDS, at=None):
        """Totals over the trailing window. Returns dict with counts."""
        at = at if at is not None else now_ts()
        cutoff = at - window_seconds
        prompts = 0
        in_tok = out_tok = total_tok = 0
        oldest_in_window = None
        for e in self._read_all():
            ts = e.get("ts", 0)
            if ts > cutoff:
                prompts += 1
                in_tok += int(e.get("input_tokens") or 0)
                out_tok += int(e.get("output_tokens") or 0)
                total_tok += int(e.get("total_tokens") or 0)
                if oldest_in_window is None or ts < oldest_in_window:
                    oldest_in_window = ts
        return {
            "window_seconds": window_seconds,
            "prompts": prompts,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_tokens": total_tok,
            "oldest_ts": oldest_in_window,
            # When the oldest in-window usage ages out (None if window empty).
            "next_capacity_at": (oldest_in_window + window_seconds
                                 if oldest_in_window else None),
        }

    def can_invoke(self, budget):
        """True if another prompt fits in the current window under budget."""
        usage = self.window_usage()
        return usage["prompts"] < budget, usage


# ---------------------------------------------------------------------------
# Prompt queue — "invoke when possible"
# ---------------------------------------------------------------------------

class PromptQueue:
    def __init__(self, path=QUEUE_FILE):
        self.path = path
        self._lock = threading.Lock()

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _write(self, items):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(items, fh, indent=2)

    def add(self, item):
        item = dict(item)
        item.setdefault("id", uuid.uuid4().hex[:12])
        item.setdefault("queued_at", now_ts())
        item.setdefault("status", "queued")
        with self._lock:
            items = self._read()
            items.append(item)
            self._write(items)
        return item

    def list(self):
        with self._lock:
            return self._read()

    def pop_next(self):
        with self._lock:
            items = self._read()
            for i, item in enumerate(items):
                if item.get("status") == "queued":
                    item["status"] = "running"
                    item["started_at"] = now_ts()
                    self._write(items)
                    return item
        return None

    def mark_done(self, item_id, ok=True, note=""):
        with self._lock:
            items = self._read()
            items = [it for it in items
                     if not (it.get("id") == item_id)]
            self._write(items)

    def remove(self, item_id):
        with self._lock:
            items = self._read()
            items = [it for it in items if it.get("id") != item_id]
            self._write(items)


# ---------------------------------------------------------------------------
# JSONL event parsing (tolerant — the exact event shape is CLI-version
# dependent, so we scan for known shapes instead of assuming one).
# ---------------------------------------------------------------------------

def _find_usage(obj):
    """Recursively find the first dict that looks like token usage."""
    if isinstance(obj, dict):
        keys = {k.lower() for k in obj.keys()}
        has_in = any(k in keys for k in ("input_tokens", "inputtokens", "prompt_tokens", "prompttokens"))
        has_out = any(k in keys for k in ("output_tokens", "outputtokens", "completion_tokens", "completiontokens"))
        has_total = any(k in keys for k in ("total_tokens", "totaltokens"))
        if (has_in and has_out) or has_total:
            def num(*names):
                for n in names:
                    for k in obj.keys():
                        if k.lower() == n:
                            try:
                                return int(obj[k])
                            except (TypeError, ValueError):
                                pass
                return 0
            total = num("total_tokens", "totaltokens")
            inp = num("input_tokens", "inputtokens", "prompt_tokens", "prompttokens")
            outp = num("output_tokens", "outputtokens", "completion_tokens", "completiontokens")
            if total == 0:
                total = inp + outp
            return {"input_tokens": inp, "output_tokens": outp, "total_tokens": total}
        for v in obj.values():
            found = _find_usage(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_usage(v)
            if found:
                return found
    return None


def _extract_text(obj):
    """Best-effort text extraction from a JSONL event object."""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        # common streaming shapes
        for key in ("delta", "text", "content", "message", "output"):
            if key in obj:
                t = _extract_text(obj[key])
                if t:
                    return t
        # OpenAI-style choices
        choices = obj.get("choices")
        if isinstance(choices, list) and choices:
            t = _extract_text(choices[0])
            if t:
                return t
        # nested message.content
        msg = obj.get("message")
        if isinstance(msg, dict):
            t = _extract_text(msg.get("content"))
            if t:
                return t
    if isinstance(obj, list):
        parts = [_extract_text(v) for v in obj]
        parts = [p for p in parts if p]
        return "".join(parts) if parts else ""
    return ""


def parse_jsonl_line(line):
    """Parse one stdout line from `muse exec --json`.

    Returns (kind, payload) where kind is one of:
      text | tool | usage | event | error | done
    """
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        # plain text line — treat as assistant text
        return ("text", {"text": line})
    if not isinstance(obj, dict):
        t = _extract_text(obj)
        return ("text", {"text": t}) if t else None

    usage = _find_usage(obj)
    etype = str(obj.get("type", "")).lower()

    if usage and (usage["total_tokens"] or usage["input_tokens"] or usage["output_tokens"]):
        return ("usage", usage)
    if any(k in etype for k in ("tool", "function_call", "tool_call")):
        name = obj.get("name") or obj.get("tool") or etype
        return ("tool", {"name": str(name), "detail": _extract_text(obj)[:300]})
    if "error" in etype or obj.get("error"):
        return ("error", {"error": str(obj.get("error") or _extract_text(obj))[:500]})
    text = _extract_text(obj)
    if text:
        return ("text", {"text": text})
    return ("event", {"raw": obj})


# ---------------------------------------------------------------------------
# Exec driver — builds the `muse exec` command from UI selections
# ---------------------------------------------------------------------------

class ExecDriver:
    def __init__(self, cli: MuseCLI, ledger: UsageLedger, auth: AuthManager = None):
        self.cli = cli
        self.ledger = ledger
        self.auth = auth
        self._run_lock = threading.Lock()
        self.active_run = None  # run-id when a run is in flight
        self.active_proc = None  # Popen of the in-flight CLI run (for Stop)
        self._stop_requested = threading.Event()

    def stop(self):
        """Ask the in-flight run to stop; terminate its subprocess if any.

        Returns True when a run was in flight (lock held or child alive).
        The run loop notices the flag, kills the child, records
        "Run stopped by user." and releases the lock in its finally block.
        """
        self._stop_requested.set()
        proc = self.active_proc
        alive = proc is not None and proc.poll() is None
        if alive:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001 - already gone is fine
                pass
        return alive or self._run_lock.locked()

    def build_command(self, prompt, *, model=None, effort=None,
                      context_window=None, max_steps=None, session_id=None,
                      auto_approve=False, prompt_file=None, image_paths=None):
        """Build the argv list. Selections map to CLI flags dynamically."""
        opts = self.cli.discover_options()
        cmd = [self.cli.binary, "exec", "--json"]
        if model and opts["supports_model_flag"]:
            cmd += ["--model", model]
        if effort and opts["supports_effort_flag"]:
            cmd += ["--reasoning-effort", effort]
        if context_window and opts["context_flag"]:
            cmd += [opts["context_flag"], str(context_window)]
        if max_steps and opts["supports_max_steps"]:
            cmd += ["--max-model-steps", str(max_steps)]
        if session_id and opts["supports_session_id"]:
            cmd += ["--session-id", session_id]
        if auto_approve and opts["supports_yolo"]:
            cmd += ["--yolo"]
        if image_paths and opts.get("supports_image_flag"):
            for _ip in image_paths:
                cmd += ["--image", _ip]
        if prompt_file and opts["supports_prompt_file"]:
            cmd += ["--prompt-file", prompt_file]
        else:
            cmd += [prompt]
        return cmd

    def run(self, prompt, *, model=None, effort=None, context_window=None,
            max_steps=None, session_id=None, auto_approve=False,
            workdir=None, on_event=None, max_runtime=1800, attachments=None):
        """Run one prompt to completion. on_event(kind, payload) streams.

        Returns a result dict and records usage in the ledger.
        Raises RuntimeError if the CLI is missing or another run is active.
        """
        if not self.cli.available:
            raise RuntimeError("The `muse` CLI was not found. Install it first: "
                               "curl -fsSL https://dev.meta.ai/install.sh | sh")
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("Another Muse Code run is already in progress.")

        run_id = uuid.uuid4().hex[:12]
        self.active_run = run_id
        self._stop_requested.clear()
        prompt_path = None
        try:
            # Write prompt to a temp file to avoid any shell-quoting issues.
            # (No shell is used at all — argv list only.)
            prompt_full = prompt + attachments_prompt_suffix(attachments or [])
            image_paths = [a["path"] for a in (attachments or []) if a["kind"] == "image"]
            with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                             encoding="utf-8") as fh:
                fh.write(prompt_full)
                prompt_path = fh.name

            cmd = self.build_command(
                prompt, model=model, effort=effort,
                context_window=context_window, max_steps=max_steps,
                session_id=session_id, auto_approve=auto_approve,
                prompt_file=prompt_path, image_paths=image_paths)

            workdir = workdir or HOME
            if not os.path.isdir(workdir):
                workdir = HOME

            child_env = self.auth.child_env() if self.auth else dict(os.environ)
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, text=True, cwd=workdir,
                bufsize=1, env=child_env)
            self.active_proc = proc

            full_text_parts = []
            usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            steps = 0
            error = None
            started = now_ts()

            def emit(kind, payload):
                if on_event:
                    try:
                        on_event(kind, payload)
                    except Exception:
                        pass

            emit("event", {"run_id": run_id, "cmd": " ".join(cmd[1:])})

            try:
                for line in proc.stdout:
                    if self._stop_requested.is_set():
                        try:
                            proc.kill()
                        except Exception:  # noqa: BLE001 - already gone is fine
                            pass
                        error = "Run stopped by user."
                        emit("error", {"error": error})
                        break
                    if now_ts() - started > max_runtime:
                        proc.kill()
                        error = f"Run exceeded the {max_runtime//60}-minute limit and was stopped."
                        emit("error", {"error": error})
                        break
                    parsed = parse_jsonl_line(line)
                    if not parsed:
                        continue
                    kind, payload = parsed
                    if kind == "text":
                        full_text_parts.append(payload.get("text", ""))
                    elif kind == "usage":
                        for k in usage:
                            usage[k] = max(usage[k], int(payload.get(k) or 0))
                    elif kind == "tool":
                        steps += 1
                    elif kind == "error" and not error:
                        error = payload.get("error")
                    emit(kind, payload)
            finally:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                # drain stderr for diagnostics
                try:
                    _, stderr_text = proc.communicate(timeout=5)
                except Exception:
                    stderr_text = ""
                if self._stop_requested.is_set():
                    # Prefer the user's stop over the SIGTERM exit code.
                    error = "Run stopped by user."
                if proc.returncode not in (0, None) and not error:
                    tail = (stderr_text or "").strip().splitlines()
                    error = ("muse exited with code %s%s"
                             % (proc.returncode,
                                (": " + tail[-1][:300]) if tail else ""))

            result = {
                "run_id": run_id,
                "ok": error is None,
                "error": error,
                "text": "".join(full_text_parts),
                "usage": usage,
                "steps": steps,
                "model": model,
                "effort": effort,
                "elapsed": round(now_ts() - started, 1),
            }
            emit("done", result)

            # Ledger: every completed invocation counts against the 5h window,
            # even if it errored (tokens may still have been consumed).
            self.ledger.record({
                "ts": now_ts(),
                "model": model,
                "effort": effort,
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "total_tokens": usage["total_tokens"],
                "steps": steps,
                "prompt_chars": len(prompt),
                "ok": result["ok"],
            })
            return result
        finally:
            if prompt_path:
                try:
                    os.unlink(prompt_path)
                except OSError:
                    pass
            self.active_run = None
            self.active_proc = None
            self._run_lock.release()

# ---------------------------------------------------------------------------
# Run records (for queued/background runs the UI polls)
# ---------------------------------------------------------------------------

def save_run_record(record):
    try:
        path = os.path.join(RUNS_DIR, record["run_id"] + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
        # prune old runs, keep newest 50
        runs = sorted(
            (f for f in os.listdir(RUNS_DIR) if f.endswith(".json")),
            key=lambda f: os.path.getmtime(os.path.join(RUNS_DIR, f)),
        )
        for old in runs[:-50]:
            try:
                os.unlink(os.path.join(RUNS_DIR, old))
            except OSError:
                pass
    except OSError:
        pass


def list_run_records(limit=20):
    records = []
    try:
        files = [f for f in os.listdir(RUNS_DIR) if f.endswith(".json")]
    except OSError:
        return []
    for f in sorted(files, key=lambda x: os.path.getmtime(os.path.join(RUNS_DIR, x)),
                    reverse=True)[:limit]:
        try:
            with open(os.path.join(RUNS_DIR, f), encoding="utf-8") as fh:
                records.append(json.load(fh))
        except (OSError, ValueError):
            continue
    return records


# ---------------------------------------------------------------------------
# Saved conversations (v0.4.7) — one JSONL file per conversation.
# Each line: {ts, role, provider, model, effort, text, error}. A file is
# a transcript, not a second model memory: the model memory itself is the
# native session (Claude --resume / Muse --session-id) below. When a file
# would grow past MAX_CONVERSATION_FILE_BYTES, a new file starts and the
# old file is kept, never overwritten or deleted.
# ---------------------------------------------------------------------------

def conversation_file_path(conversation_id):
    return os.path.join(CONVERSATIONS_DIR, "conversation-%03d.jsonl" % int(conversation_id))


def is_valid_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Attachments (v0.4.8) — paste / drop / pick a file or image in the
# composer, upload it to the local server, and pass it to the agent.
# Server files live under ~/.musepilot/attachments/ and are kept (so a
# resumed session can still read them). Names are sanitised to a bare
# filename; paths outside ATTACHMENTS_DIR are never trusted from the
# browser — resolve_attachments() drops anything that escapes it.
# ---------------------------------------------------------------------------

def sanitize_attachment_name(name):
    import re as _re
    base = os.path.basename(str(name or ""))
    base = _re.sub(r"[^A-Za-z0-9._\- ]", "_", base).strip()
    if not base or base in (".", ".."):
        base = "attachment"
    if base.startswith("."):
        base = "file" + base
    return base


def save_attachment(name, mime, data_bytes):
    if data_bytes is None:
        raise ValueError("no file data")
    if len(data_bytes) > MAX_ATTACHMENT_BYTES:
        raise ValueError("file too large (max 10 MiB)")
    ensure_dirs()
    safe = sanitize_attachment_name(name)
    aid = uuid.uuid4().hex[:12]
    path = os.path.join(ATTACHMENTS_DIR, aid + "_" + safe)
    with open(path, "wb") as fh:
        fh.write(data_bytes)
    kind = "image" if (mime or "").startswith("image/") else "file"
    return {"id": aid, "name": safe, "mime": mime or "", "size": len(data_bytes),
            "path": path, "kind": kind}


def resolve_attachments(raw):
    """Validate browser-supplied attachments; drop anything unsafe."""
    out = []
    base = os.path.abspath(ATTACHMENTS_DIR)
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        path = item.get("path") or ""
        try:
            resolved = os.path.abspath(path)
        except Exception:
            continue
        if not (resolved == base or resolved.startswith(base + os.sep)):
            continue
        if not os.path.isfile(resolved):
            continue
        import mimetypes as _mt
        mime = item.get("mime") or _mt.guess_type(resolved)[0] or ""
        kind = "image" if mime.startswith("image/") else "file"
        if kind == "file" and os.path.splitext(resolved)[1].lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            kind, mime = "image", mime or "image/png"
        out.append({"id": item.get("id") or "", "name": os.path.basename(resolved),
                    "mime": mime, "size": os.path.getsize(resolved),
                    "path": resolved, "kind": kind})
    return out


def attachments_prompt_suffix(attachments):
    if not attachments:
        return ""
    lines = ["", "", "Attached files — please read them with your file tools before answering:"]
    for a in attachments:
        if a["kind"] == "image":
            lines.append("- %s (%s, image — read the image file to see its contents)" % (a["path"], a["name"]))
        else:
            lines.append("- %s (%s)" % (a["path"], a["name"]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Background worker — auto-invokes queued prompts when the window allows
# ---------------------------------------------------------------------------

class QueueWorker(threading.Thread):
    def __init__(self, app, interval=20):
        super().__init__(daemon=True, name="musepilot-queue")
        self.app = app
        self.interval = interval
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:
                pass  # never kill the worker on a bad tick

    def tick(self):
        app = self.app
        item = app.queue.pop_next()
        if not item:
            return
        cfg = app.config
        # v0.6.0: the 5-hour window budget is a muse-harness limit only.
        # Other harnesses run on their own keys, not on that window,
        # so they must never be held by it — in the queue or in /api/chat.
        qspec = app.resolve_provider(item.get("provider") or cfg.get("provider") or "meta")
        ok, usage = ((True, {}) if qspec["harness"] != HARNESS_MUSE
                     else app.ledger.can_invoke(int(cfg.get("prompt_budget", 30))))
        if not ok:
            # put it back to queued; try again later
            with app.queue._lock:
                items = app.queue._read()
                for it in items:
                    if it.get("id") == item["id"]:
                        it["status"] = "queued"
                        it.pop("started_at", None)
                app.queue._write(items)
            return
        if app.driver._run_lock.locked():
            with app.queue._lock:
                items = app.queue._read()
                for it in items:
                    if it.get("id") == item["id"]:
                        it["status"] = "queued"
                        it.pop("started_at", None)
                app.queue._write(items)
            return
        record = {
            "run_id": uuid.uuid4().hex[:12],
            "queued_id": item["id"],
            "prompt": item["prompt"],
            "model": item.get("model"),
            "effort": item.get("effort"),
            "status": "running",
            "started_at": now_ts(),
            "text": "",
        }
        save_run_record(record)

        def on_event(kind, payload):
            if kind == "text":
                record["text"] += payload.get("text", "")
                save_run_record(record)

        qatts = resolve_attachments(item.get("attachments"))
        # Effort default is harness-specific (see _run_chat_sse).
        if qspec["harness"] == HARNESS_MUSE:
            qeff = item.get("effort") or cfg.get("default_effort")
        else:
            qeff = item.get("effort") or cfg.get("baseten_effort") or "xhigh"
        try:
            # v0.6.0: one dispatch for every harness.
            result = app.run_provider_chat(
                qspec, item["prompt"],
                effort=qeff,
                workdir=item.get("workdir") or cfg.get("default_workdir"),
                auto_approve=item.get("auto_approve", cfg.get("auto_approve", False)),
                on_event=on_event, attachments=qatts,
                model=item.get("model") or cfg.get("default_model"),
                context_window=item.get("context_window"),
                max_steps=item.get("max_steps") or cfg.get("default_max_steps"),
                session_id=app.get_muse_session_id(),
            )
            record.update({
                "status": "done" if result["ok"] else "error",
                "text": result["text"],
                "usage": result["usage"],
                "error": result.get("error"),
                "finished_at": now_ts(),
            })
        except Exception as e:  # noqa: BLE001 — keep the queue moving
            record.update({"status": "error", "error": str(e)[:500],
                           "finished_at": now_ts()})
        save_run_record(record)
        try:
            app.append_conversation(
                item.get("provider") or cfg.get("provider") or "meta",
                item["prompt"], record.get("text", ""),
                model=record.get("model"), effort=record.get("effort"),
                error=record.get("error"), attachments=qatts)
        except Exception:
            pass
        app.queue.mark_done(item["id"])


def query_baseten_model(api_key, base=None, timeout=15):
    """Query Baseten GET /v1/models and return this model's entry.

    Returns (entry, error): entry is the model's list entry dict, or
    None; error is None on success, else a short human-readable reason.
    The key is used ONLY in this server-side call and is never sent
    to the browser or logged. GET /v1/models/<id> is not a Baseten
    endpoint (405), so the list is fetched and filtered by id.
    """
    if not api_key:
        return None, "No Baseten API key saved."
    import urllib.error
    import urllib.request
    url = (base or BASETEN_API_BASE).rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + api_key,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, "Baseten API key rejected (HTTP %s)." % e.code
        return None, "Baseten /v1/models returned HTTP %s." % e.code
    except Exception as e:  # noqa: BLE001 - network errors are user-facing
        return None, "Could not reach Baseten /v1/models: %s" % str(e)[:150]
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None, "Baseten /v1/models returned no model list."
    for item in data:
        if isinstance(item, dict) and item.get("id") == BASETEN_MODEL:
            return item, None
    return None, "Baseten /v1/models did not list %s." % BASETEN_MODEL


# ---------------------------------------------------------------------------
# Claude Code driver (v0.4.5) — Baseten model via the `claude` CLI.
#
# Live-proven 2026-10-26/27, no proxy: Claude Code speaks Anthropic
# Messages, and Baseten exposes that API (beta) at
# https://inference.baseten.co (NO /v1 suffix — the /v1 form is the
# OpenAI-compatible endpoint and fails for Anthropic traffic). Env:
#   ANTHROPIC_BASE_URL=https://inference.baseten.co
#   ANTHROPIC_AUTH_TOKEN=<Baseten key>  (the same Baseten key slot)
#   ANTHROPIC_CUSTOM_HEADERS="Authorization: Bearer <Baseten key>"
#   ANTHROPIC_MODEL / ANTHROPIC_SMALL_FAST_MODEL = BASETEN_MODEL
# Claude warns it does not recognise the model and assumes 200k
# context (actual 1M, from Baseten /v1/models) — non-blocking, and
# MusePilot labels the true 1M context in model detail.
# Tools: in -p (non-interactive) mode WebSearch/WebFetch are NOT
# granted by default (live-tested: the model reported "permission not
# granted"). MusePilot therefore pre-allows read-only tools with
# --allowedTools, LAST in argv (it is variadic and would swallow
# following flags). File edits / shell are NOT pre-allowed: they run
# only when the user turns on auto-approve, which maps to
# --permission-mode acceptEdits. Session continuity: MusePilot's
# session ids are 12 hex chars, not UUIDs, so --session-id is NOT
# passed — Claude runs are per-prompt in v0.4.5 (like direct Baseten),
# but with tools. Effort maps to Claude's --effort flag
# (low/medium/high/xhigh, default xhigh).
# ---------------------------------------------------------------------------

CLAUDE_BASETEN_BASE_URL = "https://inference.baseten.co"
CLAUDE_READ_TOOLS = "WebSearch,WebFetch,Read,Glob,Grep"
CLAUDE_WRITE_TOOLS = "WebSearch,WebFetch,Read,Glob,Grep,Edit,Write,Bash"


def claude_baseten_env(api_key, base_url=None, model=None):
    # v0.6.0: base_url/model are per-provider (custom providers may point
    # the Claude Code harness at any Anthropic-compatible endpoint).
    env = dict(os.environ)
    env["ANTHROPIC_BASE_URL"] = base_url or CLAUDE_BASETEN_BASE_URL
    env["ANTHROPIC_AUTH_TOKEN"] = api_key
    env["ANTHROPIC_CUSTOM_HEADERS"] = "Authorization: Bearer " + api_key
    env["ANTHROPIC_MODEL"] = model or BASETEN_MODEL
    env["ANTHROPIC_SMALL_FAST_MODEL"] = model or BASETEN_MODEL
    return env


def claude_chat_stream(binary, api_key, prompt, effort, workdir, auto_approve, on_event, timeout=600, session_id=None, resume=False,
                     stop_event=None, register_proc=None, _env=None):
    """Run `claude -p` and stream text/tool events.

    Returns (text, usage, error). Usage total = input + cache-read +
    output, matching how MusePilot counts Baseten direct usage.
    stop_event (threading.Event, optional) aborts the run promptly with
    "Run stopped by user."; register_proc(proc) exposes the child so a
    stopper can terminate a hung process. _env overrides the child
    environment (v0.6.0: per-provider base URL / model).
    """
    import subprocess
    tools = CLAUDE_WRITE_TOOLS if auto_approve else CLAUDE_READ_TOOLS
    argv = [binary, "-p", prompt,
            "--output-format", "stream-json", "--verbose",
            "--model", BASETEN_MODEL, "--effort", effort or "xhigh"]
    # Session memory (v0.4.7): first run in a conversation uses
    # --session-id <uuid>; later runs use --resume <uuid>. Both must come
    # before --allowedTools, which is variadic and must stay last.
    if session_id and resume:
        argv += ["--resume", session_id]
    elif session_id:
        argv += ["--session-id", session_id]
    if auto_approve:
        argv += ["--permission-mode", "acceptEdits"]
    # --allowedTools LAST: it is variadic.
    argv += ["--allowedTools", tools]
    cwd = workdir if workdir and os.path.isdir(workdir) else HOME
    env = _env if _env is not None else claude_baseten_env(api_key)
    text_parts = []
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, cwd=cwd, env=env)
    except Exception as e:  # noqa: BLE001 - launch errors are user-facing
        return "", usage, "Could not launch Claude Code: %s" % str(e)[:200]
    if register_proc:
        try:
            register_proc(proc)
        except Exception:  # noqa: BLE001 - registration is best-effort
            pass
    err_tail = ""
    try:
        for line in proc.stdout:
            if stop_event is not None and stop_event.is_set():
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001 - already gone is fine
                    pass
                return "".join(text_parts), usage, "Run stopped by user."
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "assistant":
                for block in (ev.get("message") or {}).get("content") or []:
                    if block.get("type") == "text" and block.get("text"):
                        text_parts.append(block["text"])
                        if on_event:
                            on_event("text", {"text": block["text"]})
                    elif block.get("type") == "tool_use" and block.get("name"):
                        if on_event:
                            on_event("tool", {"name": block["name"]})
            elif ev.get("type") == "result":
                if ev.get("result"):
                    # Result text duplicates streamed text; do not re-emit.
                    pass
                u = ev.get("usage") or {}
                inp = int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
                outp = int(u.get("output_tokens") or 0)
                usage = {"input_tokens": inp, "output_tokens": outp, "total_tokens": inp + outp}
                if ev.get("is_error"):
                    return "".join(text_parts), usage, str(ev.get("result") or "Claude Code reported an error.")[:300]
        proc.wait(timeout=timeout)
        _, err = proc.communicate(timeout=30) if proc.stdout.closed else (None, "")
        # stderr is captured via PIPE above; read any remainder safely.
        err_tail = ""
        if proc.returncode != 0 and not text_parts:
            return "", usage, "Claude Code exited %s." % proc.returncode
        return "".join(text_parts), usage, None
    except Exception as e:  # noqa: BLE001 - stream errors are user-facing
        try:
            proc.kill()
        except Exception:
            pass
        return "".join(text_parts), usage, "Claude Code run failed: %s" % str(e)[:300]


# ---------------------------------------------------------------------------
# Baseten direct chat (v0.4.0) — OpenAI-compatible Chat Completions, streaming.
# Used ONLY when provider == "baseten". The `muse` CLI is not involved:
# it cannot be pointed at Baseten (sanctioned-front-door error, tested).
# Reasoning content in the stream is NOT forwarded as chat text (it is
# the model's thinking, not its answer); usage is taken from the final
# stream chunk when stream_options.include_usage is honoured.
# ---------------------------------------------------------------------------

def baseten_chat_stream(api_key, prompt, effort, on_event, max_tokens=BASETEN_MAX_TOKENS, attachments=None,
                      stop_event=None, base_url=None, model=None):
    """Stream one direct-API chat reply. Returns (text, usage, error).

    v0.6.0: base_url/model are per-provider (custom providers may point
    the direct harness at any OpenAI-compatible endpoint); when omitted
    the builtin Baseten defaults apply.

    Attachments (v0.4.8): Baseten direct has no file tools, so it cannot
    read a local path. Text files are inlined into the prompt (capped),
    images are sent as vision blocks. Other file types are named, with a
    note that Claude/Muse mode can read them — never silently dropped.
    stop_event (threading.Event, optional) aborts the stream promptly
    with "Run stopped by user.".
    """
    import urllib.error
    import urllib.request
    import base64 as _b64
    text_prompt = prompt
    content = None
    if attachments:
        parts = [{"type": "text", "text": ""}]
        text_chunks = [prompt]
        for a in attachments:
            if a["kind"] == "image":
                try:
                    with open(a["path"], "rb") as fh:
                        b64 = _b64.b64encode(fh.read()).decode("ascii")
                    parts.append({"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (a["mime"] or "image/png", b64)}})
                    text_chunks.append("\n[Attached image: %s — see the image above.]" % a["name"])
                except OSError:
                    text_chunks.append("\n[Attached image %s could not be read.]" % a["name"])
            elif a["mime"].startswith("text/") or os.path.splitext(a["path"])[1].lower() in (".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".html", ".css"):
                try:
                    with open(a["path"], encoding="utf-8", errors="replace") as fh:
                        data = fh.read(MAX_BASETEN_TEXT_ATTACHMENT_CHARS + 1)
                    if len(data) > MAX_BASETEN_TEXT_ATTACHMENT_CHARS:
                        data = data[:MAX_BASETEN_TEXT_ATTACHMENT_CHARS] + "\n[truncated]"
                    text_chunks.append("\n\n--- Attached file: %s ---\n%s" % (a["name"], data))
                except OSError:
                    text_chunks.append("\n[Attached file %s could not be read.]" % a["name"])
            else:
                text_chunks.append("\n[Attached file: %s — Baseten direct cannot read this file type; use Claude Code or Muse Code mode to read it.]" % a["name"])
        parts[0]["text"] = "\n".join(text_chunks)
        content = parts
        text_prompt = parts[0]["text"]
    body = {
        "model": model or BASETEN_MODEL,
        "messages": [{"role": "user", "content": content if content is not None else text_prompt}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": max_tokens,
    }
    if effort:
        body["reasoning_effort"] = effort
    req = urllib.request.Request(
        (base_url or BASETEN_API_BASE).rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + api_key,
                 "Content-Type": "application/json",
                 "Accept": "text/event-stream"},
        method="POST")
    text_parts = []
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            for raw in resp:
                if stop_event is not None and stop_event.is_set():
                    return "".join(text_parts), usage, "Run stopped by user."
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if chunk.get("usage"):
                    u = chunk["usage"]
                    usage = {"input_tokens": int(u.get("prompt_tokens") or 0),
                             "output_tokens": int(u.get("completion_tokens") or 0),
                             "total_tokens": int(u.get("total_tokens") or 0)}
                for choice in chunk.get("choices", []) or []:
                    delta = choice.get("delta") or {}
                    piece = delta.get("content") or ""
                    if piece:
                        text_parts.append(piece)
                        if on_event:
                            on_event("text", {"text": piece})
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            detail = ""
        return "", usage, "Baseten returned HTTP %s%s" % (e.code, (": " + detail) if detail else "")
    except Exception as e:  # noqa: BLE001 - network errors are user-facing
        return "", usage, "Could not reach Baseten: %s" % str(e)[:200]
    return "".join(text_parts), usage, None


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class App:
    def __init__(self):
        ensure_dirs()
        self.config = load_config()
        self.cli = MuseCLI()
        # v0.4.2: CLI provisioning. on_installed refreshes the muse
        # driver so a freshly downloaded binary is usable without a
        # restart; claude is provisioned for the user (Baseten agentic
        # path) even though MusePilot chats do not drive it yet.
        def _on_cli_installed(name, path):
            if name == "muse":
                self.cli.refresh()
        self.cli_manager = CliManager(on_installed=_on_cli_installed)
        self.ledger = UsageLedger()
        self.queue = PromptQueue()
        self.auth = AuthManager(self.cli)
        # Discovery / model-profile probes run with the same key-injected
        # environment chat runs use, so an authenticated CLI can resolve
        # its provider catalog (when it offers one) instead of the
        # unauthenticated one.
        self.cli.env_provider = self.auth.child_env
        self.driver = ExecDriver(self.cli, self.ledger, self.auth)
        self.worker = QueueWorker(self)
        self._models_cache = None
        self._models_cache_at = 0
        self._baseten_model_cache = None
        self._baseten_model_cache_at = 0
        self._migrate_legacy_claude_provider()

    # ------------------------------------------------------------------
    # Providers (v0.6.0)
    # ------------------------------------------------------------------
    def resolve_provider(self, pid=None):
        """Resolve a provider id to its full spec.

        Returns {id, name, harness, base_url, model, builtin, key_slot}.
        "meta" and "baseten" are builtin; other ids are looked up in
        config["custom_providers"]. Unknown ids fall back to Muse Code
        rather than breaking the chat.
        """
        pid = pid or self.config.get("provider") or PROVIDER_META
        if pid == PROVIDER_META:
            return {"id": PROVIDER_META, "name": "Muse Code",
                    "harness": HARNESS_MUSE, "base_url": None, "model": None,
                    "builtin": True, "key_slot": "meta"}
        if pid == PROVIDER_BASETEN:
            return {"id": PROVIDER_BASETEN, "name": "Baseten",
                    "harness": HARNESS_BASETEN, "base_url": BASETEN_API_BASE,
                    "model": BASETEN_MODEL, "builtin": True,
                    "key_slot": "baseten"}
        for cp in self.config.get("custom_providers") or []:
            if cp.get("id") == pid:
                harness = cp.get("harness") or HARNESS_BASETEN
                if harness not in CUSTOM_HARNESSES:
                    harness = HARNESS_BASETEN
                return {"id": pid, "name": cp.get("name") or pid,
                        "harness": harness,
                        "base_url": cp.get("base_url") or BASETEN_API_BASE,
                        "model": cp.get("model") or BASETEN_MODEL,
                        "builtin": False,
                        "key_slot": cp.get("key_slot") or pid}
        return self.resolve_provider(PROVIDER_META)

    def _migrate_legacy_claude_provider(self):
        """v0.6.0: the builtin "claude" provider became a custom provider
        (Claude Code harness). Migrate the config once, keeping the
        existing Baseten key slot so no key re-entry is needed."""
        if (self.config.get("provider") or PROVIDER_META) != "claude":
            return
        cps = self.config.get("custom_providers") or []
        new_id = "cp_" + uuid.uuid4().hex[:8]
        cps.append({"id": new_id, "name": "Claude Code",
                    "harness": HARNESS_CLAUDE,
                    "base_url": CLAUDE_BASETEN_BASE_URL,
                    "model": BASETEN_MODEL, "key_slot": "baseten"})
        self.config["custom_providers"] = cps
        self.config["provider"] = new_id
        save_config(self.config)

    def list_providers(self):
        """All providers for the dropdown and the management dialog,
        each with key presence. Never includes key values."""
        specs = [self.resolve_provider(PROVIDER_META),
                 self.resolve_provider(PROVIDER_BASETEN)]
        for cp in self.config.get("custom_providers") or []:
            if cp.get("id"):
                specs.append(self.resolve_provider(cp.get("id")))
        current = self.config.get("provider") or PROVIDER_META
        meta_ok = self.auth.status()["ok"]
        for spec in specs:
            if spec["harness"] == HARNESS_MUSE:
                # Muse Code uses the shared Meta auth (env / muse login /
                # keychain / file), not a per-provider key.
                spec["has_key"] = bool(meta_ok)
                spec["key_kind"] = "meta"
            else:
                slot = spec["key_slot"]
                env_hit = slot == "baseten" and bool(os.environ.get("BASETEN_API_KEY"))
                spec["has_key"] = bool(env_hit or self.auth.stored_key_present(slot))
                spec["key_kind"] = "provider"
            spec["current"] = spec["id"] == current
        return {"providers": specs, "current": current}

    @staticmethod
    def _validate_custom_provider(data):
        name = (data.get("name") or "").strip()
        harness = (data.get("harness") or "").strip()
        base_url = (data.get("base_url") or "").strip()
        model = (data.get("model") or "").strip()
        if not name or len(name) > 60:
            raise ValueError("Name is required (max 60 characters).")
        if harness not in CUSTOM_HARNESSES:
            raise ValueError("Harness must be 'claude' (Claude Code) or 'baseten' (direct API).")
        if not re.match(r"^https?://", base_url) or len(base_url) > 500:
            raise ValueError("Base URL must be an http(s) URL.")
        if not model or len(model) > 200:
            raise ValueError("Model is required (max 200 characters).")
        return {"name": name, "harness": harness,
                "base_url": base_url.rstrip("/"), "model": model}

    def create_custom_provider(self, data):
        """Create a custom provider. Returns its spec."""
        clean = self._validate_custom_provider(data)
        new_id = "cp_" + uuid.uuid4().hex[:8]
        cps = self.config.get("custom_providers") or []
        cps.append({"id": new_id, "key_slot": new_id, **clean})
        self.config["custom_providers"] = cps
        save_config(self.config)
        return self.resolve_provider(new_id)

    def update_custom_provider(self, pid, data):
        """Update a custom provider's name/base_url/model/harness."""
        cps = self.config.get("custom_providers") or []
        for cp in cps:
            if cp.get("id") == pid:
                clean = self._validate_custom_provider({**cp, **data})
                cp.update(clean)
                self.config["custom_providers"] = cps
                save_config(self.config)
                return self.resolve_provider(pid)
        raise KeyError(pid)

    def delete_custom_provider(self, pid):
        """Delete a custom provider (builtin ones refuse). Its key slot
        is removed too. If it was current, fall back to Muse Code."""
        if pid in (PROVIDER_META, PROVIDER_BASETEN):
            raise ValueError("Builtin providers cannot be deleted.")
        cps = [cp for cp in (self.config.get("custom_providers") or [])
               if cp.get("id") != pid]
        if len(cps) == len(self.config.get("custom_providers") or []):
            raise KeyError(pid)
        self.config["custom_providers"] = cps
        if (self.config.get("provider") or PROVIDER_META) == pid:
            self.config["provider"] = PROVIDER_META
        save_config(self.config)
        try:
            self.auth.delete_api_key(pid)
        except Exception:  # noqa: BLE001 - key cleanup is best-effort
            pass
        return True

    def _provider_api_key(self, spec):
        """The API key for a non-muse provider spec. Env BASETEN_API_KEY
        wins for the builtin baseten slot; otherwise the saved slot.
        Secret: never log or return via API."""
        slot = spec["key_slot"]
        if slot == "baseten":
            env = os.environ.get("BASETEN_API_KEY")
            if env:
                return env
        return self.auth.get_api_key(slot)

    def run_provider_chat(self, spec, prompt, *, effort=None, workdir=None,
                          auto_approve=False, on_event=None, attachments=None,
                          model=None, context_window=None, max_steps=None,
                          session_id=None):
        """Dispatch one chat run to the provider's harness."""
        harness = spec["harness"]
        if harness == HARNESS_CLAUDE:
            key = self._provider_api_key(spec)
            if not key:
                raise RuntimeError(
                    "No API key saved for '%s'. Enter it with the key button "
                    "next to the provider." % spec["name"])
            return self.run_claude_chat(
                prompt, effort=effort, workdir=workdir,
                auto_approve=auto_approve, on_event=on_event,
                attachments=attachments, base_url=spec["base_url"],
                model=spec["model"], api_key=key)
        if harness == HARNESS_BASETEN:
            key = self._provider_api_key(spec)
            if not key:
                raise RuntimeError(
                    "No API key saved for '%s'. Enter it with the key button "
                    "next to the provider." % spec["name"])
            return self.run_baseten_chat(
                prompt, effort=effort, on_event=on_event,
                attachments=attachments, base_url=spec["base_url"],
                model=spec["model"], api_key=key)
        return self.driver.run(
            prompt, model=model, effort=effort,
            context_window=context_window, max_steps=max_steps,
            session_id=session_id, auto_approve=auto_approve,
            workdir=workdir, on_event=on_event, attachments=attachments)

    def query_models(self, force=False, provider_id=None):
        """The model list for a provider, in query order.

        v0.6.0: non-muse harnesses list exactly the provider's configured
        model (fixed per provider). The muse harness lists the discovered
        Meta models as before.
        """
        spec = self.resolve_provider(provider_id or self.config.get("provider") or "meta")
        if spec["harness"] != HARNESS_MUSE:
            # v0.4.1: context is prepopulated by QUERYING the provider's
            # GET /v1/models (with the provider's key), not from a
            # constant. Without a key, or if the query fails, the
            # model id is still listed but context is unknown — never
            # guessed, and the error is shown.
            key = self._provider_api_key(spec)
            ctx, models_error = None, None
            if key:
                if force or not self._baseten_model_cache or (now_ts() - self._baseten_model_cache_at) >= 60:
                    entry, qerr = query_baseten_model(key, base=spec["base_url"])
                    self._baseten_model_cache = entry
                    self._baseten_model_cache_at = now_ts()
                    if qerr:
                        models_error = qerr
                else:
                    entry = self._baseten_model_cache
                if entry:
                    try:
                        ctx = int(entry.get("context_length")) if entry.get("context_length") is not None else None
                    except (TypeError, ValueError):
                        ctx = None
            else:
                models_error = "Save an API key for '%s' (key button next to the provider) to query this model's context and effort details." % spec["name"]
            harness_label = "Claude Code" if spec["harness"] == HARNESS_CLAUDE else "direct API"
            result = {
                "models": [{"id": spec["model"], "label": spec["model"] + " (%s)" % spec["name"],
                            "context_window": ctx}],
                "model_source": ("%s (%s) — /v1/models (live, key-authenticated)" % (spec["name"], harness_label)) if key and not models_error else "%s (fixed model — this provider supports only this model)" % spec["name"],
                "models_error": models_error,
                "cli_available": self.cli.available,
                "cli_version": self.cli.version(),
                "auth_ok": bool(key),
                "queried_at": now_ts(),
            }
            self._models_cache = result
            self._models_cache_at = now_ts()
            return result

        # Meta branch (v0.3.0, unchanged below): 1. Meta Model API with
        # the saved key, 2. CLI discovery (strict schema enum only).
        if self._models_cache and not force and (now_ts() - self._models_cache_at) < 60:
            return self._models_cache
        key = self.auth.get_api_key() or (os.environ.get("META_API_KEY") or None)
        models, source, error = [], None, None
        if key:
            api_models, api_error = query_api_models(key)
            if api_models:
                models, source = api_models, "Meta Model API (live, key-authenticated)"
            else:
                error = api_error
        if not models:
            opts = self.cli.discover_options(force=force)
            if opts.get("models"):
                models, source = opts["models"], opts["model_source"]
            elif error is None:
                error = opts.get("model_source") or "No model list returned."
        if not models and error is None:
            error = "No model list returned by any live source."
        result = {
            "models": models,
            "model_source": source or "none",
            "models_error": None if models else error,
            "cli_available": self.cli.available,
            "cli_version": self.cli.version(),
            "auth_ok": self.auth.status()["ok"],
            "queried_at": now_ts(),
        }
        self._models_cache = result
        self._models_cache_at = now_ts()
        return result

    def model_detail(self, model_id, provider_id=None):
        """Per-model detail, queried AFTER the model is selected.

        v0.6.0: non-muse harnesses get the fixed-model detail for the
        provider's configured model (context queried live from the
        provider's /v1/models); the muse harness keeps the CLI path.
        """
        spec = self.resolve_provider(provider_id or self.config.get("provider") or "meta")
        if spec["harness"] != HARNESS_MUSE and model_id == spec["model"]:
            # Context is queried live from the provider's /v1/models.
            key = self._provider_api_key(spec)
            ctx, max_out, ctx_err = None, None, None
            if key:
                entry, qerr = query_baseten_model(key, base=spec["base_url"])
                if entry:
                    try:
                        ctx = int(entry.get("context_length")) if entry.get("context_length") is not None else None
                    except (TypeError, ValueError):
                        ctx = None
                    try:
                        max_out = int(entry.get("max_completion_tokens")) if entry.get("max_completion_tokens") is not None else None
                    except (TypeError, ValueError):
                        max_out = None
                else:
                    ctx_err = qerr
            if ctx:
                ctx_note = "Queried from %s /v1/models: %s tokens (max completion %s tokens) - fixed model property, NOT a per-request setting (a context_window parameter is silently ignored, live-tested). Send up to that many tokens in; MusePilot caps max output per reply at %s tokens." % (spec["name"], "{:,}".format(ctx), "{:,}".format(max_out) if max_out else "unknown", BASETEN_MAX_TOKENS)
            else:
                ctx_note = "Context not queried yet: %s" % (ctx_err or "save an API key for '%s' (key button next to the provider), then refresh models. Context is never guessed in MusePilot." % spec["name"])
            if spec["harness"] == HARNESS_CLAUDE:
                effort_source = "Claude Code --effort (low/medium/high/xhigh, default xhigh)"
                effort_scope = "per-model (Claude Code) — effort maps to claude --effort; tools: WebSearch, WebFetch, Read, Glob, Grep pre-allowed, Edit/Write/Bash only with auto-approve"
                profile_note = "Claude Code agent (%s, no proxy). Session continuity in v0.4.7 — first run passes --session-id <uuid>, later runs pass --resume <uuid>; New conversation resets the uuid." % spec["base_url"]
            else:
                effort_source = "Baseten reasoning docs — Muse Spark Open row"
                effort_scope = "per-model (direct API) — low/medium/high/xhigh, default xhigh"
                profile_note = "Direct Chat Completions API. 'none'/'minimal' are not distinct levels for this model (docs: they run at low) and are not offered; reasoning cannot be turned off for Muse Spark Open."
            return {
                "model": spec["model"],
                "reasoning_efforts": list(BASETEN_EFFORTS),
                "effort_source": effort_source,
                "effort_scope": effort_scope,
                "default_reasoning_effort": "xhigh",
                "profile": None,
                "profile_note": profile_note,
                "context_flag": None,
                "context_source": "%s /v1/models (context_length, live query)" % spec["name"],
                "context_window": ctx,
                "context_note": ctx_note,
            }

        if not valid_model_id(model_id):
            return {"error": "Invalid model id."}
        opts = self.cli.discover_options()
        profile = self.cli.model_profile(model_id)
        api_ctx = None
        # If the model came from the API list, carry its reported window
        # (reference only — the CLI still cannot set it without a flag).
        try:
            for m in self.query_models().get("models", []):
                if m["id"] == model_id:
                    api_ctx = m.get("context_window")
                    break
        except Exception:  # noqa: BLE001 - detail must survive a bad API
            pass
        context_flag = opts.get("context_flag")
        return {
            "model": model_id,
            "reasoning_efforts": opts.get("reasoning_efforts", []),
            "effort_source": opts.get("effort_source"),
            "effort_scope": "global CLI tiers (the CLI defines effort once, not per model)",
            "default_reasoning_effort": (profile or {}).get("default_reasoning_effort"),
            "profile": profile,
            "profile_note": ("model-profile does not validate model ids; "
                             "a profile here does not prove this model exists."),
            "context_flag": context_flag,
            "context_source": opts.get("context_source"),
            "context_window": api_ctx,
            "context_note": (
                "No context-window flag on this CLI version — the model "
                "uses its native window, not settable from MusePilot."
                if not context_flag else
                "Context is passed via %s; the CLI publishes no size list, "
                "so enter a token count (or leave blank for the default)." % context_flag),
        }


    def run_baseten_chat(self, prompt, effort=None, on_event=None, attachments=None,
                         base_url=None, model=None, api_key=None):
        """One direct-API chat, using the driver lock and ledger like Meta runs.

        v0.6.0: base_url/model/api_key are per-provider; when omitted the
        builtin Baseten defaults apply (key from env or the saved slot).
        """
        key = api_key or self.auth.get_baseten_key()
        if not key:
            raise RuntimeError("No Baseten API key saved. Enter it with the key button next to the provider, then try again.")
        if not self.driver._run_lock.acquire(blocking=False):
            raise RuntimeError("Another run is already in progress.")
        run_id = uuid.uuid4().hex[:12]
        started = now_ts()
        self.driver._stop_requested.clear()
        eff_model = model or BASETEN_MODEL
        try:
            if on_event:
                on_event("event", {"run_id": run_id, "cmd": "baseten chat completions"})
            text, usage, error = baseten_chat_stream(
                key, prompt, effort or self.config.get("baseten_effort") or "xhigh", on_event,
                attachments=attachments, stop_event=self.driver._stop_requested,
                base_url=base_url, model=eff_model)
            result = {"run_id": run_id, "ok": error is None, "error": error,
                      "text": text, "usage": usage, "steps": 0,
                      "model": eff_model,
                      "effort": effort or self.config.get("baseten_effort") or "xhigh",
                      "elapsed": round(now_ts() - started, 1)}
            if on_event:
                on_event("done", result)
            self.ledger.record({"ts": now_ts(), "model": BASETEN_MODEL,
                                "effort": result["effort"],
                                "input_tokens": usage["input_tokens"],
                                "output_tokens": usage["output_tokens"],
                                "total_tokens": usage["total_tokens"],
                                "steps": 0, "prompt_chars": len(prompt),
                                "provider": "baseten", "ok": result["ok"]})
            return result
        finally:
            self.driver.active_proc = None
            self.driver._run_lock.release()


    def _conversation_sessions(self):
        """Per-conversation CLI session ids: {cid: {"muse": uuid, "claude": uuid}}.

        v0.5.0: opening a previous conversation restores its own CLI
        session instead of clobbering the current one. Legacy top-level
        keys are migrated into the current conversation's slot on read.
        """
        sessions = self.config.get("conversation_sessions")
        if not isinstance(sessions, dict):
            sessions = {}
            self.config["conversation_sessions"] = sessions
        return sessions

    def _conversation_session_id(self, key):
        cid = str(int(self.config.get("conversation_id") or 1))
        sessions = self._conversation_sessions()
        slot = sessions.get(cid)
        sid = slot.get(key) if isinstance(slot, dict) else None
        if not is_valid_uuid(sid):
            # Migrate the legacy top-level key once, then generate.
            sid = self.config.get("muse_session_id" if key == "muse" else "claude_session_id")
            if not is_valid_uuid(sid):
                sid = str(uuid.uuid4())
            slot = dict(slot) if isinstance(slot, dict) else {}
            slot[key] = sid
            if key == "claude":
                slot["claude_started"] = False
            sessions[cid] = slot
            self.config["conversation_sessions"] = sessions
            save_config(self.config)
        return sid

    def _set_conversation_session_id(self, key, sid):
        cid = str(int(self.config.get("conversation_id") or 1))
        sessions = self._conversation_sessions()
        slot = dict(sessions.get(cid) or {})
        slot[key] = sid
        if key == "claude":
            slot["claude_started"] = False
        sessions[cid] = slot
        self.config["conversation_sessions"] = sessions
        save_config(self.config)

    def get_muse_session_id(self):
        """Stable Muse session UUID for the current conversation."""
        return self._conversation_session_id("muse")

    def get_claude_session_id(self):
        """Stable Claude session UUID for the current conversation."""
        return self._conversation_session_id("claude")

    def get_claude_started(self):
        """Whether this conversation's Claude session was ever started."""
        cid = str(int(self.config.get("conversation_id") or 1))
        slot = self._conversation_sessions().get(cid) or {}
        if "claude_started" in slot:
            return bool(slot["claude_started"])
        # Legacy fallback: the old single global flag.
        return bool(self.config.get("claude_session_started"))

    def set_claude_started(self, value):
        cid = str(int(self.config.get("conversation_id") or 1))
        sessions = self._conversation_sessions()
        slot = dict(sessions.get(cid) or {})
        slot["claude_started"] = bool(value)
        sessions[cid] = slot
        self.config["conversation_sessions"] = sessions
        # Keep the legacy global flag in sync for older readers.
        self.config["claude_session_started"] = bool(value)
        save_config(self.config)

    def new_conversation(self):
        """Start a fresh conversation: new saved file + fresh session ids.

        v0.6.2: the (empty) file is created immediately so the new
        conversation appears in the list right away, not only after
        its first message is saved.
        """
        ensure_dirs()
        self.config["conversation_id"] = int(self.config.get("conversation_id") or 1) + 1
        self.config["claude_session_id"] = None
        self.config["claude_session_started"] = False
        self.config["muse_session_id"] = None
        save_config(self.config)
        path = conversation_file_path(self.config["conversation_id"])
        try:
            if not os.path.isfile(path):
                open(path, "a", encoding="utf-8").close()
        except OSError:
            pass
        return {"conversation_id": self.config["conversation_id"],
                "file": path,
                "messages": []}

    def append_conversation(self, provider, prompt, text, model=None, effort=None, error=None, attachments=None):
        """Append one user/assistant exchange to the current transcript file.

        Returns (path, rotated). Rotates to a new file when the current
        file would pass MAX_CONVERSATION_FILE_BYTES; the old file is kept.
        """
        ensure_dirs()
        entry = {"ts": now_ts(), "provider": provider, "prompt": prompt,
                 "text": text or "", "model": model, "effort": effort,
                 "error": error,
                 "attachments": [a.get("name") for a in (attachments or []) if isinstance(a, dict)]}
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        path = conversation_file_path(self.config.get("conversation_id") or 1)
        try:
            size = os.path.getsize(path) if os.path.exists(path) else 0
        except OSError:
            size = 0
        rotated = False
        if size and size + len(line.encode("utf-8")) > MAX_CONVERSATION_FILE_BYTES:
            self.config["conversation_id"] = int(self.config.get("conversation_id") or 1) + 1
            save_config(self.config)
            path = conversation_file_path(self.config["conversation_id"])
            rotated = True
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass
        return path, rotated

    def load_conversation(self):
        """Return {conversation_id, file, messages} for the current file."""
        ensure_dirs()
        cid = self.config.get("conversation_id") or 1
        path = conversation_file_path(cid)
        messages = []
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        messages.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return {"conversation_id": cid, "file": path, "messages": messages}

    def list_conversations(self):
        """All saved conversations, newest last: id, message count,
        first-prompt preview, last activity ts, and which is current."""
        ensure_dirs()
        current = int(self.config.get("conversation_id") or 1)
        out = []
        try:
            files = sorted(os.listdir(CONVERSATIONS_DIR))
        except OSError:
            files = []
        for fn in files:
            m = re.match(r"conversation-(\d+)\.jsonl$", fn)
            if not m:
                continue
            cid = int(m.group(1))
            path = os.path.join(CONVERSATIONS_DIR, fn)
            messages = []
            try:
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            messages.append(json.loads(line))
                        except ValueError:
                            continue
            except OSError:
                pass
            first_prompt = next((x.get("prompt") or "" for x in messages if x.get("prompt")), "")
            last_ts = messages[-1].get("ts") if messages else None
            out.append({"id": cid, "messages": len(messages),
                        "preview": first_prompt[:120], "last_ts": last_ts,
                        "current": cid == current})
        return out

    def open_conversation(self, cid):
        """Switch the current conversation to a previous one and return
        its transcript. The conversation keeps its own CLI session ids
        (see _conversation_sessions), so resuming it continues where it
        left off instead of clobbering the session."""
        ensure_dirs()
        cid = int(cid)
        path = conversation_file_path(cid)
        if not os.path.exists(path):
            raise FileNotFoundError("conversation-%03d.jsonl" % cid)
        self.config["conversation_id"] = cid
        save_config(self.config)
        return self.load_conversation()

    def run_claude_chat(self, prompt, effort=None, workdir=None, auto_approve=False, on_event=None, attachments=None,
                        base_url=None, model=None, api_key=None):
        """One Claude Code run, using the driver lock + ledger.

        v0.6.0: base_url/model/api_key are per-provider (custom providers
        may point the Claude Code harness at any Anthropic-compatible
        endpoint). When omitted, the builtin Baseten defaults apply.

        Session memory (v0.4.7): the first run in a conversation passes
        --session-id <uuid>; later runs pass --resume <uuid>. If a resume
        fails because that session is gone, retry once with a fresh uuid
        and tell the caller via the error text on total failure only — a
        successful retry keeps the conversation going on a new session.
        """
        key = api_key or self.auth.get_baseten_key()
        if not key:
            raise RuntimeError("No Baseten API key saved. Enter it with the key button next to the provider, then try again.")
        binary = find_cli_binary("claude")
        if not binary:
            raise RuntimeError("Claude Code not found. MusePilot downloads it automatically on launch, or press Download in Command-line tools.")
        if not self.driver._run_lock.acquire(blocking=False):
            raise RuntimeError("Another run is already in progress.")
        run_id = uuid.uuid4().hex[:12]
        started = now_ts()
        eff = effort or self.config.get("baseten_effort") or "xhigh"
        eff_model = model or BASETEN_MODEL
        self.driver._stop_requested.clear()
        _reg = lambda p: setattr(self.driver, "active_proc", p)
        _env = claude_baseten_env(key, base_url, eff_model)
        try:
            if on_event:
                on_event("event", {"run_id": run_id, "cmd": "claude -p"})
            session_id = self.get_claude_session_id()
            resume = self.get_claude_started()
            prompt_full = prompt + attachments_prompt_suffix(attachments or [])
            text, usage, error = claude_chat_stream(
                binary, key, prompt_full, eff,
                workdir or self.config.get("default_workdir"),
                auto_approve, on_event,
                session_id=session_id, resume=resume,
                stop_event=self.driver._stop_requested, register_proc=_reg,
                _env=_env)
            if error and resume and any(w in error.lower() for w in ("resume", "session", "not found", "no such")):
                # The stored session is gone (Claude storage cleared, etc).
                # Retry once on a fresh session rather than failing forever.
                fresh_id = str(uuid.uuid4())
                self._set_conversation_session_id("claude", fresh_id)
                text, usage, error = claude_chat_stream(
                    binary, key, prompt_full, eff,
                    workdir or self.config.get("default_workdir"),
                    auto_approve, on_event,
                    session_id=fresh_id, resume=False,
                    stop_event=self.driver._stop_requested, register_proc=_reg,
                    _env=_env)
            if not error:
                self.set_claude_started(True)
            result = {"run_id": run_id, "ok": error is None, "error": error,
                      "text": text, "usage": usage, "steps": 0,
                      "model": eff_model, "effort": eff,
                      "elapsed": round(now_ts() - started, 1)}
            if on_event:
                on_event("done", result)
            self.ledger.record({"ts": now_ts(), "model": eff_model,
                                "effort": eff,
                                "input_tokens": usage["input_tokens"],
                                "output_tokens": usage["output_tokens"],
                                "total_tokens": usage["total_tokens"],
                                "steps": 0, "prompt_chars": len(prompt),
                                "provider": "claude", "ok": result["ok"]})
            return result
        finally:
            self.driver.active_proc = None
            self.driver._run_lock.release()


def sse_send(handler, payload):
    data = json.dumps(payload)
    handler.wfile.write(f"data: {data}\n\n".encode("utf-8"))
    handler.wfile.flush()


class Handler(BaseHTTPRequestHandler):
    server_version = "MusePilot/" + VERSION

    # -- plumbing ------------------------------------------------------
    def log_message(self, *args):  # quiet
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    # -- routes --------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        app = self.server.app

        if path == "/":
            page = UI_HTML.replace("__VERSION__", VERSION)
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/options":
            force = parsed.query == "refresh=1" or "refresh=1" in parsed.query
            try:
                self._send_json(app.cli.discover_options(force=force))
            except Exception as e:  # noqa: BLE001
                self._send_json({"cli_available": False, "error": str(e)[:300]}, 500)
        elif path == "/api/models":
            from urllib.parse import parse_qs as _pqs
            _q = _pqs(parsed.query)
            force = "refresh=1" in parsed.query
            _prov = (_q.get("provider") or [None])[0]
            try:
                self._send_json(app.query_models(force=force, provider_id=_prov))
            except Exception as e:  # noqa: BLE001
                self._send_json({"models": [], "model_source": "none",
                                 "models_error": str(e)[:300]}, 500)
        elif path.startswith("/api/models/"):
            from urllib.parse import unquote, parse_qs as _pqs2
            model_id = unquote(path.rsplit("/", 1)[-1])
            _prov2 = (_pqs2(parsed.query).get("provider") or [None])[0]
            detail = app.model_detail(model_id, provider_id=_prov2)
            if "error" in detail:
                self._send_json(detail, 400)
            else:
                self._send_json(detail)
        elif path == "/api/window":
            budget = int(app.config.get("prompt_budget", 30))
            can, usage = app.ledger.can_invoke(budget)
            self._send_json({
                "budget": budget,
                "plan_tier": app.config.get("plan_tier"),
                "can_invoke": can,
                "usage": usage,
                "window_hours": WINDOW_SECONDS / 3600,
                "cli_busy": app.driver._run_lock.locked(),
            })
        elif path == "/api/queue":
            self._send_json({"items": app.queue.list()})
        elif path == "/api/runs":
            self._send_json({"runs": list_run_records()})
        elif path == "/api/conversation":
            self._send_json(app.load_conversation())
        elif path == "/api/conversations":
            self._send_json({"conversations": app.list_conversations()})
        elif path == "/api/providers":
            self._send_json(app.list_providers())
        elif path == "/api/clis":
            self._send_json({"clis": self.server.app.cli_manager.status()})
        elif path == "/api/config":
            self._send_json({"config": app.config, "plans": PLAN_TIERS})
        elif path == "/api/auth":
            self._send_json({
                "auth": app.auth.status(),
                "account": app.auth.account_info(),
            })
        else:
            self._send_json({"error": "not found"}, 404)

    def _run_chat_sse(self, app, cfg, body, prompt, chat_provider):
        """Run one chat prompt with server-sent-event streaming.

        Shared by /api/chat and /api/queue/<id>/send-now. The busy and
        budget gates are the caller's job; body carries model, effort,
        context_window, max_steps, workdir, auto_approve and attachments
        exactly like a /api/chat body.
        """
        # v0.4.7: Muse session id is a server-persisted UUID, not the
        # UI 12-hex id (which changed on every page load and broke memory).
        session_id = app.get_muse_session_id()
        run_id_holder = {}

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        # Tell any proxy in the path not to buffer the stream: each
        # sse_send() below flushes, so a chunk the CLI emits now
        # must reach the browser now, not at run end.
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def on_event(kind, payload):
            if kind == "event" and payload.get("run_id"):
                run_id_holder["run_id"] = payload["run_id"]
            try:
                sse_send(self, {"kind": kind, **payload})
            except (BrokenPipeError, ConnectionResetError):
                pass

        provider = chat_provider
        spec = app.resolve_provider(provider)
        chat_atts = resolve_attachments(body.get("attachments"))
        # Effort default is harness-specific: the muse harness uses the
        # configured default effort; claude/baseten harnesses default to
        # the Baseten effort (xhigh).
        if spec["harness"] == HARNESS_MUSE:
            eff = body.get("effort") or cfg.get("default_effort")
        else:
            eff = body.get("effort") or cfg.get("baseten_effort") or "xhigh"
        try:
            # v0.6.0: one dispatch for every harness; custom providers
            # ride the claude/baseten harnesses with their own base URL,
            # model and key.
            result = app.run_provider_chat(
                spec, prompt,
                effort=eff,
                workdir=body.get("workdir") or cfg.get("default_workdir"),
                auto_approve=bool(body.get("auto_approve", cfg.get("auto_approve", False))),
                on_event=on_event, attachments=chat_atts,
                model=body.get("model") or cfg.get("default_model"),
                context_window=body.get("context_window"),
                max_steps=body.get("max_steps") or cfg.get("default_max_steps"),
                session_id=session_id,
            )
            save_run_record({
                "run_id": run_id_holder.get("run_id", uuid.uuid4().hex[:12]),
                "prompt": prompt[:2000],
                "model": result.get("model"),
                "effort": result.get("effort"),
                "status": "done" if result["ok"] else "error",
                "text": result["text"],
                "usage": result["usage"],
                "error": result.get("error"),
                "started_at": now_ts() - result.get("elapsed", 0),
                "finished_at": now_ts(),
                "interactive": True,
            })
            try:
                app.append_conversation(
                    provider, prompt, result["text"],
                    model=result.get("model"), effort=result.get("effort"),
                    error=result.get("error"), attachments=chat_atts)
            except Exception:
                pass
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            try:
                sse_send(self, {"kind": "error", "error": str(e)[:500]})
                sse_send(self, {"kind": "done", "ok": False, "error": str(e)[:500]})
            except (BrokenPipeError, ConnectionResetError):
                pass
        return


    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        app = self.server.app

        if path == "/api/stop":
            stopped = app.driver.stop()
            self._send_json({"stopped": stopped,
                             "busy": app.driver._run_lock.locked()})
            return

        def _known_provider_id(pid):
            if pid in (PROVIDER_META, PROVIDER_BASETEN):
                return True
            return any(cp.get("id") == pid
                       for cp in app.config.get("custom_providers") or [])

        # Custom provider management (v0.6.0).
        if path == "/api/providers":
            body = self._read_json()
            try:
                spec = app.create_custom_provider(body)
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
                return
            self._send_json({"provider": spec})
            return

        m = re.match(r"^/api/providers/([^/]+)/key$", path)
        if m:
            pid = m.group(1)
            if not _known_provider_id(pid):
                self._send_json({"error": "unknown provider"}, 404)
                return
            spec = app.resolve_provider(pid)
            body = self._read_json()
            key = (body.get("key") or "").strip()
            slot = "meta" if spec["harness"] == HARNESS_MUSE else spec["key_slot"]
            ok, msg = app.auth.save_api_key(key, slot)
            self._send_json({"ok": ok, "message": msg}, 200 if ok else 400)
            return

        m = re.match(r"^/api/providers/([^/]+)$", path)
        if m:
            pid = m.group(1)
            body = self._read_json()
            try:
                spec = app.update_custom_provider(pid, body)
            except KeyError:
                self._send_json({"error": "unknown provider"}, 404)
                return
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
                return
            self._send_json({"provider": spec})
            return

        if path.startswith("/api/conversations/") and path.endswith("/open"):
            cid = path.rsplit("/", 2)[-2]
            try:
                result = app.open_conversation(int(cid))
            except (ValueError, FileNotFoundError):
                self._send_json({"error": "unknown conversation"}, 404)
                return
            self._send_json(result)
            return

        if path == "/api/chat":
            body = self._read_json()
            prompt = (body.get("prompt") or "").strip()
            if not prompt:
                self._send_json({"error": "empty prompt"}, 400)
                return
            cfg = app.config
            chat_provider = body.get("provider") or cfg.get("provider") or "meta"
            chat_spec = app.resolve_provider(chat_provider)
            if app.driver._run_lock.locked():
                busy_hint = ("%s is already running — queue this prompt instead."
                             % chat_spec["name"])
                self._send_json({"error": "busy", "hint": busy_hint}, 409)
                return
            budget = int(cfg.get("prompt_budget", 30))
            can, _usage = ((True, {}) if chat_spec["harness"] != HARNESS_MUSE
                           else app.ledger.can_invoke(budget))
            if not can and not body.get("force"):
                self._send_json({
                    "error": "window_exhausted",
                    "hint": ("The 5-hour window budget is used up. "
                             "Queue the prompt to auto-run when capacity frees, "
                             "or send with force=true to run anyway."),
                }, 429)
                return

            self._run_chat_sse(app, cfg, body, prompt, chat_provider)
            return

        if path == "/api/conversation/new":
            self._send_json(app.new_conversation())
            return

        if path == "/api/attachments":
            body = self._read_json()
            try:
                import base64 as _b64
                data = _b64.b64decode(body.get("data_base64") or "", validate=True)
                att = save_attachment(body.get("name") or "attachment",
                                      body.get("mime") or "", data)
            except Exception as e:  # noqa: BLE001 — bad upload, user-facing
                msg = str(e)
                status = 413 if "too large" in msg else 400
                self._send_json({"error": msg[:300]}, status)
                return
            self._send_json({"attachment": att})
            return

        if path.startswith("/api/queue/") and path.endswith("/send-now"):
            # Force a queued prompt to run right now: stop any in-flight
            # run first, then stream this item like /api/chat. The item
            # leaves the queue either way (re-queued on budget refusal).
            item_id = path.rsplit("/", 2)[-2]
            item = next((it for it in app.queue.list()
                         if it.get("id") == item_id), None)
            if not item:
                self._send_json({"error": "unknown queued item"}, 404)
                return
            body = self._read_json()
            force = bool(body.get("force"))
            cfg = app.config
            provider = item.get("provider") or cfg.get("provider") or "meta"
            if app.driver._run_lock.locked():
                app.driver.stop()
                deadline = time.time() + 20
                while app.driver._run_lock.locked() and time.time() < deadline:
                    time.sleep(0.2)
                if app.driver._run_lock.locked():
                    self._send_json({"error": "busy",
                                     "hint": "Could not stop the running prompt."}, 409)
                    return
            app.queue.remove(item_id)
            sn_spec = app.resolve_provider(provider)
            if sn_spec["harness"] == HARNESS_MUSE and not force:
                budget = int(cfg.get("prompt_budget", 30))
                can, _usage = app.ledger.can_invoke(budget)
                if not can:
                    app.queue.add(item)  # not lost; back to the queue
                    self._send_json({
                        "error": "window_exhausted",
                        "hint": ("The 5-hour window budget is used up. "
                                 "The prompt was put back in the queue; "
                                 "send with force=true to run anyway."),
                    }, 429)
                    return
            chat_body = {
                "prompt": item.get("prompt"),
                "provider": provider,
                "model": item.get("model"),
                "effort": item.get("effort"),
                "context_window": item.get("context_window"),
                "max_steps": item.get("max_steps"),
                "workdir": item.get("workdir"),
                "auto_approve": item.get("auto_approve", False),
                "attachments": item.get("attachments") or [],
                "force": force,
            }
            self._run_chat_sse(app, cfg, chat_body, item.get("prompt"), provider)
            return

        if path == "/api/queue":
            body = self._read_json()
            prompt = (body.get("prompt") or "").strip()
            if not prompt:
                self._send_json({"error": "empty prompt"}, 400)
                return
            cfg = app.config
            item = app.queue.add({
                "prompt": prompt,
                "provider": body.get("provider") or cfg.get("provider") or "meta",
                "model": body.get("model") or cfg.get("default_model"),
                "effort": body.get("effort") or cfg.get("default_effort"),
                "context_window": body.get("context_window"),
                "max_steps": body.get("max_steps") or cfg.get("default_max_steps"),
                "session_id": body.get("session_id"),
                "attachments": body.get("attachments") or [],
                "auto_approve": bool(body.get("auto_approve", cfg.get("auto_approve", False))),
                "workdir": body.get("workdir") or cfg.get("default_workdir"),
            })
            self._send_json({"queued": item})
            return

        if path == "/api/clis/install":
            tool = (body.get("tool") or "").strip().lower()
            force = bool(body.get("force", False))
            started, message = self.server.app.cli_manager.start_install(tool, force=force)
            self._send_json({"started": started, "message": message,
                             "clis": self.server.app.cli_manager.status()})
        if path == "/api/config":
            body = self._read_json()
            cfg = app.config
            if "provider" in body and body["provider"] in ("meta", "baseten", "claude"):
                cfg["provider"] = body["provider"]
            if "baseten_effort" in body and body["baseten_effort"] in BASETEN_EFFORTS:
                cfg["baseten_effort"] = body["baseten_effort"]
            if "baseten_model" in body and body["baseten_model"] == BASETEN_MODEL:
                cfg["baseten_model"] = BASETEN_MODEL
            for key in ("plan_tier", "prompt_budget", "default_effort",
                        "default_model", "default_max_steps", "default_workdir",
                        "default_context_window", "auto_approve", "auto_install_clis"):
                if key in body:
                    cfg[key] = body[key]
            # keep budget in sync when the tier changes (unless customised)
            if "plan_tier" in body and "prompt_budget" not in body:
                for p in PLAN_TIERS:
                    if p["id"] == body["plan_tier"]:
                        cfg["prompt_budget"] = p["default_budget"]
            save_config(cfg)
            self._send_json({"config": cfg})
            return

        if path == "/api/auth/key":
            body = self._read_json()
            key_provider = body.get("provider") or "meta"
            if key_provider not in ("meta", "baseten"):
                key_provider = "meta"
            ok, message = app.auth.save_api_key(body.get("key"), provider=key_provider)
            if not ok:
                self._send_json({"saved": False, "message": message}, 400)
                return
            # Saving a key is the trigger for the model query: invalidate
            # the model cache and query live, so Settings can populate
            # the model list immediately instead of waiting for a refresh.
            # For Baseten, query_models returns the fixed model, not Meta.
            if key_provider == "baseten":
                app.config["provider"] = "baseten"
                save_config(app.config)
            app._models_cache = None
            models_result = app.query_models(force=True)
            self._send_json({
                "saved": True, "message": message,
                "auth": app.auth.status(),
                "account": app.auth.account_info(),
                "models": models_result["models"],
                "model_source": models_result["model_source"],
                "models_error": models_result["models_error"],
            })
            return

        if path == "/api/auth/logout":
            ok, message = app.auth.logout()
            self._send_json({"logged_out": ok, "message": message,
                             "auth": app.auth.status(),
                             "account": app.auth.account_info()},
                            200 if ok else 400)
            return

        self._send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        app = self.server.app
        m = re.match(r"^/api/providers/([^/]+)/key$", parsed.path)
        if m:
            pid = m.group(1)
            spec = app.resolve_provider(pid)
            slot = "meta" if spec["harness"] == HARNESS_MUSE else spec["key_slot"]
            ok, message = app.auth.delete_api_key(slot)
            self._send_json({"ok": ok, "message": message})
            return
        m = re.match(r"^/api/providers/([^/]+)$", parsed.path)
        if m:
            pid = m.group(1)
            try:
                app.delete_custom_provider(pid)
            except KeyError:
                self._send_json({"error": "unknown provider"}, 404)
                return
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
                return
            self._send_json({"deleted": pid})
            return
        if parsed.path.startswith("/api/queue/"):
            item_id = parsed.path.rsplit("/", 1)[-1]
            app.queue.remove(item_id)
            self._send_json({"removed": item_id})
        elif parsed.path == "/api/auth/key":
            from urllib.parse import parse_qs as _parse_qs
            _prov = (_parse_qs(parsed.query).get("provider") or ["meta"])[0]
            if _prov not in ("meta", "baseten"):
                _prov = "meta"
            ok, message = app.auth.delete_api_key(provider=_prov)
            app._models_cache = None
            self._send_json({"removed": ok, "message": message},
                            200 if ok else 400)
        else:
            self._send_json({"error": "not found"}, 404)

# ---------------------------------------------------------------------------
# Embedded web UI (single page, no build step)
# ---------------------------------------------------------------------------

UI_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MusePilot</title>
<style>
  :root { --bg:#0f1115; --panel:#171a21; --panel2:#1e222b; --line:#2a2f3a;
          --txt:#e8eaf0; --mut:#9aa3b2; --acc:#6ea8fe; --ok:#4cc38a; --warn:#e5a54b; --bad:#e5636a; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--txt);
         font:14px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text",Helvetica,Arial,sans-serif;
         display:flex; height:100vh; }
  #side { width:300px; min-width:300px; background:var(--panel); border-right:1px solid var(--line);
          padding:16px; overflow-y:auto; }
  #main { flex:1; display:flex; flex-direction:column; min-width:0; }
  #topbar { padding:10px 18px; border-bottom:1px solid var(--line); background:var(--panel);
            display:flex; align-items:center; gap:14px; }
  #topbar h1 { font-size:16px; margin:0; }
  #topbar .ver { color:var(--mut); font-size:12px; }
  #meterwrap { flex:1; max-width:520px; }
  #meterbar { height:8px; background:var(--panel2); border-radius:4px; overflow:hidden; }
  #meterfill { height:100%; width:0%; background:var(--ok); border-radius:4px; transition:width .4s; }
  #meterlabel { font-size:12px; color:var(--mut); margin-top:3px; display:flex; gap:10px; }
  #dot { width:10px; height:10px; border-radius:50%; background:var(--ok); display:inline-block; }
  #chat { flex:1; overflow-y:auto; padding:18px; display:flex; flex-direction:column; gap:12px; }
  .msg { max-width:78%; padding:10px 14px; border-radius:12px; white-space:pre-wrap; word-break:break-word; }
  .msg.user { align-self:flex-end; background:#2b3550; }
  .msg.asst { align-self:flex-start; background:var(--panel2); border:1px solid var(--line); }
  .msg .meta { font-size:11px; color:var(--mut); margin-bottom:4px; }
  .msg .tools { font-size:12px; color:var(--mut); margin-top:8px; border-top:1px dashed var(--line); padding-top:6px; }
  .msg .body h1, .msg .body h2, .msg .body h3, .msg .body h4 { margin:14px 0 6px; line-height:1.3; }
  .msg .body h1 { font-size:18px; } .msg .body h2 { font-size:16px; } .msg .body h3 { font-size:15px; } .msg .body h4 { font-size:14px; }
  .msg .body p { margin:6px 0; }
  .msg .body ul, .msg .body ol { margin:6px 0; padding-left:22px; }
  .msg .body li { margin:2px 0; }
  .msg .body blockquote { margin:8px 0; padding:6px 12px; border-left:3px solid var(--acc); color:var(--mut); }
  .msg .body code { background:#0b0e14; border:1px solid var(--line); border-radius:4px; padding:1px 5px; font:12px/1.5 ui-monospace,Menlo,Consolas,monospace; }
  .msg .body pre { background:#0b0e14; border:1px solid var(--line); border-radius:8px; padding:10px; overflow-x:auto; }
  .msg .body pre code { background:none; border:0; padding:0; font-size:12px; }
  .msg .body a { color:var(--acc); }
  .msg .body table { border-collapse:collapse; margin:8px 0; font-size:13px; }
  .msg .body th, .msg .body td { border:1px solid var(--line); padding:5px 9px; text-align:left; }
  .msg .body .mermaid-block { background:#0b0e14; border:1px solid var(--line); border-radius:8px; padding:10px; overflow-x:auto; }
  .msg .body .mermaid-block svg { max-width:100%; }
  #composer { padding:14px 18px; border-top:1px solid var(--line); background:var(--panel); }
  #prompt { width:100%; min-height:76px; resize:vertical; background:var(--panel2); color:var(--txt);
            border:1px solid var(--line); border-radius:8px; padding:10px; font:inherit; }
  #composer .row { display:flex; gap:8px; margin-top:8px; align-items:center; }
  button { background:var(--acc); color:#0b0e14; border:0; border-radius:8px; padding:9px 16px;
           font-weight:600; cursor:pointer; }
  button.ghost { background:var(--panel2); color:var(--txt); border:1px solid var(--line); }
  button:disabled { opacity:.45; cursor:default; }
  .field { margin-bottom:12px; }
  .field label { display:block; font-size:12px; color:var(--mut); margin-bottom:4px; }
  .field select, .field input[type=text], .field input[type=number] {
    width:100%; background:var(--panel2); color:var(--txt); border:1px solid var(--line);
    border-radius:8px; padding:8px; font:inherit; }
  .src { font-size:11px; color:var(--mut); margin-top:3px; }
  .check { display:flex; gap:8px; align-items:center; font-size:13px; }
  #queuepanel { border-top:1px solid var(--line); background:var(--panel); max-height:220px;
                overflow-y:auto; padding:10px 18px; }
  #queuepanel h3, #side h3 { font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--mut); }
  .qitem { display:flex; gap:8px; align-items:center; font-size:13px; padding:6px 0;
           border-bottom:1px dashed var(--line); }
  .qitem .p { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--mut); }
  .qitem button { padding:4px 10px; font-size:12px; }
  .pill { font-size:11px; padding:2px 8px; border-radius:10px; background:var(--panel2);
          border:1px solid var(--line); color:var(--mut); }
  button.recording { border-color:var(--bad) !important; color:var(--bad) !important; }
  .warn { color:var(--warn); font-size:12px; }
  .working { color:var(--acc); font-size:12px; animation:mp-pulse 1.2s ease-in-out infinite; }
  @keyframes mp-pulse { 0%,100% { opacity:1; } 50% { opacity:.35; } }
  .runctrl { margin-top:8px; display:flex; gap:8px; align-items:center; }
  .runctrl button { padding:4px 10px; font-size:12px; }
  #busypill { color:var(--acc); border-color:var(--acc); animation:mp-pulse 1.2s ease-in-out infinite; }
  .modal { position:fixed; inset:0; background:rgba(0,0,0,.55); display:flex; align-items:center; justify-content:center; z-index:50; }
  .modalbox { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:20px; width:min(520px, 92vw); max-height:84vh; overflow-y:auto; }
  .modalbox h3 { margin:0 0 10px; }
  .modalbox h4 { margin:12px 0 6px; font-size:13px; color:var(--mut); }
  .modalbox input[type=text], .modalbox input[type=password], .modalbox select { width:100%; background:var(--panel2); color:var(--txt); border:1px solid var(--line); border-radius:8px; padding:8px; font:inherit; }
  .provrow { display:flex; gap:8px; align-items:center; font-size:13px; padding:8px 0; border-bottom:1px dashed var(--line); }
  .provrow .p { flex:1; min-width:0; }
  .provrow .p .nm { color:var(--txt); font-weight:600; }
  .provrow .p .dt { color:var(--mut); font-size:11px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .provrow button { padding:4px 10px; font-size:12px; }
  details.acc { margin:10px 0; border:1px solid var(--line); border-radius:8px; background:var(--panel2); }
  details.acc > summary { cursor:pointer; padding:8px 10px; font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--mut); }
  details.acc > div { padding:0 10px 10px; }
  .convitem { display:block; font-size:12px; padding:8px 10px; margin:4px 0;
              border:1px solid transparent; cursor:pointer; border-radius:8px; background:var(--panel2); }
  .convitem:hover { border-color:var(--line); }
  .convitem.current { border-color:var(--acc); background:var(--panel); }
  .convitem .ct { color:var(--txt); font-weight:500; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .convitem.current .ct { font-weight:700; }
  .convitem .cm { color:var(--mut); font-size:11px; margin-top:2px; }
  .convitem button { padding:4px 10px; font-size:12px; }
  #runs { font-size:12px; color:var(--mut); }
  .run { border-bottom:1px dashed var(--line); padding:6px 0; }
</style>
</head>
<body>
<div id="side">
  <h3>Provider</h3>
  <div class="field">
    <div style="display:flex; gap:8px">
      <select id="provider" style="flex:1"></select>
      <button class="ghost" id="keybtn" title="Enter or update the API key for this provider">🔑</button>
    </div>
    <div class="src" id="providernote"></div>
  </div>
  <h3>Model &amp; reasoning</h3>
  <div class="field"><label>Model</label><select id="model"></select><div class="src" id="modelsrc"></div></div>
  <div class="field"><label>Reasoning effort</label><select id="effort"></select><div class="src" id="effortsrc"></div></div>
  <div class="field"><label>Context window (tokens)</label><input type="number" id="ctx" min="1024" placeholder="blank = model default"><div class="src" id="ctxsrc"></div></div>
  <h3>Conversations</h3>
  <div class="field"><button class="ghost" id="newchat" style="width:100%">New conversation</button></div>
  <div class="field"><div id="convlist"><span class="src">Loading…</span></div>
  <div class="src">Previous chat sessions. Click one to open it; it resumes with its own memory.</div>
  <div class="src" id="convinfo"></div></div>
  <details class="acc" id="runsacc"><summary>Recent runs</summary><div><div id="runs"></div></div></details>
  <details class="acc"><summary>Run options</summary><div>
    <div class="field"><label>Working directory</label><input type="text" id="workdir"></div>
    <div class="field"><label>Max model steps</label><input type="number" id="maxsteps" min="1" max="500"></div>
    <div class="field check"><input type="checkbox" id="yolo"><label for="yolo" style="margin:0">Auto-approve all actions <span class="warn">(yolo)</span></label></div>
  </div></details>
  <details class="acc"><summary>Command-line tools</summary><div>
    <div class="field">
      <div class="src" id="clistatus">Checking…</div>
      <div id="clitools"></div>
      <div class="src">MusePilot downloads Muse Code and Claude Code automatically when they are missing (official installers only, verified by running --version after install). Use Download below to install or reinstall one manually.</div>
      <div class="row" style="display:flex; gap:8px; margin-top:8px; align-items:center">
        <label class="check" style="margin:0"><input type="checkbox" id="autoinstall"> Auto-download missing tools on launch</label>
      </div>
    </div>
  </div></details>
  <details class="acc"><summary>5-hour budget</summary><div>
    <div class="field"><label>Plan tier</label><select id="plan"></select></div>
    <div class="field"><label>Prompt budget per 5h <span class="warn">(estimate \u2014 tune it)</span></label><input type="number" id="budget" min="1" max="100000"></div>
    <div class="src">Meta publishes "10\u201350 prompts / 5h" for Everyday; higher tiers are 5x/20x/80x multiples. Token usage is shown for insight.</div>
  </div></details>
</div>
<div id="main">
  <div id="topbar">
    <div><h1>MusePilot</h1><span class="ver" id="verlabel">v__VERSION__</span>
      <span class="pill" id="busypill" style="display:none">● running…</span>
      <button class="ghost" id="busystop" style="display:none; padding:2px 10px; font-size:11px">Stop</button>
    </div>
    <div id="meterwrap">
      <div id="meterbar"><div id="meterfill"></div></div>
      <div id="meterlabel"><span id="dot"></span><span id="metertext">…</span><span id="metertokens"></span><span id="meternext"></span></div>
    </div>
    <span class="pill" id="cliver"></span>
  </div>
  <div id="provmodal" class="modal" style="display:none">
    <div class="modalbox">
      <h3>Custom providers</h3>
      <div class="src" style="margin-bottom:10px">Baseten is built in and cannot be deleted. Custom providers use the Claude Code harness or the direct API harness with your own base URL, model and key.</div>
      <div id="provlist"></div>
      <h4 style="margin:14px 0 8px">New provider</h4>
      <div class="field"><label>Name</label><input type="text" id="cp-name" placeholder="e.g. My Claude endpoint"></div>
      <div class="field"><label>Harness</label><select id="cp-harness">
        <option value="claude">Claude Code (agentic CLI, needs base URL + key + model)</option>
        <option value="baseten">Direct API (OpenAI-compatible, needs base URL + key + model)</option>
      </select></div>
      <div class="field"><label>Base URL</label><input type="text" id="cp-baseurl" placeholder="https://..."></div>
      <div class="field"><label>Model</label><input type="text" id="cp-model" placeholder="model id"></div>
      <div class="src" id="cp-note"></div>
      <div class="row" style="display:flex; gap:8px; margin-top:10px">
        <button id="cp-save">Save provider</button>
        <button class="ghost" id="cp-close">Close</button>
      </div>
    </div>
  </div>
  <div id="keymodal" class="modal" style="display:none">
    <div class="modalbox">
      <h3 id="keytitle">API key</h3>
      <div class="src" id="keydetail" style="margin-bottom:10px"></div>
      <div class="field"><label>API key</label>
        <input type="password" id="keyinput" autocomplete="off" placeholder="paste the key">
      </div>
      <div class="src" id="keynote"></div>
      <div class="row" style="display:flex; gap:8px; margin-top:10px; flex-wrap:wrap">
        <button id="keysave">Save key</button>
        <button class="ghost" id="keyremove">Remove</button>
        <button class="ghost" id="keylogout" style="display:none">Sign out (muse logout)</button>
        <button class="ghost" id="keyclose">Cancel</button>
      </div>
      <div class="src">Keys are stored on this machine only — macOS Keychain, or a mode-0600 file under ~/.musepilot/ elsewhere — and are never shown back or logged.</div>
    </div>
  </div>
  <div id="chat"></div>
  <div id="queuepanel">
    <h3>Queue — auto-runs when window capacity frees up</h3>
    <div id="qlist"><span class="src">Empty. Queue a prompt and it will invoke Muse Code as soon as your 5-hour budget allows.</span></div>
  </div>
  <div id="composer">
    <div id="attachments"></div>
    <textarea id="prompt" placeholder="Ask Muse Code to do something… (it can read, edit and run code in the working directory)"></textarea>
    <div class="row">
      <button id="send">Send to Muse Code</button>
      <button class="ghost" id="mic" title="Voice input (browser voice-to-text)">🎤 Voice input</button>
      <button class="ghost" id="attachbtn" title="Attach files or images — paste, drop, or pick">📎 Attach</button>
      <input type="file" id="fileinput" multiple style="display:none">
      <button class="ghost" id="queuebtn">Queue for when available</button>
      <span class="src" id="micstatus"></span>
      <span class="src" id="sendhint"></span>
    </div>
  </div>
</div>
<script>
let OPTS = null, CFG = null, SESSION = crypto.randomUUID().replace(/-/g,'').slice(0,12);
const $ = id => document.getElementById(id);

async function api(path, opts={}) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(()=>({}));
  if (!r.ok) { const e = new Error(j.hint || j.error || ('HTTP '+r.status)); e.status=r.status; e.body=j; throw e; }
  return j;
}
function esc(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

let MODELS = null;

function populateModels(modelsResult){
  MODELS = modelsResult;
  const list = modelsResult.models || [];
  const sel = $('model');
  const prev = sel.value;
  sel.innerHTML = '';
  const def = document.createElement('option'); def.value=''; def.textContent='(CLI default)'; sel.appendChild(def);
  if (!list.length) { const nop = document.createElement('option'); nop.value=''; nop.textContent = modelsResult.cli_available ? '(no models returned — see error below)' : '(muse CLI not found)'; nop.disabled = true; sel.appendChild(nop); }
  list.forEach(m => { const op=document.createElement('option'); op.value=m.id;
    op.textContent = m.label + (m.context_window ? ' · ' + (m.context_window/1048576) + 'M ctx' : '');
    sel.appendChild(op); });
  const p = currentProvider();
  const wantModel = (p && p.harness !== 'muse' && p.model) ? p.model : (CFG && CFG.default_model);
  if (wantModel) sel.value = wantModel;
  else if (prev) sel.value = prev;
  const src = 'Source: ' + (modelsResult.model_source || 'none');
  $('modelsrc').textContent = src;
  if (modelsResult.models_error) $('modelsrc').textContent += ' · ' + modelsResult.models_error;
}

function populatePerModel(detail){
  if (!detail || detail.error) return;
  const sel = $('effort');
  sel.innerHTML = '';
  if (!(detail.reasoning_efforts || []).length) { const nop=document.createElement('option'); nop.value=''; nop.textContent='(no effort list returned)'; nop.disabled=true; sel.appendChild(nop); }
  (detail.reasoning_efforts || []).forEach(e => { const op=document.createElement('option'); op.value=e; op.textContent=e; sel.appendChild(op); });
  const p = currentProvider();
  const isMuse = !p || p.harness === 'muse';
  const want = detail.default_reasoning_effort || (isMuse ? (CFG && CFG.default_effort) : (CFG && CFG.baseten_effort)) || 'high';
  if ([...sel.options].some(o => o.value === want)) sel.value = want;
  if (CFG) { if (isMuse) { if ([...sel.options].some(o => o.value === CFG.default_effort)) sel.value = CFG.default_effort; } else if ([...sel.options].some(o => o.value === CFG.baseten_effort)) sel.value = CFG.baseten_effort; }
  const esrc = 'Source: ' + (detail.effort_source || '') + ' · ' + (detail.effort_scope || '');
  $('effortsrc').textContent = esrc;
  const ctxNote = detail.context_note || '';
  $('ctxsrc').textContent = ctxNote;
  const ctxFlag = !!detail.context_flag;
  $('ctx').disabled = !ctxFlag;
  $('ctx').placeholder = ctxFlag ? 'blank = model default' : 'Native — not settable via CLI';
}

async function onModelChange(modelId, providerId){
  const sel = $('model');
  if (!modelId) { sel.value = ''; }
  else {
    if (![...sel.options].some(o => o.value === modelId)) { const op=document.createElement('option'); op.value=modelId; op.textContent=modelId; sel.appendChild(op); }
    sel.value = modelId;
  }
  const pid = providerId || currentProviderId();
  if (modelId) {
    const p = currentProvider();
    const isMuse = !p || p.harness === 'muse';
    const body = isMuse ? {default_model: modelId} : {};
    try { await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)}); if (CFG && isMuse) CFG.default_model = modelId; } catch(e) {}
    try { const d = await api('/api/models/' + encodeURIComponent(modelId) + '?provider=' + encodeURIComponent(pid)); populatePerModel(d); }
    catch(e) { $('ctxsrc').textContent = 'Could not query this model: ' + e.message; }
  } else {
    try { const o = OPTS || await api('/api/options'); populatePerModel({reasoning_efforts: o.reasoning_efforts || [], effort_source: o.effort_source, context_flag: o.context_flag, context_note: 'No model selected.'}); } catch(e) {}
  }
}

async function refreshClis(){
  try {
    const j = await api('/api/clis');
    const tools = $('clitools'); if (!tools) return;
    tools.innerHTML = '';
    let anyInstalling = false;
    for (const [key, info] of Object.entries(j.clis)) {
      if (info.installing) anyInstalling = true;
      const row = document.createElement('div');
      row.style.cssText = 'display:flex; gap:8px; align-items:center; padding:4px 0; font-size:13px';
      const txt = info.installing ? (info.label + ' — downloading…')
        : info.present ? (info.label + ' ✓ ' + (info.version || 'installed'))
        : (info.label + ' — not found');
      const span = document.createElement('span'); span.style.flex = '1'; span.textContent = txt;
      row.appendChild(span);
      const btn = document.createElement('button'); btn.className = 'ghost';
      btn.style.cssText = 'padding:4px 10px; font-size:12px';
      btn.textContent = info.installing ? 'Downloading…' : (info.present ? 'Reinstall' : 'Download');
      btn.disabled = !!info.installing;
      btn.onclick = async () => {
        btn.disabled = true;
        try { await api('/api/clis/install', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({tool: key, force: !!info.present})}); } catch(e) {}
        updateProviderLabels();
        await refreshClis();
      };
      row.appendChild(btn);
      if (info.last_error) { const e = document.createElement('div'); e.className = 'warn'; e.textContent = '⚠ ' + info.last_error; row.appendChild(e); }
      tools.appendChild(row);
    }
    $('clistatus').textContent = anyInstalling ? 'Downloading missing tools…' : 'Tools checked.';
    if (anyInstalling) setTimeout(refreshClis, 3000);
  } catch(e) { const s = $('clistatus'); if (s) s.textContent = 'Could not check tools: ' + e.message; }
}

async function loadAll(refresh=false){
  try { window.MUSEPILOT_VERSION = document.querySelector('.ver') ? document.querySelector('.ver').textContent.replace(/^v/, '').split(' ')[0] : ''; } catch(e) {}
  const [o, c] = await Promise.all([
    api('/api/options' + (refresh ? '?refresh=1' : '')),
    api('/api/config'),
  ]);
  OPTS = o; CFG = c.config;
  $('cliver').textContent = o.cli_available ? ('muse ' + (o.cli_version || 'found')) : 'muse CLI NOT FOUND';
  await refreshProviders();
  const pid = currentProviderId();
  try {
    const m = await api('/api/models' + (refresh ? '?refresh=1&' : '?') + 'provider=' + encodeURIComponent(pid))
      .catch(e => ({models: [], model_source: 'none', models_error: e.message}));
    populateModels(m);
    const p = currentProvider();
    const initial = (p && p.harness !== 'muse' && p.model) ? p.model : (CFG.default_model || (m.models && m.models[0] && m.models[0].id) || '');
    if (initial) await onModelChange(initial, pid);
    else populatePerModel({reasoning_efforts: o.reasoning_efforts || [], effort_source: o.effort_source, context_flag: o.context_flag, context_note: 'Select a model to query its effort and context options.'});
  } catch(e) {}
  if (CFG.default_context_window) { $('ctx').value = CFG.default_context_window; }
  $('workdir').value = CFG.default_workdir || '';
  $('maxsteps').value = CFG.default_max_steps || 40;
  $('yolo').checked = !!CFG.auto_approve;

  const ps = $('plan'); ps.innerHTML='';
  c.plans.forEach(p => { const op=document.createElement('option'); op.value=p.id; op.textContent=p.label;
    if (p.id === CFG.plan_tier) op.selected = true; ps.appendChild(op); });
  $('budget').value = CFG.prompt_budget;

  await refreshClis();
  await restoreConversation();
  const ai = $('autoinstall'); if (ai) { ai.checked = CFG.auto_install_clis !== false; ai.onchange = async () => { try { await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({auto_install_clis: ai.checked})}); if (CFG) CFG.auto_install_clis = ai.checked; } catch(e) {} }; }
  updateProviderLabels();
}

// v0.4.4: every user-facing label that names the destination must
// follow the selected provider. v0.4.3 fixed the top bar, but the
// Send button, composer placeholder and queue text still said
// "Muse Code" even when Baseten was selected — the bug the user
// caught in the screenshot. This is the single place that sets them.
function updateProviderLabels(){
  const p = currentProvider();
  const name = p ? p.name : 'Muse Code';
  const harness = p ? p.harness : 'muse';
  const isClaude = harness === 'claude';
  const isDirect = harness === 'baseten';
  const send = $('send'); if (send) send.textContent = 'Send to ' + name;
  const pr = $('prompt'); if (pr) pr.placeholder = isClaude
    ? 'Ask ' + name + ' to do something… (it can search the web, read and edit code in the working directory — edits need auto-approve on)'
    : isDirect
    ? 'Ask ' + name + ' a question… (chat only — it cannot read, edit, or run code)'
    : 'Ask Muse Code to do something… (it can read, edit and run code in the working directory)';
  const vl = $('verlabel'); if (vl) vl.textContent = 'v' + (window.MUSEPILOT_VERSION || '') + (isClaude ? ' · ' + name + ' (Claude Code harness)' : (isDirect ? ' · ' + name + ' (direct API)' : ' · drives muse exec'));
  const q = $('qlist'); if (q && !q.querySelector('.qitem')) {
    q.innerHTML = '<span class="src">Empty. Queue a prompt and it will ' + (isClaude ? 'invoke ' + esc(name) : (isDirect ? 'ask ' + esc(name) : 'invoke Muse Code')) + (harness === 'muse' ? ' as soon as your 5-hour budget allows.' : '.') + '</span>';
  }
  const hint = $('sendhint');
  if (hint && OPTS && !OPTS.cli_available && harness === 'muse') {
    hint.textContent = (CFG && CFG.auto_install_clis !== false) ? 'Muse Code not found — downloading it automatically (see Command-line tools).' : 'Muse Code not found — press Download in Command-line tools.';
  } else if (hint && isDirect) {
    hint.textContent = name + ' direct chat — no tools, no browsing.';
  } else if (hint && isClaude) {
    hint.textContent = name + ' (Claude Code harness) — tools: WebSearch, WebFetch, files. Edits need auto-approve.';
  } else if (hint) {
    hint.textContent = '';
  }
}

function currentSelections(){
  return {
    provider: currentProviderId(),
    model: $('model').value || null,
    effort: $('effort').value || null,
    context_window: $('ctx').value ? parseInt($('ctx').value,10) : null,
    workdir: $('workdir').value.trim() || null,
    max_steps: parseInt($('maxsteps').value,10) || null,
    auto_approve: $('yolo').checked,
    session_id: SESSION,
    attachments: ATTACHMENTS.slice(),
  };
}

// v0.4.3: render assistant markdown instead of showing it literally.
// v0.4.2 used textContent, so "### Heading" and "**bold**" arrived as
// plain text (the bug in the user's screenshot). This renderer is
// dependency-free: HTML is escaped FIRST, then markdown is applied to
// the escaped text, so a model's <script> can never become live HTML.
// Code blocks are protected before inline formatting runs. Mermaid
// diagrams render only after a reply finishes (renderMermaid), using
// the mermaid library from CDN when online; offline, the diagram
// source stays visible as a code block.
function mdInline(s){
  // Protect inline code first.
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (m, c) => { codes.push(c); return '\u0000' + (codes.length-1) + '\u0000'; });
  s = s.replace(/\[([^\]]+)\]\((https?:[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^\w*])\*([^*]+)\*/g, '$1<em>$2</em>');
  s = s.replace(/\u0000(\d+)\u0000/g, (m, i) => '<code>' + codes[+i] + '</code>');
  return s;
}
function renderMarkdown(raw){
  if (!raw) return '';
  const out = [];
  const lines = raw.split('\n');
  let i = 0, listOpen = null;
  const closeList = () => { if (listOpen) { out.push('</' + listOpen + '>'); listOpen = null; } };
  while (i < lines.length) {
    const line = lines[i];
    // Fenced code block (including mermaid).
    if (line.trim().startsWith('```')) {
      closeList();
      const lang = line.trim().slice(3).trim().toLowerCase();
      const buf = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith('```')) { buf.push(lines[i]); i++; }
      i++; // closing fence
      const code = esc(buf.join('\n'));
      if (lang === 'mermaid') {
        out.push('<div class="mermaid-block"><pre><code class="language-mermaid">' + code + '</code></pre></div>');
      } else {
        out.push('<pre><code' + (lang ? ' class="language-' + esc(lang) + '"' : '') + '>' + code + '</code></pre>');
      }
      continue;
    }
    const hm = line.match(/^(#{1,4})\s+(.*)$/);
    if (hm) { closeList(); out.push('<h' + hm[1].length + '>' + mdInline(esc(hm[2])) + '</h' + hm[1].length + '>'); i++; continue; }
    if (/^\s*---\s*$/.test(line)) { closeList(); out.push('<hr>'); i++; continue; }
    if (/^\s*>\s?/.test(line)) { closeList(); out.push('<blockquote>' + mdInline(esc(line.replace(/^\s*>\s?/, ''))) + '</blockquote>'); i++; continue; }
    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ul || ol) {
      const tag = ul ? 'ul' : 'ol';
      if (listOpen !== tag) { closeList(); out.push('<' + tag + '>'); listOpen = tag; }
      out.push('<li>' + mdInline(esc((ul || ol)[1])) + '</li>'); i++; continue;
    }
    closeList();
    if (line.trim() === '') { i++; continue; }
    out.push('<p>' + mdInline(esc(line)) + '</p>'); i++;
  }
  closeList();
  return out.join('\n');
}
function renderMermaid(){
  const blocks = document.querySelectorAll('.mermaid-block');
  if (!blocks.length) return;
  const run = () => {
    if (!window.mermaid) return;
    try {
      window.mermaid.initialize({startOnLoad: false, theme: 'dark'});
      blocks.forEach(b => {
        const codeEl = b.querySelector('code');
        if (!codeEl || b.dataset.rendered) return;
        const src = codeEl.textContent;
        const holder = document.createElement('div');
        holder.className = 'mermaid';
        holder.textContent = src;
        b.innerHTML = ''; b.appendChild(holder); b.dataset.rendered = '1';
      });
      window.mermaid.run();
    } catch(e) { /* keep the code block visible on any render failure */ }
  };
  if (window.mermaid) { run(); return; }
  const s = document.createElement('script');
  s.src = 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js';
  s.onload = run;
  s.onerror = () => {}; // offline: code block stays visible
  document.head.appendChild(s);
}

// v0.4.8 attachments: paste / drop / pick files and images in the composer.
// Files are uploaded to the local server (never to a third party) and
// passed to the agent: Claude reads them with its Read tool, Muse gets
// --image for images plus file paths in the prompt, Baseten direct gets
// text inlined and images as vision blocks.
let ATTACHMENTS = [];
function renderAttachments(){
  const el = $('attachments'); if (!el) return;
  el.innerHTML = '';
  ATTACHMENTS.forEach((a, i) => {
    const d = document.createElement('div'); d.className = 'run';
    d.innerHTML = '<span class="pill">' + (a.kind === 'image' ? 'image' : 'file') + '</span><span class="p">' + esc(a.name) + ' · ' + Math.round(a.size/1024) + ' KB</span>';
    const b = document.createElement('button'); b.className = 'ghost'; b.textContent = 'Remove';
    b.onclick = () => { ATTACHMENTS.splice(i, 1); renderAttachments(); };
    d.appendChild(b); el.appendChild(d);
  });
  if (!ATTACHMENTS.length) el.innerHTML = '<span class="src">Paste, drop, or attach files and images — they go to Claude Code and Muse Code with your prompt.</span>';
}
async function uploadAttachmentFile(file){
  const name = file.name || ('pasted-image-' + (ATTACHMENTS.length+1) + '.png');
  const dataUrl = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = rej; r.readAsDataURL(file); });
  const data_base64 = dataUrl.split(',').slice(1).join(',');
  const j = await api('/api/attachments', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name, mime: file.type || '', data_base64})});
  if (j.attachment) { ATTACHMENTS.push(j.attachment); renderAttachments(); }
  else throw new Error(j.error || 'upload failed');
}
function wireAttachments(){
  const ta = $('prompt'); if (!ta || ta.dataset.attachWired) return; ta.dataset.attachWired = '1';
  $('attachbtn').onclick = () => $('fileinput').click();
  $('fileinput').onchange = async () => { for (const f of $('fileinput').files) { try { await uploadAttachmentFile(f); } catch(e) { alert(e.message); } } $('fileinput').value = ''; };
  ta.addEventListener('paste', async (e) => {
    const files = (e.clipboardData && e.clipboardData.files) || [];
    for (const f of files) { try { await uploadAttachmentFile(f); } catch(err) { alert(err.message); } }
  });
  ta.addEventListener('dragover', (e) => { e.preventDefault(); });
  ta.addEventListener('drop', async (e) => {
    e.preventDefault();
    for (const f of (e.dataTransfer && e.dataTransfer.files) || []) { try { await uploadAttachmentFile(f); } catch(err) { alert(err.message); } }
  });
  renderAttachments();
}

function addMsg(role, meta){
  const d = document.createElement('div');
  d.className = 'msg ' + (role==='user'?'user':'asst');
  d.innerHTML = '<div class="meta">'+esc(meta)+'</div><div class="body"></div><div class="tools"></div>';
  $('chat').appendChild(d); d.scrollIntoView({behavior:'smooth', block:'end'});
  return d;
}

let chatRunning = false;  // this tab currently has a chat run in flight
let serverBusy = false;   // the server lock is held (another tab / earlier page load)

function makeRunControls(am, prompt, sel){
  // In-progress indication plus Stop / Queue buttons, visible while a run streams.
  const ctrl = document.createElement('div');
  ctrl.className = 'runctrl';
  const w = document.createElement('span');
  w.className = 'working'; w.textContent = '\u25cf working\u2026';
  const stopBtn = document.createElement('button');
  stopBtn.className = 'ghost'; stopBtn.textContent = 'Stop';
  stopBtn.title = 'Stop the running prompt';
  stopBtn.onclick = async () => { try { await fetch('/api/stop', {method:'POST'}); } catch(e){} };
  const qBtn = document.createElement('button');
  qBtn.className = 'ghost'; qBtn.textContent = 'Queue';
  qBtn.title = 'Queue what is in the prompt box to run after the current one finishes';
  qBtn.onclick = () => {
    const next = $('prompt') ? $('prompt').value.trim() : '';
    if (!next) return;
    queuePromptText(next, sel);
  };
  ctrl.appendChild(w); ctrl.appendChild(stopBtn); ctrl.appendChild(qBtn);
  am.appendChild(ctrl);
  return ctrl;
}

async function readChatStream(r, am){
  const body = am.querySelector('.body'), tools = am.querySelector('.tools');
  const rd = r.body.getReader(), dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const {done, value} = await rd.read();
    if (done) break;
    buf += dec.decode(value, {stream:true});
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, idx); buf = buf.slice(idx+2);
      const line = chunk.split('\n').find(l => l.startsWith('data:'));
      if (!line) continue;
      let ev; try { ev = JSON.parse(line.slice(5)); } catch(e){ continue; }
      if (ev.kind === 'text') { body.dataset.raw = (body.dataset.raw || '') + (ev.text || ''); body.innerHTML = renderMarkdown(body.dataset.raw); }
      else if (ev.kind === 'tool') { const t=document.createElement('div'); t.textContent='\u2699 '+ev.name; tools.appendChild(t); }
      else if (ev.kind === 'error') { const t=document.createElement('div'); t.className='warn'; t.textContent='\u26a0 '+(ev.error||'error'); body.appendChild(t); }
      else if (ev.kind === 'usage') { am.querySelector('.meta').textContent += ' \u00b7 ' + (ev.total_tokens||0).toLocaleString() + ' tok'; }
      else if (ev.kind === 'done') {
        // v0.6.2: the done event carries the full text. If streaming
        // produced nothing (no text events), fall back to it — otherwise
        // a completed run shows an empty body.
        if (!body.dataset.raw && ev.text) { body.dataset.raw = ev.text; body.innerHTML = renderMarkdown(ev.text); }
        if (ev.error && !ev.ok) { const t=document.createElement('div'); t.className='warn'; t.textContent='\u26a0 '+(ev.error||'error'); body.appendChild(t); }
      }
      am.scrollIntoView({behavior:'smooth', block:'end'});
    }
  }
}

// One full chat turn: user message, assistant message, SSE stream, cleanup.
// o = {prompt, sel, url, body, userMeta, onForce, after}
async function runTurn(o){
  const sel = o.sel;
  const um = addMsg('user', o.userMeta);
  um.querySelector('.body').textContent = o.prompt;
  const provLabel = providerChatLabel(sel.provider);
  const am = addMsg('asst', (sel.model||'default model') + ' \u00b7 ' + (sel.effort||'') + ' \u00b7 ' + provLabel);
  const body = am.querySelector('.body');
  body.dataset.raw = '';
  chatRunning = true; $('send').disabled = true; refreshBusyUI();
  let ctrl = null;
  try {
    const r = await fetch(o.url, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(o.body),
    });
    if (r.status === 429) {
      const j = await r.json().catch(()=>({}));
      body.innerHTML = '<span class="warn">5-hour budget exhausted.</span> ' + esc(j.hint||'') +
        '<br><button class="ghost" id="forcebtn" style="margin-top:8px">Run anyway</button> ' +
        '<button class="ghost" id="qbtn2" style="margin-top:8px">Queue instead</button>';
      $('forcebtn').onclick = o.onForce;
      $('qbtn2').onclick = () => queuePromptText(o.body.prompt || o.prompt, sel);
      return;
    }
    if (r.status === 409) {
      const j = await r.json().catch(()=>({}));
      body.innerHTML = '<span class="warn">\\u26a0 ' + esc(j.hint||j.error||'Already running.') + '</span>' +
        '<br><button class="ghost" id="qbtn3" style="margin-top:8px">Queue instead</button>';
      $('qbtn3').onclick = () => queuePromptText(o.body.prompt || o.prompt, sel);
      return;
    }
    if (!r.ok) { const j = await r.json().catch(()=>({})); throw new Error(j.hint||j.error||('HTTP '+r.status)); }
    ctrl = makeRunControls(am, o.prompt, sel);
    await readChatStream(r, am);
  } catch(e) {
    const t=document.createElement('div'); t.className='warn'; t.textContent='\u26a0 '+e.message; body.appendChild(t);
  } finally {
    if (ctrl) ctrl.remove();
    chatRunning = false; $('send').disabled = false; refreshBusyUI();
    renderMermaid(); refreshWindow(); refreshRuns(); refreshQueue(); refreshConversations();
    if (o.after) o.after();
  }
}

async function sendChat(force=false, promptOverride=null, selOverride=null){
  // Voice: sending stops transcription first, so the full transcribed
  // text (including the last spoken words) is what gets sent — the
  // user never has to stop the mic before tapping Send.
  if (typeof stopVoiceAndFlush === 'function') await stopVoiceAndFlush();
  const prompt = (promptOverride || $('prompt').value).trim();
  if (!prompt) return;
  const sel = selOverride || currentSelections();
  const sentAtts = ATTACHMENTS.slice(); ATTACHMENTS = []; renderAttachments();
  const attsNote = sentAtts.length ? '\n\nAttachments: ' + sentAtts.map(a => a.name).join(', ') : '';
  $('prompt').value = '';
  await runTurn({
    prompt: prompt + attsNote, sel, url: '/api/chat',
    body: Object.assign({prompt, force}, sel),
    userMeta: 'you \u00b7 ' + new Date().toLocaleTimeString(),
    onForce: () => sendChat(true, prompt),
  });
}

// Force a queued item to run right now: the server stops any in-flight
// run first, then streams this prompt like a normal chat turn.
async function sendQueuedNow(it, force=false){
  const sel = {
    provider: it.provider || ((CFG && CFG.provider) || 'meta'),
    model: it.model || null, effort: it.effort || null,
    context_window: it.context_window || null, max_steps: it.max_steps || null,
    workdir: it.workdir || null, auto_approve: !!it.auto_approve,
    attachments: it.attachments || [],
  };
  await runTurn({
    prompt: it.prompt, sel, url: '/api/queue/' + it.id + '/send-now',
    body: {force},
    userMeta: 'you \u00b7 ' + new Date().toLocaleTimeString() + ' \u00b7 from queue',
    onForce: () => sendQueuedNow(it, true),
    after: refreshQueue,
  });
}

async function queuePromptText(prompt, sel){
  if (!prompt || !prompt.trim()) return;
  await api('/api/queue', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(Object.assign({prompt}, sel))});
  refreshQueue();
}

async function queuePrompt(){
  if (typeof stopVoiceAndFlush === 'function') await stopVoiceAndFlush();
  const prompt = $('prompt').value.trim();
  if (!prompt) return;
  const sel = currentSelections();
  await queuePromptText(prompt, sel);
  $('prompt').value = ''; ATTACHMENTS = []; renderAttachments();
}

function refreshBusyUI(){
  const show = chatRunning || serverBusy;
  $('busypill').style.display = show ? '' : 'none';
  $('busystop').style.display = show ? '' : 'none';
}

async function pollBusy(){
  if (chatRunning) return;  // this tab's own run state is authoritative
  try {
    const w = await api('/api/window');
    serverBusy = !!w.cli_busy;
    refreshBusyUI();
  } catch(e){}
}

async function refreshWindow(){
  try {
    const w = await api('/api/window');
    const pct = Math.min(100, Math.round(100 * w.usage.prompts / Math.max(1,w.budget)));
    $('meterfill').style.width = pct + '%';
    $('meterfill').style.background = pct >= 100 ? 'var(--bad)' : pct >= 75 ? 'var(--warn)' : 'var(--ok)';
    $('dot').style.background = w.can_invoke ? 'var(--ok)' : 'var(--bad)';
    $('metertext').textContent = w.usage.prompts + ' / ' + w.budget + ' prompts in last 5h';
    $('metertokens').textContent = '· ' + (w.usage.total_tokens||0).toLocaleString() + ' tokens';
    $('meternext').textContent = w.usage.next_capacity_at && !w.can_invoke
      ? '· capacity frees ~' + new Date(w.usage.next_capacity_at*1000).toLocaleTimeString() : '';
    $('send').disabled = false;
  } catch(e) {}
}

async function refreshQueue(){
  try {
    const q = await api('/api/queue');
    const el = $('qlist');
    if (!q.items.length) { const h = harnessOf(currentProviderId()), n = nameOf(currentProviderId()); el.innerHTML = '<span class="src">Empty. Queue a prompt and it will ' + (h === 'claude' ? 'invoke ' + esc(n) : (h === 'baseten' ? 'ask ' + esc(n) : 'invoke Muse Code')) + (h === 'muse' ? ' as soon as your 5-hour budget allows.' : '.') + '</span>'; return; }
    el.innerHTML = '';
    q.items.forEach(it => {
      const d = document.createElement('div'); d.className='qitem';
      d.innerHTML = '<span class="pill">'+esc(it.status)+'</span><span class="p">'+esc(it.prompt.slice(0,120))+'</span>';
      const s = document.createElement('button'); s.className='ghost'; s.textContent='Send now';
      s.title = 'Run this prompt right now (stops any in-flight run first)';
      s.onclick = () => sendQueuedNow(it);
      const b = document.createElement('button'); b.className='ghost'; b.textContent='Remove';
      b.onclick = async () => { await fetch('/api/queue/'+it.id, {method:'DELETE'}); refreshQueue(); };
      d.appendChild(s); d.appendChild(b); el.appendChild(d);
    });
  } catch(e) {}
}

function renderConversationMessages(c){
  $('chat').innerHTML = '';
  if (c.file) $('convinfo').textContent = 'Saved to ' + c.file + ' · ' + (c.messages ? c.messages.length : 0) + ' exchanges';
  if (c.messages) c.messages.forEach(m => {
    const um = addMsg('user', 'you'); um.querySelector('.body').textContent = (m.prompt || '') + (m.attachments && m.attachments.length ? '\n\nAttachments: ' + m.attachments.join(', ') : '');
    const am = addMsg('asst', (m.model || 'default model') + ' · ' + (m.effort || '') + ' · ' + (m.provider || ''));
    const body = am.querySelector('.body'); body.dataset.raw = m.text || ''; body.innerHTML = renderMarkdown(body.dataset.raw);
    if (m.error) { const t=document.createElement('div'); t.className='warn'; t.textContent='⚠ '+m.error; body.appendChild(t); }
  });
  renderMermaid();
}

async function restoreConversation(){
  try {
    const c = await api('/api/conversation');
    renderConversationMessages(c);
  } catch(e) {}
}

async function refreshConversations(){
  try {
    const c = await api('/api/conversations');
    const el = $('convlist');
    const items = (c.conversations || []).slice().reverse();  // newest first
    if (!items.length) { el.innerHTML = '<span class="src">No previous conversations.</span>'; return; }
    el.innerHTML = '';
    items.forEach(it => {
      const d = document.createElement('div');
      d.className = 'convitem' + (it.current ? ' current' : '');
      d.title = 'Open conversation ' + it.id + ' — resumes with its own memory';
      const when = it.last_ts ? new Date(it.last_ts*1000).toLocaleString() : '';
      const t = document.createElement('div'); t.className = 'ct';
      t.textContent = (it.preview || '(empty conversation)');
      const m = document.createElement('div'); m.className = 'cm';
      const n = it.messages || 0;
      m.textContent = n + (n === 1 ? ' message' : ' messages') + (when ? ' · ' + when : '') + (it.current ? ' · current' : '');
      d.appendChild(t); d.appendChild(m);
      d.onclick = () => openConversation(it.id);
      el.appendChild(d);
    });
  } catch(e) {}
}

async function openConversation(id){
  try {
    const c = await api('/api/conversations/' + id + '/open', {method:'POST'});
    SESSION = crypto.randomUUID().replace(/-/g,'').slice(0,12);
    renderConversationMessages(c);
    refreshConversations();
  } catch(e) {}
}

async function refreshRuns(){
  try {
    const r = await api('/api/runs');
    const el = $('runs'); el.innerHTML = '';
    r.runs.slice(0,8).forEach(run => {
      const d = document.createElement('div'); d.className='run';
      const when = run.finished_at ? new Date(run.finished_at*1000).toLocaleTimeString() : '';
      d.textContent = (run.status||'?') + ' · ' + (run.model||'default') + ' · ' + when +
        (run.usage && run.usage.total_tokens ? ' · ' + run.usage.total_tokens.toLocaleString() + ' tok' : '');
      d.title = (run.prompt||'').slice(0,300);
      el.appendChild(d);
    });
    if (!r.runs.length) el.innerHTML = '<span class="src">No runs yet.</span>';
  } catch(e) {}
}

// ---------------------------------------------------------------- voice
// Voice-to-text ONLY (no text-to-voice): the browser's Web Speech API
// transcribes in the browser; audio is never sent to the MusePilot server.
// Live interim results appear in the prompt textarea as you speak, and
// because the transcript lands in the normal textarea it stays editable
// before sending. stopVoiceAndFlush() is what Send calls first: it stops
// recognition and waits for the final result (or a short timeout) so the
// last spoken words are not lost.
let voiceRec = null, voiceListening = false, voiceWant = false;
let voiceBase = '', voiceFinal = '', voiceFromSpeech = false, voiceStopResolve = null;
const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
function voiceSupported(){ return !!SpeechRec; }
function voiceSetStatus(msg){ const el = $('micstatus'); if (el) el.textContent = msg || ''; }
function voiceApply(){
  voiceFromSpeech = true;
  $('prompt').value = voiceBase + voiceFinal;
  // Keep interim out of the saved value: interim is shown by appending
  // it temporarily below in onresult; see voiceApplyWithInterim.
  voiceFromSpeech = false;
}
function stopVoiceAndFlush(){
  if (!voiceListening || !voiceRec) return Promise.resolve();
  voiceWant = false;
  return new Promise(resolve => {
    voiceStopResolve = resolve;
    try { voiceRec.stop(); } catch(e) { resolve(); voiceStopResolve = null; }
    setTimeout(() => { if (voiceStopResolve) { voiceStopResolve(); voiceStopResolve = null; } }, 600);
  });
}
function startVoice(){
  if (!voiceSupported()) { voiceSetStatus('Voice input is not supported in this browser — try Chrome or Edge.'); return; }
  if (voiceListening) { stopVoiceAndFlush(); return; }
  voiceRec = new SpeechRec();
  voiceRec.continuous = true;
  voiceRec.interimResults = true;
  voiceRec.lang = navigator.language || 'en-US';
  voiceBase = $('prompt').value;
  if (voiceBase && !voiceBase.endsWith(' ') && !voiceBase.endsWith('\n')) voiceBase += ' ';
  voiceFinal = '';
  voiceWant = true; voiceListening = true;
  $('mic').textContent = '⏹ Stop voice'; $('mic').classList.add('recording');
  voiceSetStatus('Listening… speak now. Tap Send any time — it stops the mic and sends everything.');
  voiceRec.onresult = (ev) => {
    let final = '', interim = '';
    for (let i = ev.resultIndex; i < ev.results.length; i++) {
      const r = ev.results[i];
      if (r.isFinal) final += r[0].transcript;
      else interim += r[0].transcript;
    }
    // Final results accumulate; interim is the current in-progress phrase.
    // Recompute final from all results so a restart does not duplicate text.
    let allFinal = '';
    for (let i = 0; i < ev.results.length; i++) if (ev.results[i].isFinal) allFinal += ev.results[i][0].transcript;
    voiceFinal = allFinal;
    voiceFromSpeech = true;
    $('prompt').value = voiceBase + voiceFinal + interim;
    voiceFromSpeech = false;
    $('prompt').focus();
  };
  voiceRec.onerror = (ev) => {
    const err = ev.error || 'error';
    if (err === 'not-allowed' || err === 'service-not-allowed') voiceSetStatus('Mic blocked — allow microphone access for this page, then try again.');
    else if (err === 'no-speech') voiceSetStatus('No speech heard — still listening.');
    else if (err === 'audio-capture') voiceSetStatus('No microphone found.');
    else voiceSetStatus('Voice input error: ' + err);
  };
  voiceRec.onend = () => {
    voiceListening = false;
    $('mic').textContent = '🎤 Voice input'; $('mic').classList.remove('recording');
    if (voiceStopResolve) { voiceStopResolve(); voiceStopResolve = null; }
    if (voiceWant) {
      // Chrome stops recognition after a pause; restart while the user
      // still wants it, keeping the transcript accumulated so far.
      try {
        voiceBase = $('prompt').value;
        if (voiceBase && !voiceBase.endsWith(' ') && !voiceBase.endsWith('\n')) voiceBase += ' ';
        voiceFinal = ''; voiceListening = true;
        $('mic').textContent = '⏹ Stop voice'; $('mic').classList.add('recording');
        voiceRec.start();
      } catch(e) { voiceSetStatus('Voice input stopped.'); }
    } else {
      voiceSetStatus('');
    }
  };
  try { voiceRec.start(); } catch(e) { voiceSetStatus('Could not start voice input: ' + e.message); voiceListening = false; $('mic').textContent = '🎤 Voice input'; $('mic').classList.remove('recording'); }
}
$('mic').onclick = startVoice;
if (!voiceSupported()) { $('mic').disabled = true; $('mic').title = 'Voice input needs a browser with the Web Speech API (Chrome/Edge).'; voiceSetStatus('Voice input needs Chrome or Edge (Web Speech API). Typing still works.'); }
// If the user edits the prompt by hand while listening, re-base the
// transcript on their edited text so their edit is not overwritten by
// the next speech result.
$('prompt').addEventListener('input', () => {
  if (voiceListening && !voiceFromSpeech) {
    voiceBase = $('prompt').value;
    if (voiceBase && !voiceBase.endsWith(' ') && !voiceBase.endsWith('\n')) voiceBase += ' ';
    voiceFinal = '';
  }
});

// ---------------------------------------------------------------- providers (v0.6.0)
let PROVIDERS = [];

async function refreshProviders(){
  const j = await api('/api/providers');
  PROVIDERS = j.providers || [];
  const sel = $('provider');
  const prev = sel.value || j.current || 'meta';
  sel.innerHTML = '';
  PROVIDERS.forEach(p => {
    const op = document.createElement('option');
    op.value = p.id;
    op.textContent = p.name + (p.builtin ? '' : ' (custom)');
    sel.appendChild(op);
  });
  const mo = document.createElement('option');
  mo.value = '__manage__'; mo.textContent = '\u2699 Manage custom providers\u2026';
  sel.appendChild(mo);
  sel.value = PROVIDERS.some(p => p.id === prev) ? prev : (j.current || 'meta');
  updateProviderNote();
}

function currentProvider(){
  const v = $('provider') ? $('provider').value : null;
  return (PROVIDERS.find(p => p.id === v)) || null;
}

function currentProviderId(){
  const p = currentProvider();
  return p ? p.id : ((CFG && CFG.provider) || 'meta');
}
function harnessOf(pid){ const p = PROVIDERS.find(x => x.id === pid); return p ? p.harness : 'muse'; }
function nameOf(pid){ const p = PROVIDERS.find(x => x.id === pid); return p ? p.name : 'Muse Code'; }
function providerChatLabel(pid){
  const h = harnessOf(pid), n = nameOf(pid);
  return h === 'claude' ? n + ' · Claude Code · tools' : (h === 'baseten' ? n + ' · direct · no tools' : 'Muse Code');
}

function updateProviderNote(){
  const p = currentProvider();
  const el = $('providernote');
  if (!p) { el.textContent = ''; return; }
  const harness = p.harness === 'claude' ? 'Claude Code' : (p.harness === 'baseten' ? 'Direct API' : 'Muse Code');
  const keyTxt = p.harness === 'muse' ? (p.has_key ? 'signed in' : 'not signed in') : (p.has_key ? 'key saved' : 'no key saved');
  el.textContent = harness + ' \u00b7 ' + keyTxt;
}

async function onProviderChange(){
  const v = $('provider').value;
  if (v === '__manage__') { openProviderDialog(); $('provider').value = (CFG && CFG.provider) || 'meta'; return; }
  try { await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({provider: v})}); if (CFG) CFG.provider = v; } catch(e){}
  updateProviderLabels();
  updateProviderNote();
  await refreshModelsForProvider(v);
  const p = currentProvider();
  if (p && p.harness !== 'muse' && !p.has_key) openKeyModal(p);
}

async function refreshModelsForProvider(pid){
  try {
    const m = await api('/api/models?refresh=1&provider=' + encodeURIComponent(pid));
    populateModels(m);
    const models = m.models || [];
    if (models.length) await onModelChange(models[0].id, pid);
  } catch(e){}
}

// ---- key modal ----
let keyModalProvider = null;
function openKeyModal(p){
  keyModalProvider = p;
  $('keytitle').textContent = 'API key \u2014 ' + p.name;
  $('keyinput').value = '';
  $('keynote').textContent = '';
  $('keydetail').textContent = p.harness === 'muse'
    ? 'Muse Code can also use `muse login`. Saving a key here stores META_API_KEY on this machine.'
    : 'Required before ' + p.name + ' can run. Stored on this machine only.';
  $('keylogout').style.display = p.harness === 'muse' ? '' : 'none';
  $('keymodal').style.display = 'flex';
  setTimeout(() => $('keyinput').focus(), 50);
}
function closeKeyModal(){ $('keymodal').style.display = 'none'; keyModalProvider = null; }

// ---- custom providers dialog ----
let editingProviderId = null;
async function openProviderDialog(){
  editingProviderId = null;
  $('cp-name').value = ''; $('cp-baseurl').value = ''; $('cp-model').value = '';
  $('cp-harness').value = 'claude'; $('cp-note').textContent = '';
  $('cp-save').textContent = 'Save provider';
  await refreshProviderDialog();
  $('provmodal').style.display = 'flex';
}
function closeProviderDialog(){ $('provmodal').style.display = 'none'; editingProviderId = null; }

async function refreshProviderDialog(){
  const j = await api('/api/providers');
  PROVIDERS = j.providers || [];
  const el = $('provlist'); el.innerHTML = '';
  PROVIDERS.forEach(p => {
    const d = document.createElement('div'); d.className = 'provrow';
    const info = document.createElement('div'); info.className = 'p';
    const nm = document.createElement('div'); nm.className = 'nm';
    nm.textContent = p.name + (p.builtin ? ' (built-in)' : '');
    const dt = document.createElement('div'); dt.className = 'dt';
    const harness = p.harness === 'claude' ? 'Claude Code' : (p.harness === 'baseten' ? 'Direct API' : 'Muse Code');
    dt.textContent = harness + ' \u00b7 ' + (p.model || 'discovered models') + (p.base_url ? ' \u00b7 ' + p.base_url : '') + ' \u00b7 ' + (p.has_key ? 'key saved' : 'no key');
    info.appendChild(nm); info.appendChild(dt); d.appendChild(info);
    const kb = document.createElement('button'); kb.className = 'ghost';
    kb.textContent = p.has_key ? 'Key \u2713' : 'Key'; kb.title = 'Enter or update the API key';
    kb.onclick = () => { closeProviderDialog(); openKeyModal(p); };
    d.appendChild(kb);
    if (!p.builtin) {
      const eb = document.createElement('button'); eb.className = 'ghost'; eb.textContent = 'Edit';
      eb.onclick = () => {
        editingProviderId = p.id;
        $('cp-name').value = p.name;
        $('cp-harness').value = p.harness;
        $('cp-baseurl').value = p.base_url || '';
        $('cp-model').value = p.model || '';
        $('cp-save').textContent = 'Update provider';
        $('cp-note').textContent = '';
      };
      const db = document.createElement('button'); db.className = 'ghost'; db.textContent = 'Delete';
      db.onclick = async () => {
        if (!confirm('Delete provider "' + p.name + '"? Its saved key is removed too.')) return;
        try { await fetch('/api/providers/' + encodeURIComponent(p.id), {method:'DELETE'}); }
        catch(e){}
        editingProviderId = null; $('cp-save').textContent = 'Save provider';
        await refreshProviderDialog(); await refreshProviders();
      };
      d.appendChild(eb); d.appendChild(db);
    }
    el.appendChild(d);
  });
}

async function saveCustomProvider(){
  const data = {
    name: $('cp-name').value.trim(),
    harness: $('cp-harness').value,
    base_url: $('cp-baseurl').value.trim(),
    model: $('cp-model').value.trim(),
  };
  $('cp-note').textContent = '';
  try {
    let r;
    if (editingProviderId) {
      r = await fetch('/api/providers/' + encodeURIComponent(editingProviderId), {
        method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data)});
    } else {
      r = await fetch('/api/providers', {
        method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data)});
    }
    const j = await r.json().catch(()=>({}));
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    editingProviderId = null; $('cp-save').textContent = 'Save provider';
    $('cp-name').value = ''; $('cp-baseurl').value = ''; $('cp-model').value = '';
    await refreshProviderDialog(); await refreshProviders();
    // Select the new/edited provider, and prompt for its key if missing.
    const pid = (j.provider && j.provider.id) || null;
    if (pid) {
      $('provider').value = pid;
      await onProviderChange();
    }
  } catch(e) { $('cp-note').textContent = e.message; }
}

async function saveProviderKey(){
  const p = keyModalProvider; if (!p) return;
  const key = $('keyinput').value;
  if (!key.trim()) { $('keynote').textContent = 'Enter a key first.'; return; }
  try {
    const r = await fetch('/api/providers/' + encodeURIComponent(p.id) + '/key', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({key})});
    const j = await r.json().catch(()=>({}));
    if (!r.ok) throw new Error(j.message || j.error || ('HTTP ' + r.status));
    $('keynote').textContent = j.message || 'Saved.';
    $('keyinput').value = '';
    await refreshProviders();
    if ($('provmodal').style.display !== 'none') await refreshProviderDialog();
  } catch(e) { $('keynote').textContent = e.message; }
}

async function removeProviderKey(){
  const p = keyModalProvider; if (!p) return;
  try {
    const j = await fetch('/api/providers/' + encodeURIComponent(p.id) + '/key', {method:'DELETE'}).then(r => r.json());
    $('keynote').textContent = j.message || 'Removed.';
    await refreshProviders();
    if ($('provmodal').style.display !== 'none') await refreshProviderDialog();
  } catch(e) { $('keynote').textContent = e.message; }
}

$('send').onclick = () => sendChat(false);
$('queuebtn').onclick = queuePrompt;
async function saveEffort(val){
  const p = currentProvider();
  const isMuse = !p || p.harness === 'muse';
  const body = isMuse ? {default_effort: val} : {baseten_effort: val};
  try { await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)}); if (CFG) { if (isMuse) CFG.default_effort = val; else CFG.baseten_effort = val; } } catch(e) {}
}
async function saveContext(val){
  try { await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({default_context_window: val ? parseInt(val,10) : null})}); if (CFG) CFG.default_context_window = val ? parseInt(val,10) : null; } catch(e) {}
}
$('provider').onchange = onProviderChange;
$('keybtn').onclick = () => { const p = currentProvider(); if (p) openKeyModal(p); };
$('cp-save').onclick = saveCustomProvider;
$('cp-close').onclick = closeProviderDialog;
$('keysave').onclick = saveProviderKey;
$('keyremove').onclick = removeProviderKey;
$('keyclose').onclick = closeKeyModal;
$('keylogout').onclick = async () => {
  if (!confirm('Sign out of Muse Code? This clears the stored `muse login` credential (not a saved API key).')) return;
  try {
    const j = await api('/api/auth/logout', {method:'POST'});
    $('keynote').textContent = j.message || 'Signed out.';
  } catch(e) { $('keynote').textContent = e.message; }
  await refreshProviders();
};
$('provmodal').addEventListener('click', e => { if (e.target === $('provmodal')) closeProviderDialog(); });
$('keymodal').addEventListener('click', e => { if (e.target === $('keymodal')) closeKeyModal(); });
$('model').onchange = () => onModelChange($('model').value);
$('effort').onchange = () => saveEffort($('effort').value);
$('ctx').onchange = () => saveContext($('ctx').value);
$('prompt').addEventListener('keydown', e => { if ((e.metaKey||e.ctrlKey) && e.key==='Enter') sendChat(false); });
$('newchat').onclick = async () => { try { const c = await api('/api/conversation/new', {method:'POST'}); if (c.file) $('convinfo').textContent = 'Saved to ' + c.file + ' · 0 exchanges'; } catch(e) {} SESSION = crypto.randomUUID().replace(/-/g,'').slice(0,12); $('chat').innerHTML=''; refreshConversations(); };
$('busystop').onclick = async () => { try { await fetch('/api/stop', {method:'POST'}); } catch(e){} };
$('plan').onchange = async () => {
  const plan = $('plan').value;
  const j = await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({plan_tier: plan})});
  $('budget').value = j.config.prompt_budget; refreshWindow();
};
$('budget').onchange = async () => {
  await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({prompt_budget: parseInt($('budget').value,10)||30})});
  refreshWindow();
};

// v0.6.0: auth status now lives in the provider note (updateProviderNote)
// and the key modal. The key modal's logout button handles `muse logout`.
async function loadAuth(){ try { await api('/api/providers'); } catch(e){} }

loadAll().then(()=>{ refreshWindow(); refreshQueue(); refreshRuns(); refreshConversations(); loadAuth(); wireAttachments(); });
setInterval(refreshWindow, 10000);
setInterval(refreshQueue, 5000);
setInterval(refreshRuns, 15000);
setInterval(refreshConversations, 15000);
setInterval(pollBusy, 3000);
</script>
</body>
</html>'''

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def pick_port(preferred=8471):
    import socket
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return preferred


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="MusePilot — chat UI for Muse Code")
    ap.add_argument("--port", type=int, default=8471)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)

    app = App()
    port = pick_port(args.port)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.app = app
    app.worker.start()
    # v0.4.2 auto-download: only in the real server launch, never from
    # App() construction (tests) — and only for tools that are missing.
    if app.config.get("auto_install_clis", True):
        for tool, info in app.cli_manager.status().items():
            if not info["present"]:
                started, msg = app.cli_manager.start_install(tool)
                print(f"auto-install {tool}: {msg}")

    url = f"http://127.0.0.1:{port}/"
    print(f"{APP_NAME} v{VERSION} — {url}")
    if app.cli.available:
        print(f"muse CLI: {app.cli.binary} ({app.cli.version() or 'version unknown'})")
    else:
        print("WARNING: `muse` CLI not found on PATH. The UI will still open; "
              "install it with: curl -fsSL https://dev.meta.ai/install.sh | sh")
    print(f"Data directory: {DATA_DIR}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.worker.stop()
        server.server_close()


if __name__ == "__main__":
    main(sys.argv[1:])
