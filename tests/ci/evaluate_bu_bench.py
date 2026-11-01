"""Evaluate the 100 tasks in ``tests/BU_Bench_V1.json``.

The runner uses one subprocess per task so browser sessions cannot interfere
with each other.  A task is successful only when Browser Use's built-in trace
judge returns ``verdict=True``.  Results are written to JSON while the final
summary is printed to stdout.

Example:
    python tests/ci/evaluate_bu_bench.py --max-parallel 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import warnings
from collections import Counter
from pathlib import Path
from typing import Any

BENCHMARK_PATH = Path(__file__).resolve().parents[1] / 'BU_Bench_V1.json'
REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_CATEGORIES = {
	'WebBenchREAD',
	'OM2W2',
	'InteractionTests',
	'GAIA',
	'BrowseComp',
}


def load_tasks(path: Path) -> list[dict[str, Any]]:
	"""Load and validate the benchmark before starting any browser sessions."""
	with path.open(encoding='utf-8') as file:
		tasks = json.load(file)
	if not isinstance(tasks, list) or len(tasks) != 100:
		raise ValueError(f'Expected exactly 100 tasks, found {len(tasks) if isinstance(tasks, list) else "non-list"}')

	counts = Counter(task.get('category') for task in tasks)
	if set(counts) != EXPECTED_CATEGORIES or set(counts.values()) != {20}:
		raise ValueError(f'Expected five categories with 20 tasks each, found {dict(counts)}')
	for task in tasks:
		if not all(isinstance(task.get(key), str) and task[key] for key in ('task_id', 'category', 'confirmed_task')):
			raise ValueError(f'Invalid task record: {task!r}')
	return tasks


async def run_one(task: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
	"""Run and judge one benchmark task in an isolated browser session."""
	from browser_use import Agent, BrowserProfile, BrowserSession, ChatBrowserUse

	logging.disable(logging.CRITICAL)
	warnings.filterwarnings('ignore')
	session = None
	try:
		api_key = os.getenv('BROWSER_USE_API_KEY')
		if not api_key:
			raise RuntimeError('BROWSER_USE_API_KEY is not set')

		llm = ChatBrowserUse(model=args.model, api_key=api_key)
		session = BrowserSession(
			browser_profile=BrowserProfile(
				headless=not args.headed,
				user_data_dir=None,
				chromium_sandbox=False,
			)
		)
		agent = Agent(
			task=task['confirmed_task'],
			llm=llm,
			browser_session=session,
			use_judge=True,
			judge_llm=llm,
			ground_truth=str(task['answer']) if 'answer' in task else None,
			task_id=task['task_id'],
		)
		history = await agent.run(max_steps=args.max_steps)
		judgement = history.judgement() or {}
		return {
			'task_id': task['task_id'],
			'category': task['category'],
			'success': judgement.get('verdict') is True,
			'judgement': judgement,
			'error': None,
		}
	except Exception as exc:
		return {
			'task_id': task['task_id'],
			'category': task['category'],
			'success': False,
			'judgement': None,
			'error': f'{type(exc).__name__}: {exc}',
		}
	finally:
		if session is not None:
			await session.kill()


async def run_subprocess(task: dict[str, Any], args: argparse.Namespace, semaphore: asyncio.Semaphore) -> dict[str, Any]:
	"""Run one task in a fresh Python process."""
	async with semaphore:
		payload = json.dumps(task, ensure_ascii=False)
		command = [
			sys.executable,
			str(Path(__file__).resolve()),
			'--task-json',
			payload,
			'--model',
			args.model,
			'--max-steps',
			str(args.max_steps),
		]
		if args.headed:
			command.append('--headed')
		process = await asyncio.create_subprocess_exec(
			*command,
			stdout=asyncio.subprocess.PIPE,
			stderr=asyncio.subprocess.PIPE,
			env={**os.environ, 'PYTHONPATH': os.pathsep.join([str(REPO_ROOT), *sys.path])},
		)
		stdout, stderr = await process.communicate()
		if process.returncode != 0:
			return {
				'task_id': task['task_id'],
				'category': task['category'],
				'success': False,
				'judgement': None,
				'error': f'subprocess exited {process.returncode}: {stderr.decode(errors="replace")[-500:]}',
			}
		try:
			return json.loads(stdout.decode().strip().splitlines()[-1])
		except (json.JSONDecodeError, IndexError) as exc:
			return {
				'task_id': task['task_id'],
				'category': task['category'],
				'success': False,
				'judgement': None,
				'error': f'Invalid subprocess output: {exc}',
			}


def print_summary(results: list[dict[str, Any]]) -> None:
	"""Print overall and per-category success rates."""
	def rate(passed: int, total: int) -> str:
		return f'{passed}/{total} ({passed / total * 100:.1f}%)' if total else '0/0 (0.0%)'

	passed = sum(result['success'] for result in results)
	print(f'TOTAL: {rate(passed, len(results))}')
	for category in sorted(EXPECTED_CATEGORIES):
		category_results = [result for result in results if result['category'] == category]
		print(f'{category}: {rate(sum(result["success"] for result in category_results), len(category_results))}')


async def run_all(tasks: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
	semaphore = asyncio.Semaphore(args.max_parallel)
	return await asyncio.gather(*(run_subprocess(task, args, semaphore) for task in tasks))


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument('--benchmark', type=Path, default=BENCHMARK_PATH)
	parser.add_argument('--output', type=Path, default=BENCHMARK_PATH.with_name('BU_Bench_V1_results.json'))
	parser.add_argument('--max-parallel', type=int, default=5)
	parser.add_argument('--max-steps', type=int, default=30)
	parser.add_argument('--model', default='bu-2-0-mini-preview')
	parser.add_argument('--headed', action='store_true')
	parser.add_argument('--task-json')
	return parser.parse_args()


def main() -> int:
	args = parse_args()
	if args.task_json:
		task = json.loads(args.task_json)
		print(json.dumps(asyncio.run(run_one(task, args)), ensure_ascii=False))
		return 0

	tasks = load_tasks(args.benchmark)
	if not os.getenv('BROWSER_USE_API_KEY'):
		print('BROWSER_USE_API_KEY is not set; no benchmark tasks were started.', file=sys.stderr)
		return 2
	results = asyncio.run(run_all(tasks, args))
	args.output.parent.mkdir(parents=True, exist_ok=True)
	args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
	print_summary(results)
	print(f'Results: {args.output.resolve()}')
	return 0 if all(result['error'] is None for result in results) else 1


if __name__ == '__main__':
	raise SystemExit(main())
