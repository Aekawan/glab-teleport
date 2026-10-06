# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/) and the project uses [Semantic Versioning](https://semver.org/).

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
