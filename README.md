# MusePilot

A macOS chat interface that drives **Muse Code** (Meta's `muse` CLI). Pick a model,
a reasoning-effort tier and a context window, chat, and let MusePilot track your
5-hour usage window — queuing prompts to auto-run when capacity frees up.

Zero dependencies: one Python 3 script, standard library only. No build step,
no npm.

## What's inside (v0.6.5)

- **Queue button visibility** — the main Queue button now appears only
  when something is already running (this tab or another). When idle,
  Send is the action; Queue is hidden.

## What's inside (v0.6.4)

- **Muse Code output fixed** — the v0.6.3 extractor concatenated
  session ids and event names into the chat (garbage). Now metadata
  events (session/runtime/lifecycle) are dropped and text comes only
  from content fields, so you see just the model's reply.

## What's inside (v0.6.3)

- **Muse Code streaming fixed** — the text extractor now deep-scans
  `muse exec --json` output for any string content instead of only
  known fields, so whatever the CLI emits appears in the chat as it
  arrives (nothing silently dropped).

## What's inside (v0.6.2)

- **Busy Queue button** — when a run is already going, the "already
  running" message now has its promised **Queue instead** button (it
  was text-only before).
- **New conversations appear immediately** — the empty file is created
  on New, so it shows in the list right away instead of only after the
  first message.

## What's inside (v0.6.1)

- **Queue button fixed** — during a run it now queues what is in the
  prompt box (your next prompt), not a duplicate of the running prompt.
- **Conversation list restyled** — two-line cards: the first prompt on
  top, message count and time below; the current conversation is
  highlighted.

## What's inside (v0.6.0)

- **Left bar redesign — the Settings window is gone.** Provider, model,
  reasoning effort and context window now live directly on the left bar,
  always visible, with the conversation list right below them.
- **Provider dropdown** — Muse Code, Baseten (pre-created, built in),
  and your own custom providers. Pick "Manage custom providers…" to
  open a dialog where you create, edit and delete custom providers.
  Each custom provider picks a harness — **Claude Code** (agentic CLI)
  or **Direct API** (OpenAI-compatible) — plus its own base URL, model
  and API key. Once created it appears in the dropdown.
- **Keys** — selecting a provider with no saved key pops the key
  dialog automatically. The 🔑 button next to the provider updates the
  key any time; custom providers can also manage their key from the
  providers dialog. Keys stay on the machine (Keychain or a mode-0600
  file), one slot per provider.
- **Recent runs** moved into a collapsed accordion below the
  conversation list; Run options, Command-line tools and the 5-hour
  budget are collapsed accordions too, so the bar stays short.

## What's inside (v0.5.0)

- **Stop button** — while a prompt is running you now see a pulsing
  "● working…" indicator with **Stop** and **Queue** buttons right under
  the reply. Stop kills the running CLI process immediately (all three
  providers) instead of leaving you stuck on "already running".
- **Send now for queued prompts** — every queued item has a **Send now**
  button. It stops any in-flight run first, then runs that prompt
  immediately with the same streaming view as a normal send. The
  5-hour budget still applies (the item goes back in the queue on
  refusal) unless you run it with force.
- **In-progress indication everywhere** — the top bar shows a pulsing
  "● running…" pill with a Stop button whenever the server is busy,
  even if the run was started from another tab or before the page loaded
  (checked every 3 seconds).
- **Previous conversations** — the sidebar now lists every saved chat
  session with its first prompt, message count and time. Click one to
  open it; each conversation keeps its own CLI session memory, so
  reopening one resumes where it left off instead of starting blank.
- **Queue while running** — the Queue button is now visible during a
  run, so you can line up the next prompt without waiting.

## What's inside (v0.4.9)

- **5-hour budget is Muse Code only** — the budget gate was applied to
  every provider, so Claude Code and Baseten direct were blocked with
  "5-hour budget exhausted" even though they run on Baseten, not on
  Meta's 5-hour window. The gate now applies only to Muse Code, in
  both direct sends and queued prompts. Claude and Baseten prompts run
  even when the Muse window is used up.
