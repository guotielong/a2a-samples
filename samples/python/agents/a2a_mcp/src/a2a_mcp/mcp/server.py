# type: ignore
import json
import os
import sqlite3
import traceback
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from llama_index.embeddings.dashscope import DashScopeEmbedding

from a2a_mcp.common.utils import init_api_key
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.logging import get_logger

logger = get_logger(__name__)
AGENT_CARDS_DIR = 'agent_cards'
MODEL = 'text-embedding-v2'  # DashScope embedding model per user sample
SQLLITE_DB = 'travel_agency.db'
PLACES_API_URL = 'https://places.googleapis.com/v1/places:searchText'

_embedder: "DashScopeEmbedding | None" = None  # lazy init in generate_embedding


def _mock_places_response(query: str, max_results: int = 5) -> dict:
    """Generate a small, deterministic mock response that resembles
    the Google Places `places:searchText` response shape.

    This is used when no API key is available so the rest of the
    system can be exercised without network access or credentials.
    """
    # Keep ids deterministic so tests can rely on them
    results = []
    base_names = [
        f"{query} Central",
        f"{query} Plaza",
        f"{query} Hotel",
        f"{query} Station",
        f"{query} Services",
    ]
    for i, name in enumerate(base_names[:max_results], start=1):
        results.append(
            {
                "id": f"mock-{i}",
                "displayName": name,
                "formattedAddress": f"{i} {name} St, Example City",
            }
        )

    return {"places": results}
def generate_embedding(texts: str | list[str]) -> list[float] | list[list[float]]:
    """Generate embedding(s) using DashScopeEmbedding with lazy initialization.

    Usage:
    - str -> list[float]
    - list[str] -> list[list[float]] in same order
    """
    global _embedder
    if _embedder is None:
        key = os.getenv('DASHSCOPE_API_KEY') or os.getenv('OPENAI_API_KEY')
        if not key:
            raise RuntimeError('DashScope API key not set (DASHSCOPE_API_KEY/OPENAI_API_KEY).')
        try:
            _embedder = DashScopeEmbedding(model_name=MODEL)
        except Exception as e:  # pragma: no cover - network issues
            logger.error(f'Failed to initialize DashScopeEmbedding: {e}', exc_info=True)
            raise RuntimeError('Failed to initialize DashScopeEmbedding') from e
    try:
        if isinstance(texts, str):
            return _embedder.get_text_embedding_batch([texts])[0]  # type: ignore[arg-type]
        return _embedder.get_text_embedding_batch(texts)  # type: ignore[arg-type]
    except Exception as e:  # pragma: no cover
        logger.error(f'Failed embedding generation: {e}', exc_info=True)
        raise


def load_agent_cards():
    """Loads agent card data from JSON files within a specified directory.

    Returns:
        A list containing JSON data from an agent card file found in the specified directory.
        Returns an empty list if the directory is empty, contains no '.json' files,
        or if all '.json' files encounter errors during processing.
    """
    card_uris = []
    agent_cards = []
    dir_path = Path(AGENT_CARDS_DIR)
    if not dir_path.is_dir():
        logger.error(
            f'Agent cards directory not found or is not a directory: {AGENT_CARDS_DIR}'
        )
        return agent_cards

    logger.info(f'Loading agent cards from card repo: {AGENT_CARDS_DIR}')

    for filename in os.listdir(AGENT_CARDS_DIR):
        if filename.lower().endswith('.json'):
            file_path = dir_path / filename

            if file_path.is_file():
                logger.info(f'Reading file: {filename}')
                try:
                    with file_path.open('r', encoding='utf-8') as f:
                        data = json.load(f)
                        card_uris.append(
                            f'resource://agent_cards/{Path(filename).stem}'
                        )
                        agent_cards.append(data)
                except json.JSONDecodeError as jde:
                    logger.error(f'JSON Decoder Error {jde}')
                except OSError as e:
                    logger.error(f'Error reading file {filename}: {e}.')
                except Exception as e:
                    logger.error(
                        f'An unexpected error occurred processing {filename}: {e}',
                        exc_info=True,
                    )
    logger.info(
        f'Finished loading agent cards. Found {len(agent_cards)} cards.'
    )
    return card_uris, agent_cards


