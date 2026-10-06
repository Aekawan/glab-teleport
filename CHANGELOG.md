# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/) and the project uses [Semantic Versioning](https://semver.org/).

## [0.4.0] — 2026-10-06

### Added
- `--json` on every command: one JSON document on stdout, progress on stderr. Write commands only plan unless `--yes` is given.
- Agent skill bundled with the package: `glab-teleport skill install` (safe agent workflow, JSON field reference) for
  Claude Code, Codex, OpenCode and pi. It installs for the agents found on the machine, or pick with
  `--for claude,codex,opencode,pi|all`; `--dir` for any other Agent Skills folder.
- Read-only mode: `glab-teleport config read_only true` or `GLAB_TELEPORT_READ_ONLY=1` turns every write into a plan.

### Fixed
- `verify` counts source projects that are not on the target yet: the Projects row shows e.g. `11 → 6 ✗` instead of
  `6/6 ✓`, and `--json` reports `"complete": false` with `not_on_target`.
- Plans describe a match as "N shared commits" (several branches can share one commit) instead of "N matching refs".

## [0.3.0] — 2026-10-06

### Added
- `sync` works for any group or project, not only earlier teleports: pick “Another group/project…” in the menu, or run
  `glab-teleport sync <source> [target]`. The target is suggested from history, names and existing projects.
- Sync plans tell refs that are merely behind (updated) from refs changed on the target (kept, never overwritten).

### Changed
- The picker gives labels priority over descriptions in narrow terminals.
- The sync list shows the real target project path.

## [0.2.0] — 2026-10-06

### Added
- `sync [source] [target]`: bring an earlier teleport up to date, for a project or a whole group. The source wins; the target,
  layout, components and options are remembered from earlier runs. `--prune` removes branches, tags and variables deleted on
  the source; `--no-overwrite` only adds what is missing; `--dry-run` shows what would change.
- Repositories whose branches and tags already match are skipped without downloading anything.
- Sync entry in interactive mode; sync runs list only the projects that changed.

## [0.1.0] — 2026-10-06

### Added
- `group` and `project` commands to teleport groups (any subgroup depth) and projects between GitLab instances.
- Components selectable with `--only`: `repo`, `env`, `runner`, `settings`, `extras`.
- Commit-verified matching of projects that already exist on the target; layouts `auto`, `keep`, `flat`, `join`; `--relocate`.
- Verification after every run and a standalone `verify` command; variables compared per key and environment scope.
- Reports in the terminal, `report.md` and `report.json`.
- Interactive mode with a fuzzy picker; English and Thai interface.
- `login` (token page, OAuth with PKCE, or paste), `logout`, `doctor`, `config`.
- `audit`, `refs` and `repoint` for instance-wide migrations.
