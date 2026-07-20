"""Deterministic land-risk classifier (generic, config-driven).

Reads a built branch's actual `git diff --numstat` and emits a fail-closed LOW / NOT_LOW verdict.
LOW iff EVERY changed path is inside the allowlist AND outside the denylist, AND the diff is under
the size caps. Anything else (denylisted path, path outside the allowlist, oversize, binary/unsizable,
malformed input) is NOT_LOW. Never returns LOW on ambiguity - a miss costs a human stop, never an
unreviewed auto-land.

The `gated-land` pipeline only auto-lands a `risk=LOW` change; everything else routes to a human. So
this file encodes YOUR "safe to auto-land unreviewed?" policy. The DEFAULT_* below are conservative,
generic starting points - EDIT THEM for your repo (your sensitive dirs, secret files, and manifests),
or extend them at call time via `--deny` / `--deny-substr` (append-only) and `--max-lines` /
`--max-files` (tighten-only). The allowlist is deliberately NOT runtime-overridable.

Caller contract: feed `git diff --numstat --no-renames <base>..<head>` on stdin. `--no-renames` splits
a rename into a plain delete + add so each path is policy-checkable; as a backstop the classifier also
fails closed on any residual rename marker (`old => new`, `{a => b}`) so a rename can never be LOW."""
import sys, json, fnmatch
from collections import namedtuple

Change = namedtuple("Change", "added deleted path")
Config = namedtuple("Config", "allow deny_glob deny_substr max_lines max_files")


def _glob_any(path, patterns):
    # A pattern ending in '/**' is a directory-prefix match; else fnmatch (note: fnmatch '*'
    # crosses '/', which is acceptable here - denylist over-matching only makes the gate stricter).
    # Matching is case-insensitive (casefold) so a case-variant path (e.g. src/Safety/gate.py on a
    # case-insensitive filesystem) cannot slip past a denylist glob.
    p = path.casefold()
    for pat in patterns:
        q = pat.casefold()
        if q.endswith("/**"):
            if p == q[:-3] or p.startswith(q[:-2]):  # e.g. 'docs/api/**' -> 'docs/api/'
                return pat
        elif fnmatch.fnmatch(p, q):
            return pat
    return None


def _is_rename(path):
    # `git diff --numstat` renders a rename as `old => new` or a braced form
    # `src/{safety => security}/gate.py`. The classifier cannot reliably policy-check such a compound
    # path, so it fails closed (NOT_LOW) and expects the caller to pass `--no-renames` (which splits a
    # rename into a plain delete + add). Belt-and-suspenders: even if the caller forgets, a rename
    # can never be classified LOW.
    return " => " in path or ("{" in path and "}" in path)


def _count(tok):
    if tok == "-":
        return None
    try:
        v = int(tok)
    except ValueError:
        raise ValueError("bad numstat count: %r" % tok)
    if v < 0:
        # `git diff --numstat` never emits negative counts; a negative would let a huge addition be
        # cancelled by a negative on another line, sneaking an oversize diff under the cap. Fail closed.
        raise ValueError("negative numstat count (invalid): %r" % tok)
    return v


def parse_numstat(text):
    changes = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)  # path may contain spaces; 2-split keeps the path field whole
        if len(parts) != 3:
            raise ValueError("malformed numstat line (need 3 tab-fields): %r" % line)
        added, deleted, path = parts
        changes.append(Change(_count(added), _count(deleted), path))
    return changes


def classify_risk(changes, cfg):
    reasons = []
    if len(changes) > cfg.max_files:
        reasons.append("too many files: %d > %d" % (len(changes), cfg.max_files))
    total = 0
    for c in changes:
        if c.added is None or c.deleted is None:
            reasons.append("binary/unsizable file (cannot count lines): %s" % c.path)
        else:
            total += c.added + c.deleted
    if total > cfg.max_lines:
        reasons.append("too many changed lines: %d > %d" % (total, cfg.max_lines))
    lc_substr = [(s, s.casefold()) for s in cfg.deny_substr]
    for c in changes:
        if _is_rename(c.path):
            reasons.append("rename/complex path (re-run diff with --no-renames): %s" % c.path)
            continue
        g = _glob_any(c.path, cfg.deny_glob)
        if g:
            reasons.append("denylisted path (%s): %s" % (g, c.path))
            continue
        lp = c.path.casefold()
        sub = next((orig for orig, cf in lc_substr if cf in lp), None)
        if sub:
            reasons.append("denylisted token (%s): %s" % (sub, c.path))
            continue
        if not _glob_any(c.path, cfg.allow):
            reasons.append("outside allowlist: %s" % c.path)
    return {"risk": "NOT_LOW" if reasons else "LOW", "reasons": reasons}


