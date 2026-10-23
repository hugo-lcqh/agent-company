#!/usr/bin/env python3
"""Apply only the named combos from this checkout to the local 9router database.

Offline unit tests (in-memory SQLite; no installer, credentials, or HTTP calls):
    python3 -I -B -m doctest -v 9router/apply-combos.py
"""

import argparse
import http.client
import json
import os
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """CREATE TABLE IF NOT EXISTS combos (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    kind TEXT,
    models TEXT NOT NULL,
    createdAt TEXT NOT NULL,
    updatedAt TEXT NOT NULL
)"""
UPSERT = """INSERT INTO combos (id, name, kind, models, createdAt, updatedAt)
    VALUES (?, ?, 'llm', ?, ?, ?)
    ON CONFLICT(name) DO UPDATE SET
        kind = excluded.kind, models = excluded.models, updatedAt = excluded.updatedAt
"""
MESSAGES_URL = "http://localhost:20128/api/v1/messages"


def token(value):
    """Reject empty tokens and whitespace/control characters at the input boundary.

    >>> token('cx/model')
    True
    >>> [token(value) for value in ('', ' spaced', 'bad\\nname', None, 42)]
    [False, False, False, False, False]
    """
    return (isinstance(value, str) and bool(value)
            and all(not char.isspace() and ord(char) >= 33 and ord(char) != 127
                    for char in value))


def validate_combos(value):
    """Validate the complete document before any database writes.

    >>> validate_combos({'w-code': ['cx/model', 'ag/fallback']})
    {'w-code': ['cx/model', 'ag/fallback']}
    >>> for bad in ({}, [], {'w': []}, {'w': 'model'}, {'w': [None]}, {'bad\\n': ['m']}):
    ...     try:
    ...         validate_combos(bad)
    ...     except ValueError:
    ...         print('rejected')
    rejected
    rejected
    rejected
    rejected
    rejected
    rejected
    """
    if not isinstance(value, dict) or not value:
        raise ValueError("combos.json must be a nonempty object of name -> model list.")
    for name, models in value.items():
        if not token(name) or not isinstance(models, list) or not models or not all(map(token, models)):
            raise ValueError("Each combo needs a nonempty name and a nonempty list of model tokens.")
    return value


def unique_object(pairs):
    """Refuse duplicate JSON names rather than silently overwriting a combo.

    >>> json.loads('{"w": [], "w": []}', object_pairs_hook=unique_object)
    Traceback (most recent call last):
        ...
    ValueError: Duplicate name in combos.json.
    """
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate name in combos.json.")
        result[name] = value
    return result


def changed_combos(connection, combos):
    """Return (name, JSON models, existing id) only for inserts or real changes."""
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'combos'"
    ).fetchone()
    changes = []
    for name, models in combos.items():
        row = connection.execute(
            "SELECT id, kind, models FROM combos WHERE name = ?", (name,)
        ).fetchone() if exists else None
        if row and row[1] == "llm":
            try:
                if json.loads(row[2]) == models:
                    continue
            except (TypeError, ValueError):
                pass  # Replace malformed models only in an explicitly named combo.
        changes.append((name, json.dumps(models, separators=(",", ":")), row[0] if row else None))
    return changes


def write_changes(connection, changes, timestamp):
    """Write within the caller's transaction, preserving existing ids/createdAt.

    >>> db = sqlite3.connect(':memory:')
    >>> combos = validate_combos({'w-code': ['cx/model', 'ag/fallback']})
    >>> write_changes(db, changed_combos(db, combos), 'first')
    >>> first = db.execute('SELECT * FROM combos').fetchone()
    >>> uuid.UUID(first[0]).version
    4
    >>> first[1:3], json.loads(first[3]), first[4:]
    (('w-code', 'llm'), ['cx/model', 'ag/fallback'], ('first', 'first'))
    >>> changed_combos(db, combos)
    []
    >>> _ = db.execute("UPDATE combos SET models = ?", (json.dumps(combos['w-code'], indent=2),))
    >>> changed_combos(db, combos)  # Whitespace-only differences do not write again.
    []
    >>> _ = db.execute("INSERT INTO combos VALUES ('other', 'personal', 'custom', '[]', 'old', 'old')")
    >>> write_changes(db, changed_combos(db, {'w-code': ['new/model']}), 'second')
    >>> row = db.execute("SELECT * FROM combos WHERE name = 'w-code'").fetchone()
    >>> (row[0] == first[0], json.loads(row[3]), row[4:])
    (True, ['new/model'], ('first', 'second'))
    >>> db.execute("SELECT kind, updatedAt FROM combos WHERE name = 'personal'").fetchone()
    ('custom', 'old')
    >>> _ = db.execute("UPDATE combos SET kind = NULL, models = 'broken' WHERE name = 'w-code'")
    >>> write_changes(db, changed_combos(db, combos), 'third')
    >>> db.execute("SELECT kind, updatedAt FROM combos WHERE name = 'w-code'").fetchone()
    ('llm', 'third')
    >>> db.commit()
    >>> db.execute('BEGIN').close()
    >>> write_changes(db, changed_combos(db, {'rollback': ['m']}), 'fourth')
    >>> db.rollback()
    >>> db.execute("SELECT COUNT(*) FROM combos WHERE name = 'rollback'").fetchone()
    (0,)
    >>> write_changes(db, changed_combos(db, {"name'); DROP TABLE combos; --": ['m']}), 'fifth')
    >>> db.execute('SELECT COUNT(*) FROM combos').fetchone()
    (3,)
    >>> db.close()
    >>> source, snapshot = sqlite3.connect(':memory:'), sqlite3.connect(':memory:')
    >>> write_changes(source, changed_combos(source, combos), 'before')
    >>> source.commit()
    >>> source.backup(snapshot)
    >>> write_changes(source, changed_combos(source, {'w-code': ['changed']}), 'after')
    >>> snapshot.execute("SELECT updatedAt FROM combos").fetchone()
    ('before',)
    >>> source.close()
    >>> snapshot.close()
    """
    connection.execute(SCHEMA)
    for name, models, existing_id in changes:
        connection.execute(UPSERT, (existing_id or str(uuid.uuid4()), name, models, timestamp, timestamp))


