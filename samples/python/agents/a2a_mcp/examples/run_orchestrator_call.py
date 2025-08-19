"""Example: invoke Orchestrator Agent using the A2A Python SDK (no manual JSON-RPC).

This replaces the previous low-level httpx streaming script with the high-level
`A2AClient` used elsewhere in the sample (see `workflow.py`). It will:
  * Load an Agent Card (default: `agent_cards/orchestrator_agent.json`).
  * Attempt a streaming `send_message`; if server lacks streaming support, fall back.
  * Print incremental TaskStatus updates and artifacts.
  * Finally print any completion / summary payload.

Usage (PowerShell):
  uv run --env-file .env examples/run_orchestrator_call.py \
    --query "Plan a 5 day trip to Paris on a 3000 USD budget"

Optional:
  --agent-card path/to/card.json  (override default)
  --raw (print each raw JSON envelope line)

Run:
uv run --env-file .env examples/run_orchestrator_call.py --query "Plan a 5 day trip to Paris on a 3000 USD budget"
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import click
import httpx

from a2a.client import A2AClient
from a2a.client.errors import A2AClientHTTPError
from a2a.types import (
    AgentCard,
    MessageSendParams,
    SendMessageRequest,
    SendMessageSuccessResponse,
    SendStreamingMessageRequest,
    SendStreamingMessageSuccessResponse,
    TaskArtifactUpdateEvent,
    TaskStatusUpdateEvent,
    TaskState,
)


def load_agent_card(path: Path) -> AgentCard:
    data = json.loads(path.read_text(encoding="utf-8"))
    return AgentCard(**data)


async def run(query: str, card_path: Path, raw: bool, timeout: int):  # noqa: C901 (clarity)
    if not card_path.exists():
        raise FileNotFoundError(f"Agent card not found: {card_path}")
    agent_card = load_agent_card(card_path)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as httpx_client:
        client = A2AClient(httpx_client, agent_card)
        payload = {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": query}],
                "messageId": uuid4().hex,
            }
        }
        stream_req = SendStreamingMessageRequest(
            id=str(uuid4()), params=MessageSendParams(**payload)
        )
        print(f"Connecting to {agent_card.url} (streaming attempt)...")
        try:
            async for envelope in client.send_message_streaming(stream_req):
                # envelope.root.* is the success / event container
                if raw:
                    if hasattr(envelope, "model_dump_json"):
                        print(envelope.model_dump_json())
                    else:
                        print(envelope)
                    continue
                root = envelope.root
                if isinstance(root, SendStreamingMessageSuccessResponse):
                    result = root.result
                    if isinstance(result, TaskStatusUpdateEvent):
                        status = result.status
                        msg_txt = None
                        if status.message and status.message.parts:
                            part0 = status.message.parts[0].root
                            msg_txt = getattr(part0, "text", None) or getattr(part0, "data", None)
                        print(f"[status] state={status.state} msg={msg_txt}")
                        if status.state == TaskState.completed:
                            # final completed chunk for this internal task
                            pass
                    elif isinstance(result, TaskArtifactUpdateEvent):
                        art = result.artifact
                        print(f"[artifact] name={art.name} type={art.parts[0].root.__class__.__name__}")
                    else:
                        # Could be final summary dict emitted by orchestrator (non-typed)
                        print("[event]", root)
                else:
                    print("[unhandled]", root)
        except A2AClientHTTPError as e:
            print(f"Streaming attempt failed ({e}); falling back to non-streaming send_message")
            non_stream_req = SendMessageRequest(
                id=str(uuid4()), params=MessageSendParams(**payload)
            )
            resp = await client.send_message(non_stream_req)
            if raw:
                print(resp.model_dump_json())
            elif isinstance(resp, SendMessageSuccessResponse):
                print("[response]", resp.root)
            else:
                print(resp)


@click.command()
@click.option("--query", required=True, help="User query to orchestrate")
@click.option(
    "--agent-card",
    "agent_card_path",
    default="agent_cards/orchestrator_agent.json",
    show_default=True,
    help="Path to orchestrator Agent Card JSON",
)
@click.option("--timeout", default=120, show_default=True, help="HTTP client timeout seconds")
@click.option("--raw", is_flag=True, help="Print raw JSON envelopes")
def cli(query: str, agent_card_path: str, timeout: int, raw: bool):
    asyncio.run(run(query, Path(agent_card_path), raw, timeout))


if __name__ == "__main__":  # pragma: no cover
    cli()
