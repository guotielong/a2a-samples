"""Minimal caller for the Orchestrator Agent root POST endpoint (/).

We discovered only these routes are exposed:
  POST /
  GET /.well-known/agent-card.json
  GET /.well-known/agent.json

So orchestration must be triggered by POST /. This script sends a user query
as an A2A-style message and prints incremental bytes (supports streaming / chunked).

Usage (PowerShell):
  uv run --env-file .env examples/run_orchestrator_call.py --query "Plan a 5 day trip to Paris on a 3000 USD budget" --url http://localhost:10101/

If you want a simpler payload try --mode simple which sends {"input": "..."}.
Default mode builds a full message envelope similar to what downstream A2A nodes expect.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Optional, List

import click
import httpx


def build_message_envelope(query: str):
    return {
        "role": "user",
        "parts": [
            {"kind": "text", "text": query}
        ],
        "messageId": uuid.uuid4().hex,
    }


def build_jsonrpc(method: str, message_env: dict):
    return {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": method,
        "params": {"message": message_env},
    }


async def post_and_stream(url: str, payload: dict, timeout: int, show_raw: bool):
    """POST payload and attempt to consume a (possibly) streaming response.

    More defensive than previous version:
      * Prints chunks as they arrive.
      * Captures partial output if the server closes early (RemoteProtocolError).
      * Falls back to printing whatever JSON could be decoded.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        collected: list[bytes] = []
        try:
            async with client.stream("POST", url, json=payload) as resp:
                print(f"Status: {resp.status_code}")
                try:
                    async for chunk in resp.aiter_bytes():
                        if not chunk:
                            continue
                        collected.append(chunk)
                        if show_raw:
                            # Show raw bytes progressively
                            print(chunk.decode(errors="ignore"), end="", flush=True)
                except httpx.RemoteProtocolError as e:  # incomplete chunked read
                    print(f"\n[warn] RemoteProtocolError while streaming: {e}. Attempting to parse partial body...")
                # After stream ends (cleanly or with error), attempt JSON parse on collected bytes
                body_bytes = b"".join(collected)
                if not show_raw:
                    # Only pretty print JSON if not already dumping raw
                    try:
                        parsed = json.loads(body_bytes.decode(errors="ignore")) if body_bytes else {}
                        print(json.dumps(parsed, indent=2))
                    except Exception:
                        print(body_bytes.decode(errors="ignore"))
        except httpx.ReadTimeout:
            print("Timed out while reading response.")
        except Exception as e:
            print(f"Unexpected error performing request: {e}")


@click.command()
@click.option("--url", default="http://localhost:10101/", show_default=True, help="Orchestrator root POST endpoint")
@click.option("--query", required=True, help="User query to orchestrate")
@click.option("--method", default=None, help="Explicit JSON-RPC method name. If omitted, will try a list.")
@click.option("--timeout", default=120, show_default=True, help="Total HTTP timeout seconds")
@click.option("--show-raw", is_flag=True, help="Print raw response bytes (fallback if not JSON)")
def cli(url: str, query: str, method: Optional[str], timeout: int, show_raw: bool):
    message_env = build_message_envelope(query)
    candidates: List[str] = [method] if method else [
        # Try streaming first (may fail if server not implementing JSON-RPC streaming)
        "message/stream",
        # Fallback non-streaming variant
        "message/send",
    ]
    for m in candidates:
        if m is None:
            continue
        print(f"\n=== Trying method: {m} ===")
        payload = build_jsonrpc(m, message_env)
        print(json.dumps(payload, indent=2))
        try:
            asyncio.run(post_and_stream(url, payload, timeout, show_raw))
        except KeyboardInterrupt:
            raise
        except Exception as e:  # catch unexpected top-level errors so we can try next method
            print(f"[warn] Exception invoking method '{m}': {e}")
        print("(If error.code == -32601 (method not found), trying next candidate)")


if __name__ == "__main__":
    cli()
