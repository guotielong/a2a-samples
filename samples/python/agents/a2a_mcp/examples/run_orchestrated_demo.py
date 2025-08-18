"""Run an end-to-end orchestration demo against a running Orchestrator Agent.

This script is defensive: since the exact REST paths of the A2AStarletteApplication
may differ, it attempts a small set of candidate endpoints and payload shapes.

Workflow:
 1. Discover (optional) OpenAPI at /openapi.json to guess execute/poll paths.
 2. Submit a user query (planning request) to orchestrator (POST).
 3. Poll task status until completed OR timeout.
 4. Print intermediate status changes & final summary/artifacts.

Adjust environment with CLI options if your deployment uses different paths.

Example (PowerShell):
  uv run --env-file .env examples/run_orchestrated_demo.py --query "Plan a 5 day trip to Paris on a 3000 USD budget" \
    --base-url http://localhost:10101

If automatic detection fails, try specifying --execute-path and --status-path-format manually.

NOTE: This script is best-effort because the underlying a2a.server.* framework
is external to this repo. Edit CANDIDATE_EXECUTE_PATHS / PAYLOAD_BUILDERS if needed.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

import click
import httpx

DEFAULT_TIMEOUT_SEC = 180
POLL_INTERVAL_SEC = 2.0

# Heuristic candidate endpoints for creating a task
CANDIDATE_EXECUTE_PATHS = [
    "/tasks/execute",  # hypothetical explicit execute
    "/tasks",          # RESTful create
    "/execute",        # simple execute
]

# Heuristic candidate payload builders (try in order)
PAYLOAD_BUILDERS: List[Callable[[str], Dict[str, Any]]] = [
    lambda q: {"input": q},
    lambda q: {"query": q},
    lambda q: {"message": q},
]

# Heuristic fields we might find task id in
TASK_ID_FIELDS = ["task_id", "taskId", "id", "task", "data.id"]

# Candidate status path format strings; {task_id} placeholder is required
CANDIDATE_STATUS_PATHS = [
    "/tasks/{task_id}",
    "/tasks/{task_id}/status",
    "/status/{task_id}",
]

# Fields in a status response to inspect for state + completion
STATE_FIELDS = ["state", "task_state", "status.state", "status"]
COMPLETED_VALUES = {"completed", "done", "finished", "success"}
INPUT_REQUIRED_VALUES = {"input_required"}
WORKING_VALUES = {"working", "in_progress", "running"}

ARTIFACT_FIELDS = ["artifacts", "data.artifacts", "result.artifacts"]
SUMMARY_FIELDS = ["summary", "data.summary", "result.summary", "content", "final"]


def _dig(obj: Any, dotted: str) -> Any:
    parts = dotted.split(".")
    cur = obj
    for p in parts:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return None
    return cur


def _extract_first(obj: Dict[str, Any], candidates: List[str]) -> Any:
    for c in candidates:
        if "." in c:
            val = _dig(obj, c)
        else:
            val = obj.get(c)
        if val is not None:
            return val
    return None

@dataclass
class ExecutionContext:
    execute_path: str
    status_path_format: str
    task_id: str


async def discover_openapi(base_url: str) -> Dict[str, Any] | None:
    url = base_url.rstrip("/") + "/openapi.json"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(url)
            if r.status_code == 200:
                return r.json()
    except Exception:
        return None
    return None


def guess_paths_from_openapi(spec: Dict[str, Any]) -> tuple[str | None, str | None]:
    execute = None
    status_fmt = None
    paths = spec.get("paths", {})
    for p, meta in paths.items():
        lower = p.lower()
        if execute is None and any(k in lower for k in ["execute", "task"]):
            if "post" in meta:
                execute = p
        if status_fmt is None and "{task_id}" in p:
            status_fmt = p
    return execute, status_fmt


async def try_submit(base_url: str, path: str, query: str) -> tuple[str | None, Dict[str, Any] | None]:
    url = base_url.rstrip("/") + path
    async with httpx.AsyncClient(timeout=15) as client:
        for build in PAYLOAD_BUILDERS:
            payload = build(query)
            try:
                resp = await client.post(url, json=payload)
                status = resp.status_code
                text = resp.text[:500]
                print(f"[debug] POST {url} payload={payload} -> {status}")
                if status < 300:
                    data = resp.json() if resp.content else {}
                    task_id = _extract_first(data, TASK_ID_FIELDS)
                    if task_id:
                        return task_id, data
                else:
                    # show a snippet of error body for diagnostics
                    print(f"[debug] response body snippet: {text}")
            except Exception as e:
                print(f"[debug] exception POST {url} payload={payload}: {e}")
                continue
    return None, {"error": f"All payload builders failed for {path}"}


async def detect_and_execute(base_url: str, query: str, explicit_execute_path: str | None, explicit_status_fmt: str | None) -> ExecutionContext:
    # OpenAPI discovery
    spec = await discover_openapi(base_url) if not (explicit_execute_path and explicit_status_fmt) else None
    exec_path = explicit_execute_path
    status_fmt = explicit_status_fmt
    if spec and (not exec_path or not status_fmt):
        g_exec, g_status = guess_paths_from_openapi(spec)
        exec_path = exec_path or g_exec
        status_fmt = status_fmt or g_status

    # Fallback candidates
    exec_candidates = [exec_path] if exec_path else CANDIDATE_EXECUTE_PATHS
    status_candidates = [status_fmt] if status_fmt else CANDIDATE_STATUS_PATHS

    attempt_errors: list[str] = []
    for ep in exec_candidates:
        if not ep:
            continue
        task_id, raw = await try_submit(base_url, ep, query)
        if task_id:
            # pick first status format that looks plausible
            for sp in status_candidates:
                if "{task_id}" in sp:
                    return ExecutionContext(execute_path=ep, status_path_format=sp, task_id=str(task_id))
        else:
            attempt_errors.append(f"{ep}: {raw}")
    raise RuntimeError("Failed to submit task. Tried endpoints ->\n" + "\n".join(attempt_errors))


async def poll(base_url: str, ctx: ExecutionContext, timeout_sec: int):
    deadline = time.time() + timeout_sec
    url_fmt = base_url.rstrip("/") + ctx.status_path_format
    seen_state = None
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            if time.time() > deadline:
                raise TimeoutError("Polling timed out")
            url = url_fmt.format(task_id=ctx.task_id)
            try:
                r = await client.get(url)
                if r.status_code >= 400:
                    await asyncio.sleep(POLL_INTERVAL_SEC)
                    continue
                data = r.json() if r.content else {}
            except Exception:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                continue

            state = _extract_first(data, STATE_FIELDS)
            if state and state != seen_state:
                print(f"[state] {state}")
                seen_state = state

            if state and state.lower() in COMPLETED_VALUES:
                print("[completed]")
                artifacts = _extract_first(data, ARTIFACT_FIELDS)
                summary = _extract_first(data, SUMMARY_FIELDS)
                if artifacts:
                    print("Artifacts:")
                    print(json.dumps(artifacts, indent=2))
                if summary:
                    print("Summary:")
                    print(summary if isinstance(summary, str) else json.dumps(summary, indent=2))
                return
            await asyncio.sleep(POLL_INTERVAL_SEC)


@click.command()
@click.option("--base-url", default="http://localhost:10101", show_default=True)
@click.option("--query", required=True, help="User planning request")
@click.option("--execute-path", default=None, help="Override execute path (e.g. /tasks)")
@click.option("--status-path-format", default=None, help="Override status path format (must contain {task_id})")
@click.option("--timeout", default=DEFAULT_TIMEOUT_SEC, show_default=True)
def cli(base_url: str, query: str, execute_path: str | None, status_path_format: str | None, timeout: int):
    """Synchronous Click entrypoint that drives the async orchestration logic.

    (之前的版本把 click 命令本身声明为 async 并在 __main__ 中再 asyncio.run, 会导致
    'coroutine was never awaited' 警告。这里改成同步包装。)"""

    async def _run():
        ctx = await detect_and_execute(base_url, query, execute_path, status_path_format)
        print(f"Submitted task {ctx.task_id} via {ctx.execute_path}, polling {ctx.status_path_format}")
        await poll(base_url, ctx, timeout)

    asyncio.run(_run())


if __name__ == "__main__":
    cli()
