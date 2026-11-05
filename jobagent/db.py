"""Database access for SQLite (local) and PostgreSQL (hosted, e.g. Neon on Vercel).

Application code writes portable SQL with `?` placeholders, COALESCE, ON CONFLICT and RETURNING.
The Database wrapper translates placeholders for PostgreSQL and returns rows as dicts."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
GENESIS_HASH = "0" * 64


class DatabaseError(Exception):
    pass


class IntegrityError(DatabaseError):
    """Unique / foreign-key / check constraint violation (dialect independent)."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _translate(sql: str) -> str:
    """'?' placeholders -> '%s' for psycopg. Literal '%' must be doubled for psycopg."""
    out, in_str = [], False
    for ch in sql:
        if ch == "'":
            in_str = not in_str
            out.append(ch)
        elif ch == "?" and not in_str:
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
    return "".join(out)


class Database:
    """Thin wrapper over a DB-API connection. Not shared across threads without the internal lock."""

    def __init__(self, url: str):
        self.url = url
        self._lock = threading.RLock()
        self._depth = 0
        if url.startswith(("postgres://", "postgresql://")):
            self.dialect = "postgres"
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as e:  # pragma: no cover
                raise DatabaseError("psycopg is required for PostgreSQL") from e
            try:
                # prepare_threshold=None: no server-side prepared statements, which pooled endpoints
                # (PgBouncer transaction mode, e.g. Neon's pooled URL) do not support.
                self._conn = psycopg.connect(url, autocommit=True, row_factory=dict_row, connect_timeout=15,
                                             prepare_threshold=None)
            except Exception as e:
                raise DatabaseError(f"Cannot connect to PostgreSQL: {type(e).__name__}") from e
            self._integrity_errors = (psycopg.errors.IntegrityError,)
            self._errors = (psycopg.Error,)
        else:
            self.dialect = "sqlite"
            path = url[len("sqlite:///"):] if url.startswith("sqlite:///") else url
            if path != ":memory:":
                try:
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    raise DatabaseError(f"Cannot create database directory: {e}") from e
            try:
                self._conn = sqlite3.connect(path, timeout=15, isolation_level=None, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA foreign_keys = ON")
                self._conn.execute("PRAGMA busy_timeout = 15000")
                if path != ":memory:":
                    self._conn.execute("PRAGMA journal_mode = WAL")
                self._conn.execute("PRAGMA synchronous = NORMAL")
            except sqlite3.DatabaseError as e:
                raise DatabaseError(f"Cannot open database: {e}") from e
            self._integrity_errors = (sqlite3.IntegrityError,)
            self._errors = (sqlite3.Error,)
            self.path = path

    # ------------------------------------------------------------ execution
    def _exec(self, sql: str, params: tuple | list = ()):
        if self.dialect == "postgres":
            sql = _translate(sql)
        try:
            return self._conn.execute(sql, tuple(params))
        except self._integrity_errors as e:
            raise IntegrityError(str(e).splitlines()[0]) from e
        except self._errors as e:
            raise DatabaseError(str(e).splitlines()[0] if str(e) else type(e).__name__) from e

    @staticmethod
    def _row(r) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, memoryview):
                d[k] = bytes(v)
        return d

    def one(self, sql: str, params: tuple | list = ()) -> dict | None:
        with self._lock:
            return self._row(self._exec(sql, params).fetchone())

    def all(self, sql: str, params: tuple | list = ()) -> list[dict]:
        with self._lock:
            return [self._row(r) for r in self._exec(sql, params).fetchall()]

    def scalar(self, sql: str, params: tuple | list = ()) -> Any:
        r = self.one(sql, params)
        return next(iter(r.values())) if r else None

    def run(self, sql: str, params: tuple | list = ()) -> int:
        """Execute a statement; returns affected row count."""
        with self._lock:
            cur = self._exec(sql, params)
            return cur.rowcount if cur.rowcount is not None else 0

    def insert(self, sql: str, params: tuple | list = ()) -> int:
        """INSERT ... RETURNING id -> new id. The statement must not include RETURNING itself."""
        with self._lock:
            r = self._exec(sql.rstrip().rstrip(";") + " RETURNING id", params).fetchone()
            return int(r["id"] if isinstance(r, dict) else r[0])

    def blob(self, data: bytes):
        return data if self.dialect == "postgres" else sqlite3.Binary(data)

    # ------------------------------------------------------------ transactions
    @contextmanager
    def transaction(self) -> Iterator["Database"]:
        """Write transaction. SQLite: BEGIN IMMEDIATE takes the write lock up-front so concurrent writers
        serialise. PostgreSQL: SERIALIZABLE is not needed because correctness relies on unique constraints
        and conditional UPDATEs. Nested calls join the outer transaction."""
        with self._lock:
            if self._depth > 0:
                self._depth += 1
                try:
                    yield self
                finally:
                    self._depth -= 1
                return
            self._exec("BEGIN IMMEDIATE" if self.dialect == "sqlite" else "BEGIN")
            self._depth = 1
            try:
                yield self
            except BaseException:
                self._depth = 0
                try:
                    self._exec("ROLLBACK")
                except DatabaseError:
                    pass
                raise
            else:
                self._depth = 0
                self._exec("COMMIT")

    @property
    def in_transaction(self) -> bool:
        return self._depth > 0

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------ migrations
    def migrate(self) -> list[str]:
        self._exec("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        applied = {r["version"] for r in self.all("SELECT version FROM schema_migrations")}
        done = []
        for path in sorted((MIGRATIONS_DIR / self.dialect).glob("*.sql")):
            version = path.stem
            if version in applied:
                continue
            statements = split_sql(path.read_text(encoding="utf-8"))
            # SQLite table rebuilds (CREATE new / copy / DROP / RENAME) must run with foreign keys OFF, otherwise
            # dropping a parent table cascades into its children. Integrity is re-verified before COMMIT.
            # (PRAGMA foreign_keys cannot change inside a transaction, so it is toggled around it.)
            fk_off = self.dialect == "sqlite"
            if fk_off:
                self._exec("PRAGMA foreign_keys = OFF")
            try:
                with self.transaction():
                    for st in statements:
                        self._exec(st)
                    if fk_off:
                        bad = self._exec("PRAGMA foreign_key_check").fetchall()
                        if bad:
                            raise DatabaseError(f"foreign key check failed for {len(bad)} row(s), e.g. {tuple(bad[0])}")
                    self._exec("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (version, iso()))
            except DatabaseError as e:
                raise DatabaseError(f"Migration {version} failed and was rolled back: {e}") from e
            finally:
                if fk_off:
                    self._exec("PRAGMA foreign_keys = ON")
            done.append(version)
        return done


def split_sql(sql: str) -> list[str]:
    """Split a migration file into statements, keeping trigger bodies and $$-quoted functions intact."""
    statements, buf, in_trigger, in_dollar = [], [], False, False
    for line in sql.splitlines():
        stripped = line.strip()
        if not buf and (not stripped or stripped.startswith("--")):
            continue
        if stripped.startswith("--"):
            continue
        buf.append(line)
        upper = stripped.upper()
        if line.count("$fn$") % 2 == 1:
            in_dollar = not in_dollar
        if upper.startswith("CREATE TRIGGER") and "EXECUTE FUNCTION" not in upper and "FOR EACH ROW" not in upper:
            in_trigger = True
        if in_dollar:
            continue
        if in_trigger:
            if upper.endswith("END;"):
                statements.append("\n".join(buf))
                buf, in_trigger = [], False
        elif stripped.endswith(";"):
            statements.append("\n".join(buf))
            buf = []
    if "".join(buf).strip():
        statements.append("\n".join(buf))
    return [s.strip().rstrip(";") for s in statements if s.strip()]


def open_db(url: str) -> Database:
    db = Database(url)
    db.migrate()
    return db


# ---------------------------------------------------------------- JSON helpers

def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def loads(s: str | None, default: Any = None) -> Any:
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- audit log

def _audit_hash(prev_hash: str, payload: dict) -> str:
    return hashlib.sha256((prev_hash + dumps(payload)).encode("utf-8")).hexdigest()


def audit(db: Database, *, actor: str, entity: str, entity_id: Any, action: str,
          from_status: str | None = None, to_status: str | None = None, detail: dict | None = None,
          idempotency_key: str | None = None) -> int | None:
    """Append a hash-chained audit event. With an idempotency key, repeating the same event is a no-op."""
    with db.transaction():
        if idempotency_key and db.one("SELECT id FROM audit_events WHERE idempotency_key = ?", (idempotency_key,)):
            return None
        if db.dialect == "postgres":
            # Serialise chain appends across connections.
            db.run("SELECT pg_advisory_xact_lock(774411)")
        last = db.one("SELECT hash FROM audit_events ORDER BY id DESC LIMIT 1")
        prev_hash = last["hash"] if last else GENESIS_HASH
        at = iso()
        eid = str(entity_id) if entity_id is not None else None
        payload = {"at": at, "actor": actor, "entity": entity, "entity_id": eid, "action": action,
                   "from_status": from_status, "to_status": to_status, "detail": detail or {}}
        h = _audit_hash(prev_hash, payload)
        return db.insert(
            """INSERT INTO audit_events(at, actor, entity, entity_id, action, from_status, to_status,
                   detail_json, idempotency_key, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (at, actor, entity, eid, action, from_status, to_status, dumps(detail or {}), idempotency_key,
             prev_hash, h))


def verify_audit_chain(db: Database) -> tuple[bool, int | None]:
    prev = GENESIS_HASH
    for r in db.all("SELECT * FROM audit_events ORDER BY id"):
        payload = {"at": r["at"], "actor": r["actor"], "entity": r["entity"], "entity_id": r["entity_id"],
                   "action": r["action"], "from_status": r["from_status"], "to_status": r["to_status"],
                   "detail": loads(r["detail_json"], {})}
        if r["prev_hash"] != prev or _audit_hash(prev, payload) != r["hash"]:
            return False, r["id"]
        prev = r["hash"]
    return True, None


# ---------------------------------------------------------------- cross-process lock

class LockHeld(Exception):
    pass


@contextmanager
def named_lock(db: Database, name: str, ttl_minutes: int = 30) -> Iterator[str]:
    """DB-backed mutex shared by every process/function using the same database.
    A lock past its expiry (crashed run) may be taken over."""
    owner = str(uuid.uuid4())
    now = utcnow()
    with db.transaction():
        row = db.one("SELECT owner, expires_at FROM locks WHERE name = ?", (name,))
        if row and (parse_iso(row["expires_at"]) or now) > now:
            raise LockHeld(f"Another run holds lock '{name}' until {row['expires_at']}.")
        # Conditional upsert: only replaces an expired lock, so two racers cannot both win.
        n = db.run(
            "INSERT INTO locks(name, owner, acquired_at, expires_at) VALUES (?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET owner=excluded.owner, acquired_at=excluded.acquired_at, "
            "expires_at=excluded.expires_at WHERE locks.expires_at <= ?",
            (name, owner, iso(now), iso(now + timedelta(minutes=ttl_minutes)), iso(now)))
        got = db.one("SELECT owner FROM locks WHERE name = ?", (name,))
        if not got or got["owner"] != owner:
            raise LockHeld(f"Another run acquired lock '{name}'.")
    try:
        yield owner
    finally:
        try:
            with db.transaction():
                db.run("DELETE FROM locks WHERE name = ? AND owner = ?", (name, owner))
        except DatabaseError:
            pass


# ---------------------------------------------------------------- backup / restore (SQLite)

def backup(db: Database, dest_dir: Path) -> Path:
    if db.dialect != "sqlite":
        raise DatabaseError("Use your PostgreSQL provider's backups / point-in-time restore (e.g. Neon branches).")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"jobagent-{utcnow().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}.db"
    target = sqlite3.connect(str(dest))
    try:
        db._conn.backup(target)
    finally:
        target.close()
    return dest


def restore(backup_path: Path, db_path: Path) -> None:
    """Restore a SQLite backup over the database file. Caller ensures no other process is running."""
    check = sqlite3.connect(str(backup_path))
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise DatabaseError("Backup failed integrity check; refusing to restore.")
    finally:
        check.close()
    for suffix in ("-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    import shutil
    shutil.copyfile(backup_path, db_path)
