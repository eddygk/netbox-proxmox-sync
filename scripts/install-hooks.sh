#!/usr/bin/env bash
# Install the versioned git hooks for this repo (run once after cloning).
set -euo pipefail
repo_root="$(git rev-parse --show-toplevel)"
cp "$repo_root/scripts/pre-commit" "$repo_root/.git/hooks/pre-commit"
chmod +x "$repo_root/.git/hooks/pre-commit"
echo "Installed pre-commit secret guard -> .git/hooks/pre-commit"