- **Run anyway actually works** — the Run anyway button called Send
  with no prompt, but Send had already cleared the prompt box, so it
  silently did nothing. It now resends the original prompt with
  force=true. For Muse Code this bypasses the gate, as intended;
  Queue instead was already restoring the prompt and still works.

## What's inside (v0.4.8)

- **File and image attachments** — 📎 Attach in the composer, or paste
  or drag-and-drop files and images straight into the prompt box.
  Attached files appear as chips and are cleared after sending, just
  like the prompt. Files are uploaded to the local MusePilot server
  (`~/.musepilot/attachments/`, kept, so a resumed session can re-read
  them) and never sent to a third party by MusePilot itself.
- **How each provider gets them** — Claude Code: the file paths are
  added to the prompt and Claude reads them with its pre-allowed Read
  tool (images included). Muse Code: images also get the native
  `--image <path>` flag, plus file paths in the prompt. Baseten
  direct (no tools): text files are inlined into the message and
  images are sent as vision blocks; other file types get an honest
  note that Claude/Muse mode can read them.
- **Limits** — 10 MiB per file, oversize uploads are rejected (413).
  Attachment names are saved in the conversation transcript.
- **Restore bug fixed** — v0.4.7's conversation restore was wired to
  the Tools Download button instead of page load, so refreshing never
  restored the chat. Restore now runs on page load and clears the chat
  first so it cannot duplicate messages.

## What's inside (v0.4.7)

- **Memory for Claude Code and Muse Code, plus saved conversations** —
  Claude Code: first prompt in a conversation passes `--session-id
  <uuid>`, later prompts pass `--resume <uuid>`, so follow-ups like
  "are you sure?" now see earlier turns. If a resume fails because
  that session is gone, MusePilot retries once on a fresh session
  instead of failing forever. Muse Code: the session id is now a
  real UUID saved on the server (it used to be a 12-hex id that
  changed on every page load, which broke memory), passed as
  `muse exec --session-id <uuid>` on every prompt in the
  conversation. New conversation resets both session ids.
- **Conversation saved to a file** — every exchange is appended to
  `~/.musepilot/conversations/conversation-001.jsonl` (one JSON
  object per exchange, with prompt, reply, provider, model and
  effort). The current conversation is restored when you reopen
  the app, and the saved file is shown under New conversation.
  When a file would grow past 1 MiB, a new file starts
  (`conversation-002.jsonl`, and so on) and the old file is kept,
  never overwritten or deleted.
- **What memory is not** — it is session memory like Codex CLI,
  not Cursor-style codebase indexing. It does not remember other
  conversations, and it does not automatically know your whole
  repo: code itself lives on disk, and the model re-reads files
  with Read/Glob/Grep. Baseten direct chat is still stateless:
  its exchanges are saved to the same file, but the model is not
  sent the history.
- **Tests** — `test_musepilot_v047.py` 30/30 with a fake Claude
  binary: first run `--session-id`, second run `--resume` with the
  same uuid, new conversation gives a fresh uuid, resume-failure
  fallback, Muse uuid stability, transcript saving, 1 MiB
  rotation, and the new conversation API. Live Baseten resume
  still needs a check on a real Mac with a real key, because the
  model is unrecognised by Claude Code and that combination is
  unsupported by Anthropic.

## What's inside (v0.4.6)

- **Version fixed** — no code changes from v0.4.5. The bundle's
  Info.plist still said 0.2.0 while the code was 0.4.5, so Finder
  showed the old version after installing the new build. Info.plist
  is now 0.4.6, matching the code, and a version-sync test
  (`test_musepilot_v046.py`) now fails the build if they ever drift
  again.

## What's inside (v0.4.5)