def build_agent_card_embeddings() -> pd.DataFrame:
    """Loads agent cards, generates embeddings for them, and returns a DataFrame.

    Returns:
        Optional[pd.DataFrame]: A Pandas DataFrame containing the original
        'agent_card' data and their corresponding 'Embeddings'. Returns None
        if no agent cards were loaded initially or if an exception occurred
        during the embedding generation process.
    """
    card_uris, agent_cards = load_agent_cards()
    logger.info('Generating Embeddings for agent cards')
    try:
        if agent_cards:
            df = pd.DataFrame(
                {'card_uri': card_uris, 'agent_card': agent_cards}
            )
            # Prepare JSON string versions for embedding
            json_blobs = [json.dumps(card) for card in df['agent_card']]
            embeddings = generate_embedding(json_blobs)  # returns list[list[float]]
            df['card_embeddings'] = embeddings
            logger.info('Done generating embeddings for agent cards')
            return df
        logger.info('No agent cards loaded; skipping embedding generation')
    except Exception as e:
        logger.error(f'An unexpected error occurred : {e}.', exc_info=True)
        return None


def serve(host, port, transport):  # noqa: PLR0915
    """Initializes and runs the Agent Cards MCP server.

    Args:
        host: The hostname or IP address to bind the server to.
        port: The port number to bind the server to.
        transport: The transport mechanism for the MCP server (e.g., 'stdio', 'sse').

    Raises:
        ValueError: If required provider API keys are not set.
    """
    init_api_key()
    logger.info('Starting Agent Cards MCP Server')
    mcp = FastMCP('agent-cards', host=host, port=port)

    df = build_agent_card_embeddings()

    @mcp.tool(
        name='find_agent',
        description='Finds the most relevant agent card based on a natural language query string.',
    )
    def find_agent(query: str) -> dict:
        """Finds the most relevant agent card based on a query string.

        This function takes a user query, typically a natural language question or a task generated by an agent,
        generates its embedding, and compares it against the
        pre-computed embeddings of the loaded agent cards. It uses the dot
        product to measure similarity and identifies the agent card with the
        highest similarity score.

        Args:
            query: The natural language query string used to search for a
                   relevant agent.

        Returns:
            The json representing the agent card deemed most relevant
            to the input query based on embedding similarity.
        """
        # Validate that embeddings dataframe exists and has data
        if df is None or df.empty:
            raise RuntimeError(
                'No agent cards available. Ensure agent card JSON files exist in the agent_cards directory.'
            )

        # Generate query embedding
        query_embedding = generate_embedding(query)

        # Compute similarity (dot product) and select best match
        try:
            dot_products = np.dot(
                np.stack(df['card_embeddings']), query_embedding
            )
        except Exception as e:
            logger.error(
                f'Error computing similarity for query "{query}": {e}',
                exc_info=True,
            )
            raise

        best_match_index = int(np.argmax(dot_products))
        logger.debug(
            f'Found best match at index {best_match_index} with score {dot_products[best_match_index]}'
        )
        # Return the dict directly; FastMCP will serialize it to JSON text content.
        return df.iloc[best_match_index]['agent_card']

    @mcp.tool()
    def query_places_data(query: str):
        """Query Google Places."""
        logger.info(f'Search for places : {query}')
        # Allow forcing mock responses for testing: set MOCK_PLACES=1
        use_mock = os.getenv('MOCK_PLACES', '0') == '1'
        api_key = os.getenv('GOOGLE_PLACES_API_KEY')

        if not api_key or use_mock:
            if not api_key:
                logger.info('GOOGLE_PLACES_API_KEY is not set, using mock places response')
            else:
                logger.info('MOCK_PLACES=1 detected, using mock places response')
            return _mock_places_response(query, max_results=10)

        headers = {
            'X-Goog-Api-Key': api_key,
            'X-Goog-FieldMask': 'places.id,places.displayName,places.formattedAddress',
            'Content-Type': 'application/json',
        }
        payload = {
            'textQuery': query,
            'languageCode': 'en',
            'maxResultCount': 10,
        }

        try:
            response = requests.post(
                PLACES_API_URL, headers=headers, json=payload
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as http_err:
            logger.info(f'HTTP error occurred: {http_err}')
            logger.info(f'Response content: {response.text}')
        except requests.exceptions.ConnectionError as conn_err:
            logger.info(f'Connection error occurred: {conn_err}')
        except requests.exceptions.Timeout as timeout_err:
            logger.info(f'Timeout error occurred: {timeout_err}')
        except requests.exceptions.RequestException as req_err:
            logger.info(
                f'An unexpected error occurred with the request: {req_err}'
            )
        except json.JSONDecodeError:
            logger.info(
                f'Failed to decode JSON response. Raw response: {response.text}'
            )

        return {'places': []}

    @mcp.tool()
    def query_travel_data(query: str) -> dict:
        """ "name": "query_travel_data",
        "description": "Retrieves the most up-to-date, ariline, hotel and car rental availability. Helps with the booking.
        This tool should be used when a user asks for the airline ticket booking, hotel or accommodation booking, or car rental reservations.",
        "parameters": {
            "type": "object",
            "properties": {
            "query": {
                "type": "string",
                "description": "A SQL to run against the travel database."
            }
            },
            "required": ["query"]
        }
        """
        # The above is to influence gemini to pickup the tool.
        logger.info(f'Query sqllite : {query}')

        if not query or not query.strip().upper().startswith('SELECT'):
            raise ValueError(f'In correct query {query}')

        try:
            with sqlite3.connect(SQLLITE_DB) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(query)
                rows = cursor.fetchall()
                result = {'results': [dict(row) for row in rows]}
                # Return dict (not JSON string) for consistency with other tools
                return result
        except Exception as e:
            logger.error(f'Exception running query {e}')
            logger.error(traceback.format_exc())
            if 'no such column' in e:
                return {
                    'error': f'Please check your query, {e}. Use the table schema to regenerate the query'
                }
            return {'error': {e}}

    @mcp.resource('resource://agent_cards/list', mime_type='application/json')
    def get_agent_cards() -> dict:
        """Retrieves all loaded agent cards as a json / dictionary for the MCP resource endpoint.

        This function serves as the handler for the MCP resource identified by
        the URI 'resource://agent_cards/list'.

        Returns:
            A json / dictionary structured as {'agent_cards': [...]}, where the value is a
            list containing all the loaded agent card dictionaries. Returns
            {'agent_cards': []} if the data cannot be retrieved.
        """
        resources = {}
        logger.info('Starting read resources')
        resources['agent_cards'] = df['card_uri'].to_list()
        return resources

    @mcp.resource(
        'resource://agent_cards/{card_name}', mime_type='application/json'
    )
    def get_agent_card(card_name: str) -> dict:
        """Retrieves an agent card as a json / dictionary for the MCP resource endpoint.

        This function serves as the handler for the MCP resource identified by
        the URI 'resource://agent_cards/{card_name}'.

        Returns:
            A json / dictionary
        """
        resources = {}
        logger.info(
            f'Starting read resource resource://agent_cards/{card_name}'
        )
        resources['agent_card'] = (
            df.loc[
                df['card_uri'] == f'resource://agent_cards/{card_name}',
                'agent_card',
            ]
        ).to_list()

        return resources

    logger.info(
        f'Agent cards MCP Server at {host}:{port} and transport {transport}'
    )
    mcp.run(transport=transport)
