# Github_Repo_Push

Unified repository dashboard and push manager for the Panbear1983 GitHub account.
Consolidates and supersedes [Github_Push_Automator](https://github.com/Panbear1983/Github_Push_Automator) (archived 2026-08-17).

## Features

- **Repo Registry**: Config-driven management of all local/remote repos (`config/repos.yaml`)
- **Push Manager**: Systematic push with ignore-pattern staging, secret-scan guardrail, dry-run, and audit logging
- **README Generation**: Creates a README.md for repos that lack one before publishing
- **Profile README**: Maintains `Panbear1983/Panbear1983` — full regeneration from config (byte-identical to the live README) plus a surgical per-entry editor
- **Ad-hoc Push**: `ghrp adhoc` publishes whatever repo you're standing in, registry or not
- **Dashboard**: Textual TUI control panel — sync status, dirty flag, GitHub push dates, plus push/add actions driving the same guarded machinery as the CLI

## Quick Start

```bash
# From the repo root (or install with: pip install -e .)
export PYTHONPATH=src

# List registered repos / check sync status
python3 -m github_repo_push.cli registry-list
python3 -m github_repo_push.cli status-all

# Push a repo (dry-run first; dry-run is strictly read-only)
python3 -m github_repo_push.cli push Alpaca_Paper_Trader --dry-run
python3 -m github_repo_push.cli push Alpaca_Paper_Trader

# Push everything (dirty repos are skipped unless --commit)
python3 -m github_repo_push.cli push-all --dry-run
python3 -m github_repo_push.cli push-all --commit

# Ad-hoc: publish the repo you're standing in
python3 -m github_repo_push.cli adhoc . --description "Short description." --register

# Profile README
python3 -m github_repo_push.cli profile-preview
python3 -m github_repo_push.cli profile-update --push

# Dashboard
./dashboard.sh
```

## Commands

| Command | Description |
|---------|-------------|
| `registry-init` | Scan local + remote, create/refresh `config/repos.yaml` |
| `registry-list` | List registered repos |
| `status-all` | Sync status for every enabled repo |
| `push <repo> [--dry-run] [--force] [-m msg] [--update-profile]` | Push one repo |
| `push-all [--dry-run] [--only-changed] [--commit]` | Push the fleet (dirty repos skipped without `--commit`) |
| `adhoc [path] [--description d] [--visibility v] [--skip-profile] [--register]` | Registry-less push of an arbitrary repo |
| `ensure-readme [path]` | Generate README.md if missing |
| `profile-preview` | Render the profile README to stdout |
| `profile-update [--push]` | Regenerate (and optionally push) the profile README |
| `dashboard` | Launch the Textual TUI control panel |

### Dashboard

Rows stream in as repo states arrive (parallel fetch, progress in the header).
Columns: Repo, Status, Dirty (uncommitted worktree), Local/Remote Branch,
Size, **GitHub Push** (the account's real `pushedAt`) and **ghrp Push** (this
tool's audit log). Key bindings:

| Key | Action |
|-----|--------|
| `p` | Push the selected repo — confirm modal shows what will be committed/skipped, with a Dry-run option |
| `a` | Add a repo to GitHub without the browser: pick an unregistered local folder (or type a path), choose visibility, and it registers, `gh repo create`s, and pushes |
| `r` | Refresh all rows |
| `q` | Quit |

All dashboard actions run the same `Syncer` path as the CLI, so ignore
patterns, the secret scan, and branch protections always apply; outcomes are
appended to the audit log and shown in the log pane.

## Configuration

Canonical config lives in this repo's `config/` directory:

- `repos.yaml` — the registry: local path, remote, branches, per-repo ignore patterns, profile metadata, `enabled` flag
- `profile_readme.yaml` — profile README sections/entries (titles, sub-path links, heading levels, preambles); mirrors the live README
- `push_rules.yaml` — global defaults: owner, commit message template, global ignore patterns, protected branches

Runtime data (append-only `push_history.jsonl` audit log) lives in
`~/.hermes/profiles/orchestrator/github_repo_push/data/`.

Overrides: `GHRP_CONFIG_DIR`, `GHRP_DATA_DIR`.

## Safety model

- **Dry-run never mutates**: no staging, no commits, no remote creation.
- **Ignore patterns are enforced**: staging is file-by-file; `.env`, DBs, caches, logs never enter a commit made by this tool.
- **Secret scan**: pushes of public repos run `scripts/secret_scan.sh` over the outgoing diff and are blocked on a hit.
- **No blind fleet commits**: `push-all` skips dirty worktrees unless `--commit` is explicit.
- **Diverged/behind refusal**: pushes are refused when local is behind or diverged unless `--force` (uses `--force-with-lease`).
- **Profile fidelity**: the generator's output is locked byte-identical to the live profile README by a golden test.

## Layout

```
config/                      # canonical config (version-controlled)
scripts/secret_scan.sh       # push guardrail (grep-based, dependency-free)
src/github_repo_push/
  cli.py                     # click CLI (flat commands listed above)
  models.py                  # pydantic models
  registry.py                # registry + profile/push-rules config load/save
  syncer.py                  # push workflow + audit history
  git_ops.py                 # git wrapper
  github_api.py              # gh CLI wrapper
  ignore.py                  # ignore-pattern staging filter
  readme_gen.py              # target-repo README generation (ported)
  profile_markdown.py        # surgical profile-entry editor (ported)
  profile_readme.py          # full profile README generation
  dashboard.py               # dashboard data aggregation
tests/                       # unittest suite incl. profile golden test
dashboard.sh                 # TUI launcher (PYTHONPATH wrapper)
```