def backup_database(db_path, backup_dir):
    """Use SQLite's online backup API, including committed WAL pages.

    The caller holds BEGIN IMMEDIATE on a separate connection so another writer
    cannot change the database between this snapshot and the subsequent upserts.
    """
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    fd, filename = tempfile.mkstemp(prefix=db_path.name + ".bak-" + stamp + "-",
                                    suffix=".sqlite", dir=backup_dir)
    os.close(fd)  # mkstemp creates mode 600, even if the source contains secrets.
    backup_path = Path(filename)
    try:
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=10)) as source:
            with closing(sqlite3.connect(str(backup_path))) as destination:
                source.backup(destination)
    except BaseException:
        backup_path.unlink(missing_ok=True)
        raise
    return backup_path


def apply_combos(db_path, combos, backup_dir, dry_run=False):
    """No database creation; all requested updates commit or roll back together."""
    mode = "ro" if dry_run else "rw"
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=" + mode, uri=True, timeout=10)) as connection:
        connection.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
        try:
            changes = changed_combos(connection, combos)
            if not changes:
                print("Combos are already up to date.")
                return
            if dry_run:
                for name, _, existing_id in changes:
                    print(f"Would {'update' if existing_id is not None else 'create'} combo {ascii(name)}")
                print("Dry run: no database writes or backups.")
                return
            backup = backup_database(db_path, backup_dir)
            print(f"Database backup: {backup}")
            timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            write_changes(connection, changes, timestamp)
            connection.commit()
            for name, _, _ in changes:
                print(f"Applied combo {ascii(name)}")
        finally:
            if connection.in_transaction:
                connection.rollback()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward the local router key to a redirect destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def valid_message(value):
    """Do not report success for JSON error envelopes returned with HTTP 200.

    >>> valid_message({'type': 'message', 'content': [], 'stop_reason': 'end_turn'})
    True
    >>> [valid_message(value) for value in ({'error': 'bad'}, [], {'type': 'error'})]
    [False, False, False]
    >>> valid_message({'type': 'message', 'content': [], 'stop_reason': 'refusal'})
    False
    """
    return (isinstance(value, dict) and value.get("type") == "message"
            and isinstance(value.get("content"), list) and not value.get("error")
            and value.get("stop_reason") != "refusal")


def check_models(combos, key_path):
    """One small nonstreaming request per unique model; never print HTTP bodies."""
    models = list(dict.fromkeys(model for values in combos.values() for model in values))
    try:
        key = key_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        key = ""
    if not key or not all(33 <= ord(char) <= 126 for char in key):
        for model in models:
            print(f"ERR {ascii(model)} (missing or invalid 9router.key)")
        return False
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    success = True
    for model in models:
        label = ascii(model.replace(key, "[redacted]"))
        payload = json.dumps({"model": model, "max_tokens": 32, "stream": False,
                              "messages": [{"role": "user", "content": "Reply OK."}]}).encode()
        request = urllib.request.Request(MESSAGES_URL, data=payload, headers={
            "Content-Type": "application/json", "x-api-key": key,
            "anthropic-version": "2023-06-01", "x-9router-token-saver": "off",
        }, method="POST")
        try:
            with opener.open(request, timeout=30) as response:
                body = response.read(1024 * 1024 + 1)
                if not 200 <= response.status < 300 or len(body) > 1024 * 1024 or not valid_message(json.loads(body)):
                    raise ValueError("Invalid message response")
            print(f"OK {label}")
        except urllib.error.HTTPError as exc:
            print(f"ERR {label} (HTTP {exc.code})")
            exc.close()
            success = False
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
            # Exception messages and server bodies may echo credentials.
            print(f"ERR {label} (unavailable or invalid response)")
            success = False
    return success


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--db", type=Path, default=Path.home() / ".9router/db/data.sqlite",
                        help="existing 9router SQLite database")
    parser.add_argument("--dry-run", action="store_true", help="preview only; no writes or model requests")
    parser.add_argument("--check", action="store_true", help="also check each unique model (uses provider quota)")
    args = parser.parse_args(argv)
    try:
        combos = validate_combos(json.loads(Path(__file__).with_name("combos.json").read_text(encoding="utf-8"),
                                           object_pairs_hook=unique_object))
        db_path = args.db.expanduser().resolve()
        if not db_path.exists():
            print(f"9router database not found: {db_path}")
            print("Start 9router, finish its setup at http://localhost:20128, then rerun this command.")
            return 0
        if not db_path.is_file():
            raise ValueError("The database path must be a regular SQLite file.")
        apply_combos(db_path, combos, Path.home() / ".9router/db/backups", args.dry_run)
        if args.check:
            if args.dry_run:
                print("Dry run: would check each unique model; no requests sent.")
            elif not check_models(combos, Path.home() / ".claude/company/9router.key"):
                return 1
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