DEFAULT_MAX_LINES = 400
DEFAULT_MAX_FILES = 15

# ---------------------------------------------------------------------------
# EDIT THESE for your repo. Conservative generic defaults: only docs/tests/src
# auto-land, and anything looking like secrets / CI / agent-config / dependency
# manifests is forced to a human review. When in doubt, add - the gate only ever
# gets stricter, never weaker.
# ---------------------------------------------------------------------------
DEFAULT_ALLOW = ["docs/**", "tests/**", "test/**", "src/**"]

# Directory / exact-path globs that force NOT_LOW even inside the allowlist.
DEFAULT_DENY_GLOB = [
    ".env", "*.env", "**/.env", "**/*.env",                       # secrets
    ".github/workflows/**", ".gitlab-ci.yml", ".circleci/**",     # CI (self-hosting risk)
    ".claude/hooks/**", ".claude/settings.json",
    ".claude/settings.local.json",                                # agent config / permissions
    "requirements.txt", "requirements-*.txt", "pyproject.toml",
    "setup.py", "setup.cfg", "Pipfile", "poetry.lock",            # python manifests
    "package.json", "package-lock.json", "yarn.lock",
    "pnpm-lock.yaml",                                             # node manifests
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum", "Gemfile",
    "Gemfile.lock",                                              # other manifests/lockfiles
    "Dockerfile", "**/Dockerfile", "*.tf",                        # infra
]

# Substring tokens (repo-relative path contains, case-insensitive). Generic security-sensitive
# stems - a path containing any of these routes to a human. Extend for your codebase's sensitive
# families via `--deny-substr` (append-only) or dev-loop.conf.
DEFAULT_DENY_SUBSTR = sorted({
    "secret", "password", "credential", "token", "apikey", "api_key",
    "auth", "oauth", "login", "session", "crypto", "signing", "privatekey", "private_key",
    "migration", "schema", "payment", "billing", "charge", "webhook", "permission",
})


def build_config(flags):
    # ALL flags are TIGHTENING-ONLY - config can only make the gate stricter, never weaker, so no CLI
    # invocation can widen the auto-land envelope:
    #   --deny / --deny-substr : APPEND to the built-in denylist (can add restrictions, never wipe them).
    #   --max-lines / --max-files : clamped to min(given, default) - can lower a cap, never raise it.
    # The allowlist is intentionally NOT runtime-overridable (a custom allow could only broaden the
    # protected set - a pure footgun; the static DEFAULT_ALLOW is the source of truth). A trailing flag
    # with no value, or any unknown flag, raises ValueError (caught by main -> exit 2 + NOT_LOW), never
    # an IndexError crash.
    deny_glob, deny_substr = [], []
    max_lines, max_files = DEFAULT_MAX_LINES, DEFAULT_MAX_FILES

    def _val(i, name):
        if i + 1 >= len(flags):
            raise ValueError("flag %s requires a value" % name)
        return flags[i + 1]

    i = 0
    while i < len(flags):
        a = flags[i]
        if a == "--deny":
            deny_glob.append(_val(i, a)); i += 1
        elif a == "--deny-substr":
            deny_substr.append(_val(i, a)); i += 1
        elif a == "--max-lines":
            max_lines = min(int(_val(i, a)), DEFAULT_MAX_LINES); i += 1   # tighten only
        elif a == "--max-files":
            max_files = min(int(_val(i, a)), DEFAULT_MAX_FILES); i += 1   # tighten only
        else:
            raise ValueError("unknown flag: %r" % a)
        i += 1
    return Config(
        allow=DEFAULT_ALLOW,
        deny_glob=DEFAULT_DENY_GLOB + deny_glob,        # append: defaults always retained
        deny_substr=DEFAULT_DENY_SUBSTR + deny_substr,  # append: defaults always retained
        max_lines=max_lines, max_files=max_files)


