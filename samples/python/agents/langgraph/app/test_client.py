import logging
import os

from uuid import uuid4

import httpx

from a2a.client import A2ACardResolver, ClientFactory

from a2a.types import (
    AgentCard,
    Message,
)
from a2a.utils.constants import (
    AGENT_CARD_WELL_KNOWN_PATH,
    EXTENDED_AGENT_CARD_PATH,
)


async def main() -> None:
    # Configure logging to show INFO level messages
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)  # Get a logger instance

    # --8<-- [start:A2ACardResolver]

    # Prefer env override; default to server's default port (10001)
    base_url = os.getenv('A2A_BASE_URL', 'http://localhost:8080')

    # Disable system proxies to ensure localhost traffic doesn't go through an HTTP proxy
    # and add a reasonable timeout for local development.
    async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(30.0)) as httpx_client:
        # Initialize A2ACardResolver
        resolver = A2ACardResolver(
            httpx_client=httpx_client,
            base_url=base_url,
            # agent_card_path uses default, extended_agent_card_path also uses default
        )
        # --8<-- [end:A2ACardResolver]

    # Fetch Public Agent Card and Initialize Client
        final_agent_card_to_use: AgentCard | None = None

        try:
            logger.info(
                f'Attempting to fetch public agent card from: {base_url}{AGENT_CARD_WELL_KNOWN_PATH}'
            )
            _public_card = (
                await resolver.get_agent_card()
            )  # Fetches from default public path
            logger.info('Successfully fetched public agent card:')
            logger.info(
                _public_card.model_dump_json(indent=2, exclude_none=True)
            )
            final_agent_card_to_use = _public_card
            logger.info(
                '\nUsing PUBLIC agent card for client initialization (default).'
            )

            if _public_card.supports_authenticated_extended_card:
                try:
                    logger.info(
                        '\nPublic card supports authenticated extended card. '
                        'Attempting to fetch from: '
                        f'{base_url}{EXTENDED_AGENT_CARD_PATH}'
                    )
                    auth_headers_dict = {
                        'Authorization': 'Bearer dummy-token-for-extended-card'
                    }
                    _extended_card = await resolver.get_agent_card(
                        relative_card_path=EXTENDED_AGENT_CARD_PATH,
                        http_kwargs={'headers': auth_headers_dict},
                    )
                    logger.info(
                        'Successfully fetched authenticated extended agent card:'
                    )
                    logger.info(
                        _extended_card.model_dump_json(
                            indent=2, exclude_none=True
                        )
                    )
                    final_agent_card_to_use = (
                        _extended_card  # Update to use the extended card
                    )
                    logger.info(
                        '\nUsing AUTHENTICATED EXTENDED agent card for client '
                        'initialization.'
                    )
                except Exception as e_extended:
                    logger.warning(
                        f'Failed to fetch extended agent card: {e_extended}. '
                        'Will proceed with public card.',
                        exc_info=True,
                    )
            elif (
                _public_card
            ):  # supports_authenticated_extended_card is False or None
                logger.info(
                    '\nPublic card does not indicate support for an extended card. Using public card.'
                )

        except Exception as e:
            logger.error(
                f'Critical error fetching public agent card: {e}', exc_info=True
            )
            raise RuntimeError(
                'Failed to fetch the public agent card. Cannot continue.'
            ) from e

        # Normalize agent card URL if it points to 0.0.0.0 (bind address)
        try:
            from urllib.parse import urlparse, urlunparse

            if final_agent_card_to_use and final_agent_card_to_use.url:
                card_url_parsed = urlparse(final_agent_card_to_use.url)
                base_url_parsed = urlparse(base_url)
                if card_url_parsed.hostname in (None, '0.0.0.0'):
                    logger.info(
                        f"Normalizing agent card URL from '{final_agent_card_to_use.url}' "
                        f"to use base '{base_url}'."
                    )
                    normalized = card_url_parsed._replace(
                        scheme=base_url_parsed.scheme,
                        netloc=base_url_parsed.netloc,
                    )
                    final_agent_card_to_use.url = urlunparse(normalized)
        except Exception as e_norm:
            logger.warning(
                f'Failed to normalize agent card URL: {e_norm}', exc_info=True
            )

        # --8<-- [start:send_message]
        # Initialize client using ClientFactory
        from a2a.client import ClientConfig
        config = ClientConfig()
        factory = ClientFactory(config)
        # Create client with card parameter
        client = factory.create(card=final_agent_card_to_use)
        logger.info('Client initialized successfully using ClientFactory.')

        # Use the ClientFactory client API
        message = Message(
            role='user',
            parts=[{'kind': 'text', 'text': 'how much is 10 USD in INR?'}],
            message_id=uuid4().hex,
        )
        async for response_tuple in client.send_message(message):
            # ClientFactory client returns tuples, extract the actual response
            if isinstance(response_tuple, tuple) and len(response_tuple) > 0:
                response = response_tuple[0]  # Get the first element of the tuple
                if hasattr(response, 'model_dump'):
                    print(response.model_dump(mode='json', exclude_none=True))
                else:
                    print(response)  # Fallback to direct print
            else:
                print(response_tuple)  # Fallback to direct print
            break  # Get the first (and likely only) response for non-streaming
        # --8<-- [end:send_message]

        # --8<-- [start:Multiturn]
        # Use the ClientFactory client API for first multiturn message
        task_id = None
        context_id = None
        
        multiturn_message = Message(
            role='user',
            parts=[{'kind': 'text', 'text': 'How much is the exchange rate for 1 USD?'}],
            message_id=uuid4().hex,
        )
        async for response_tuple in client.send_message(multiturn_message):
            # ClientFactory client returns tuples, extract the actual response
            if isinstance(response_tuple, tuple) and len(response_tuple) > 0:
                response = response_tuple[0]  # Get the first element of the tuple
                if hasattr(response, 'model_dump'):
                    print(response.model_dump(mode='json', exclude_none=True))
                    # Extract task_id and context_id from response
                    if hasattr(response, 'id'):
                        task_id = response.id
                    if hasattr(response, 'context_id'):
                        context_id = response.context_id
                    elif hasattr(response, 'contextId'):
                        context_id = response.contextId
                else:
                    print(response)  # Fallback to direct print
                    # Try to extract IDs from dict-like response
                    if isinstance(response, dict):
                        task_id = response.get('id')
                        context_id = response.get('contextId') or response.get('context_id')
            else:
                response = response_tuple
                print(response_tuple)  # Fallback to direct print
            break  # Get the first response

        # Use the ClientFactory client API for second multiturn message
        second_multiturn_message = Message(
            role='user',
            parts=[{'kind': 'text', 'text': 'CAD'}],
            message_id=uuid4().hex,
            task_id=task_id,
            context_id=context_id,
        )
        async for response_tuple in client.send_message(second_multiturn_message):
            # ClientFactory client returns tuples, extract the actual response
            if isinstance(response_tuple, tuple) and len(response_tuple) > 0:
                second_response = response_tuple[0]  # Get the first element of the tuple
                if hasattr(second_response, 'model_dump'):
                    print(second_response.model_dump(mode='json', exclude_none=True))
                else:
                    print(second_response)  # Fallback to direct print
            else:
                print(response_tuple)  # Fallback to direct print
            break  # Get the first response
        # --8<-- [end:Multiturn]

        # --8<-- [start:send_message_streaming]
        
        # Use the ClientFactory client API for streaming
        # For ClientFactory client, send_message already returns an async generator (streaming)
        streaming_message = Message(
            role='user',
            parts=[{'kind': 'text', 'text': 'how much is 10 USD in INR?'}],
            message_id=uuid4().hex,
        )
        async for response_tuple in client.send_message(streaming_message):
            # ClientFactory client returns tuples, extract the actual response
            if isinstance(response_tuple, tuple) and len(response_tuple) > 0:
                chunk = response_tuple[0]  # Get the first element of the tuple
                if hasattr(chunk, 'model_dump'):
                    print(chunk.model_dump(mode='json', exclude_none=True))
                else:
                    print(chunk)  # Fallback to direct print
            else:
                print(response_tuple)  # Fallback to direct print
        # --8<-- [end:send_message_streaming]


if __name__ == '__main__':
    import asyncio

    asyncio.run(main())
