#!/usr/bin/env bash
# Remove this checkout's links, not other installations or 9router combos.
set +xv
set -euo pipefail
umask 077

usage() { printf '%s\n' 'Usage: bash uninstall.sh [--dry-run] [--yes] [--purge]'; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
dry_run=0
yes=0
purge=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) dry_run=1; shift ;;
    --yes) yes=1; shift ;;
    --purge) purge=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "Unknown option: $1" ;;
  esac
done
[[ ${HOME:-} == /* && $HOME != / ]] || die 'HOME must be an absolute home directory.'
command -v python3 >/dev/null 2>&1 || die 'Missing required command: python3'
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ $dry_run -eq 0 && $yes -eq 0 ]]; then
  [[ -t 0 ]] || die 'Non-interactive uninstall needs --yes.'
  if [[ $purge -eq 1 ]]; then
    printf '%s\n' 'Purge permanently deletes 9router.key, postiz.env and .venv. Backups are kept.'
  fi
  printf 'Uninstall company 9r? [y/N] '
  IFS= read -r answer || die 'No confirmation received.'
  case "$answer" in y|Y|yes|YES) ;; *) printf '%s\n' 'Cancelled.'; exit 0 ;; esac
fi

python3 -I - "$repo_dir" "$HOME" "$dry_run" "$purge" <<'PY'
import json
import os
import shutil
import stat
import sys
import tempfile
from datetime import datetime
from pathlib import Path

repo, home = map(Path, sys.argv[1:3])
dry_run, purge = (arg == "1" for arg in sys.argv[3:5])
company = home / ".claude/company"
claude_md = home / ".claude/CLAUDE.md"
begin, end = b"<!-- company-9r:begin -->", b"<!-- company-9r:end -->"


def exists(path):
    return path.exists() or path.is_symlink()


def remove(path):
    print(f"{'Would remove' if dry_run else 'Remove'} {path}")
    if not dry_run:
        path.unlink()


def remove_block(data):
    if not data.count(begin) and not data.count(end):
        return data
    if data.count(begin) != 1 or data.count(end) != 1:
        raise ValueError("CLAUDE.md has duplicate or incomplete company-9r markers.")
    start, stop = data.index(begin), data.index(end)
    if stop < start:
        raise ValueError("CLAUDE.md has reversed company-9r markers.")
    return data[:start] + data[stop + len(end):]


try:
    if company.is_symlink():
        raise ValueError("~/.claude/company is a symlink; refusing to modify its contents.")
    if exists(claude_md) and not claude_md.is_file():
        raise ValueError("~/.claude/CLAUDE.md is not a readable file.")
    old = claude_md.read_bytes() if claude_md.exists() else b""
    updated = remove_block(old)  # Validate markers before making any changes.
    links = [(repo / "company" / name, company / name) for name in
             ("workers.json", "contract.md", "tools.py", "mcp_server.py", "schemas")]
    links += [(repo / "skills" / name, home / ".claude/skills" / name)
              for name in ("engineering", "marketing")]
    links.append((repo / "bin/9r", home / ".local/bin/9r"))
    links.append((repo / "bin/9r-config", home / ".local/bin/9r-config"))
    for source, destination in links:
        if destination.is_symlink() and destination.resolve() == source.resolve():
            remove(destination)

    if updated != old:
        backup = claude_md.with_name(claude_md.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        while exists(backup):
            backup = backup.with_name(backup.name + "-1")
        print(f"{'Would back up' if dry_run else 'Back up'} {claude_md} -> {backup}")
        print(f"{'Would remove' if dry_run else 'Remove'} company-9r block from {claude_md}")
        if not dry_run:
            permissions = stat.S_IMODE(claude_md.stat().st_mode)
            fd, temp_name = tempfile.mkstemp(prefix=".CLAUDE.md-", dir=claude_md.parent)
            temp = Path(temp_name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    os.fchmod(stream.fileno(), permissions)
                    stream.write(updated)
                shutil.copy2(claude_md, backup)
                os.replace(temp, claude_md)
            finally:
                if temp.exists():
                    temp.unlink()

    # Remove only generated configurations that have not been customized.
    mcp = company / "mcp.json"
    expected_mcp = {"mcpServers": {"company-tools": {
        "command": str(company / ".venv/bin/python"), "args": [str(company / "mcp_server.py")]}}}
    if mcp.is_file() and not mcp.is_symlink():
        try:
            if json.loads(mcp.read_bytes()) == expected_mcp:
                remove(mcp)
        except (ValueError, UnicodeError):
            pass
    for name, description in (("eng", "Company engineering workers"), ("mkt", "Company marketing workers")):
        plugin = company / "plugins" / name
        metadata, skills = plugin / ".claude-plugin", plugin / "skills"
        manifest = metadata / "plugin.json"
        if any(path.is_symlink() for path in (company / "plugins", plugin, metadata, skills, manifest)):
            continue
        if not manifest.is_file() or not skills.is_dir() or any(skills.iterdir()):
            continue
        try:
            if json.loads(manifest.read_bytes()) != {"name": name, "version": "1.0.0", "description": description}:
                continue
        except (ValueError, UnicodeError):
            continue
        if set(plugin.iterdir()) != {metadata, skills} or set(metadata.iterdir()) != {manifest}:
            continue
        remove(manifest)
        if not dry_run:
            skills.rmdir()
            metadata.rmdir()
            plugin.rmdir()

    if purge:
        for name in ("9router.key", "postiz.env", ".venv"):
            path = company / name
            if not exists(path):
                continue
            if path.is_symlink() or path.is_file():
                remove(path)
            elif name == ".venv" and path.is_dir():
                print(f"{'Would remove' if dry_run else 'Remove'} {path}")
                if not dry_run:
                    shutil.rmtree(path)
            else:
                raise ValueError(f"Refusing to purge unexpected directory: {path}")
except (OSError, ValueError) as exc:
    raise SystemExit(f"Error: {exc}") from None
PY

if [[ $dry_run -eq 1 ]]; then
  printf '%s\n' 'Would unregister company-tools from Claude user scope if present.'
elif command -v claude >/dev/null 2>&1; then
  if mcp_list=$(NO_COLOR=1 claude mcp list 2>/dev/null); then
    if [[ $mcp_list =~ (^|$'\n')[[:space:]]*company-tools: ]] && grep -qF "$HOME/.claude/company/mcp_server.py" <<<"$mcp_list"; then
      if ! claude mcp remove company-tools -s user; then
        printf '%s\n' 'Warning: could not remove the user MCP entry; check claude mcp list.' >&2
      fi
    fi
  else
    printf '%s\n' 'Warning: cannot list MCP servers; run: claude mcp remove company-tools -s user' >&2
  fi
else
  printf '%s\n' 'Warning: claude not found; remove company-tools from Claude user MCP settings manually.' >&2
fi
if [[ $purge -eq 0 ]]; then
  printf '%s\n' 'Kept 9router.key, postiz.env and .venv. Use --purge to remove them.'
fi
printf '%s\n' 'Kept backups, custom plugins, unrelated files and all 9router combos.'
if [[ $dry_run -eq 1 ]]; then
  printf '%s\n' 'Dry run complete. No changes made.'
else
  printf '%s\n' 'Uninstall complete.'
fi
