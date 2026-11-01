"""Evaluation result storage for Browser Use.

``EvalResultStore`` persists one row per agent run into a local SQLite file
(task, verdict, failure reason, model, commit hash, timings, trace path).
Failed runs are additionally exported as JSON traces under ``traces_dir`` so
they can be replayed or inspected offline.

The module only depends on the standard library; the optional ``history``
argument passed to :meth:`EvalResultStore.record` only needs a ``to_json``
method (implemented by ``AgentHistoryList``).
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_results (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_name        TEXT    NOT NULL,
    task             TEXT    NOT NULL DEFAULT '',
    verdict          INTEGER,             -- NULL = not judged, 0/1 = fail/pass
    failure_reason   TEXT    NOT NULL DEFAULT '',
    self_reported    INTEGER,
    judged           INTEGER NOT NULL DEFAULT 0,
    model            TEXT    NOT NULL DEFAULT '',
    model_provider   TEXT    NOT NULL DEFAULT '',
    commit_hash      TEXT    NOT NULL DEFAULT '',
    source           TEXT    NOT NULL DEFAULT 'agent',
    duration_seconds REAL,
    n_steps          INTEGER,
    max_steps        INTEGER,
    trace_path       TEXT    NOT NULL DEFAULT '',
    metadata         TEXT    NOT NULL DEFAULT '{}',
    created_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_task       ON eval_results (task_name, created_at);
CREATE INDEX IF NOT EXISTS idx_eval_created    ON eval_results (created_at);
"""

_commit_cache: dict[str, str] = {}


def _git_commit_hash(repo_root: str | Path | None = None) -> str:
    """Return the short HEAD commit hash of the repository, or '' if unknown."""
    root = str(Path(repo_root).resolve()) if repo_root else None
    if root in _commit_cache:
        return _commit_cache[root]
    try:
        if root:
            out = subprocess.run(
                ['git', '-C', root, 'rev-parse', '--short', 'HEAD'],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        else:
            out = subprocess.run(
                ['git', 'rev-parse', '--short', 'HEAD'],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        value = out.stdout.strip() if out.returncode == 0 else ''
    except Exception:
        value = ''
    if root is not None:
        _commit_cache[root] = value
    return value


def _safe_filename(name: str) -> str:
    clean = re.sub(r'[^A-Za-z0-9_.-]+', '_', name or 'task').strip('_')
    return clean[:80] or 'task'


def _now_utc() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class EvalResultStore:
    """SQLite-backed store of per-run evaluation results."""

    def __init__(
        self,
        db_path: str | Path = 'data/eval_results.db',
        *,
        traces_dir: str | Path | None = None,
        repo_root: str | Path | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.traces_dir = (
            Path(traces_dir)
            if traces_dir is not None
            else self.db_path.parent / 'traces'
        )
        self.repo_root = repo_root
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- writing -----------------------------------------------------------

    def record(
        self,
        *,
        task_name: str,
        task: str = '',
        verdict: bool | None = None,
        failure_reason: str = '',
        self_reported: bool | None = None,
        judged: bool | None = None,
        model: str = '',
        model_provider: str = '',
        commit_hash: str | None = None,
        source: str = 'agent',
        duration_seconds: float | None = None,
        n_steps: int | None = None,
        max_steps: int | None = None,
        history: Any = None,
        traces_dir: str | Path | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Persist one run. Returns the new row id.

        When ``verdict is False`` and ``history`` exposes ``to_json()``, the
        trajectory is exported as a JSON trace and its path is stored.
        """
        if judged is None:
            judged = verdict is not None
        commit = commit_hash
        if commit is None:
            commit = _git_commit_hash(self.repo_root)

        cur = self.conn.cursor()
        now = _now_utc()
        cur.execute(
            """
            INSERT INTO eval_results (
                task_name, task, verdict, failure_reason, self_reported, judged,
                model, model_provider, commit_hash, source, duration_seconds,
                n_steps, max_steps, trace_path, metadata, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)
            """,
            (
                task_name,
                task,
                (1 if verdict is True else 0) if verdict is not None else None,
                failure_reason,
                (1 if self_reported is True else 0) if self_reported is not None else None,
                1 if judged else 0,
                model,
                model_provider,
                commit,
                source,
                duration_seconds,
                n_steps,
                max_steps,
                json.dumps(metadata or {}, ensure_ascii=False),
                now,
            ),
        )
        self.conn.commit()
        row_id = int(cur.lastrowid)

        # Export a trace for failed runs so they can be inspected offline.
        trace_path = ''
        if verdict is False and history is not None and hasattr(history, 'to_json'):
            try:
                trace_path = self._export_trace(
                    history,
                    row_id=row_id,
                    task_name=task_name,
                    traces_dir=Path(traces_dir) if traces_dir else self.traces_dir,
                )
                cur.execute(
                    'UPDATE eval_results SET trace_path = ? WHERE id = ?',
                    (str(trace_path), row_id),
                )
                self.conn.commit()
            except Exception:
                trace_path = ''
        return row_id

    @staticmethod
    def _export_trace(
        history: Any,
        *,
        row_id: int,
        task_name: str,
        traces_dir: Path,
    ) -> Path:
        traces_dir.mkdir(parents=True, exist_ok=True)
        stamp = _now_utc().replace(':', '').replace('-', '')
        name = f'{_safe_filename(task_name)}_{row_id}_{stamp}'
        path = traces_dir / f'{name}.json'
        payload = history.to_json() if hasattr(history, 'to_json') else {}
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        return path

    # -- reading -----------------------------------------------------------

    def list_runs(
        self,
        *,
        task_name: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return recent runs (newest first) as dicts."""
        clauses: list[str] = []
        params: list[Any] = []
        if task_name:
            clauses.append('task_name = ?')
            params.append(task_name)
        if since:
            clauses.append('created_at >= ?')
            params.append(since)
        if until:
            clauses.append('created_at < ?')
            params.append(until)
        where = f'WHERE {" AND ".join(clauses)}' if clauses else ''
        sql = f'SELECT * FROM eval_results {where} ORDER BY created_at DESC'
        if limit:
            sql += ' LIMIT ? OFFSET ?'
            params.extend([limit, offset])
        cur = self.conn.cursor()
        cur.execute(sql, params)
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            d['verdict'] = (d['verdict'] == 1) if d['verdict'] is not None else None
            d['self_reported'] = (
                (d['self_reported'] == 1) if d['self_reported'] is not None else None
            )
            try:
                d['metadata'] = json.loads(d.get('metadata') or '{}')
            except Exception:
                d['metadata'] = {}
            rows.append(d)
        return rows

    def summary(
        self,
        *,
        task_name: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate statistics per task over the given window."""
        rows = self.list_runs(task_name=task_name, since=since, until=until)
        per_task: dict[str, dict[str, Any]] = {}
        for d in rows:
            key = d['task_name']
            acc = per_task.setdefault(
                key,
                {'runs': 0, 'passes': 0, 'fails': 0, 'unjudged': 0, 'traces': 0},
            )
            acc['runs'] += 1
            v = d['verdict']
            if v is True:
                acc['passes'] += 1
            elif v is False:
                acc['fails'] += 1
                if d.get('trace_path'):
                    acc['traces'] += 1
            else:
                acc['unjudged'] += 1
        totals = {'runs': 0, 'passes': 0, 'fails': 0, 'unjudged': 0, 'traces': 0}
        for acc in per_task.values():
            for k in totals:
                totals[k] += acc[k]
        return {'per_task': per_task, 'total': totals}
