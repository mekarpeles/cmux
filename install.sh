#!/usr/bin/env bash
set -e

# cmux installer — installs tmux (if needed) and cmux via pipx
# Usage: curl -fsSL https://raw.githubusercontent.com/.../install.sh | bash

REPO="git+https://github.com/mekarpeles/cmux.git"

# ── detect OS ─────────────────────────────────────────────────────────────────
OS="$(uname -s)"

# ── tmux ──────────────────────────────────────────────────────────────────────
if ! command -v tmux &>/dev/null; then
    echo "cmux: installing tmux..."
    case "$OS" in
        Darwin)
            if ! command -v brew &>/dev/null; then
                echo "cmux: Homebrew is required on macOS. Install it from https://brew.sh first." >&2
                exit 1
            fi
            brew install tmux
            ;;
        Linux)
            if command -v apt-get &>/dev/null; then
                sudo apt-get update -qq && sudo apt-get install -y tmux
            elif command -v dnf &>/dev/null; then
                sudo dnf install -y tmux
            elif command -v yum &>/dev/null; then
                sudo yum install -y tmux
            elif command -v pacman &>/dev/null; then
                sudo pacman -S --noconfirm tmux
            else
                echo "cmux: unsupported Linux distro — install tmux manually then re-run." >&2
                exit 1
            fi
            ;;
        *)
            echo "cmux: unsupported OS '$OS'" >&2
            exit 1
            ;;
    esac
else
    echo "cmux: tmux already installed ($(tmux -V))"
fi

# ── pipx ──────────────────────────────────────────────────────────────────────
if ! command -v pipx &>/dev/null; then
    echo "cmux: installing pipx..."
    case "$OS" in
        Darwin)
            brew install pipx
            pipx ensurepath
            ;;
        Linux)
            if command -v apt-get &>/dev/null; then
                sudo apt-get install -y pipx
            elif command -v dnf &>/dev/null; then
                sudo dnf install -y pipx
            elif command -v pip3 &>/dev/null; then
                pip3 install --user pipx
            else
                echo "cmux: could not install pipx — install it manually then re-run." >&2
                exit 1
            fi
            pipx ensurepath
            ;;
    esac
else
    echo "cmux: pipx already installed"
fi

# ── cmux ──────────────────────────────────────────────────────────────────────
echo "cmux: installing cmux..."
CMUX_TMP="$(mktemp -d)"
git clone --depth=1 "https://github.com/mekarpeles/cmux.git" "$CMUX_TMP/cmux"
pipx install "$CMUX_TMP/cmux"
rm -rf "$CMUX_TMP"

# ── cq ────────────────────────────────────────────────────────────────────────
# Per-agent issue tracker. cmux agents rely on `cq issue list`/`cq issue create`
# for task tracking (see README's "Task tracking" section) — without it, every
# agent home dir is missing its queue and `cq` commands fail with "not found".
echo "cmux: installing cq..."
CQ_TMP="$(mktemp -d)"
git clone --depth=1 "https://github.com/mekarpeles/cq.git" "$CQ_TMP/cq"
pipx install "$CQ_TMP/cq"
rm -rf "$CQ_TMP"

echo ""
echo "cmux installed. You may need to restart your shell or run:"
echo "  source ~/.bashrc  (Linux)"
echo "  source ~/.zshrc   (macOS)"
echo ""
echo "Then try: cmux alice"
