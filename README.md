# skills

Claude Code skills, distributed as a plugin marketplace so you can install them **without cloning
this repo**.

Two plugins:

| Plugin | Skills | Portable? |
|---|---|---|
| **`dev-loop-core`** | `review-gate`, `session-retro` | ✅ works in any repo |
| **`gated-land`** | `ship`, `gate-loop`, `land` | ⚙️ opinionated; configure per-repo via `.dev-loop.conf` |

## Install

```
/plugin marketplace add weeeBox/skills
/plugin install dev-loop-core@skills
/plugin install gated-land@skills      # optional; depends on dev-loop-core
```

`/plugin marketplace add` fetches the marketplace into Claude Code's plugin cache - you never clone or
manage this repo by hand. Update later with `/plugin marketplace update skills`.

## Prerequisites

- **`review-gate`, `ship`, `gate-loop`, `land`** drive external reviewer models, each shipped as its own
  Claude Code plugin marketplace. Install whichever you want to gate on:
  - **codex** - the primary ship/no-ship gate.
    [`openai/codex-plugin-cc`](https://github.com/openai/codex-plugin-cc):
    `/plugin marketplace add openai/codex-plugin-cc` then `/plugin install codex`.
  - **deepseek** - a second, independent blocking gate (needs `DEEPSEEK_API_KEY`).
    [`weeeBox/deepseek-plugin-cc`](https://github.com/weeeBox/deepseek-plugin-cc):
    `/plugin marketplace add weeeBox/deepseek-plugin-cc` then `/plugin install deepseek`.
  - **agy** (Antigravity) - advisory only.
    [`weeeBox/agy-plugin-cc`](https://github.com/weeeBox/agy-plugin-cc):
    `/plugin marketplace add weeeBox/agy-plugin-cc` then `/plugin install agy`.
  - The gates **fail closed**: a missing/errored reviewer is a human stop, never a silent pass.
- **`session-retro`** needs only the `claude` CLI (it runs headless `claude -p` calls). No API key if
  you are on a plan-based login.

---

## `dev-loop-core`

### `review-gate`

Runs codex + deepseek (blocking) + agy (advisory) over one unit of work - a **plan** (before coding) or
a **branch diff** (before merge) - in parallel, with a wedge watchdog, a per-worktree lock, and
fail-closed verdict handling. It **stops for a human**; it never fixes, re-gates, merges, or pushes.

> This is the canonical home of `review-gate`. The standalone `weeeBox/review-gate` repo is deprecated
> in favor of this bundle.

### `session-retro`

Analyzes a day of Claude Code session transcripts across all your projects and writes a friction report
with **evidence-backed, concretely-actionable** improvement recommendations to
`~/.claude/session-reports/<date>.md`, plus a metrics trend line and a recommendation-outcome ledger.

**The security design is the point.** Transcript content is untrusted (it can contain
prompt-injection). So the synthesis runs as tool-less `claude -p` map-reduce calls
(`--tools "" --strict-mcp-config --max-turns 1`): the model never gets **any** tools while processing
transcript-derived content. Do not "improve" it by giving the synthesis calls tools - that reopens the
injection boundary.

Run it on demand:

```
python3 "$CLAUDE_PLUGIN_ROOT/skills/session-retro/scripts/scan_sessions.py" scan --date YYYY-MM-DD
bash "$CLAUDE_PLUGIN_ROOT/skills/session-retro/scripts/run_retro.sh"
```

**Optional daily autorun.** Point a scheduler at `run_retro.sh`:

- macOS (`launchd`) - a `~/Library/LaunchAgents/<your-label>.plist` running
  `bash <path>/scripts/run_retro.sh` on a `StartCalendarInterval`. Kick it with
  `launchctl kickstart -k gui/$(id -u)/<your-label>`.
- Linux (`cron`) - e.g. `7 8 * * * bash <path>/scripts/run_retro.sh`.

---

## `gated-land`

An **opinionated, autonomous gate-and-land pipeline** for a solo developer merging to `main`:

- **`ship`** - one-shot: run the full suite, then run `review-gate`'s diff stage once, then stop for a
  human merge decision. Attended.
- **`gate-loop`** - autonomous: suite + a test-tamper guard + `review-gate` in a loop (≤3 rounds,
  resuming the warm codex agent), producing a green `session/<slug>` branch. Stops before integration.
- **`land`** - a serialized local merge queue: merge the candidate into a throwaway worktree, run the
  suite on the *merged* commit, statically risk-classify the diff, gate codex + deepseek on the
  integration commit, and auto-commit only on `SHIP + SHIP + risk=LOW` (everything else stops for a
  human), CAS-protected against a moved target.

These depend on `dev-loop-core` (they drive the `review-gate` skill).

### Configure it to your repo

```
cp "$CLAUDE_PLUGIN_ROOT/dev-loop.conf.example" /path/to/your/repo/.dev-loop.conf
$EDITOR /path/to/your/repo/.dev-loop.conf     # set TEST_COMMAND, BASE_BRANCH
```

`.dev-loop.conf` lives in **your** repo (not the plugin dir) so it survives plugin updates and is
version-controlled per project. It wires the vendored merge queue (`engine/lander.sh`) and the
risk classifier (`engine/risk_classify.py`) - both self-contained, no dependency on the author's repo.

### The risk classifier is *your* policy

`land` only auto-commits a `risk=LOW` change; everything else routes to a human. `risk=LOW` means every
changed path is inside an allowlist and outside a denylist, under size caps - a fail-closed
deterministic check (`engine/risk_classify.py`). The shipped defaults are conservative and **generic**
(secrets / CI / dependency manifests / auth-ish tokens force a human). **Edit `DEFAULT_ALLOW` /
`DEFAULT_DENY_*` in `engine/risk_classify.py` for your codebase's sensitive paths**, or tighten
per-repo via `LANDER_RISK_EXTRA_FLAGS` in `.dev-loop.conf`. The config is append-only / tighten-only -
it can never *widen* the auto-land envelope.

Verify it: `python3 "$CLAUDE_PLUGIN_ROOT/engine/risk_classify.py" --selftest`.

## License

MIT - see [LICENSE](LICENSE).
