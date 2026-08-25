#!/bin/sh
# Install paddock. Run it as often as you like: it replaces what is there.
#
#   curl -LsSf https://raw.githubusercontent.com/paddock-sh/paddock/main/install.sh | sh
#
# Environment:
#   PADDOCK_REF=<branch|tag>  install that ref instead of the default branch
#   PADDOCK_YES=1             answer yes to the prompts, for unattended installs

set -eu

REPO="https://github.com/paddock-sh/paddock"
UV_INSTALLER="https://astral.sh/uv/install.sh"

say() {
    printf '%s\n' "$1"
}

warn() {
    printf 'warning: %s\n' "$1" >&2
}

die() {
    printf 'error: %s\n' "$1" >&2
    exit 1
}

have() {
    command -v "$1" > /dev/null 2>&1
}

# Yes without asking when PADDOCK_YES is set. No when there is nobody to ask,
# which is the case under `curl | sh` with no terminal attached.
confirm() {
    if [ "${PADDOCK_YES:-}" = "1" ]; then
        return 0
    fi
    if [ ! -r /dev/tty ]; then
        return 1
    fi
    printf '%s [y/N] ' "$1" > /dev/tty
    read -r reply < /dev/tty || return 1
    case "$reply" in
        y | Y | yes | YES) return 0 ;;
        *) return 1 ;;
    esac
}

ensure_uv() {
    if have uv; then
        return 0
    fi
    say "paddock installs with uv, and uv is not here."
    if ! confirm "Install uv from $UV_INSTALLER?"; then
        die "uv is needed. Install it from https://docs.astral.sh/uv/, or rerun with PADDOCK_YES=1 to let this script do it."
    fi
    curl -LsSf "$UV_INSTALLER" | sh
    # uv lands in ~/.local/bin, which this shell may not have looked at yet.
    PATH="$HOME/.local/bin:$PATH"
    export PATH
    if ! have uv; then
        die "uv is still not on PATH after installing it. Open a new shell and run this again."
    fi
}

install_paddock() {
    target="$REPO"
    if [ -n "${PADDOCK_REF:-}" ]; then
        target="$target@$PADDOCK_REF"
    fi
    say "Installing paddock from $target"
    uv tool install --force "git+$target"
}

check_path() {
    if have paddock; then
        return 0
    fi
    warn "paddock is installed, but it is not on your PATH."
    warn "Run 'uv tool update-shell' and open a new shell."
}

check_node() {
    if have node; then
        return 0
    fi
    warn "node was not found. The sandbox runtime is fetched with npx, so install Node.js before the first sandbox launch."
}

main() {
    ensure_uv
    install_paddock
    check_path
    check_node
    say ""
    say "Done. Two steps left:"
    say "  1. Run 'paddock init' to wire the chooser into herdr's config."
    say "  2. Press prefix+c inside herdr."
}

main "$@"
