# type:ignore
import asyncio
import json
import os

from contextlib import asynccontextmanager

import click

from fastmcp.utilities.logging import get_logger
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, ReadResourceResult


logger = get_logger(__name__)

env = {
    'GOOGLE_API_KEY': os.getenv('GOOGLE_API_KEY'),
}


@asynccontextmanager
async def init_session(host, port, transport):
    """Initializes and manages an MCP ClientSession based on the specified transport.

    This asynchronous context manager establishes a connection to an MCP server
    using either Server-Sent Events (SSE) or Standard I/O (STDIO) transport.
    It handles the setup and teardown of the connection and yields an active
    `ClientSession` object ready for communication.

    Args:
        host: The hostname or IP address of the MCP server (used for SSE).
        port: The port number of the MCP server (used for SSE).
        transport: The communication transport to use ('sse' or 'stdio').

    Yields:
        ClientSession: An initialized and ready-to-use MCP client session.

    Raises:
        ValueError: If an unsupported transport type is provided (implicitly,
                    as it won't match 'sse' or 'stdio').
        Exception: Other potential exceptions during client initialization or
                   session setup.
    """
    if transport == 'sse':
        url = f'http://{host}:{port}/sse'
        print(f'Connecting to MCP server at {url}')
        async with sse_client(url) as (read_stream, write_stream):
            async with ClientSession(
                read_stream=read_stream, write_stream=write_stream
            ) as session:
                logger.debug('SSE ClientSession created, initializing...')
                await session.initialize()
                logger.info('SSE ClientSession initialized successfully.')
                yield session
    elif transport == 'stdio':
        if not os.getenv('GOOGLE_API_KEY'):
            logger.error('GOOGLE_API_KEY is not set')
            raise ValueError('GOOGLE_API_KEY is not set')
        stdio_params = StdioServerParameters(
            command='uv',
            args=['run', 'a2a-mcp'],
            env=env,
        )
        async with stdio_client(stdio_params) as (read_stream, write_stream):
            async with ClientSession(
                read_stream=read_stream,
                write_stream=write_stream,
            ) as session:
                logger.debug('STDIO ClientSession created, initializing...')
                await session.initialize()
                logger.info('STDIO ClientSession initialized successfully.')
                yield session
    else:
        logger.error(f'Unsupported transport type: {transport}')
        raise ValueError(
            f"Unsupported transport type: {transport}. Must be 'sse' or 'stdio'."
        )


async def find_agent(session: ClientSession, query) -> CallToolResult:
    """Calls the 'find_agent' tool on the connected MCP server.

    Args:
        session: The active ClientSession.
        query: The natural language query to send to the 'find_agent' tool.

    Returns:
        The result of the tool call.
    """
    logger.info(f"Calling 'find_agent' tool with query: '{query[:50]}...'")
    return await session.call_tool(
        name='find_agent',
        arguments={
            'query': query,
        },
    )


def _extract_json_from_call(result: CallToolResult):  # type: ignore
    """Best-effort extraction of JSON/dict data from a CallToolResult.

    Handles the following cases gracefully:
    - content entry already a JSON-serializable dict
    - content.text contains valid JSON
    - empty or whitespace-only content -> returns None
    Logs warnings instead of raising to avoid cascading failures.
    """
    try:
        if not result.content:
            logger.warning('CallToolResult has no content entries')
            return None
        first = result.content[0]
        # Some MCP libs may attach already parsed objects under .data or .json
        if hasattr(first, 'json'):  # type: ignore[attr-defined]
            json_attr = getattr(first, 'json')
            if callable(json_attr):
                try:
                    return json_attr()
                except Exception:
                    logger.warning('Failed to call json() method', exc_info=True)
            elif json_attr:
                return json_attr
        # Check for model_dump_json method (newer Pydantic)
        if hasattr(first, 'model_dump_json'):  # type: ignore[attr-defined]
            try:
                return getattr(first, 'model_dump_json')()
            except Exception:
                logger.warning('Failed to call model_dump_json() method', exc_info=True)
        text = getattr(first, 'text', None)
        if text is None:
            logger.warning('First content entry has no text attribute')
            return None
        if isinstance(text, (dict, list)):
            return text
        if not isinstance(text, str):
            logger.warning(f'Unexpected content.text type {type(text)}')
            return None
        stripped = text.strip()
        if not stripped:
            logger.warning('Content text is empty after stripping')
            return None
        # If it already looks like JSON try to parse
        if stripped[0] in '{[':
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                logger.error('Failed to decode JSON from tool result text', exc_info=True)
                return None
        # Otherwise return raw text
        return stripped
    except Exception:
        logger.error('Unexpected error extracting JSON from call result', exc_info=True)
        return None


def _extract_json_from_resource(result: ReadResourceResult):  # type: ignore
    try:
        if not result.contents:
            logger.warning('ReadResourceResult has no contents entries')
            return None
        first = result.contents[0]
        text = getattr(first, 'text', None)
        if isinstance(text, (dict, list)):
            return text
        if not isinstance(text, str):
            return None
        stripped = text.strip()
        if not stripped:
            return None
        if stripped[0] in '{[':
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                logger.error('Failed to decode JSON from resource result text', exc_info=True)
                return None
        return stripped
    except Exception:
        logger.error('Unexpected error extracting JSON from resource result', exc_info=True)
        return None