- **Claude Code as a provider** — a third Settings radio: Claude Code
  (Baseten model, with tools). It drives the `claude` CLI with your
  Baseten key: ANTHROPIC_BASE_URL=https://inference.baseten.co (no
  /v1), ANTHROPIC_AUTH_TOKEN + Authorization header = Baseten key,
  ANTHROPIC_MODEL = meta-models/muse-spark-open-1026. Effort maps to
  `claude --effort` (low/medium/high/xhigh, default xhigh). In
  non-interactive mode WebSearch/WebFetch are not granted by default,
  so MusePilot pre-allows WebSearch, WebFetch, Read, Glob, Grep with
  `--allowedTools` (last in argv). File edits and shell run ONLY when
  auto-approve is on (`--permission-mode acceptEdits`). Runs are
  per-prompt in this version (MusePilot session ids are not Claude
  UUIDs, so --session-id is not passed). Direct Baseten chat stays
  as the second radio, unchanged.
- **All labels follow the provider (v0.4.4 fix, included here)** —
  Send button, composer placeholder, queue text, top bar and reply
  meta now say Muse Code / Baseten / Claude Code according to the
  selected provider, instead of always saying Muse Code.

## What's inside (v0.4.3)

- **Markdown rendering in chat** — assistant replies are now rendered
  as markdown (headings, bold/italic, lists, links, tables, blockquotes)
  instead of showing `###` and `**` literally (the v0.4.2 bug: chat
  used `textContent`). Code blocks render as `<pre><code>`, inline
  code as `<code>`. HTML is escaped first, so model output can never
  inject live HTML.
- **Mermaid diagrams** — ```mermaid blocks render as diagrams after
  a reply finishes, using the mermaid library from CDN when online;
  offline, the diagram source stays visible as a code block.
- **Honest provider labels** — the top bar no longer says "drives
  muse exec" when Baseten is selected; it says "Baseten direct chat
  (no tools)", and each Baseten reply is labelled "Baseten direct ·
  no tools". Baseten mode is still direct Chat Completions, with no
  tools and no browsing — see the Claude Code note below.

## What's inside (v0.4.2)

- **Auto-download of Muse Code + Claude Code** — on launch, MusePilot
  checks for both CLIs and downloads any missing one automatically
  (official installers only: Muse Code from `https://dev.meta.ai/install.sh`,
  Claude Code from `https://claude.ai/install.sh`, downloaded to a temp
  file and run with an argv list, then verified with `--version` — a
  failed install is shown as an error, never as success). A new
  **Command-line tools** panel in the sidebar shows each tool's status
  and version, with a manual Download / Reinstall button and an
  "Auto-download missing tools on launch" toggle (on by default, saved
  to `~/.musepilot/config.json`). Detection searches PATH plus
  `~/.local/bin`, `/usr/local/bin`, and `/opt/homebrew/bin`, so a CLI
  installed in a terminal is found even when MusePilot is launched
  from Finder. Note: installing a CLI is not signing in — first run
  of each CLI still asks you to sign in (Muse Code key in Settings,
  Claude Code via its own login).

## What's inside (v0.4.1)

- **Baseten effort + context are now prepopulated by querying the model**
  — when you save a Baseten key (or refresh models), MusePilot calls
  Baseten `GET /v1/models` live and fills in the context window from
  that model's `context_length` (1,048,576 tokens), plus its max
  completion tokens and `reasoning` feature. `GET /v1/models/<id>`
  does not exist on Baseten (HTTP 405), so the list is fetched and
  filtered by id. Without a key, or if the query fails, context is
  shown as unknown with an error — never guessed from a constant.
- **Baseten effort list corrected to the model's documented levels:**
  `low, medium, high, xhigh` (default `xhigh`), from Baseten's
  reasoning docs, Muse Spark Open row. v0.4.0's 7-value list came
  from Baseten's generic validator, but for this model `none` and
  `minimal` are not distinct levels (they run at `low`) and `max`
  is not listed for Spark Open, so they are no longer offered.
  The API list itself does not publish an effort list — the docs
  row is labelled as the source in the UI.

