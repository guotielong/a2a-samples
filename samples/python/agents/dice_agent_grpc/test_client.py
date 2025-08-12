import logging  # Import the logging module

from uuid import uuid4

import grpc
import httpx

from a2a.client import A2ACardResolver, A2AGrpcClient
from a2a.grpc import a2a_pb2, a2a_pb2_grpc
from a2a.types import (
    AgentCard,
    Message,
    MessageSendParams,
    Part,
    Role,
    TextPart,
)
from a2a.utils import proto_utils


logging.basicConfig(level=logging.INFO)

async def main() -> None:
    # Configure logging to show INFO level messages
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)  # Get a logger instance

    # base_url is the location for the base agent_card being served by http server
    base_url = 'http://localhost:11000'

    agent_card: AgentCard | None = None
    async with httpx.AsyncClient() as httpx_client:
        resolver = A2ACardResolver(
            httpx_client=httpx_client,
            base_url=base_url,
        )
        #fetch the base agent card
        agent_card = await resolver.get_agent_card()

    if not agent_card:
        raise ValueError('Agent card not found')

    final_agent_card_to_use = agent_card

    async with grpc.aio.insecure_channel(agent_card.url) as channel:
        stub = a2a_pb2_grpc.A2AServiceStub(channel)

        # if the base agent card has authenticated_extended_card flag set
        # use the gRPC channel to get the authenticated agent card
        # use the authenticated agent card for interacting with the gRPC service
        if agent_card.supports_authenticated_extended_card:
            try:
                logger.info(
                    'Attempting to fetch authenticated agent card from grpc endpoint'
                )
                proto_card = await stub.GetAgentCard(a2a_pb2.GetAgentCardRequest())
                logger.info('Successfully fetched agent card:')
                logger.info(proto_card)
                final_agent_card_to_use = proto_utils.FromProto.agent_card(
                    proto_card
                )
            except Exception as e:
                logging.error('Failed to get authenticated agent card. Exiting.', e)
                return


        client = A2AGrpcClient(stub, agent_card=final_agent_card_to_use)
        logger.info('A2AClient initialized.')

        request = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(root=TextPart(text='roll a 5 sided dice'))],
                message_id=str(uuid4()),
            )
        )

        response = await client.send_message(request)
        logging.info(response.model_dump(mode='json', exclude_none=True))

        stream_response = client.send_message_streaming(request)

        async for chunk in stream_response:
            logging.info(chunk.model_dump(mode='json', exclude_none=True))


if __name__ == '__main__':
    import asyncio

    asyncio.run(main())
