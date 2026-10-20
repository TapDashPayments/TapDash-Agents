#!/bin/sh
# Back up this repo's main branch to GitHub TapDashPayments/TapDash-Agents, under durable-park/ (history
# kept). The rewrite is deterministic, so every later backup is a plain fast-forward push. Never forces:
# if GitHub's main has other commits, the push is refused and nothing is overwritten.
#   scripts/backup_to_github.sh            # push
#   scripts/backup_to_github.sh --dry-run  # build the export and show it, push nothing
set -eu
REPO=$(cd "$(dirname "$0")/.." && pwd)
FILTER=${DP_FILTER_REPO:-$HOME/.local/share/dp-backup-venv/bin/git-filter-repo}
KEY=${DP_DEPLOY_KEY:-$HOME/.ssh/tapdash_agents_deploy}
HOSTS=${DP_KNOWN_HOSTS:-$HOME/.ssh/known_hosts_github}
REMOTE=git@github.com:TapDashPayments/tapdash-agents.git
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
git clone -q --no-local --branch main "$REPO" "$WORK/export"
cd "$WORK/export"
"$FILTER" --quiet --force --to-subdirectory-filter durable-park
if [ "${1:-}" = "--dry-run" ]; then
    git log --oneline -3
    echo "files: $(git ls-files | wc -l), all under: $(git ls-files | cut -d/ -f1 | sort -u | tr '\n' ' ')"
    exit 0
fi
GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o UserKnownHostsFile=$HOSTS -o StrictHostKeyChecking=yes" \
    git push "$REMOTE" main:main
