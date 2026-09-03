#!/usr/bin/env bash
# setup_git.sh — восстановить Git/GitHub для проекта Kerio Migrator.
#
# Скрипт восстанавливает:
#   * локальный Git-репозиторий и ветку main;
#   * GitHub CLI и его авторизацию, если доступен токен;
#   * git-идентичность автора;
#   * remote origin проекта Kerio Migrator;
#   * подключение credential helper через gh.
#
# Запуск:
#   bash setup_git.sh
#   или:
#   chmod +x setup_git.sh && ./setup_git.sh
#
# Скрипт не выполняет push и не перезаписывает файлы из origin/main.

set -u

REPO_URL="https://github.com/squids911/Kerio-migrator.git"
REPO_USER="squids911"
REPO_EMAIL="squids911@users.noreply.github.com"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"

log()  { printf "==> %s\n" "$*"; }
info() { printf "    %s\n" "$*"; }
err()  { printf "ERROR: %s\n" "$*" >&2; }

cd "$PROJECT_DIR" || { err "Cannot cd to $PROJECT_DIR"; exit 1; }

# 0) Git repository
log "Check local Git repository..."
if [ ! -d ".git" ]; then
    info "Git repository not found. Initializing it..."
    git init -b main 2>/dev/null || {
        git init || { err "Failed to initialize Git repository."; exit 1; }
        git checkout -B main 2>/dev/null || true
    }
fi
info "Project directory: $PROJECT_DIR"
info "Git root: $(git rev-parse --show-toplevel 2>/dev/null || printf '%s' "$PROJECT_DIR")"

# 1) GitHub CLI (gh)
log "Check gh CLI..."
if ! command -v gh >/dev/null 2>&1; then
    info "gh not found. Installing via apt..."
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update -qq
        sudo apt-get install -y -qq gh || {
            err "Failed to install gh (please install it manually)."
        }
    else
        err "gh missing and apt-get unavailable. Install GitHub CLI manually."
    fi
fi
if command -v gh >/dev/null 2>&1; then
    info "gh version: $(gh --version 2>/dev/null | head -1)"
else
    info "gh is unavailable; continuing with regular git configuration."
fi

# 2) GitHub CLI auth
if command -v gh >/dev/null 2>&1; then
    log "Check gh authentication..."
    if ! gh auth status >/dev/null 2>&1; then
        info "Not authenticated via gh."
        if [ -n "${GH_TOKEN:-}" ]; then
            info "GH_TOKEN found in environment, using it."
            printf '%s' "$GH_TOKEN" | gh auth login \
                --hostname github.com \
                --git-protocol https \
                --with-token || true
        else
            info "No GH_TOKEN environment variable."
            info "Run one of the following and then rerun this script:"
            info "  gh auth login"
            info "  printf '%s' \"\$GH_TOKEN\" | gh auth login --with-token"
        fi
    fi

    if gh auth status >/dev/null 2>&1; then
        gh auth status 2>&1 | sed 's/^/    /'
        info "Authenticated as: $(gh api user --jq .login 2>/dev/null || true)"
        log "Setup git credential helper (gh)..."
        gh auth setup-git || true
    else
        err "gh authentication is not available. git push may request credentials."
    fi
fi

# 3) Git identity and remote origin
log "Set Git identity..."
git config user.name  "$REPO_USER"
git config user.email "$REPO_EMAIL"

log "Set remote 'origin'..."
if git remote get-url origin >/dev/null 2>&1; then
    git remote set-url origin "$REPO_URL"
else
    git remote add origin "$REPO_URL"
fi
info "origin -> $(git remote get-url origin)"

# 4) Ensure main branch and fetch remote metadata.
# No merge/reset/pull is performed: local project files are not overwritten.
log "Ensure branch 'main' and fetch..."
git checkout -B main 2>/dev/null || true
git fetch origin main 2>&1 | sed 's/^/    /' || true

# 5) Show status
log "Status:"
git status -sb | sed 's/^/    /' || true
origin_head="$(git log --oneline -1 origin/main 2>/dev/null || true)"
[ -n "$origin_head" ] && info "origin/main: $origin_head"

log "Git/GitHub setup for Kerio Migrator complete."
