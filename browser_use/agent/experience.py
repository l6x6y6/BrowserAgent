"""Cross-run experience memory for Browser Use agents.

This module turns past runs into reusable knowledge:

* ``ExperienceMemory`` is a tiny SQLite-backed store that keeps distilled
  lessons/strategies keyed by a task fingerprint (keyword based). It is used
  at runtime to inject lessons from similar past tasks into the system prompt
  and afterwards to learn from the run that just finished.
* ``distill_lesson`` is a deterministic (LLM-free) function that compresses a
  failed trajectory into a short lesson + strategy. This is the "few-shot
  distillation" step: trajectories become decision rules that are deduplicated
  inside the store and optionally exported to a human-readable markdown file
  (``export_few_shots``) that can be loaded back as a dynamic prompt file.

Design notes:

* Only the Python standard library is required (``sqlite3``); no browser, LLM
  or vector database is involved.
* Matching is intentionally cheap: an exact keyword-fingerprint match first,
  then a Jaccard overlap on the normalized keyword sets. No embeddings, so it
  is deterministic and unit-test friendly.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    from pydantic import BaseModel

    HAS_PYDANTIC = True
except Exception:  # pragma: no cover - pydantic is a hard dependency of this project
    HAS_PYDANTIC = False

    class BaseModel:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)


# ---------------------------------------------------------------------------
# Tokenization / fingerprinting
# ---------------------------------------------------------------------------

_ENGLISH_STOPWORDS = {
    'a', 'an', 'the', 'and', 'or', 'but', 'of', 'to', 'in', 'on', 'for', 'with',
    'at', 'by', 'from', 'as', 'is', 'are', 'was', 'were', 'be', 'been', 'this',
    'that', 'these', 'those', 'it', 'its', 'do', 'does', 'did', 'have', 'has',
    'had', 'can', 'could', 'should', 'would', 'will', 'shall', 'may', 'might',
    'must', 'not', 'no', 'so', 'if', 'then', 'than', 'too', 'very', 'just',
    'please', 'find', 'go', 'get', 'try', 'make', 'using', 'use', 'me', 'my',
    'you', 'your', 'we', 'our', 'us', 'about', 'into', 'onto', 'over', 'under',
    'up', 'down', 'out', 'off', 'all', 'any', 'some', 'each', 'every', 'both',
    'more', 'most', 'other', 'such', 'only', 'own', 'same', 'after', 'before',
    'because', 'while', 'during', 'when', 'where', 'which', 'who', 'whom',
    'what', 'how', 'why', 'i',
}

_WORD_RE = re.compile(r'[a-zA-Z][a-zA-Z0-9]*')


def _normalize_task_keywords(task: str) -> tuple[str, ...]:
    """Return a de-duplicated, sorted tuple of keywords extracted from a task.

    English words are lower-cased and stop-word filtered; CJK text is chunked
    into bigrams so that short Chinese tasks still share overlapping tokens.
    """
    text = (task or '').lower()
    words: set[str] = set()
    for m in _WORD_RE.finditer(text):
        w = m.group(0)
        if len(w) >= 2 and w not in _ENGLISH_STOPWORDS:
            words.add(w)
    # Chinese bigrams over each contiguous CJK run
    runs = re.findall(r'[\u4e00-\u9fff]+', text)
    for run in runs:
        if len(run) == 1:
            words.add(run)
        else:
            words.update(run[i : i + 2] for i in range(len(run) - 1))
    return tuple(sorted(words))


def task_fingerprint(task: str) -> str:
    """Stable fingerprint for a task string (keyword-set hash)."""
    keywords = _normalize_task_keywords(task)
    if not keywords:
        return 'task:' + hashlib.sha1((task or '').lower().encode('utf-8')).hexdigest()[:16]
    raw = '|'.join(keywords)
    return 'task:' + hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]


def _keyword_overlap(a: Sequence[str], b: Sequence[str]) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    if inter == 0:
        return 0.0
    return inter / max(1.0, float(len(sa | sb)))


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class ExperienceEntry(BaseModel):
    """A single distilled experience to persist."""

    task: str
    outcome: str = 'failure'  # 'failure' | 'success'
    lesson: str
    strategy: str = ''
    keywords: tuple[str, ...] = ()
    source: str = 'agent'  # agent | judge | manual | fewshot
    model: str = ''
    sample_actions: str = ''
    sample_errors: str = ''

    @property
    def task_fingerprint(self) -> str:
        return task_fingerprint(self.task)


class ExperienceLesson(BaseModel):
    """A lesson returned by ``ExperienceMemory.query``."""

    id: int
    task: str
    task_fingerprint: str
    outcome: str
    lesson: str
    strategy: str
    count: int
    source: str
    model: str
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# Deterministic lesson distillation (few-shot extraction from trajectories)
# ---------------------------------------------------------------------------


def distill_lesson(
    task: str,
    *,
    outcome: str = 'failure',
    actions: Sequence[str] = (),
    errors: Sequence[str] = (),
    agent_run_error: str | None = None,
    judge_reason: str | None = None,
    n_steps: int = 0,
    model: str = '',
) -> ExperienceEntry:
    """Compress a finished trajectory into one lesson/strategy pair.

    This is deliberately rule-based (no LLM call): it keeps the loop cheap and
    deterministic. The *lesson* describes what happened; the *strategy*
    encodes a suggested behavioural fix for next time.
    """
    error_bits = [e for e in (agent_run_error, judge_reason) if e]
    if not error_bits:
        error_bits = [e for e in errors if e][-2:]
    error_text = ' | '.join(error_bits) if error_bits else 'task was not completed'
    truncated = ' > '.join(actions[-6:]) if actions else '(no actions recorded)'

    def _first_error_kind(text: str) -> str:
        low = (text or '').lower()
        if any(token in low for token in (
            'timeout',
            'timed out',
            'deadline exceeded',
        )):
            return 'timeout'
        if any(token in low for token in (
            'captcha',
            'challenge',
            'verification',
            'two-factor',
            '2fa',
            'login required',
            'sign in',
            'authentication required',
        )):
            return 'authentication-required'
        if any(token in low for token in (
            '401',
            '403',
            '429',
            '500',
            '502',
            '503',
            'forbidden',
            'rate limit',
            'access denied',
            'connection refused',
            'dns',
            'network unreachable',
        )):
            return 'access-or-network-blocked'
        if any(token in low for token in (
            'not found',
            'no element',
            'selector',
            'stale element',
            'wrong page',
            'unexpected page',
        )):
            return 'page-or-element-unavailable'
        return 'action-or-task-failed'

    kind = _first_error_kind(error_text)

    strategy_map = {
        'timeout': (
            'Break the task into smaller steps and avoid waiting on operations that can hang; '
            'after each action verify the page actually responded before continuing.'
        ),
        'page-or-element-unavailable': (
            'Verify the current URL, title, and page content before interacting. Wait for the '
            'page to load and prefer stable selectors.'
        ),
        'authentication-required': (
            'Pause automatic retries and ask the user to complete the login or verification '
            'step. Resume only after confirming that the authenticated page is available.'
        ),
        'access-or-network-blocked': (
            'Check permissions, rate limits, network connectivity, proxy settings, and session '
            'state before retrying the request.'
        ),
        'action-or-task-failed': (
            'Inspect the failed action and its parameters. If the same action fails twice, '
            'switch to a different approach instead of repeating it.'
        ),
    }

    lesson = (
        f"Task {task[:140]!r} ended as {outcome} after {n_steps} step(s) "
        f"(kind={kind}). Error context: {error_text[:300]}. "
        f"Last actions: {truncated[:400]}."
    )
    return ExperienceEntry(
        task=task,
        outcome=outcome,
        lesson=lesson,
        strategy=strategy_map[kind],
        source='agent',
        model=model,
        sample_actions=truncated[:400],
        sample_errors=error_text[:300],
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiences (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    task_fingerprint  TEXT    NOT NULL,
    lesson_fingerprint TEXT   NOT NULL,
    task              TEXT    NOT NULL,
    keywords          TEXT    NOT NULL DEFAULT '[]',
    outcome           TEXT    NOT NULL DEFAULT 'failure',
    lesson            TEXT    NOT NULL,
    strategy          TEXT    NOT NULL DEFAULT '',
    count             INTEGER NOT NULL DEFAULT 1,
    source            TEXT    NOT NULL DEFAULT 'agent',
    model             TEXT    NOT NULL DEFAULT '',
    sample_actions    TEXT    NOT NULL DEFAULT '',
    sample_errors     TEXT    NOT NULL DEFAULT '',
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_experience_dedup
    ON experiences (task_fingerprint, lesson_fingerprint);
CREATE INDEX IF NOT EXISTS idx_experience_keywords
    ON experiences (task_fingerprint, count DESC);
"""


