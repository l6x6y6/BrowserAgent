"""Regression CLI over evaluation results stored by :class:`EvalResultStore`.

Example usage::

    python -m browser_use.evaluation.cli report --db data/eval_results.db
    python -m browser_use.evaluation.cli compare --db data/eval_results.db \\
        --baseline 2026-09-01T00:00:00Z --until 2026-10-01T00:00:00Z
    python -m browser_use.evaluation.cli traces --db data/eval_results.db --failures
"""

from __future__ import annotations

import argparse
from pathlib import Path

from browser_use.evaluation.store import EvalResultStore


def _pct(part: int, total: int) -> str:
    if total <= 0:
        return '-'
    return f'{100.0 * part / total:.1f}%'


def _fmt_summary(total: dict[str, int]) -> str:
    runs = total['runs']
    return (
        f'runs={runs}  pass={total["passes"]}  fail={total["fails"]}  '
        f'unjudged={total["unjudged"]}  traces={total["traces"]}  '
        f'pass_rate={_pct(total["passes"], runs)}'
    )


def _task_table(per_task: dict[str, dict[str, int]]) -> list[list[str]]:
    header = ['task', 'runs', 'pass', 'fail', 'unjudged', 'pass_rate']
    body = []
    for name, acc in sorted(per_task.items()):
        body.append(
            [
                name,
                str(acc['runs']),
                str(acc['passes']),
                str(acc['fails']),
                str(acc['unjudged']),
                _pct(acc['passes'], acc['runs']),
            ]
        )
    return [header, *body]


def _print_table(rows: list[list[str]]) -> None:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print('  '.join(cell.ljust(widths[i]) for i, cell in enumerate(r)))


def cmd_report(args: argparse.Namespace) -> int:
    store = EvalResultStore(args.db)
    s = store.summary(task_name=args.task)
    print(f'DB: {Path(args.db).resolve()}')
    print(_fmt_summary(s['total']))
    print()
    _print_table(_task_table(s['per_task']))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    store = EvalResultStore(args.db)
    base_since, base_until = None, None
    head_since, head_until = args.since, args.until

    if args.baseline:
        # single split point: everything before == baseline, everything after == head
        base_until = args.baseline
        head_since = args.baseline

    base = store.summary(since=base_since, until=base_until)
    head = store.summary(since=head_since, until=head_until)
    if args.baseline is None and not (args.since or args.until):
        print('compare needs --baseline <split-timestamp> or --since/--until filters.')
        return 2

    print(f'Baseline: {_fmt_summary(base["total"])}')
    print(f'Head:     {_fmt_summary(head["total"])}')
    print()
    names = sorted(set(base['per_task']) | set(head['per_task']))
    rows = [['task', 'base_pass', 'head_pass', 'delta']]
    for name in names:
        b = base['per_task'].get(name, {'passes': 0, 'runs': 0})
        h = head['per_task'].get(name, {'passes': 0, 'runs': 0})
        delta = ''
        if b['runs'] and h['runs']:
            diff = (h['passes'] / h['runs']) - (b['passes'] / b['runs'])
            delta = f'{diff * 100:+.1f}pp'
        rows.append(
            [
                name,
                f"{b['passes']}/{b['runs']}",
                f"{h['passes']}/{h['runs']}",
                delta,
            ]
        )
    _print_table(rows)
    return 0


def cmd_traces(args: argparse.Namespace) -> int:
    store = EvalResultStore(args.db)
    runs = store.list_runs(task_name=args.task, since=args.since, until=args.until)
    rows = [['id', 'task', 'verdict', 'trace_path']]
    for r in runs:
        if args.failures and r['verdict'] is not False:
            continue
        verdict = 'pass' if r['verdict'] is True else ('fail' if r['verdict'] is False else '-')
        rows.append([str(r['id']), r['task_name'], verdict, r['trace_path'] or ''])
    _print_table(rows)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='browser-use-eval',
        description='Inspect Browser Use evaluation results (SQLite).',
    )
    sub = parser.add_subparsers(dest='command', required=True)

    p_report = sub.add_parser('report', help='per-task pass-rate summary')
    p_report.add_argument('--db', default='data/eval_results.db')
    p_report.add_argument('--task', default=None, help='filter by task name')
    p_report.set_defaults(func=cmd_report)

    p_cmp = sub.add_parser('compare', help='compare two time windows / regressions')
    p_cmp.add_argument('--db', default='data/eval_results.db')
    p_cmp.add_argument('--baseline', default=None,
                       help='split timestamp: everything before is baseline, after is head')
    p_cmp.add_argument('--since', default=None, help='head window start (ISO)')
    p_cmp.add_argument('--until', default=None, help='head window end (ISO)')
    p_cmp.set_defaults(func=cmd_compare)

    p_traces = sub.add_parser('traces', help='list persisted trace files')
    p_traces.add_argument('--db', default='data/eval_results.db')
    p_traces.add_argument('--task', default=None)
    p_traces.add_argument('--since', default=None)
    p_traces.add_argument('--until', default=None)
    p_traces.add_argument('--failures', action='store_true', help='only failed runs')
    p_traces.set_defaults(func=cmd_traces)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
