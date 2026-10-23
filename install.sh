#!/usr/bin/env bash
# Install live links to this checkout; keep credentials and runtime data local.
set +xv # Never trace credentials, even when invoked with bash -x.
set -euo pipefail
umask 077

usage() {
  printf '%s\n' 'Usage: bash install.sh [--dry-run] [--yes] [--no-combos] [--with-habblaud DIR]'
}
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

dry_run=0
yes=0
no_combos=0
habblaud_dir=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) dry_run=1; shift ;;
    --yes) yes=1; shift ;;
    --no-combos) no_combos=1; shift ;;
    --with-habblaud)
      [[ $# -ge 2 && -n $2 && $2 != --* ]] || die '--with-habblaud needs a directory.'
      habblaud_dir=$2
      shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "Unknown option: $1" ;;
  esac
done
[[ ${HOME:-} == /* && $HOME != / ]] || die 'HOME must be an absolute home directory.'
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
company="$HOME/.claude/company"
venv="$company/.venv"
key_file="$company/9router.key"
router_key=${NINER_KEY:-}
unset NINER_KEY

missing=()
for tool in claude python3 sqlite3 curl; do
  command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if [[ ${#missing[@]} -gt 0 ]]; then
  printf 'Missing required command: %s\n' "${missing[@]}" >&2
  exit 1
fi
use_uv=0
if command -v uv >/dev/null 2>&1; then
  use_uv=1
else
  printf '%s\n' 'uv not found; using python3 -m venv and pip.'
  python3 -I -m venv --help >/dev/null 2>&1 || die 'Missing Python venv support. Install it and retry.'
fi
python3 -I - <<'PY'
import sys
if sys.version_info < (3, 12):
    raise SystemExit("Error: python3 3.12+ is required by bin/9r.")
try:
    import sqlite3
except ImportError:
    raise SystemExit("Error: Python sqlite3 support is missing.")
PY
if [[ -n $habblaud_dir ]]; then
  [[ -f $repo_dir/habblaud/setup-habblaud.sh ]] || die 'Missing habblaud/setup-habblaud.sh.'
fi

# Preview and validate before touching any destination. FD 3 keeps the key out
# of command arguments, environment variables and logs. --yes never prompts;
# it needs NINER_KEY or an existing key. Dry runs never request a secret.
configure_files() {
  python3 -I - "$repo_dir" "$HOME" "$1" 3<<<"$router_key" <<'PY'
import json
import os
import shutil
import stat
import sys
import tempfile
from datetime import datetime
from pathlib import Path

repo, home = map(Path, sys.argv[1:3])
mode = sys.argv[3]
dry_run = mode == "dry-run"
company = home / ".claude/company"
claude_md = home / ".claude/CLAUDE.md"
begin, end = b"<!-- company-9r:begin -->", b"<!-- company-9r:end -->"


def exists(path):
    return path.exists() or path.is_symlink()


def block_span(data):
    if not data.count(begin) and not data.count(end):
        return None
    if data.count(begin) != 1 or data.count(end) != 1:
        raise ValueError("CLAUDE.md has duplicate or incomplete company-9r markers.")
    start, stop = data.index(begin), data.index(end)
    if stop < start:
        raise ValueError("CLAUDE.md has reversed company-9r markers.")
    return start, stop + len(end)


def backup(path, keep=False):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = path.with_name(path.name + ".bak-" + stamp)
    index = 0
    while exists(target):
        index += 1
        target = path.with_name(path.name + ".bak-" + stamp + "-" + str(index))
    print(f"{'Would back up' if dry_run else 'Back up'} {path} -> {target}")
    if not dry_run:
        # keep=True copies so the original stays in place until os.replace swaps it atomically
        shutil.copy2(path, target) if keep else path.rename(target)


def directory(path):
    if path.is_dir() and not path.is_symlink():
        return
    if exists(path):
        backup(path)
    print(f"{'Would create' if dry_run else 'Create'} directory {path}")
    if not dry_run:
        path.mkdir(parents=True, exist_ok=True)


def write_file(path, data, permissions=0o600):
    if path.is_file() and not path.is_symlink() and path.read_bytes() == data:
        if not dry_run:
            path.chmod(permissions)
        return
    print(f"{'Would write' if dry_run else 'Write'} {path}")
    if dry_run:
        if exists(path):
            backup(path)
        return
    fd, temp_name = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), permissions)
            stream.write(data)
        if exists(path):
            backup(path, keep=True)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


try:
    links = [(repo / "company" / name, company / name) for name in
             ("workers.json", "contract.md", "tools.py", "mcp_server.py", "schemas")]
    links += [(repo / "skills" / name, home / ".claude/skills" / name)
              for name in ("engineering", "marketing")]
    links.append((repo / "bin/9r", home / ".local/bin/9r"))
    for source in [src for src, _ in links] + [repo / "requirements.txt", repo / "claude-md/company-9r.md"]:
        if not source.exists():
            raise ValueError(f"Missing repository file: {source}")
    if company.is_symlink():
        raise ValueError("~/.claude/company must be a real directory; move its symlink aside first.")
    if exists(company / ".venv") and (not (company / ".venv").is_dir() or (company / ".venv").is_symlink()):
        raise ValueError("~/.claude/company/.venv must be a real directory.")
    if exists(claude_md) and not claude_md.is_file():
        raise ValueError("~/.claude/CLAUDE.md is not a readable file.")
    old = claude_md.read_bytes() if claude_md.exists() else b""
    span = block_span(old)
    block = (repo / "claude-md/company-9r.md").read_bytes().strip(b"\r\n")
    template_span = block_span(block)
    if template_span is None:
        block = begin + b"\n" + block + b"\n" + end
    elif template_span != (0, len(block)):
        raise ValueError("The company-9r template has text outside its markers.")
    if span is None:
        separator = b"" if not old or old.endswith(b"\n") else b"\n"
        updated = old + separator + block + b"\n"
    else:
        updated = old[:span[0]] + block + old[span[1]:]
    with os.fdopen(3, encoding="utf-8") as stream:
        key = stream.read()[:-1]  # Bash here-strings add exactly one newline.
    if key and not all(33 <= ord(char) <= 126 for char in key):
        raise ValueError("The 9router key must be one nonempty ASCII token.")
    if mode == "validate":
        sys.exit(0)

    # Do not replace users' top-level dotfile directory symlinks.
    for parent in (home / ".claude", home / ".claude/skills", home / ".local/bin"):
        if exists(parent) and not parent.is_dir():
            raise ValueError(f"Not a directory: {parent}")
        if not dry_run:
            parent.mkdir(parents=True, exist_ok=True)
    directory(company)
    for source, destination in links:
        if destination.is_symlink() and destination.resolve() == source.resolve():
            continue
        if exists(destination):
            backup(destination)
        print(f"{'Would link' if dry_run else 'Link'} {destination} -> {source}")
        if not dry_run:
            destination.symlink_to(source, target_is_directory=source.is_dir())

    directory(company / "plugins")
    for name, description in (("eng", "Company engineering workers"), ("mkt", "Company marketing workers")):
        plugin = company / "plugins" / name
        directory(plugin)
        directory(plugin / ".claude-plugin")
        directory(plugin / "skills")
        manifest = plugin / ".claude-plugin/plugin.json"
        if not exists(manifest):
            data = {"name": name, "version": "1.0.0", "description": description}
            write_file(manifest, (json.dumps(data, indent=2) + "\n").encode())

    if key:
        write_file(company / "9router.key", (key + "\n").encode())
    elif dry_run:
        print("Would request a 9router key, or keep the existing key (mode 600).")
    else:
        raise ValueError("Missing 9router key.")
    if not dry_run:
        # Rotated-key backups are secrets too, including older permissive files.
        for saved_key in company.glob("9router.key.bak-*"):
            if saved_key.is_file() and not saved_key.is_symlink():
                saved_key.chmod(0o600)
    if updated != old:
        permissions = stat.S_IMODE(claude_md.stat().st_mode) if claude_md.exists() else 0o600
        write_file(claude_md, updated, permissions)
    mcp = {"mcpServers": {"company-tools": {
        "command": str(company / ".venv/bin/python"), "args": [str(company / "mcp_server.py")]}}}
    write_file(company / "mcp.json", (json.dumps(mcp, indent=2) + "\n").encode())
except (OSError, ValueError) as exc:
    # File/validation errors never contain the key or an HTTP response body.
    raise SystemExit(f"Error: {exc}") from None
PY
}

configure_files validate
if [[ $dry_run -eq 0 ]]; then
  if [[ $yes -eq 0 ]]; then
    [[ -t 0 ]] || die 'Non-interactive install needs --yes and NINER_KEY (or an existing key).'
    printf 'Install company 9r for %s? [y/N] ' "$HOME"
    IFS= read -r answer || die 'No confirmation received.'
    case "$answer" in y|Y|yes|YES) ;; *) printf '%s\n' 'Cancelled.'; exit 0 ;; esac
  fi
  if [[ -z $router_key && -f $key_file ]]; then
    router_key=$(<"$key_file")
  fi
  if [[ -z $router_key ]]; then
    [[ $yes -eq 0 && -t 0 ]] || die 'Set NINER_KEY or rerun interactively to enter the 9router key.'
    IFS= read -r -s -p '9router API key: ' router_key || die 'No key received.'
    printf '\n'
  fi
  [[ -n $router_key ]] || die 'The 9router key cannot be empty.'
  configure_files apply

  if [[ ! -x $venv/bin/python ]]; then
    if [[ $use_uv -eq 1 ]]; then
      uv venv --python "$(command -v python3)" "$venv"
    else
      python3 -I -m venv "$venv"
    fi
  fi
  if [[ $use_uv -eq 1 ]]; then
    uv pip install --python "$venv/bin/python" -r "$repo_dir/requirements.txt"
  else
    if ! "$venv/bin/python" -I -m pip --version >/dev/null 2>&1; then
      "$venv/bin/python" -I -m ensurepip --upgrade
    fi
    "$venv/bin/python" -I -m pip install -r "$repo_dir/requirements.txt"
  fi

  # No proxy, redirects, curlrc, response body, or key-bearing command arguments.
  if printf 'Authorization: Bearer %s\n' "$router_key" | curl -q --silent --fail \
      --noproxy '*' --max-time 5 --output /dev/null --header @- \
      http://localhost:20128/v1/models >/dev/null 2>&1; then
    printf '%s\n' '9router is reachable.'
  else
    printf '%s\n' 'Warning: 9router is unavailable or rejected the key. Start it on localhost:20128 and retry.' >&2
  fi
  unset router_key

  if ! mcp_list=$(NO_COLOR=1 claude mcp list 2>/dev/null); then
    die 'Cannot list Claude MCP servers. Check Claude setup and rerun.'
  fi
  if [[ $mcp_list =~ (^|$'\n')[[:space:]]*company-tools: ]]; then
    printf '%s\n' 'company-tools is already registered.'
  else
    claude mcp add -s user company-tools -- "$venv/bin/python" "$company/mcp_server.py"
  fi
else
  configure_files dry-run
  printf 'Would create/reuse %s and install requirements.txt.\n' "$venv"
  printf '%s\n' 'Would check 9router and register company-tools if absent.'
  unset router_key
fi

if [[ $no_combos -eq 0 ]]; then
  if [[ $dry_run -eq 1 ]]; then
    python3 -I "$repo_dir/9router/apply-combos.py" --dry-run
  else
    python3 -I "$repo_dir/9router/apply-combos.py"
  fi
fi
if [[ -n $habblaud_dir ]]; then
  if [[ $dry_run -eq 1 ]]; then
    printf 'Would run: bash %q %q\n' "$repo_dir/habblaud/setup-habblaud.sh" "$habblaud_dir"
  else
    bash "$repo_dir/habblaud/setup-habblaud.sh" "$habblaud_dir"
  fi
fi
printf '\n%s\n' 'Next steps:'
printf '%s\n' '  [ ] Keep this checkout in place; git pull updates the linked files.'
case ":${PATH:-}:" in
  *":$HOME/.local/bin:"*) ;;
  *) printf '%s\n' '  [ ] Add to your shell profile: export PATH="$HOME/.local/bin:$PATH"' ;;
esac
printf '%s\n' '  [ ] Start 9router on localhost:20128 and connect your providers.'
if [[ ! -e $company/postiz.env ]]; then
  printf '  [ ] Optional Postiz: copy %q to %q, fill it in, and chmod 600.\n' \
    "$repo_dir/config/postiz.env.example" "$company/postiz.env"
fi
printf '%s\n' '  [ ] Restart Claude Code; check: claude mcp list' '  [ ] List worker roles: 9r'
if [[ $dry_run -eq 1 ]]; then
  printf '%s\n' 'Dry run complete. No installation changes made.'
else
  printf '%s\n' 'Installation complete.'
fi
