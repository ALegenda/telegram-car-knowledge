#!/usr/bin/env bash
# Usage: scripts/deploy-bot.sh root@your-server [port] [ignored-env-file]
set -euo pipefail
target=${1:?SSH user@host required}
port=${2:-22}
env_file=${3:-.env.bot}
[[ "$target" =~ ^[a-zA-Z0-9_-]+@[a-zA-Z0-9.-]+$ ]] || exit 2
[[ "$port" =~ ^[0-9]+$ ]] || exit 2
repo=$(git rev-parse --show-toplevel)
cd "$repo"
[[ -f "$env_file" ]] || { echo 'Bot env file is missing.' >&2; exit 2; }
if [[ -n $(git status --porcelain --untracked-files=normal) ]]; then
    echo 'Commit the bot changes before deploying a reproducible release.' >&2
    exit 2
fi
release_id=$(git rev-parse HEAD)
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
git archive --format=tar.gz HEAD -o "$scratch/release.tar.gz"
ssh -p "$port" -o ConnectTimeout=15 "$target" 'umask 077; mkdir -p /opt/tg-knowledge-bot/uploads'
scp -P "$port" "$scratch/release.tar.gz" "$target:/opt/tg-knowledge-bot/uploads/$release_id.tar.gz"
scp -P "$port" "$env_file" "$target:/opt/tg-knowledge-bot/uploads/$release_id.env"
ssh -p "$port" "$target" bash -s -- "$release_id" < scripts/install-bot.sh
