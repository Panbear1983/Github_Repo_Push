# Github_Repo_Push

Unified repository dashboard and push manager for the Panbear1983 GitHub account.

## Features

- **Repo Registry**: Config-driven management of all local/remote repos
- **Push Manager**: Systematic push with review, dry-run, and audit logging
- **Profile README**: Auto-maintained `Panbear1983/Panbear1983` profile with AI-assisted descriptions
- **Dashboard**: Textual TUI showing sync status, push history, sizes, dates for all repos
- **Cron Scheduling**: Per-repo toggleable scheduled pushes (future)

## Quick Start

```bash
# Install
pip install -e .

# Initialize registry (scans local + remote)
ghrp registry init

# List all registered repos
ghrp registry list

# Check sync status
ghrp status-all

# Push a repo (with dry-run first)
ghrp push Alpaca_Paper_Trader --dry-run
ghrp push Alpaca_Paper_Trader

# Update profile README
ghrp profile update --push

# Launch dashboard
ghrp dashboard
```

## Architecture

```
config/
  repos.yaml          # Repo registry: local_path, remote, push_rules, profile config
  profile_readme.yaml # Profile README sections, featured repos, AI description prompts
  push_rules.yaml     # Global push policies, ignore patterns, branch protection
src/github_repo_push/
  cli.py              # Main CLI (click)
  models.py           # Pydantic models
  registry.py         # Registry load/sync
  syncer.py           # Push logic
  profile_readme.py   # Profile README generation
  dashboard.py        # Dashboard data aggregation
  git_ops.py          # Git operations
  github_api.py       # GitHub API wrapper
dashboard/
  app.py              # Textual TUI
  views.py            # Table, detail, history views
data/
  push_history.jsonl  # Append-only audit log
  dashboard_cache.json
scripts/
  init_registry.py    # One-time registry creation
```

## Configuration

### `config/repos.yaml`
```yaml
repos:
  - name: Alpaca_Paper_Trader
    local_path: "~/Desktop/Old_Projects/GitHub/Alpaca_Paper_Trader"
    remote: "Panbear1983/Alpaca_Paper_Trader"
    default_branch: "main"
    push_branch: "main"
    protected_branches: ["main"]
    require_pr: false
    ignore_patterns:
      - "*.log"
      - "__pycache__/"
      - ".env"
      - "data/*.db"
    profile_section: "Applied Automation"
    profile_featured: true
    profile_description_prompt: "Supervised paper-trading automation with disclosure research, Textual TUI controls, state management, and scheduled reporting."
    tags: [python, trading, alpaca, paper-trading]
    cron_schedule: null  # e.g., "0 2 * * *" for daily 2am
    cron_enabled: false
```

### `config/profile_readme.yaml`
```yaml
profile_repo: "Panbear1983/Panbear1983"
sections:
  - name: "Featured Security Work"
    description: "SOC automation research and threat hunting case studies"
    repos: ["Multi-Funtion_SOC_Agent_Research"]
  - name: "Applied Automation"
    description: "Production automation pipelines for finance, real estate, and reporting"
    repos: ["Alpaca_Paper_Trader", "Financial_Reporting_Bot", "Kash_Realestate_Property_Database", "Github_Push_Automator"]
  - name: "Earlier Data and ML Work"
    description: "Computer vision, recommendation, forecasting, and analysis projects"
    repos: ["Machine_Learning_Projects", "ML_PROJECT_Tracking_Barbell_Exercises"]
```

## Commands

| Command | Description |
|---------|-------------|
| `ghrp registry init` | Scan local + remote, create config |
| `ghrp registry list` | List registered repos |
| `ghrp registry validate` | Check paths & remotes |
| `ghrp push <repo> [--dry-run] [--message "msg"]` | Push single repo |
| `ghrp push-all [--only-changed] [--parallel N]` | Push all/some repos |
| `ghrp status <repo>` | Show local vs remote diff |
| `ghrp status-all` | Table of all repos sync status |
| `ghrp profile preview` | Render README to stdout |
| `ghrp profile update [--push]` | Regenerate & push profile README |
| `ghrp dashboard` | Launch Textual TUI |
| `ghrp dashboard export --format json|csv|html` | Export dashboard data |
| `ghrp doctor` | Health check |