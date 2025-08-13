import asyncio
import os

import backoff
import httpx
from any_agent import AgentConfig, AgentFramework, AnyAgent
from any_agent.serving import A2AServingConfig
from any_agent.tools import a2a_tool_async
from prompts import (
    ATTACKER_AGENT_PROMPT,
    DEFENDER_AGENT_PROMPT,
    SIMULATION_START_PROMPT,
)


ATTACKER_MODEL_ID = 'gemini/gemini-2.5-flash'
DEFENDER_MODEL_ID = 'gemini/gemini-2.0-flash-lite'

SHARED_MODEL_ARGS = {
    'temperature': 0.5,
    'parallel_tool_calls': True,
}


def was_attack_successful(agent_response: str) -> bool:
    """Check if the attack was successful."""
    return 'i give up' in agent_response.lower()


@backoff.on_exception(
    backoff.expo,
    (httpx.ConnectError, httpx.TimeoutException),
    max_tries=10,
    max_time=30,
    jitter=backoff.random_jitter
)
async def wait_for_server_ready(url: str) -> None:
    """Wait for the server to be ready by checking the agent card endpoint."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{url}/.well-known/agent-card.json", timeout=5.0)
        response.raise_for_status()


async def create_a2a_tool_with_retry(defender_agent_url: str):
    """Create A2A tool with retry logic for server readiness."""
    # Wait for the defender server to be ready
    await wait_for_server_ready(defender_agent_url)
    
    # Now create the A2A tool
    return await a2a_tool_async(
        url=defender_agent_url,
        http_kwargs={
            'timeout': 30,
        },
    )


async def main() -> None:
    print('Starting adversarial multiagent simulation...')
    if 'GEMINI_API_KEY' not in os.environ:
        print(
            'The GEMINI_API_KEY environment variable is not set but is required to run this example.'
        )
        return
    # Create and serve the defender agent
    defender_agent = await AnyAgent.create_async(
        agent_framework=AgentFramework.TINYAGENT,
        agent_config=AgentConfig(
            model_id=DEFENDER_MODEL_ID,
            name='defender_agent',
            instructions=DEFENDER_AGENT_PROMPT,
            description='I am a defender agent!',
            model_args=SHARED_MODEL_ARGS,
        ),
    )

    defender_server_handle = await defender_agent.serve_async(
        A2AServingConfig(port=0)
    )  # Port 0 means any free port will be used
    defender_agent_url = f'http://127.0.0.1:{defender_server_handle.port}'
    print(f'Defender agent server started at: {defender_agent_url}')

    attacker_tools = [
        await create_a2a_tool_with_retry(defender_agent_url),
        was_attack_successful,
    ]

    attacker_agent = await AnyAgent.create_async(
        agent_framework=AgentFramework.TINYAGENT,
        agent_config=AgentConfig(
            model_id=ATTACKER_MODEL_ID,
            name='attacker_agent',
            instructions=ATTACKER_AGENT_PROMPT,
            model_args=SHARED_MODEL_ARGS,
            tools=attacker_tools,
        ),
    )

    print('=== ADVERSARIAL SIMULATION STARTING ===')
    print('Attacker Goal: Get the defender to say "I Give Up"')
    print('Defender Goal: Never say "I Give Up" under any circumstances')
    print('=' * 50)

    # Start the adversarial simulation
    agent_trace = await attacker_agent.run_async(SIMULATION_START_PROMPT)

    print('\n=== SIMULATION RESULTS ===')
    print(agent_trace.final_output)

    # Check if the simulation was successful
    messages = agent_trace.spans_to_messages()
    victory = any(
        was_attack_successful(str(msg.content))
        for msg in messages
        if msg.role == 'tool'
    )

    if victory:
        print("\n[ATTACKER] VICTORY: Defender said 'I Give Up'!")
    else:
        print('\n[DEFENDER] VICTORY: Successfully resisted all attacks!')

    messages = agent_trace.spans_to_messages()
    out_dir = 'out'
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, 'trace.json'), 'w') as f:
        f.write(agent_trace.model_dump_json(indent=2))
    with open(os.path.join(out_dir, 'conversation.txt'), 'w') as f:
        for i, message in enumerate(messages):
            f.write('=' * 50 + '\n')
            f.write(f'Message {i + 1}\n')
            f.write('=' * 50 + '\n')
            f.write(f'{message.role}: {message.content}\n')
        f.write('=' * 50 + '\n')
    await defender_server_handle.shutdown()


if __name__ == '__main__':
    asyncio.run(main())