## What's inside (v0.4.0)

- **Provider radio in Settings: Meta / Baseten** — Meta is the v0.3.0 path
  (queried models, `muse` CLI). Baseten is a separate path: it calls
  Baseten's OpenAI-compatible Chat Completions API directly, because the
  `muse` CLI cannot be pointed at Baseten (live-tested: it rejects a
  non-sanctioned base_url). Baseten's model is **hardcoded to
  `meta-models/muse-spark-open-1026` only** — that provider supports no
  other model in MusePilot. The Baseten API key is **never hardcoded**:
  enter it in Settings under Baseten (separate slot from the Meta key,
  Keychain or mode-0600 file, never shown back).
- **Baseten reasoning effort: selectable, queried** — live-tested
  2026-10-26: `minimal, low, medium, high, xhigh, max, none` are all
  accepted by Baseten, default `xhigh`; low used 231 reasoning tokens
  vs xhigh 432 on the same prompt. Select one in Settings; it is saved
  locally and sent as `reasoning_effort` on every Baseten chat.
- **Baseten context window: fixed, not selectable** — 1,048,576 tokens,
  read from Baseten `GET /v1/models` (`context_length`). It is a model
  property, not a per-request setting (a `context_window` parameter is
  silently ignored by Baseten, live-tested). The UI shows the fixed
  window and disables the field in Baseten mode, instead of offering
  fake sizes. Max output per Baseten reply is capped at 8192 tokens.
- **All Settings selections are saved locally** — provider, model,
  reasoning effort, and context value are written to
  `~/.musepilot/config.json` on change and restored on next launch,
  for **both** Meta and Baseten (v0.3.0 saved only the model reliably).

## What's inside (v0.3.0)

- **Chat UI** — prompt Muse Code from a clean chat window; replies, tool calls
  and token usage stream live over SSE as `muse exec --json` emits them.
- **Settings / Connect** — top-right **Settings** button. Paste your Muse
  Code API key there; saving it immediately queries the available models.
  The key is stored on this machine only (macOS Keychain, or a mode-0600
  file under `~/.musepilot/` elsewhere) and is never shown back or logged.
- **Model / reasoning / context, queried — never hardcoded** — the model
  list is queried in order from (1) the Meta Model API (`GET /v1/models`,
  key-authenticated) and (2) the installed `muse` binary via
  `muse schema generate-json-schema` / `muse exec --help` (strict enum
  match only). There is NO fallback list: if no live source returns
  models, the UI says so and offers a manual model-id entry. Selecting a
  model re-queries that model's detail: effort tiers live from the CLI
  schema/help (they are global tiers, and the UI says so), the model's
  default effort and knobs from `muse model-profile show`, and context
  information — CLI 1.4.x has **no** context-window flag, so the window
  is the model's native one, not settable from MusePilot, and the UI
  shows that instead of offering fake sizes. Whatever you select is
  passed to `muse exec` as `--model` and `--reasoning-effort`.
- **Voice input (voice-to-text only)** — a 🎤 button in the composer uses
  the browser's Web Speech API. Transcription is live, lands in the
  editable prompt box (edit it before sending), and tapping **Send**
  auto-stops the mic and sends the full transcribed text — you never
  have to stop the mic first. Audio never goes to the MusePilot server.
  Needs Chrome/Edge; typing still works everywhere else.
- **5-hour window tracker** — every invocation is recorded in
  `~/.musepilot/usage.jsonl`. The header meter shows prompts used vs. your
  budget in the trailing 5 hours, tokens consumed, and when the next capacity
  frees up (usage ages out exactly 5h after it happened — a rolling window).
- **Queue: "invoke when possible"** — hit *Queue for when available* (or send
  while the budget is exhausted) and a background worker auto-invokes Muse Code
  the moment your window allows. The queue survives restarts.
