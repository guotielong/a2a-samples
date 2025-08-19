"""Example: invoke Orchestrator Agent using the A2A Python SDK (no manual JSON-RPC).

This replaces the previous low-level httpx streaming script with the high-level
`A2AClient` used elsewhere in the sample (see `workflow.py`). It will:
  * Load an Agent Card (default: `agent_cards/orchestrator_agent.json`).
  * Attempt a streaming `send_message`; if server lacks streaming support, fall back.
  * Print incremental TaskStatus updates and artifacts.
  * Handle interactive input when the agent requires additional information.
  * Finally print any completion / summary payload.

Usage (PowerShell):
  uv run --env-file .env examples/run_orchestrator_call.py \
    --query "Plan a 5 day trip to Paris on a 3000 USD budget"

When the agent requests additional information (e.g., departure city), you will be
prompted to provide input interactively.

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

from a2a.client.client import ClientConfig
from a2a.client.client_factory import ClientFactory
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
        # Configure new factory/client
        config = ClientConfig(streaming=True, httpx_client=httpx_client)
        factory = ClientFactory(config)
        client = factory.create(agent_card)

        # Build message (new API expects a Message object)
        from a2a.types import Message, Part, TextPart, Role

        message = Message(
            messageId=str(uuid4()),
            role=Role.user,
            parts=[Part(root=TextPart(text=query))],
        )

        print(f"Connecting to {agent_card.url} (auto streaming if supported)...")

        # Helper for task id extraction (reusing previous logic)
        def _find_task_ids(obj) -> set[str]:  # type: ignore
            ids: set[str] = set()
            try:
                if hasattr(obj, "model_dump"):
                    obj = obj.model_dump()
            except Exception:
                pass
            if isinstance(obj, dict):
                for k, v in obj.items():
                    kl = k.lower()
                    if kl in ("taskid", "task_id") and isinstance(v, str):
                        ids.add(v)
                    if kl == "task" and isinstance(v, dict):
                        for cand_key in ("id", "taskId", "task_id"):
                            cand = v.get(cand_key)
                            if isinstance(cand, str):
                                ids.add(cand)
                    if isinstance(v, (dict, list)):
                        ids |= _find_task_ids(v)
            elif isinstance(obj, list):
                for it in obj:
                    ids |= _find_task_ids(it)
            return ids

        async def handle_user_input(task_id: str, question: str) -> bool:
            """Handle user input when task requires input. Returns True if input was provided."""
            print(f"\n[INPUT REQUIRED] {question}")
            print("Please enter your response (or 'quit' to exit):")
            
            # Get user input
            user_input = input("> ").strip()
            
            if user_input.lower() in ['quit', 'exit', 'q']:
                print("[INFO] User chose to exit.")
                return False
            
            if not user_input:
                print("[WARNING] Empty input provided, skipping...")
                return False
            
            # Send user input as a new message
            try:
                input_message = Message(
                    messageId=str(uuid4()),
                    role=Role.user,
                    parts=[Part(root=TextPart(text=user_input))],
                )
                
                print(f"[INFO] Sending user input: {user_input}")
                
                # Continue the conversation with the user input
                async for event in client.send_message(input_message):
                    if isinstance(event, tuple):
                        task, update = event
                        if update and isinstance(update, TaskStatusUpdateEvent):
                            status = update.status
                            if status.state == TaskState.input_required:
                                # Extract question from the status message
                                question_text = "Please provide additional information."
                                if status.message and status.message.parts:
                                    part0 = status.message.parts[0].root
                                    msg_content = getattr(part0, "text", None) or getattr(part0, "data", None)
                                    if msg_content:
                                        try:
                                            import json
                                            parsed = json.loads(msg_content) if isinstance(msg_content, str) and msg_content.startswith('{') else {"question": msg_content}
                                            question_text = parsed.get("question", msg_content)
                                        except:
                                            question_text = str(msg_content)
                                
                                # Recursively handle more input if needed
                                return await handle_user_input(task.id, question_text)
                            else:
                                # Continue processing other events
                                msg_txt = None
                                if status.message and status.message.parts:
                                    part0 = status.message.parts[0].root
                                    msg_txt = getattr(part0, "text", None) or getattr(part0, "data", None)
                                print(f"[status] state={status.state} msg={msg_txt} task_id={task.id}")
                        elif update and isinstance(update, TaskArtifactUpdateEvent):
                            art = update.artifact
                            print(f"[artifact] name={art.name} type={art.parts[0].root.__class__.__name__} task_id={task.id}")
                    else:
                        # Final message response
                        if raw and hasattr(event, 'model_dump_json'):
                            print(event.model_dump_json())
                        else:
                            print("[message]", event)
                
                return True
                
            except Exception as e:
                print(f"[ERROR] Failed to send user input: {e}")
                return False

        first_task_id: str | None = None
        mismatch_detected = False

        try:
            async for event in client.send_message(message):
                # event is either (Task, UpdateEvent) | Message
                if isinstance(event, tuple):
                    task, update = event
                    ids = {task.id} if getattr(task, 'id', None) else set()
                    if first_task_id is None and ids:
                        first_task_id = next(iter(ids))
                        print(f"[trace] first_task_id={first_task_id}")
                    elif first_task_id and any(tid != first_task_id for tid in ids):
                        mismatch_detected = True
                        print(f"[trace][WARNING] task_id mismatch: expected={first_task_id} got={ids}")

                    if update is None:
                        # Initial Task object
                        print(f"[task] state={task.status.state} id={task.id}")
                    else:
                        if isinstance(update, TaskStatusUpdateEvent):
                            status = update.status
                            msg_txt = None
                            if status.message and status.message.parts:
                                part0 = status.message.parts[0].root
                                msg_txt = getattr(part0, "text", None) or getattr(part0, "data", None)
                            
                            print(f"[status] state={status.state} msg={msg_txt} task_id={task.id}")
                            
                            # Handle input required state
                            if status.state == TaskState.input_required:
                                question = "Please provide additional information."
                                if msg_txt:
                                    try:
                                        # Try to parse JSON to extract question
                                        import json
                                        parsed = json.loads(msg_txt) if isinstance(msg_txt, str) and msg_txt.startswith('{') else {"question": msg_txt}
                                        question = parsed.get("question", msg_txt)
                                    except:
                                        question = str(msg_txt)
                                
                                # Handle user input
                                input_success = await handle_user_input(task.id, question)
                                if not input_success:
                                    print("[INFO] Exiting due to user request or input failure.")
                                    break
                                    
                        elif isinstance(update, TaskArtifactUpdateEvent):
                            art = update.artifact
                            print(f"[artifact] name={art.name} type={art.parts[0].root.__class__.__name__} task_id={task.id}")
                        else:
                            print(f"[event] untyped={update} task_id={task.id}")
                else:
                    # A final Message response instead of task stream
                    if raw and hasattr(event, 'model_dump_json'):
                        print(event.model_dump_json())
                    else:
                        print("[message]", event)
        except A2AClientHTTPError as e:
            print(f"Client error: {e}")
        finally:
            if mismatch_detected:
                print("[trace] Completed with task id mismatches detected above.")
            elif first_task_id:
                print(f"[trace] Completed. All observed task ids matched {first_task_id}.")
            else:
                print("[trace] Completed. No task id observed.")


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
