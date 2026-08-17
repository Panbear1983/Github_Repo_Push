#!/bin/bash
# Secret scanner for Github_Repo_Push pushes (adapted from
# Financial_Reporting_Bot/scripts/git-hooks/_scan.sh). Dependency-free grep.
#
# Usage:  secret_scan.sh <diff-source-command...>
#   the args are a command that prints the content to scan on stdout, e.g.
#     secret_scan.sh git diff origin/main..HEAD
#   STAGED_FILES (env, newline-separated) enables the blocked-filename guard.
# Exits non-zero (blocks the push) if anything looks like a secret.

set -uo pipefail
RED=$'\033[1;31m'; NC=$'\033[0m'

# ---- high-signal secret value patterns (low false-positive) --------------
PATTERNS=(
  'sk-or-v1-[A-Za-z0-9]{20,}'                       # OpenRouter API key
  '[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}'                # Telegram bot token
  'sk-[A-Za-z0-9]{32,}'                             # OpenAI-style key
  'AKIA[0-9A-Z]{16}'                                # AWS access key id
  'ghp_[A-Za-z0-9]{36}'                             # GitHub PAT
  'gho_[A-Za-z0-9]{36}'                             # GitHub OAuth token
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'              # private key blob
  '(OPENROUTER[A-Z_]*KEY|BRAVE_API_KEY|TELEGRAM_BOT_TOKEN|[A-Z_]*_SECRET|[A-Z_]*API_KEY|[A-Z_]*_TOKEN)[[:space:]]*[:=][[:space:]]*['"'"'"]?[A-Za-z0-9_.\-]{16,}'
)
# placeholder values that are safe (templates/examples)
PLACEHOLDER='your_key_here|your_token_here|your_id_here|changeme|xxxx|<[^>]+>|example|placeholder|REDACTED'

# ---- filenames that must never be pushed ---------------------------------
BLOCKED_FILES='(^|/)\.env$|(^|/)\.env\.[^t]|(^|/)[^/]*\.pem$|(^|/)[^/]*\.key$|(^|/)id_rsa|(^|/)credentials(/|$)|(^|/)auth\.json$|(^|/)session_state\.json$'

fail=0

# 1) filename guard
if [ -n "${STAGED_FILES:-}" ]; then
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    if printf '%s\n' "$f" | grep -qE "$BLOCKED_FILES"; then
      echo "${RED}BLOCKED file (never push secrets/personal data): $f${NC}"
      fail=1
    fi
  done <<< "$STAGED_FILES"
fi

# 2) content guard — scan the diff produced by the passed command
content="$("$@" 2>/dev/null)"
for pat in "${PATTERNS[@]}"; do
  hits="$(printf '%s\n' "$content" | grep -aEn "^\+.*$pat" 2>/dev/null | grep -avE "$PLACEHOLDER")"
  if [ -n "$hits" ]; then
    echo "${RED}BLOCKED: possible secret matching /$pat/:${NC}"
    printf '%s\n' "$hits" | head -5
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "${RED}--> Push aborted by secret-scan guardrail.${NC}"
  exit 1
fi
exit 0
