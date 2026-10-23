"""Quality control and orchestration tools for Omnigent company agents.

Provides git worktree management, verification gates, and handoff manifest validation
for autonomous agent pipelines.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


def create_task_worktree(task_id: str, base_branch: str = "main") -> dict[str, Any]:
    """Create an ephemeral git worktree at .orch-worktrees/<task_id>.

    Isolates agent file modifications from the main workspace so parallel
    agents can work concurrently without workspace collisions or 'project busy' locks.

    :param task_id: Unique task identifier (alphanumeric, dashes, underscores).
    :param base_branch: The source branch or commit to branch off from (default: 'main').
    :returns: Dictionary with execution status, worktree path, and branch name.
    """
    clean_task_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", task_id.strip())
    if not clean_task_id:
        return {"status": "error", "error": "Invalid or empty task_id provided."}

    repo_root = Path.cwd()
    worktree_dir = repo_root / ".orch-worktrees" / clean_task_id
    branch_name = f"orch/{clean_task_id}"

    try:
        # Verify git repository
        git_check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if git_check.returncode != 0:
            return {"status": "error", "error": f"Current directory is not a git repo: {git_check.stderr}"}

        worktree_dir.parent.mkdir(parents=True, exist_ok=True)

        if worktree_dir.exists():
            return {
                "status": "success",
                "message": "Worktree directory already exists, reusing it.",
                "worktree_path": str(worktree_dir.resolve()),
                "branch": branch_name,
                "task_id": clean_task_id,
            }

        # Check if branch exists
        branch_check = subprocess.run(
            ["git", "rev-parse", "--verify", branch_name],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )

        if branch_check.returncode == 0:
            cmd = ["git", "worktree", "add", str(worktree_dir), branch_name]
        else:
            # Fallback to current HEAD if base_branch does not exist
            base_check = subprocess.run(
                ["git", "rev-parse", "--verify", base_branch],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=False,
            )
            target_base = base_branch if base_check.returncode == 0 else "HEAD"
            cmd = ["git", "worktree", "add", "-b", branch_name, str(worktree_dir), target_base]

        add_res = subprocess.run(
            cmd,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )

        if add_res.returncode != 0:
            return {
                "status": "error",
                "error": f"Failed to add git worktree: {add_res.stderr.strip() or add_res.stdout.strip()}",
            }

        return {
            "status": "success",
            "worktree_path": str(worktree_dir.resolve()),
            "branch": branch_name,
            "task_id": clean_task_id,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def cleanup_task_worktree(task_id: str) -> dict[str, Any]:
    """Clean up and remove an ephemeral git worktree at .orch-worktrees/<task_id>.

    Prunes the worktree and cleans references once the task and QC stages complete.

    :param task_id: Unique task identifier of the worktree to clean up.
    :returns: Dictionary with cleanup status and details.
    """
    clean_task_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", task_id.strip())
    if not clean_task_id:
        return {"status": "error", "error": "Invalid or empty task_id."}

    repo_root = Path.cwd()
    worktree_dir = repo_root / ".orch-worktrees" / clean_task_id
    branch_name = f"orch/{clean_task_id}"

    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_dir)],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )

        if worktree_dir.exists():
            shutil.rmtree(worktree_dir, ignore_errors=True)

        # Remove branch
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )

        return {
            "status": "success",
            "task_id": clean_task_id,
            "cleaned": True,
            "path": str(worktree_dir),
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def verify_git_scope(allowed_files: list[str], worktree_path: str = ".") -> dict[str, Any]:
    """Verify that changes made in the git worktree match the declared allowed scope.

    Runs `git status --porcelain` to identify modified/added/deleted files and checks
    for undeclared file changes or hallucinations outside scope.

    :param allowed_files: List of file paths authorized for modification.
    :param worktree_path: Target directory of the repository/worktree (default: '.').
    :returns: Verification results including mismatch status, changed files, and violations.
    """
    target_path = Path(worktree_path).resolve()
    if not target_path.exists():
        return {"status": "error", "error": f"Worktree path does not exist: {worktree_path}"}

    try:
        status_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(target_path),
            capture_output=True,
            text=True,
            check=False,
        )

        if status_res.returncode != 0:
            return {"status": "error", "error": f"git status failed: {status_res.stderr.strip()}"}

        modified_files: list[str] = []
        for line in status_res.stdout.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            # Format: 'XY path' or 'XY orig -> dest'
            parts = line_str.split(maxsplit=1)
            if len(parts) == 2:
                file_part = parts[1]
                if " -> " in file_part:
                    file_part = file_part.split(" -> ")[1]
                modified_files.append(file_part.strip().strip('"'))

        normalized_allowed = {
            os.path.normpath(p).lstrip("./") for p in allowed_files if isinstance(p, str) and p.strip()
        }
        normalized_modified = {os.path.normpath(p).lstrip("./") for p in modified_files}

        violations = sorted(list(normalized_modified - normalized_allowed))
        unmodified_allowed = sorted(list(normalized_allowed - normalized_modified))
        in_scope = len(violations) == 0

        return {
            "status": "verified" if in_scope else "mismatch",
            "in_scope": in_scope,
            "total_modified": len(normalized_modified),
            "modified_files": sorted(list(normalized_modified)),
            "allowed_files": sorted(list(normalized_allowed)),
            "violations": violations,
            "unmodified_allowed": unmodified_allowed,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def run_engineering_gate(test_command: str, worktree_path: str = ".") -> dict[str, Any]:
    """Execute unit tests, linter (ruff), and security scan (bandit) in worktree_path.

    Runs commands independently and writes consolidated test logs to tests.log.

    :param test_command: Test execution command, e.g., 'pytest tests/unit'.
    :param worktree_path: Target directory where code resides (default: '.').
    :returns: Test, linter, and security audit status along with log file location.
    """
    target_path = Path(worktree_path).resolve()
    log_file = target_path / "tests.log"
    logs: list[str] = []

    tests_passed = False
    linter_passed = True
    security_passed = True

    # 1. Run Unit Tests
    logs.append(f"=== 1. Test Command: {test_command} ===")
    try:
        test_res = subprocess.run(
            test_command,
            shell=True,
            cwd=str(target_path),
            capture_output=True,
            text=True,
            check=False,
        )
        tests_passed = test_res.returncode == 0
        logs.append(test_res.stdout)
        if test_res.stderr:
            logs.append("[STDERR]\n" + test_res.stderr)
        logs.append(f"Exit code: {test_res.returncode}\n")
    except Exception as exc:
        logs.append(f"Exception running test_command: {exc}\n")

    # 2. Run Ruff Linter if available
    ruff_bin = shutil.which("ruff")
    if ruff_bin:
        logs.append("=== 2. Linter (ruff check) ===")
        try:
            ruff_res = subprocess.run(
                [ruff_bin, "check", "."],
                cwd=str(target_path),
                capture_output=True,
                text=True,
                check=False,
            )
            linter_passed = ruff_res.returncode == 0
            logs.append(ruff_res.stdout)
            if ruff_res.stderr:
                logs.append("[STDERR]\n" + ruff_res.stderr)
            logs.append(f"Exit code: {ruff_res.returncode}\n")
        except Exception as exc:
            logs.append(f"Exception running ruff: {exc}\n")
    else:
        logs.append("=== 2. Linter: ruff not installed in PATH, skipped ===\n")

    # 3. Run Bandit Security Audit if available
    bandit_bin = shutil.which("bandit")
    if bandit_bin:
        logs.append("=== 3. Security Audit (bandit) ===")
        try:
            bandit_res = subprocess.run(
                [bandit_bin, "-r", ".", "-ll", "-q"],
                cwd=str(target_path),
                capture_output=True,
                text=True,
                check=False,
            )
            security_passed = bandit_res.returncode == 0
            logs.append(bandit_res.stdout)
            if bandit_res.stderr:
                logs.append("[STDERR]\n" + bandit_res.stderr)
            logs.append(f"Exit code: {bandit_res.returncode}\n")
        except Exception as exc:
            logs.append(f"Exception running bandit: {exc}\n")
    else:
        logs.append("=== 3. Security Audit: bandit not installed in PATH, skipped ===\n")

    full_log_content = "\n".join(logs)
    try:
        log_file.write_text(full_log_content, encoding="utf-8")
    except Exception:
        pass

    all_passed = tests_passed and linter_passed and security_passed

    return {
        "status": "passed" if all_passed else "failed",
        "tests_passed": tests_passed,
        "linter_passed": linter_passed,
        "security_passed": security_passed,
        "test_command": test_command,
        "log_path": str(log_file),
        "log_preview": full_log_content[:2000],
    }


def run_marketing_gate(post_path: str) -> dict[str, Any]:
    """Validate Markdown post structure, SEO requirements, and self-contained assets.

    Checks:
    - Heading hierarchy (H1 present, H2 present)
    - URL slug format (kebab-case)
    - Meta description length (50-160 chars)
    - Self-contained SVG/HTML (no external calls like http/https)
    - Optional repo check script: `checks/check_posts.py`

    :param post_path: Path to the markdown file to evaluate.
    :returns: Validation outcome, list of detected issues, and quality score.
    """
    file_path = Path(post_path).resolve()
    if not file_path.exists() or not file_path.is_file():
        return {
            "status": "failed",
            "seo_passed": False,
            "self_contained_passed": False,
            "issues": [f"File not found: {post_path}"],
        }

    issues: list[str] = []
    content = file_path.read_text(encoding="utf-8", errors="replace")

    # 1. Heading check
    has_h1 = bool(re.search(r"^#\s+[^\n]+", content, re.MULTILINE))
    has_h2 = bool(re.search(r"^##\s+[^\n]+", content, re.MULTILINE))
    if not has_h1:
        issues.append("Missing H1 title heading (# Title)")
    if not has_h2:
        issues.append("Missing at least one H2 section heading (## Section)")

    # 2. Slug check
    stem = file_path.stem
    slug_match = re.search(r"^slug:\s*([^\n\r]+)", content, re.MULTILINE)
    slug = slug_match.group(1).strip().strip("'\"") if slug_match else stem
    if not re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", slug):
        issues.append(f"Invalid slug format: '{slug}'. Must be lower-case kebab-case.")

    # 3. Meta description check
    meta_match = re.search(r"^(?:description|meta_description):\s*([^\n\r]+)", content, re.MULTILINE)
    if meta_match:
        meta_desc = meta_match.group(1).strip().strip("'\"")
        if len(meta_desc) < 50 or len(meta_desc) > 160:
            issues.append(
                f"Meta description length ({len(meta_desc)} chars) outside optimal SEO range (50-160 chars)."
            )
    else:
        # Check first paragraph length
        paragraphs = [p.strip() for p in content.split("\n\n") if p.strip() and not p.startswith("#")]
        if not paragraphs or len(paragraphs[0]) < 50:
            issues.append("Missing explicit meta description and lead paragraph is too short (< 50 chars).")

    # 4. Self-contained SVG/HTML check (no external resource loading)
    external_refs = re.findall(
        r"""(?:src|href|url)\s*=\s*['"](https?://[^\s'"]+)['"]|url\((https?://[^\s)]+)\)""",
        content,
        re.IGNORECASE,
    )
    external_urls = [u[0] or u[1] for u in external_refs if u[0] or u[1]]
    if external_urls:
        issues.append(
            f"Found external asset/script/style URLs (assets must be self-contained): {external_urls[:3]}"
        )

    # 5. Optional checks/check_posts.py run
    check_script = Path("checks/check_posts.py")
    script_passed = True
    if check_script.exists():
        try:
            res = subprocess.run(
                ["python3", str(check_script), str(file_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                script_passed = False
                issues.append(f"checks/check_posts.py failed: {res.stderr.strip() or res.stdout.strip()}")
        except Exception as exc:
            script_passed = False
            issues.append(f"Error running checks/check_posts.py: {exc}")

    seo_passed = has_h1 and has_h2 and (not any("slug" in i or "Meta description" in i for i in issues))
    self_contained = len(external_urls) == 0
    passed = len(issues) == 0

    return {
        "status": "passed" if passed else "failed",
        "seo_passed": seo_passed,
        "self_contained_passed": self_contained,
        "external_check_passed": script_passed,
        "slug": slug,
        "issues": issues,
    }


def validate_handoff_manifest(manifest_json: str) -> dict[str, Any]:
    """Validate that an inter-agent handoff manifest JSON strictly contains all 9 required fields.

    Required fields:
    - task_id: Identifier of the task
    - status: Execution status (e.g., 'completed', 'blocked')
    - summary: High-level summary of work accomplished
    - files_changed: List of files created or modified
    - tests: Details of executed tests or test results
    - evidence: Proof of verification (logs, diffs, metrics)
    - risks: Identified risks or edge cases
    - requires_decision: Boolean flag indicating if human input is needed
    - next_action: Concrete recommended follow-up step

    :param manifest_json: JSON string or markdown block containing the manifest.
    :returns: Validation outcome, missing fields (if any), and parsed manifest dictionary.
    """
    raw_str = manifest_json.strip()
    if raw_str.startswith("```"):
        raw_str = re.sub(r"^```(?:json)?\s*", "", raw_str, flags=re.MULTILINE)
        raw_str = re.sub(r"\s*```$", "", raw_str, flags=re.MULTILINE).strip()

    try:
        data = json.loads(raw_str)
    except Exception as exc:
        return {
            "valid": False,
            "status": "invalid_json",
            "error": f"JSON decode error: {exc}",
            "missing_fields": [],
            "manifest": None,
        }

    if not isinstance(data, dict):
        return {
            "valid": False,
            "status": "invalid_format",
            "error": f"Expected JSON object, got {type(data).__name__}",
            "missing_fields": [],
            "manifest": None,
        }

    required_fields = [
        "task_id",
        "status",
        "summary",
        "files_changed",
        "tests",
        "evidence",
        "risks",
        "requires_decision",
        "next_action",
    ]

    missing = [f for f in required_fields if f not in data]
    type_errors: list[str] = []

    if "files_changed" in data and not isinstance(data["files_changed"], list):
        type_errors.append("Field 'files_changed' must be a list of file paths.")
    if "requires_decision" in data and not isinstance(data["requires_decision"], bool):
        type_errors.append("Field 'requires_decision' must be a boolean.")

    is_valid = len(missing) == 0 and len(type_errors) == 0

    return {
        "valid": is_valid,
        "status": "valid" if is_valid else "rejected",
        "missing_fields": missing,
        "type_errors": type_errors,
        "manifest": data if is_valid else None,
    }


# ── Postiz Self-Hosted API Integration ─────────────────────────────────────────


def _get_postiz_config() -> tuple[str, str]:
    """Resolve normalized Postiz API Base URL and API key from env or postiz.env."""
    raw_url = os.environ.get("POSTIZ_API_URL", "").strip()
    api_key = os.environ.get("POSTIZ_API_KEY", "").strip()
    if not raw_url or not api_key:
        # Fallback: file key chmod 600, không commit (mặc định ~/.claude/company/postiz.env)
        env_file = Path(os.environ.get("POSTIZ_ENV_FILE", "~/.claude/company/postiz.env")).expanduser()
        file_config: dict[str, str] = {}
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                k, _, v = line.strip().partition("=")
                if k in ("POSTIZ_API_URL", "POSTIZ_API_KEY"):
                    file_config[k] = v.strip().strip("\"'").strip()
        raw_url = raw_url or file_config.get("POSTIZ_API_URL", "")
        api_key = api_key or file_config.get("POSTIZ_API_KEY", "")
    raw_url = raw_url.rstrip("/")
    if not raw_url:
        raise RuntimeError("POSTIZ_API_URL chưa cấu hình (env hoặc ~/.claude/company/postiz.env).")
    if not (raw_url.endswith("/public/v1") or raw_url.endswith("/api/public/v1")):
        if "/api" in raw_url:
            raw_url = f"{raw_url}/public/v1"
        else:
            raw_url = f"{raw_url}/api/public/v1"
    return raw_url, api_key


def _postiz_request(method: str, path: str, payload: Any = None) -> Any:
    """Gọi Postiz Public API; raise RuntimeError kèm body lỗi."""
    import urllib.error
    import urllib.request

    base_url, api_key = _get_postiz_config()
    if not api_key or api_key == "postiz-api-key-placeholder":
        raise RuntimeError("POSTIZ_API_KEY chưa cấu hình (env hoặc ~/.claude/company/postiz.env).")
    headers = {"Authorization": api_key, "User-Agent": "Omnigent-Postiz-Integration/1.0", "Content-Type": "application/json"}
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(f"{base_url}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8")
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Postiz API {e.code}: {e.read().decode('utf-8', 'replace')}") from e


def _media_cache_file() -> Path:
    """File id → path ảnh do upload_postiz_asset ghi (mặc định ~/.claude/company/postiz_media.json)."""
    return Path(os.environ.get("POSTIZ_MEDIA_CACHE", "~/.claude/company/postiz_media.json")).expanduser()


def _media_objs(media: list[str | dict[str, str]] | None) -> list[dict[str, str]]:
    """Chuẩn hoá ảnh thành {id, path}: Postiz cần path để đăng ảnh lên Facebook/Threads.

    Media ID trần được tra path trong cache upload; thiếu path thì raise thay vì gửi ảnh hỏng.
    """
    cache_file = _media_cache_file()
    cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.is_file() else {}
    objs = []
    for m in media or []:
        if isinstance(m, str):
            m = {"id": m.strip(), "path": cache.get(m.strip(), "")}
        if not m.get("id") or not m.get("path"):
            raise ValueError(
                f"Ảnh {m.get('id')!r} thiếu path: truyền {{'id': ..., 'path': ...}} "
                "hoặc upload lại bằng upload_postiz_asset để lưu path."
            )
        objs.append(m)
    return objs


def _normalize_iso_datetime(dt_str: str) -> tuple[str, str]:
    """Parse a datetime string and return (iso_vietnam_tz, iso_utc)."""
    clean_str = dt_str.strip()
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    vn_tz = ZoneInfo("Asia/Ho_Chi_Minh")
    try:
        if clean_str.endswith("Z"):
            clean_str = clean_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(clean_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=vn_tz)
        dt_vn = dt.astimezone(vn_tz)
        dt_utc = dt.astimezone(timezone.utc)
        return dt_vn.isoformat(), dt_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except Exception:
        return clean_str, clean_str


def get_postiz_channels() -> dict[str, Any]:
    """Fetch connected social media channels from the self-hosted Postiz instance.

    Calls GET {POSTIZ_API_URL}/integrations with the Authorization header.
    Returns channel identifiers, names, and provider types to map posts accurately without hallucination.

    :returns: Dictionary with channels list, total count, and connection status.
    """
    base_url, api_key = _get_postiz_config()
    endpoint = f"{base_url}/integrations"

    if not api_key or api_key == "postiz-api-key-placeholder":
        return {
            "status": "warning",
            "message": "POSTIZ_API_KEY is not configured or using default placeholder.",
            "channels": [],
            "total": 0,
            "endpoint": endpoint,
        }

    headers = {
        "Authorization": api_key,
        "x-api-key": api_key,
        "User-Agent": "Omnigent-Postiz-Integration/1.0",
        "Accept": "application/json",
    }

    try:
        data = None
        status_code = 500
        resp_text = ""
        try:
            import httpx

            with httpx.Client(timeout=15.0) as client:
                resp = client.get(endpoint, headers=headers)
                status_code = resp.status_code
                resp_text = resp.text
                if status_code == 200:
                    data = resp.json()
        except ImportError:
            import urllib.request

            req = urllib.request.Request(endpoint, headers=headers)
            with urllib.request.urlopen(req, timeout=15.0) as uresp:
                status_code = uresp.status
                resp_text = uresp.read().decode("utf-8")
                if status_code == 200:
                    data = json.loads(resp_text)

        if status_code == 200 and data is not None:
            channels_raw = data if isinstance(data, list) else data.get("integrations", data.get("data", []))
            channels = []
            for ch in channels_raw:
                if isinstance(ch, dict):
                    channels.append({
                        "id": ch.get("id"),
                        "name": ch.get("name") or ch.get("username") or ch.get("identifier"),
                        "providerIdentifier": ch.get("providerIdentifier") or ch.get("identifier") or ch.get("provider") or ch.get("type"),
                        "disabled": ch.get("disabled", False),
                    })
            return {
                "status": "success",
                "total": len(channels),
                "channels": channels,
            }
        else:
            return {
                "status": "error",
                "status_code": status_code,
                "error": f"Postiz API returned {status_code}: {resp_text}",
                "channels": [],
            }
    except Exception as exc:
        return {
            "status": "error",
            "error": f"Failed to connect to Postiz at {endpoint}: {exc}",
            "channels": [],
        }


def upload_postiz_asset(file_path: str) -> dict[str, Any]:
    """Upload an image or banner asset to the self-hosted Postiz server.

    Validates file existence at file_path, then performs a multipart/form-data
    POST to {POSTIZ_API_URL}/upload with Authorization header.

    :param file_path: Path to the image/banner file (PNG, JPG, SVG, WebP, etc.).
    :returns: Dictionary with asset id, remote path/url, and upload status.
    """
    path_obj = Path(file_path).resolve()
    if not path_obj.exists() or not path_obj.is_file():
        return {
            "status": "error",
            "error": f"Asset file not found at: {file_path}",
        }

    base_url, api_key = _get_postiz_config()
    endpoint = f"{base_url}/upload"

    if not api_key or api_key == "postiz-api-key-placeholder":
        return {
            "status": "warning",
            "message": "POSTIZ_API_KEY is not configured or using default placeholder.",
            "file_path": str(path_obj),
            "id": f"mock-{path_obj.stem}",
            "path": f"/uploads/{path_obj.name}",
        }

    import mimetypes
    mime_type, _ = mimetypes.guess_type(str(path_obj))
    if not mime_type:
        mime_type = "image/svg+xml" if path_obj.suffix.lower() == ".svg" else "application/octet-stream"

    headers = {
        "Authorization": api_key,
        "x-api-key": api_key,
        "User-Agent": "Omnigent-Postiz-Integration/1.0",
    }

    # Multipart bằng urllib (stdlib) để không phụ thuộc httpx trong venv.
    import urllib.error
    import urllib.request
    import uuid

    boundary = uuid.uuid4().hex
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path_obj.name}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("utf-8") + path_obj.read_bytes() + f"\r\n--{boundary}--\r\n".encode("utf-8")
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

    try:
        req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {
            "status": "error",
            "status_code": e.code,
            "error": f"Postiz upload failed ({e.code}): {e.read().decode('utf-8', 'replace')}",
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": f"Failed to upload asset to Postiz at {endpoint}: {exc}",
        }

    asset_id = str(res_data.get("id") or res_data.get("fileId") or res_data.get("path"))
    asset_path = str(res_data.get("path") or res_data.get("url") or res_data.get("id"))
    # Lưu path để publish/edit chỉ cần media ID vẫn gửi đủ {id, path}.
    cache_file = _media_cache_file()
    cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.is_file() else {}
    cache[asset_id] = asset_path
    cache_file.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    return {
        "status": "success",
        "id": asset_id,
        "path": asset_path,
        "file_name": path_obj.name,
        "mime_type": mime_type,
    }


def publish_to_postiz(
    integration_id: str,
    content: str,
    scheduled_at_iso: str,
    media_ids: list[str | dict[str, str]] | None = None,
    post_type: str = "draft",
) -> dict[str, Any]:
    """Create or schedule a post on a connected Postiz integration channel.

    Validates post_type ('draft' or 'schedule'), normalizes scheduled_at_iso
    to Asia/Ho_Chi_Minh (+07:00), and issues POST {POSTIZ_API_URL}/posts.

    :param integration_id: Target social channel integration ID from get_postiz_channels.
    :param content: Post body text/copy.
    :param scheduled_at_iso: Desired publication time in ISO-8601 format.
    :param media_ids: Optional media from upload_postiz_asset: media ID (path looked up from upload cache) or {"id", "path"}.
    :param post_type: Either 'draft' (default, safe for review) or 'schedule'.
    :returns: Result dictionary containing post_id, status, scheduled_at, and API response.
    """
    try:
        image_objs = _media_objs(media_ids)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}

    clean_type = post_type.lower().strip()
    if clean_type not in ("draft", "schedule"):
        return {
            "status": "error",
            "error": f"Invalid post_type '{post_type}'. Must be either 'draft' or 'schedule'.",
        }

    if not integration_id or not integration_id.strip():
        return {
            "status": "error",
            "error": "integration_id is required. Run get_postiz_channels() first to retrieve valid IDs.",
        }

    clean_content = content.strip()
    if not clean_content:
        return {
            "status": "error",
            "error": "content cannot be empty.",
        }

    dt_vn, dt_utc = _normalize_iso_datetime(scheduled_at_iso)
    base_url, api_key = _get_postiz_config()
    endpoint = f"{base_url}/posts"

    provider = None
    if api_key and api_key != "postiz-api-key-placeholder":
        chans = get_postiz_channels().get("channels", [])
        provider = next((c["providerIdentifier"] for c in chans if c["id"] == integration_id.strip()), None)
        if not provider:
            return {"status": "error", "error": f"integration_id {integration_id} không có trong get_postiz_channels()."}

    payload = {
        "type": clean_type,
        "date": dt_utc,
        "shortLink": False,
        "tags": [],
        "posts": [
            {
                "integration": {"id": integration_id.strip()},
                "value": [
                    {
                        "content": clean_content,
                        "image": image_objs,
                    }
                ],
                "settings": {"__type": provider or "unknown"},
            }
        ],
    }

    if not api_key or api_key == "postiz-api-key-placeholder":
        return {
            "status": "warning",
            "message": "POSTIZ_API_KEY is not configured or using default placeholder.",
            "dry_run": True,
            "post_type": clean_type,
            "integration_id": integration_id,
            "scheduled_at_vn": dt_vn,
            "scheduled_at_utc": dt_utc,
            "payload_preview": payload,
        }

    headers = {
        "Authorization": api_key,
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "User-Agent": "Omnigent-Postiz-Integration/1.0",
    }

    try:
        data = None
        status_code = 500
        resp_text = ""
        try:
            import httpx

            with httpx.Client(timeout=30.0) as client:
                resp = client.post(endpoint, headers=headers, json=payload)
                status_code = resp.status_code
                resp_text = resp.text
                if status_code in (200, 201):
                    data = resp.json()
        except ImportError:
            import urllib.request

            req_bytes = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(endpoint, data=req_bytes, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30.0) as uresp:
                status_code = uresp.status
                resp_text = uresp.read().decode("utf-8")
                if status_code in (200, 201):
                    data = json.loads(resp_text)

        if status_code in (200, 201) and data is not None:
            first = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
            post_id = first.get("postId") or first.get("id")
            return {
                "status": "success",
                "post_id": post_id,
                "post_type": clean_type,
                "integration_id": integration_id,
                "scheduled_at_vn": dt_vn,
                "response": data,
            }
        else:
            return {
                "status": "error",
                "status_code": status_code,
                "error": f"Postiz post creation failed ({status_code}): {resp_text}",
                "payload": payload,
            }
    except Exception as exc:
        return {
            "status": "error",
            "error": f"Failed to post to Postiz at {endpoint}: {exc}",
        }


def list_postiz_posts(start_iso: str | None = None, end_iso: str | None = None) -> dict[str, Any]:
    """Liệt kê bài trên Postiz (mặc định 7 ngày trước tới 60 ngày sau).

    :param start_iso: Mốc bắt đầu ISO-8601 (tuỳ chọn).
    :param end_iso: Mốc kết thúc ISO-8601 (tuỳ chọn).
    :returns: Danh sách bài gồm id, state (QUEUE/DRAFT/PUBLISHED/ERROR), publishDate (UTC), kênh và nội dung.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    start = _normalize_iso_datetime(start_iso)[1] if start_iso else (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end = _normalize_iso_datetime(end_iso)[1] if end_iso else (now + timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    try:
        data = _postiz_request("GET", f"/posts?startDate={start}&endDate={end}")
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    posts = data if isinstance(data, list) else data.get("posts", [])
    return {
        "status": "success",
        "total": len(posts),
        "posts": [
            {
                "id": p.get("id"),
                "state": p.get("state"),
                "publishDate": p.get("publishDate"),
                "integration_id": (p.get("integration") or {}).get("id"),
                "provider": (p.get("integration") or {}).get("providerIdentifier"),
                "content": p.get("content"),
            }
            for p in posts
        ],
    }


def edit_postiz_post(
    post_id: str,
    media_ids: list[str | dict[str, str]],
    content: str | None = None,
    scheduled_at_iso: str | None = None,
) -> dict[str, Any]:
    """Sửa bài QUEUE/DRAFT trên Postiz: nội dung, ảnh, giờ đăng. Giữ nguyên post_id.

    Public API không có PUT/PATCH; POST /posts với value[].id = post_id sẽ ghi đè tại chỗ.
    API không trả ảnh hiện tại nên media_ids BẮT BUỘC: truyền đủ ảnh mong muốn sau khi sửa, [] để bỏ ảnh.

    :param post_id: ID bài lấy từ list_postiz_posts.
    :param media_ids: Ảnh của bài sau khi sửa (bắt buộc, [] = không ảnh): media ID (tra path từ cache upload) hoặc {"id", "path"}.
    :param content: Nội dung mới (bỏ trống = giữ nguyên).
    :param scheduled_at_iso: Giờ đăng mới ISO-8601, không có múi giờ thì hiểu là +07:00 (bỏ trống = giữ nguyên).
    :returns: Kết quả gồm post_id, state, publishDate sau khi sửa.
    """
    if media_ids is None:
        return {"status": "error", "error": "media_ids bắt buộc ([] nếu không có ảnh) vì API không trả ảnh hiện tại."}
    try:
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        data = _postiz_request("GET", f"/posts?startDate={(now - timedelta(days=7)).strftime(fmt)}&endDate={(now + timedelta(days=120)).strftime(fmt)}")
        posts = data if isinstance(data, list) else data.get("posts", [])
        post = next((p for p in posts if p.get("id") == post_id), None)
        if not post:
            return {"status": "error", "error": f"Không tìm thấy bài {post_id} (-7 tới +120 ngày)."}
        if post.get("state") not in ("QUEUE", "DRAFT"):
            return {"status": "error", "error": f"Chỉ sửa được bài QUEUE/DRAFT; bài đang {post.get('state')}."}
        settings = post.get("settings") or {}
        if isinstance(settings, str):
            settings = json.loads(settings)
        settings.setdefault("__type", post["integration"]["providerIdentifier"])
        payload = {
            "type": "draft" if post["state"] == "DRAFT" else "schedule",
            "date": _normalize_iso_datetime(scheduled_at_iso)[1] if scheduled_at_iso else post["publishDate"],
            "shortLink": False,
            "tags": [],
            "posts": [{
                "group": post["group"],
                "integration": {"id": post["integration"]["id"]},
                "value": [{
                    "id": post_id,
                    "content": post["content"] if content is None else content,
                    "image": _media_objs(media_ids),
                }],
                "settings": settings,
            }],
        }
        res = _postiz_request("POST", "/posts", payload)
        return {"status": "success", "post_id": post_id, "state": post["state"], "publishDate": payload["date"], "response": res}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
