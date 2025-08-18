"""Minimal example to run the LangGraphPlannerAgent directly (without MCP find_agent).

Usage (PowerShell):

  uv run --env-file .env examples/run_planner_example.py --query "Plan a 5 day trip to Paris on a 3000 USD budget"

This will:
  * instantiate LangGraphPlannerAgent
  * invoke the agent with the provided query
  * stream intermediate tokens
  * print the final structured tasks when completed

Environment:
  GOOGLE_API_KEY (or GEMINI related key expected by init_api_key()) must be set in .env
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import click

from a2a_mcp.agents.langgraph_planner_agent import LangGraphPlannerAgent

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(message)s')
logger = logging.getLogger("planner_example")


async def run(query: str, session_id: str):
    agent = LangGraphPlannerAgent()

    logger.info("Streaming planner output ...")
    async for chunk in agent.stream(query, session_id, task_id="demo-task"):
        if chunk.get("response_type") == "text":
            logger.info(f"[partial] {chunk['content']}")
        if chunk.get("is_task_complete"):
            if chunk.get("response_type") == "data":
                logger.info("Planner completed. Structured tasks:")
                logger.info(json.dumps(chunk["content"], indent=2))
            break


@click.command()
@click.option("--query", required=True, help="User request for planning")
@click.option("--session-id", default="demo-session", help="Session / thread id")
def cli(query: str, session_id: str):
    asyncio.run(run(query, session_id))


if __name__ == "__main__":
    cli()