- **Conversations** — each chat thread keeps a session id, so follow-ups
  continue the same Muse Code session (`--session-id`). *New conversation*
  starts fresh.
- **Run options** — working directory, max model steps, and an explicit
  auto-approve toggle (maps to `--yolo`, off by default).

## Requirements

- macOS 12+ with Python 3.8+ (`/usr/bin/python3` — ships with macOS / Xcode CLT)
- The Muse Code CLI installed and signed in:
  `curl -fsSL https://dev.meta.ai/install.sh | sh`

## Install

1. Unzip `MusePilot-mac.zip` and drag **MusePilot.app** to Applications.
2. First launch: right-click → **Open** (it's not Apple-notarized, so Gatekeeper
   will ask once). A small Terminal window shows the server log; the chat UI
   opens in your browser at `http://127.0.0.1:8471/`.
3. Prefer the terminal? Run `./run.sh` instead — same app, no bundle.

## Authentication

Authenticate in **Settings** (or the sidebar) — or outside the app:

- `muse login` in a terminal (browser OAuth flow). The credential caches at
  `~/.config/muse/auth.json`.
- Or export `META_API_KEY` (takes precedence over a stored login).
- Or paste a key in Settings: saved to your login Keychain on macOS, or a
  mode-0600 file under `~/.musepilot/` elsewhere, and injected as
  `META_API_KEY` into `muse` subprocesses. This also fixes the
  double-clickable `.app` not inheriting your shell's exported variables.
  An already-exported `META_API_KEY` still wins, matching the CLI's own
  precedence.

The sidebar shows an **Authentication** status pill — click it for a detail
view: which credential source is active and, when the CLI exposes it, the
signed-in account email and tier. It also offers **Sign out**
(`muse logout`). Saving a key in Settings immediately re-queries models;
a rejected key shows as an API error, not a silent fallback list.

## The 5-hour budget, honestly

Meta documents the $5/mo **Everyday** tier as roughly **10–50 prompts every
5 hours**, metered by tokens in a rolling window, and describes higher tiers as
5x/20x/80x multiples. Meta does **not** publish an exact token budget, so
MusePilot tracks what it can measure precisely (every prompt + token counts from
`muse exec --json`) and gates on an *estimated* prompt budget:

| Tier | Default budget (prompts / 5h) |
|---|---|
| Everyday — $5/mo | 30 |
| High — $15/mo | 150 |
| Power — $50/mo | 600 |
| Max — $200/mo | 2400 |

Tune the number in the sidebar to match what you actually observe. The meter is
informational — it only sees prompts sent *through MusePilot*, not ones you run
in your own terminal.

## Notes & limitations

- Selections are only passed as flags your CLI actually supports (detected from
  `--help`). If a future `muse` version renames a flag, the UI adapts instead
  of breaking.
- `muse exec --json`'s event shape isn't publicly documented, so output parsing
  is best-effort and tolerant of unknown shapes.
- The local server binds to **127.0.0.1 only** — nothing leaves your machine
  except the `muse` CLI's own traffic to Meta.
- Auto-approve (`--yolo`) lets the agent run commands without asking. It's off
  by default; only enable it in directories you trust.
- Prompts and settings live in `~/.musepilot/` (`usage.jsonl`, `queue.json`,
  `config.json`, `runs/`).

## For developers

`musepilot.py` is the entire app. Tests run against a fake `muse` binary:

```
HOME=/tmp/mptest-v030-home PATH=/tmp/fakebin-v030:$PATH python3 test_musepilot_v030.py
```

`test_musepilot_v030.py` has 52 checks: strict discovery (including the
`CodeModeLanguage` trap), API model query, file-backed key storage,
per-model detail, SSE streaming *timing* (first chunk before stream end),
and voice/Settings UI presence. The fake CLI is a test fixture, not part
of the shipped app; `test_musepilot.py` (older suite) is kept for the
ledger/queue/HTTP coverage it already had.