def demo():
    cfg = build_config([])
    assert classify_risk(parse_numstat("3\t0\tdocs/x.md\n"), cfg)["risk"] == "LOW"
    assert classify_risk(parse_numstat("5\t2\tsrc/util/helpers.py\n"), cfg)["risk"] == "LOW"
    # secrets / CI / manifests are denied even inside the allowlist
    assert classify_risk(parse_numstat("1\t1\t.env\n"), cfg)["risk"] == "NOT_LOW"
    assert classify_risk(parse_numstat("1\t1\t.github/workflows/ci.yml\n"), cfg)["risk"] == "NOT_LOW"
    assert classify_risk(parse_numstat("1\t1\tpackage.json\n"), cfg)["risk"] == "NOT_LOW"
    # substring token match (auth) routes to a human
    assert classify_risk(parse_numstat("1\t1\tsrc/authz/gate.py\n"), cfg)["risk"] == "NOT_LOW"
    # path outside the allowlist fails closed
    assert classify_risk(parse_numstat("1\t1\tscripts/deploy.sh\n"), cfg)["risk"] == "NOT_LOW"
    # binary / unsizable fails closed
    assert classify_risk([Change(None, None, "docs/pic.png")], cfg)["risk"] == "NOT_LOW"
    # rename markers fail closed even though neither side is individually denied
    assert classify_risk([Change(1, 1, "docs/{a => b}.md")], cfg)["risk"] == "NOT_LOW"
    assert classify_risk([Change(1, 1, "docs/a.md => .env")], cfg)["risk"] == "NOT_LOW"
    # case-variant sensitive path still denied
    assert classify_risk([Change(1, 1, "SRC/authZ/Gate.py")], cfg)["risk"] == "NOT_LOW"
    # a custom --deny does NOT wipe the built-in denylist (append semantics)
    assert classify_risk([Change(1, 1, ".env")], build_config(["--deny", "custom/**"]))["risk"] == "NOT_LOW"
    # config is tightening-only: --max-lines cannot RAISE the cap (clamped to the default)
    assert build_config(["--max-lines", "9999"]).max_lines == DEFAULT_MAX_LINES
    assert build_config(["--max-files", "9999"]).max_files == DEFAULT_MAX_FILES
    assert build_config(["--max-lines", "50"]).max_lines == 50   # lowering is allowed
    # negative numstat counts are invalid -> fail closed
    raised = False
    try:
        parse_numstat("1000\t0\ta.md\n-600\t0\tb.md\n")
    except ValueError:
        raised = True
    assert raised
    # an unknown/valueless flag is a graceful ValueError, not an IndexError crash
    raised = False
    try:
        build_config(["--allow"])   # --allow is not a valid flag -> unknown-flag ValueError
    except ValueError:
        raised = True
    assert raised
    raised = False
    try:
        parse_numstat("no-tabs\n")
    except ValueError:
        raised = True
    assert raised
    print("SELFTEST-OK")


def main(argv):
    flags = argv[1:]
    if flags == ["--selftest"]:      # standalone only; --selftest with any other flag falls through
        demo(); return 0             # to normal parsing -> unknown flag -> ValueError -> exit 2
    # --gate: encode the verdict in the EXIT CODE (0=LOW, 1=NOT_LOW, 2=could-not-classify) for shell
    # callers (the lander) to gate on; JSON still goes to stdout. Without --gate, exit is unchanged
    # (0 for LOW or NOT_LOW, 2 for could-not-classify).
    gate = "--gate" in flags
    flags = [f for f in flags if f != "--gate"]
    try:
        cfg = build_config(flags)
        changes = parse_numstat(sys.stdin.read())
    except ValueError as e:
        print(json.dumps({"risk": "NOT_LOW", "reasons": ["could not classify: %s" % e]}))
        return 2
    result = classify_risk(changes, cfg)
    print(json.dumps(result))
    if gate:
        return 0 if result["risk"] == "LOW" else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
