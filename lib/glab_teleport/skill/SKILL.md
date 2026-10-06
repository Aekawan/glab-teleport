---
name: glab-teleport
description: Move, migrate, copy, sync or verify GitLab groups and projects between two GitLab instances (self-hosted or gitlab.com) with the glab-teleport CLI — every branch and tag, CI/CD variables in every environment scope, runners, protected branches, environments and settings, with a verification report. Use this skill whenever the user wants to migrate GitLab repositories or groups to another GitLab, keep a new GitLab in sync with an old one, check whether a migration is complete or up to date, compare CI/CD variables between two GitLabs, or asks about glab-teleport — even if they don't name the tool (e.g. "move our repos to the new GitLab", "is the new GitLab up to date?", "ย้าย gitlab", "sync gitlab", "เช็คว่าย้ายครบไหม").
---

# glab-teleport

`glab-teleport` copies GitLab groups and projects from a **source** GitLab to a **target** GitLab, then verifies both sides match and writes a report. It is a published CLI (`npm install -g glab-teleport`, Python 3.9+ and git required).

Your job is to drive it safely on the user's behalf. A migration touches shared company infrastructure, so the user must see what will happen and agree before anything is written. The tool is designed for this: with `--json` it never changes anything unless you add `--yes`.

## The rules, and why

1. **Plan before you write.** Every write command (`group`, `project`, `sync`) first runs without `--yes`. That returns a plan and changes nothing. Show the plan, then ask. A teleport can create dozens of projects in a structure that is tedious to undo, which has happened before.
2. **`--yes` only after an explicit "yes" from the user for that exact plan.** If the plan changes (different target, options or components), show it again.
3. **Never handle credentials.** Don't read, print or ask for tokens. Don't open `~/.config/glab-teleport/credentials.json` or `runner-tokens.txt`. If login is needed, ask the user to run `glab-teleport login` in their own terminal. It is interactive and keeps secrets out of the chat.
4. **Destructive flags need a specific request from the user.** Add `--force-push` (overwrites target branches), `--prune` (deletes target branches/tags/variables missing on the source), `--overwrite` on `group`/`project`, or `--relocate` (moves existing target projects; needs Owner) only when the user asked for that effect. Explain what it will do first.
5. **Read-only commands are free.** `doctor`, `verify`, `audit`, `refs`, `report` and any command without `--yes` change nothing. Run them without asking.

If a command prints "Read-only mode is on", the machine is configured so that nothing can be written (`glab-teleport config read_only true`). Report the plan and tell the user. Don't try to work around it.

## Workflow

Always pass `--json` and parse stdout. Human-readable progress goes to stderr. Use `--lang th` for Thai human output if the user writes in Thai. The JSON content stays the same either way.

### 0. Check the setup

```bash
glab-teleport --version --json            # missing? → npm install -g glab-teleport
glab-teleport doctor --json
```

`doctor` returns `checks[]` with `ok`, `level` and `hint`. If source/target are not configured or not signed in, ask the user to run `glab-teleport login` themselves, then re-run doctor. DNS failures usually mean VPN.

### 1. Work out source and target

- **The user names them:** use their paths (`group/sub/project`). Web URLs and git URLs also work.
- **The user asks to "sync"/"update" something teleported before:** `glab-teleport sync <source> --json` remembers the target. Without a source, list earlier runs with `glab-teleport report --json`.
- **The user is unsure what is migrated:** `glab-teleport audit [source-group] [target-group] --json` gives per-project status: `synced`, `outdated`, `empty`, `conflict`, `missing`.

### 2. Plan (no changes)

```bash
glab-teleport group   <source-group>   <target-group>  --json      # whole group, any subgroup depth
glab-teleport project <source-project> <target>       --json      # target = group (keeps the name) or full path
glab-teleport sync    <source> [target]               --json      # bring an earlier teleport up to date
```

Optional: `--only repo,env,runner,settings,extras` to limit what is transferred (default: all). `--layout keep` is the default and mirrors the source structure. Only use `flat`/`join`/`auto` if the user asks.

The result has `"mode": "plan"`, `"confirmed": false`, a `summary` (`projects`, `new`, `synced`, `update`, `move`, `skip`), `warnings`, and `items[]` (`source`, `target`, `state`, `reason`, `code`). See [references/json.md](references/json.md) for every field.

Present the plan compactly:
- source → target, layout, and what will be transferred
- counts by state
- every `skip` with its reason (these will not be touched)
- `warnings`, especially commits that exist only on the target (`code.diverged`): those branches are kept, and the user should know someone works on the target
- for `new`/`update`, the most relevant targets; don't paste 50 rows unless asked

Then ask a clear yes/no question, e.g. "Teleport these 52 projects to platform/payments?"

### 3. Execute (only after "yes")

Run the **same command** plus `--yes`:

```bash
glab-teleport group <source> <target> --json --yes
```

Large groups take minutes. Let it finish. Re-running is safe: work already done is detected and skipped.

### 4. Report back

The result is the run report: `counts` (`ok`, `warn`, `manual`, `fail`), `totals` (source vs target branches, tags, variables, protected rules, environments, runners), `scopes` (CI/CD variables per environment scope), `projects[]` with `issues[]`, `skipped[]`, and `report_path` (a Markdown report to share).

Summarise like this:

1. **Outcome:** for example, "52/52 projects verified; 201/201 CI/CD variables across scopes *, prod, uat match."
2. **Failures** (`level: fail`): what failed and the likely fix.
3. **Manual actions** (`level: manual`): what the user must do and who can do it. Common ones:
   - *runner #N is never_contacted*: register it on a runner host with `gitlab-runner register --url <target> --token <token from ~/.glab-teleport/runner-tokens.txt>`. Point to the file, never print the token.
   - *hidden variable*: the source hides the value, so the user must set it on the target by hand.
   - *no account on target yet*: those members must sign in to the new GitLab once, then run `sync … --only extras`.
   - *needs the Owner role*: group runners, moving projects. Someone with Owner must do it or grant the role.
4. **Report location:** `report_path`.

## Verifying and syncing

- **Is it complete or identical?** Run `glab-teleport verify <source> <target> --json` (read-only). Variables are compared per key and environment scope on value, type, protected, masked and raw, for projects and their groups. Values are never shown. Read `complete` first: `skipped[]` lists source projects that are not on the target at all, and they count as missing even if every project that *is* there checks out.
- **Catching up while the team still works on the old GitLab:** plan with `sync <source> --json`, show what changed, then rerun with `--yes`. Projects already up to date are skipped quickly. Branches changed on the target are reported, never overwritten, unless the user explicitly wants `--force-push`.
- **After the move:** `glab-teleport refs <target> --json` finds files and variables that still point at the old GitLab. `glab-teleport repoint` writes a script teammates run to switch their local clones.

## When something goes wrong

- **`"ok": false` with `error`:** read the message. It usually says what to run next (e.g. `glab-teleport login`, VPN, missing target group).
- **`skip` with "name clash" or "unrelated code":** don't force it. Explain the item and let the user decide (another target path, or a separate `project` command).
- **The target group is scheduled for deletion:** ask an Owner to delete it immediately, or wait.
- **The user is Maintainer but the plan needs moves:** the tool stops before changing anything. Explain that an Owner must run it or grant the role.