class ExperienceMemory:
    """SQLite-backed store of distilled lessons keyed by task fingerprint.

    Parameters
    ----------
    db_path:
        Path of the SQLite file. Created (with schema) on first use.
    min_similarity:
        Minimum keyword Jaccard overlap for a past lesson to count as
        "similar" to the current task during ``query``.
    export_path:
        Default file used by ``export_few_shots``. Defaults to
        ``<db_dir>/few_shot_lessons.md``.
    """

    def __init__(
        self,
        db_path: str | Path = 'data/experience.db',
        *,
        min_similarity: float = 0.25,
        export_path: str | Path | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.min_similarity = max(0.0, min(1.0, min_similarity))
        if export_path is None:
            export_path = self.db_path.parent / 'few_shot_lessons.md'
        self.export_path = Path(export_path)
        self._conn: sqlite3.Connection | None = None

    # -- connection helpers ------------------------------------------------

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

    def __del__(self) -> None:  # pragma: no cover - defensive
        try:
            self.close()
        except Exception:
            pass

    # -- writing -----------------------------------------------------------

    @staticmethod
    def _lesson_fingerprint(lesson: str) -> str:
        norm = re.sub(r'\s+', ' ', (lesson or '').lower()).strip()
        return hashlib.sha1(norm.encode('utf-8')).hexdigest()[:16]

    def add(self, entry: ExperienceEntry) -> int:
        """Insert ``entry`` or bump the count of an identical lesson.

        Returns the row id.
        """
        keywords = tuple(entry.keywords) or _normalize_task_keywords(entry.task)
        now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        fp = entry.task_fingerprint
        lesson_fp = self._lesson_fingerprint(entry.lesson)
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT id, count FROM experiences
            WHERE task_fingerprint = ? AND lesson_fingerprint = ?
            """,
            (fp, lesson_fp),
        )
        row = cur.fetchone()
        if row is not None:
            cur.execute(
                """
                UPDATE experiences
                SET count = count + 1, updated_at = ?,
                    outcome = ?, strategy = ?, model = ?,
                    sample_actions = ?, sample_errors = ?
                WHERE id = ?
                """,
                (
                    now,
                    entry.outcome,
                    entry.strategy,
                    entry.model,
                    entry.sample_actions,
                    entry.sample_errors,
                    row['id'],
                ),
            )
            self.conn.commit()
            return int(row['id'])
        cur.execute(
            """
            INSERT INTO experiences (
                task_fingerprint, lesson_fingerprint, task, keywords, outcome,
                lesson, strategy, count, source, model, sample_actions,
                sample_errors, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
            """,
            (
                fp,
                lesson_fp,
                entry.task,
                json.dumps(list(keywords)),
                entry.outcome,
                entry.lesson,
                entry.strategy,
                entry.source,
                entry.model,
                entry.sample_actions,
                entry.sample_errors,
                now,
                now,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def learn_from_run(
        self,
        entry: ExperienceEntry,
        *,
        export: bool = False,
    ) -> int:
        """Persist a distilled lesson and (optionally) refresh the few-shot file."""
        row_id = self.add(entry)
        if export:
            self.export_few_shots()
        return row_id

    # -- reading -----------------------------------------------------------

    def query(self, task: str, *, limit: int = 5) -> list[ExperienceLesson]:
        """Return lessons for similar past tasks, best matches first.

        Exact fingerprint matches are returned before overlapping-keyword
        matches. Results are ordered by count (frequency) and recency.
        """
        if not task or not (task.strip()):
            return []
        keywords = _normalize_task_keywords(task)
        fp = task_fingerprint(task)
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT * FROM experiences
            WHERE task_fingerprint = ?
            ORDER BY count DESC, updated_at DESC
            LIMIT ?
            """,
            (fp, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        if len(rows) < limit:
            # fuzzy pass: shared keywords with enough overlap
            cur.execute(
                'SELECT * FROM experiences ORDER BY count DESC, updated_at DESC LIMIT 500'
            )
            scored: list[tuple[float, dict]] = []
            for r in cur.fetchall():
                d = dict(r)
                try:
                    stored: list[str] = json.loads(d.get('keywords') or '[]')
                except Exception:
                    stored = list(_normalize_task_keywords(d.get('task') or ''))
                overlap = _keyword_overlap(keywords, stored)
                if overlap >= self.min_similarity:
                    scored.append((overlap, d))
            scored.sort(key=lambda x: x[0], reverse=True)
            rows.extend(d for _, d in scored[: limit - len(rows)])
        return [self._to_lesson(d) for d in rows]

    def count(self) -> int:
        cur = self.conn.cursor()
        cur.execute('SELECT COUNT(*) AS c FROM experiences')
        return int(cur.fetchone()['c'])

    # -- few-shot export ---------------------------------------------------

    def export_few_shots(self, path: str | Path | None = None) -> Path:
        """Write deduplicated lessons to a markdown file (dynamic prompt file).

        The exported file is what powers the "few-shot decision rules" prompt
        when the store is not directly available (see ``load_few_shots``).
        """
        target = Path(path) if path is not None else self.export_path
        target.parent.mkdir(parents=True, exist_ok=True)
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT * FROM experiences
            ORDER BY count DESC, updated_at DESC
            """
        )
        rows = [dict(r) for r in cur.fetchall()]
        lines = [
            '# Few-Shot Experience Lessons (auto-generated)',
            '',
            f'> {len(rows)} deduplicated lessons. Generated for '
            f'{time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}.',
            '',
        ]
        for i, d in enumerate(rows, 1):
            lines.extend(
                [
                    f'## {i}. [{d["outcome"]}] count={d["count"]} source={d["source"]}',
                    f'- Fingerprint: `{d["task_fingerprint"]}`',
                    f'- Task: {d["task"][:160]}',
                    f'- Lesson: {d["lesson"]}',
                    f'- Strategy: {d["strategy"] or "(none)"}',
                    f'- Model: {d["model"] or "unknown"}',
                    '',
                ]
            )
        target.write_text('\n'.join(lines), encoding='utf-8')
        return target

    @staticmethod
    def load_few_shots(path: str | Path) -> str:
        """Read an exported few-shot file back into a prompt block."""
        p = Path(path)
        if not p.exists():
            return ''
        return p.read_text(encoding='utf-8')

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _to_lesson(d: dict[str, Any]) -> ExperienceLesson:
        if HAS_PYDANTIC:
            return ExperienceLesson(
                id=int(d['id']),
                task=d['task'],
                task_fingerprint=d['task_fingerprint'],
                outcome=d['outcome'],
                lesson=d['lesson'],
                strategy=d['strategy'],
                count=int(d['count']),
                source=d['source'],
                model=d['model'],
                created_at=d['created_at'],
                updated_at=d['updated_at'],
            )
        return ExperienceLesson(**d)  # type: ignore[call-arg]

    def __iter__(self) -> Iterable[dict[str, Any]]:  # pragma: no cover - convenience
        cur = self.conn.cursor()
        cur.execute('SELECT * FROM experiences ORDER BY updated_at DESC')
        return iter([dict(r) for r in cur.fetchall()])
