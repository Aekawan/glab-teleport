# glab-teleport

**Move GitLab groups and projects to another GitLab instance — with every branch, tag, CI/CD variable and environment scope verified on arrival.**

[![npm](https://img.shields.io/npm/v/glab-teleport)](https://www.npmjs.com/package/glab-teleport)
![node](https://img.shields.io/badge/node-%3E%3D16-informational)
![python](https://img.shields.io/badge/python-%3E%3D3.9-informational)
![dependencies](https://img.shields.io/badge/dependencies-none-success)

[ภาษาไทย](README.th.md) · [Install](#install) · [Quick start](#quick-start) · [Commands](#commands) · [How it works](#how-it-works) · [Safety](#safety-and-privacy) · [FAQ](#faq)

---

Moving from one GitLab to another usually means a pile of `git push --mirror` scripts, CI/CD variables copied by hand, and a nagging doubt that something was missed. `glab-teleport` does the whole job in one command, shows you the plan first, and ends with a report that proves source and target match.

```text
$ glab-teleport group payments platform/payments

Plan  group · payments → platform/payments
  Layout    keep — keep subgroups as they are
  Transfer  repo  env  runner  settings  extras
  Paths     relative to payments/ → platform/payments/

  STATUS     SOURCE              TARGET
  ✓ in sync  api                 api                 code in sync
  ↻ update   web                 web                 2 refs missing
  + new      services/ledger     services/ledger
  + new      services/invoicing  services/invoicing

  4 projects · 2 new · 1 in sync · 1 update

Teleport 4 projects now? [y/N] y

Teleporting  4 projects · 4 at a time
  ✓ payments/api                       repo ✓  env ✓  runner ✓  settings ✓  extras ✓   6s
  ✓ payments/web                       repo ✓  env ✓  runner ✓  settings ✓  extras ✓   9s
  ✓ payments/services/ledger           repo ✓  env ✓  runner –  settings ✓  extras ✓  14s
  ☞ payments/services/invoicing        repo ✓  env ☞  runner –  settings ✓  extras ✓  12s

Teleport result  group · payments → platform/payments · 41s

                     Source  Target
  Projects                4       4  ✓
  Branches               23      23  ✓
  Tags                   11      11  ✓
  Protected rules         9       9  ✓
  CI/CD variables        64      64  ✓
    scope *              28      28
    scope production     18      18
    scope staging        18      18
  Environments            6       6  ✓

  ✓ 3 verified   ☞ 1 need manual action

  Needs attention
  ☞ payments/services/invoicing
      variables: 1 × hidden on source — value cannot be verified: STRIPE_KEY [production]

  Report  ~/.glab-teleport/runs/20261006-104512-group-payments/report.md
```

## Features

- **Whole groups or single projects:** nested subgroups at any depth, or several projects into one group.
- **Everything that makes a project work:**
  - every branch and tag, including Git LFS and the wiki
  - CI/CD variables in every environment scope, at group and project level
  - environments, protected branches and tags, project settings
  - runners, pipeline schedules, webhooks, deploy keys and members
- **Pick what to move:** `--only repo`, `--only env`, `--only runner,env` or everything (the default).
- **Commit-verified matching:** projects that already exist on the target are found by their commits, even after being renamed or moved. They are updated in place, never duplicated, and unrelated code is never overwritten.
- **Your structure, your call:**
  - keep the source layout, flatten it, or fold subgroup names into project names
  - `keep` (default) mirrors the source exactly; `auto` copies whatever the target already uses
  - `--relocate` moves earlier, inconsistent copies into place
- **Proof, not hope:** after every run, each `(key, environment_scope)` variable is compared on value, type, protected, masked and raw. Every branch and tag SHA is compared too. You get `report.md` and `report.json`.
- **Clean terminal, English or Thai:** a fuzzy picker for interactive use (`glab-teleport`), one line per project while running, and the details in the report. Switch language with `--lang th`.
- **Works anywhere:** any self-managed GitLab or gitlab.com. Sign in with a token or OAuth. No dependencies beyond Python 3.9+ and git.

## Install

```bash
npm install -g glab-teleport
```

or, without Node:

```bash
pipx install git+https://github.com/Aekawan/glab-teleport
```

Requirements: Python 3.9 or newer (the one bundled with macOS works) and `git`. `git-lfs` is only needed for repositories that use LFS.

## Quick start

```bash
glab-teleport login     # enter both GitLab URLs and sign in to each
glab-teleport doctor    # check network, credentials, scopes and permissions
glab-teleport           # pick what to teleport from a list
```

`login` asks for the **source** and **target** URLs and signs in to both. Data flows through your machine, so the two GitLab servers never need to reach each other.

| Sign-in method | When to use |
|---|---|
| **Create a token in the browser** (default) | Works on every GitLab. The token page opens with the name and scopes pre-filled; click *Create* and paste. |
| **OAuth** | Browser sign-in like `glab auth login --web`. Needs an OAuth application on that GitLab (any user can create one under *User Settings → Applications*; redirect URI `http://localhost:7171/auth/redirect`, not confidential). An application already configured for `glab` is reused automatically. Tokens refresh on their own. |
| **Paste a token** | Personal, group or project access token you already have. |

| | Scopes | Role |
|---|---|---|
| Source | `read_api`, `read_repository` | Maintainer (needed to read CI/CD variables) |
| Target | `api`, `write_repository` | Maintainer of the target group (Owner to create group runners or move projects) |

## Commands

```bash
glab-teleport                                        # interactive mode
glab-teleport group  <source-group> <target-group>   # a group with all subgroups
glab-teleport project <source-project>... <target>   # projects into a group, or to an exact path
glab-teleport verify <source> <target>               # read-only comparison and report
glab-teleport audit [source-group] [target-group]    # migration status of a whole instance
glab-teleport refs [target]                          # find leftovers pointing at the source GitLab
glab-teleport repoint                                # script that repoints teammates' local clones
glab-teleport report [latest|<run>]                  # list runs or reprint a summary
glab-teleport login | logout | doctor | config
```

Shortcut: `glab-teleport payments platform/payments` works out whether the source is a group or a project.

### What to transfer

| `--only` | Moves |
|---|---|
| `repo` | Branches and tags (incl. LFS), wiki, default branch, protected branches/tags |
| `env` | CI/CD variables in every environment scope (group and project level), environments |
| `runner` | Group and project runners recreated with the same tags, protection and timeout; registration tokens are saved for you |
| `settings` | Merge options, CI config path, timeouts, features, archived state |
| `extras` | Pipeline schedules (created paused), webhooks, deploy keys, members |

### Useful options

| Option | |
|---|---|
| `--dry-run` | Show the plan and stop |
| `--layout keep\|flat\|join\|auto` | Subgroup structure on the target (default `keep`, same as the source) |
| `--relocate` | Move projects already on the target into the chosen structure (GitLab transfer, history kept) |
| `--include PATH` / `--exclude PATH` | Limit a group teleport to some subgroups or projects |
| `--with-parent-vars` | Also bring variables inherited from parent groups of the source |
| `--rewrite-urls` | Rewrite source repository URLs found inside variable values |
| `--overwrite` | Overwrite target variables/rules whose values differ |
| `--force-push` | Overwrite target branches that diverged (destructive; off by default) |
| `-y`, `--jobs N`, `--lang th`, `--no-color` | Non-interactive, parallelism, language, plain output |

## How it works

1. **Plan:** both sides are read and every branch/tag SHA is compared. Each source project gets a state: `in sync`, `update`, `new`, `move` or `skip`. Projects queued for deletion are ignored.
2. **Transfer:**
   - groups are created first, then projects run in parallel
   - git data goes through a temporary bare repository on your machine, and the pushed SHAs are re-read from the target to confirm them
   - API objects are created only when missing, so a re-run is safe
3. **Verify:** everything that was transferred is compared again, read-only.
4. **Report:**
   - a terminal summary, plus `report.md` and `report.json` in `~/.glab-teleport/runs/`
   - runner registration tokens go to a `runner-tokens.txt` beside the report (mode 600)

When subgroups are flattened, their CI/CD variables are attached **only to the projects that used to live under them**. They are never spread across the whole group, so secrets stay with the team that owned them.

## Safety and privacy

- **Plan first:** nothing changes until you confirm the plan, and `--dry-run` never writes.
- **No surprise deployments:** branches are pushed with `ci.skip`, so copying history never starts pipelines on the target. Pipeline schedules are created paused.
- **Existing work is protected:**
  - unrelated code on the target is never overwritten, and diverged branches are reported instead of being force-pushed
  - differing variables are kept unless you pass `--overwrite`
- **Secrets stay out of output:** variable values are compared in memory and never printed or written to reports.
- **Credential storage:** credentials are stored per host in `~/.config/glab-teleport/credentials.json` with mode 600. Tokens are scrubbed from error messages.
- **CI use:** set `GLAB_TELEPORT_SOURCE_URL`, `GLAB_TELEPORT_TARGET_URL`, `GLAB_TELEPORT_SOURCE_TOKEN` and `GLAB_TELEPORT_TARGET_TOKEN`.

## What is not moved

The GitLab API does not allow reading these items' secrets or recreating them:

- issues and merge requests
- container registry images and packages
- CI job history
- deploy tokens and access tokens

Use GitLab's *project export/import* for issues and merge requests. Hidden (write-only) CI/CD variables are listed in the report so you can set them by hand.

## FAQ

**Can I run it again?**
Yes. Re-runs are incremental: in-sync projects stay as they are, missing refs are pushed, and only missing variables are created.

**The target already has some of my projects, in a different structure.**
They are found by commits and updated in place. Add `--layout keep --relocate` to move them into the source structure instead.

**Someone kept working on the target. Will my run overwrite it?**
No. Branches with newer commits on the target are reported as *not updated*. Only `--force-push` overwrites them.

**Does it work between two gitlab.com namespaces, or inside one instance?**
Yes, but inside one instance GitLab's own *Transfer* is faster for a plain move.

**Where are logs and reports?**
In `~/.glab-teleport/` (override with `GLAB_TELEPORT_HOME`). Run `glab-teleport report` to list past runs.

## Development

```bash
git clone https://github.com/Aekawan/glab-teleport && cd glab-teleport
python3 -m unittest discover -s tests      # no third-party packages needed
node bin/glab-teleport.js --help
```

The code is plain Python 3.9+ standard library: `lib/glab_teleport/` (CLI, planner, transfer, verification, reports) plus a small Node launcher for npm. User-facing text is written as `t("English", "ไทย")` next to where it is used.

## License

MIT — see [LICENSE](LICENSE).

