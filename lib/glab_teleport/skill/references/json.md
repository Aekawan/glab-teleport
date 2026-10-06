# glab-teleport `--json` output reference

Every command prints exactly one JSON document on stdout. Human-readable progress goes to stderr. The exit code is `0` on success and non-zero when something failed (`"ok": false`).

## Errors (any command)

```json
{"ok": false, "error": "Not signed in to the target GitLab (https://git.example.org). Run: glab-teleport login"}
```

## `doctor`

```json
{"ok": true, "problems": 0, "source": "https://gitlab.old.example.com", "target": "https://git.new.example.org",
 "checks": [{"ok": true, "level": "ok", "check": "signed in as alice", "hint": ""},
            {"ok": false, "level": "warn", "check": "scopes: read_api", "hint": "missing read_repository"}]}
```

`level`: `ok`, `warn` (works, but worth mentioning), `fail` (must be fixed).

## Plan: `group` / `project` / `sync` without `--yes` (or with `--dry-run`)

```json
{"ok": true, "mode": "plan", "command": "group|project|sync", "kind": "group|project",
 "source": "payments", "target": "platform/payments", "target_exists": true, "layout": "keep",
 "components": ["repo", "env", "runner", "settings", "extras"], "options": {"prune": true},
 "summary": {"projects": 12, "new": 3, "synced": 7, "update": 2, "move": 0, "skip": 0},
 "warnings": ["1 projects have commits on the target that are not on the source; those branches are kept"],
 "items": [{"source": "payments/api", "target": "platform/payments/api", "state": "update", "reason": "",
            "move_from": null, "archived": false, "conflict": false,
            "code": {"state": "outdated", "missing": ["refs/heads/feature/x"], "different": ["refs/heads/main"],
                     "behind": ["refs/heads/main"], "diverged": [], "extra": []}}],
 "confirmed": false, "next": "Nothing was changed. Show this plan to the user; after they approve, run the same command with --yes."}
```

`state`:
- `new`: will be created on the target.
- `synced`: already identical. Variables, rules and settings are still applied/checked.
- `update`: exists on the target. Missing or outdated refs will be pushed.
- `move`: exists elsewhere on the target and will be moved (only with `--relocate`, needs Owner).
- `skip`: will not be touched. Read `reason`. `conflict: true` means the target holds unrelated code.

`code` (null for new projects):
- `missing`: refs that will be created.
- `behind`: refs the target merely lags on. Safe to update.
- `diverged`: refs that have commits only on the target. Never overwritten without `--force-push`.
- `extra`: refs only on the target. Removed only with `--prune`.

## Result: `group` / `project` / `sync` with `--yes`, and `verify`

```json
{"ok": true, "mode": "teleport|sync|verify", "report_path": "~/.glab-teleport/runs/<id>/report.md",
 "kind": "group", "source": "payments", "target": "platform/payments", "layout": "keep",
 "source_url": "...", "target_url": "...", "components": ["repo", "env"], "seconds": 41,
 "counts": {"ok": 10, "warn": 1, "manual": 1, "fail": 0},
 "totals": {"source_branches": 23, "target_branches": 23, "source_tags": 11, "target_tags": 11,
            "source_vars": 64, "matched_vars": 64, "exact_vars": 63, "source_protected": 9, "matched_protected": 9,
            "source_envs": 6, "matched_envs": 6, "source_runners": 2, "matched_runners": 2},
 "scopes": {"*": [28, 28], "production": [18, 18], "staging": [18, 18]},
 "projects": [{"source": "payments/api", "target": "platform/payments/api", "status": "ok|warn|manual|fail",
               "changes": ["2 branches/tags updated", "3 variables"],
               "issues": [{"level": "manual", "area": "variables", "text": "1 × hidden on source — value cannot be verified: STRIPE_KEY [production]"}],
               "refs": {"source_branches": 5, "target_branches": 5, "missing": [], "different": [], "extra": []},
               "variables": [{"key": "DB_URL", "scope": "production", "type": "env_var", "protected": true, "masked": false,
                              "status": "ok", "detail": ""}],
               "steps": {"repo": {"status": "ok", "detail": "5 branches, 2 tags"}}}],
 "skipped": [{"source": "...", "target": "...", "reason": "..."}],
 "groups": [], "moves": [], "runner_tokens": "~/.glab-teleport/runner-tokens.txt", "runners_created": 1}
```

- `verify` adds `"complete": true|false` and `"not_on_target": n`. `skipped[]` then lists the source projects that are **not on the target yet** — a migration with any of these is not complete, even when every checked project is `ok`. `ok` is false if anything failed or is missing.
- Group-level CI/CD variables are in `groups[]` and `totals.source_group_vars` / `matched_group_vars` (present only when the source group has any).
- `scopes[scope] = [source_count, target_count]`
- Variable values never appear anywhere in the output.
- `issues[].level`: `fail` (broken), `manual` (a person must act), `warn` (worth mentioning), `info` (e.g. a variable that exists only on the target).

## Other commands

- `report --json`: `{"ok": true, "runs": [{"id", "mode", "kind", "source", "target", "started", "counts", "report_path"}]}`
- `report latest --json`: the full result document of the latest run.
- `audit [src] [dst] --json`: `{"summary": {"synced": n, "outdated": n, "empty": n, "conflict": n, "missing": n}, "by_group": {...}, "rows": [{"source", "target", "match", "category", "sync"}], "target_only": [...], "report_path"}`
- `refs [target] --json`: `{"references": [{"project", "kind", "where", "found", "fix"}]}` (secrets in URLs are masked)
- `repoint --json`: `{"script": "/abs/path/repoint.sh", "repositories": n}`
- `config --json`: `{"lang", "source", "target", "file"}`