async def find_resource(session: ClientSession, resource) -> ReadResourceResult:
    """Reads a resource from the connected MCP server.

    Args:
        session: The active ClientSession.
        resource: The URI of the resource to read (e.g., 'resource://agent_cards/list').

    Returns:
        The result of the resource read operation.
    """
    logger.info(f'Reading resource: {resource}')
    return await session.read_resource(resource)


async def search_flights(session: ClientSession) -> CallToolResult:
    """Calls the 'search_flights' tool on the connected MCP server.

    Args:
        session: The active ClientSession.
        query: The natural language query to send to the 'search_flights' tool.

    Returns:
        The result of the tool call.
    """
    # TODO: Implementation pending
    logger.info("Calling 'search_flights' tool'")
    return await session.call_tool(
        name='search_flights',
        arguments={
            'departure_airport': 'SFO',
            'arrival_airport': 'LHR',
            'start_date': '2025-06-03',
            'end_date': '2025-06-09',
        },
    )


async def search_hotels(session: ClientSession) -> CallToolResult:
    """Calls the 'search_hotels' tool on the connected MCP server.

    Args:
        session: The active ClientSession.
        query: The natural language query to send to the 'search_hotels' tool.

    Returns:
        The result of the tool call.
    """
    # TODO: Implementation pending
    logger.info("Calling 'search_hotels' tool'")
    return await session.call_tool(
        name='search_hotels',
        arguments={
            'location': 'A Suite room in St Pancras Square in London',
            'check_in_date': '2025-06-03',
            'check_out_date': '2025-06-09',
        },
    )


async def query_db(session: ClientSession) -> CallToolResult:
    """Calls the 'query' tool on the connected MCP server.

    Args:
        session: The active ClientSession.
        query: The natural language query to send to the 'query_db' tool.

    Returns:
        The result of the tool call.
    """
    logger.info("Calling 'query_db' tool'")
    return await session.call_tool(
        name='query_travel_data',
        arguments={
            'query': "SELECT id, name, city, hotel_type, room_type, price_per_night FROM hotels WHERE city='London'",
        },
    )


# Test util
async def main(host, port, transport, query, resource, tool):
    """Main asynchronous function to connect to the MCP server and execute commands.

    Used for local testing.

    Args:
        host: Server hostname.
        port: Server port.
        transport: Connection transport ('sse' or 'stdio').
        query: Optional query string for the 'find_agent' tool.
        resource: Optional resource URI to read.
        tool: Optional tool name to execute. Valid options are:
            'search_flights', 'search_hotels', or 'query_db'.
    """
    logger.info('Starting Client to connect to MCP')
    async with init_session(host, port, transport) as session:
        if query:
            result = await find_agent(session, query)
            data = _extract_json_from_call(result)
            try:
                logger.info(json.dumps(data, indent=2) if data is not None else data)
            except TypeError as e:
                logger.error(f'Failed to serialize data to JSON: {e}')
                logger.info(f'Raw data: {data}')
        if resource:
            result = await find_resource(session, resource)
            data = _extract_json_from_resource(result)
            try:
                logger.info(json.dumps(data, indent=2) if data is not None else data)
            except TypeError as e:
                logger.error(f'Failed to serialize data to JSON: {e}')
                logger.info(f'Raw data: {data}')
        if tool:
            if tool == 'search_flights':
                results = await search_flights(session)
                logger.info(results.model_dump())
            if tool == 'search_hotels':
                result = await search_hotels(session)
                data = _extract_json_from_call(result)
                try:
                    logger.info(json.dumps(data, indent=2) if data is not None else data)
                except TypeError as e:
                    logger.error(f'Failed to serialize data to JSON: {e}')
                    logger.info(f'Raw data: {data}')
            if tool == 'query_db':
                result = await query_db(session)
                data = _extract_json_from_call(result)
                try:
                    logger.info(json.dumps(data, indent=2) if data is not None else data)
                except TypeError as e:
                    logger.error(f'Failed to serialize data to JSON: {e}')
                    logger.info(f'Raw data: {data}')


# Command line tester
@click.command()
@click.option('--host', default='localhost', help='SSE Host')
@click.option('--port', default='10100', help='SSE Port')
@click.option('--transport', default='sse', help='MCP Transport')
@click.option('--find_agent', help='Query to find an agent')
@click.option('--resource', help='URI of the resource to locate')
@click.option('--tool_name', type=click.Choice(['search_flights', 'search_hotels', 'query_db']),
              help='Tool to execute: search_flights, search_hotels, or query_db')
def cli(host, port, transport, find_agent, resource, tool_name):
    """A command-line client to interact with the Agent Cards MCP server."""
    asyncio.run(main(host, port, transport, find_agent, resource, tool_name))


if __name__ == '__main__':
    cli()
