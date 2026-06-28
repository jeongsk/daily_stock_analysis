#!/usr/bin/env bash
# Install a uv-exported, hash-pinned requirements file with pip.
#
# pip cannot consume such a file verbatim in two cases handled here:
#
#  1. VCS/git deps (e.g. `alphasift @ git+https://...`): pip's --require-hashes
#     mode (auto-enabled as soon as any --hash is present) has no way to hash
#     version-control repositories, so a plain install aborts with
#     "Can't verify hashes ... version control repositories". These are pulled
#     out and installed separately with --no-deps (their runtime deps are pinned
#     + hashed in the main install below).
#
#  2. Platform-divergent deps (via --exclude): a version locked on one platform
#     may have no compatible wheel on another. Example: longbridge is locked to
#     4.3.3, whose wheels are manylinux_2_39 only (needs glibc >= 2.39); the
#     Debian bookworm Docker image (glibc 2.36) cannot use them, while older
#     longbridge (0.2.75) has bookworm wheels but no macOS arm64 wheel. The
#     caller installs a platform-appropriate version itself and passes the
#     package name here so its hash-pinned block is dropped.
#
# The requirements file remains the single source of truth; this script never
# hardcodes a git URL or a version.
#
# Usage:
#   bash scripts/pip-install-requirements.sh [--exclude PKG ...] <requirements.txt>
set -euo pipefail

excludes=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --exclude)
            [ "$#" -ge 2 ] || { echo "pip-install-requirements: --exclude needs an argument" >&2; exit 2; }
            excludes+=("$2"); shift 2 ;;
        --exclude=*)
            excludes+=("${1#--exclude=}"); shift ;;
        --)
            shift; break ;;
        -*)
            echo "pip-install-requirements: unknown option: $1" >&2; exit 2 ;;
        *)
            break ;;
    esac
done

req_file="${1:?usage: $0 [--exclude PKG ...] <requirements.txt>}"
if [ ! -f "$req_file" ]; then
    echo "pip-install-requirements: requirements file not found: $req_file" >&2
    exit 1
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

git_reqs="$tmp_dir/git-requirements.txt"
hashed_reqs="$tmp_dir/hashed-requirements.txt"

git_pattern='^[A-Za-z0-9_.-]+\s*@\s*git\+'

# Build the hash-pinned file: drop VCS/git lines and any whole --exclude package
# block (the "name==ver \" line plus its indented --hash / "# via" continuations).
EXCLUDE_PKGS="${excludes[*]:-}" awk '
    BEGIN {
        n = split(ENVIRON["EXCLUDE_PKGS"], a, /[ \t]+/)
        for (i = 1; i <= n; i++) if (a[i] != "") excl[a[i]] = 1
        skip = 0
    }
    # VCS/git direct-URL spec: installed separately below.
    # (POSIX awk has no \s, so spell whitespace as [ \t].)
    /^[A-Za-z0-9_.-]+[ \t]*@[ \t]*git\+/ { skip = 1; next }
    # Package line starts at column 0 with a name char (not comment/blank/indent).
    /^[^ \t#]/ {
        name = $0
        sub(/[<>=!~ \t@].*/, "", name)
        skip = (name in excl) ? 1 : 0
        if (!skip) print
        next
    }
    # Continuation (--hash / "# via"), comment and blank lines inherit skip.
    { if (!skip) print }
' "$req_file" > "$hashed_reqs"

grep -E "$git_pattern" "$req_file" > "$git_reqs" || true

if [ -s "$git_reqs" ]; then
    echo "==> installing VCS/git dependencies (--no-deps):"
    cat "$git_reqs"
    python -m pip install --no-cache-dir --no-deps -r "$git_reqs"
fi

if [ "${#excludes[@]}" -gt 0 ]; then
    echo "==> excluded from hash-pinned install (installed separately by caller): ${excludes[*]}"
fi

echo "==> installing hash-pinned dependencies:"
python -m pip install --no-cache-dir -r "$hashed_reqs"
