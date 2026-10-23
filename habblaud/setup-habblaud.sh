#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

[[ $# -le 1 ]] || fail "Usage: bash ${BASH_SOURCE[0]} [target_dir]"
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
[[ -r "$script_dir/UPSTREAM_COMMIT" ]] || fail "Missing $script_dir/UPSTREAM_COMMIT"
commit=$(<"$script_dir/UPSTREAM_COMMIT")
[[ "$commit" =~ ^[[:xdigit:]]{40}$ ]] || fail "UPSTREAM_COMMIT must contain a full 40-character commit hash."

target_dir=${1:-"$HOME/habblaud"}
[[ "$target_dir" == /* ]] || target_dir="$PWD/$target_dir"
if [[ ! -e "$target_dir" ]]; then
    # Habblaud is third-party software, licensed under MIT.
    git clone -- https://github.com/marmottajr/habblaud "$target_dir"
fi
[[ -d "$target_dir" ]] || fail "Target is not a directory: $target_dir"
target_dir=$(cd -- "$target_dir" && pwd -P)
repo_root=$(git -C "$target_dir" rev-parse --show-toplevel 2>/dev/null) || fail "Target is not a Git repository: $target_dir"
[[ "$repo_root" == "$target_dir" ]] || fail "Target must be the repository root: $target_dir"
git -C "$target_dir" cat-file -e "$commit^{commit}" 2>/dev/null || fail "Pinned commit $commit is missing; fetch upstream history and retry."

shopt -s nullglob
patches=("$script_dir"/patches/*.patch)
if [[ -n "${patches[*]:-}" ]]; then
    scratch=$(mktemp -d "${TMPDIR:-/tmp}/habblaud-setup.XXXXXXXX")
    trap 'rm -rf -- "$scratch"' EXIT
    paths=()
    for patch in "${patches[@]}"; do
        [[ -f "$patch" && -r "$patch" ]] || fail "Cannot read patch: $patch"
        git -C "$target_dir" apply --numstat -z -- "$patch" > "$scratch/paths" || fail "Invalid patch: $patch"
        while IFS= read -r -d '' entry; do
            path=${entry#*$'\t'}
            path=${path#*$'\t'}
            if [[ -n "$path" ]]; then
                paths+=("$path")
            else
                # With -z, renames/copies have separate old and new path records.
                IFS= read -r -d '' old_path || fail "Invalid old path in patch: $patch"
                IFS= read -r -d '' new_path || fail "Invalid new path in patch: $patch"
                paths+=("$old_path" "$new_path")
            fi
        done < "$scratch/paths"
    done
    [[ -n "${paths[*]:-}" ]] || fail "Patches contain no file changes."

    # Do not overwrite staged edits, even if they happen to match a setup patch.
    git --literal-pathspecs -C "$target_dir" diff --cached --quiet HEAD -- "${paths[@]}" ||
        fail "Warning: staged changes in files touched by patches. Commit or stash those changes before retrying."

    # Model HEAD plus already-applied setup patches in an isolated index. Comparing
    # whole files also detects edits outside patch hunks; reverse --check alone cannot.
    # The real index and working files are never changed by this preflight.
    GIT_INDEX_FILE="$scratch/index" git -C "$target_dir" read-tree HEAD
    for patch in "${patches[@]}"; do
        if git -C "$target_dir" apply --check --reverse -- "$patch" >/dev/null 2>&1; then
            if ! GIT_INDEX_FILE="$scratch/index" git -C "$target_dir" apply --cached --check --reverse -- "$patch" >/dev/null 2>&1; then
                GIT_INDEX_FILE="$scratch/index" git -C "$target_dir" apply --cached -- "$patch" ||
                    fail "Warning: cannot distinguish local changes from patch $patch; commit or stash changes before retrying."
            fi
        fi
    done
    GIT_INDEX_FILE="$scratch/index" git --literal-pathspecs -C "$target_dir" diff --quiet -- "${paths[@]}" ||
        fail "Warning: uncommitted changes in files touched by patches (beyond setup patches). Commit or stash those changes before retrying."
    untracked=$(GIT_INDEX_FILE="$scratch/index" git --literal-pathspecs -C "$target_dir" ls-files --others -- "${paths[@]}")
    [[ -z "$untracked" ]] || fail "Warning: untracked files would be touched by patches. Move them aside before retrying: $untracked"
fi

git -C "$target_dir" checkout --detach "$commit" || fail "Cannot checkout pinned commit $commit; resolve local changes and retry."
if [[ -n "${patches[*]:-}" ]]; then
    for patch in "${patches[@]}"; do
        if git -C "$target_dir" apply --check --reverse -- "$patch" >/dev/null 2>&1; then
            printf 'Already applied: %s\n' "${patch##*/}"
        elif git -C "$target_dir" apply --check -- "$patch"; then
            git -C "$target_dir" apply -- "$patch"
            printf 'Applied: %s\n' "${patch##*/}"
        else
            fail "Patch ${patch##*/} neither applies cleanly nor is already applied. Resolve the conflict before retrying."
        fi
    done
fi

printf '\nHabblaud is ready at commit %s. Run manually:\n' "$commit"
printf '  cd %q\n  npm install\n  npm run docker:up\n' "$target_dir"
printf '\nThe upstream script generates docker-compose.override.yml with this machine\047s paths and email. Do NOT commit that file.\n'
