# type: ignore

import logging
import os

from collections.abc import AsyncIterable
from typing import Any, Literal

from a2a_mcp.common import prompts
from a2a_mcp.common.base_agent import BaseAgent
from a2a_mcp.common.types import TaskList
from a2a_mcp.common.utils import init_api_key
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field


memory = MemorySaver()
logger = logging.getLogger(__name__)


class ResponseFormat(BaseModel):
    """Respond to the user in this format."""

    status: Literal['input_required', 'completed', 'error'] = 'input_required'
    # Clarifying question or error message. Required when status is input_required or error.
    question: str = Field(
        default="",
        description='Input needed from the user (or brief error). Empty when status=="completed".'
    )
    # Optional until plan is completed.
    content: TaskList | None = Field(
        default=None,
        description='List of tasks when the plan is generated (present when status=="completed")'
    )


class LangGraphPlannerAgent(BaseAgent):
    """Planner Agent backed by LangGraph."""

    def __init__(self):
        init_api_key()

        logger.info('Initializing LanggraphPlannerAgent')

        super().__init__(
            agent_name='PlannerAgent',
            description='Breakdown the user request into executable tasks',
            content_types=['text', 'text/plain'],
        )

        # Use DashScope OpenAI-compatible endpoint (fallback to base_url env if provided)
        self.model = ChatOpenAI(
            model=os.getenv('DASHSCOPE_MODEL', 'qwen-plus'),
            temperature=0.0,
            base_url='https://dashscope.aliyuncs.com/compatible-mode/v1',
            api_key=os.getenv('DASHSCOPE_API_KEY'),
        )

        self.graph = create_react_agent(
            self.model,
            checkpointer=memory,
            prompt=prompts.PLANNER_COT_INSTRUCTIONS,
            # prompt=prompts.TRIP_PLANNER_INSTRUCTIONS_1,
            response_format=ResponseFormat,
            tools=[],
        )

    def invoke(self, query, sessionId) -> str:
        config = {'configurable': {'thread_id': sessionId}}
        # DashScope requires the literal word 'json' to appear in the messages
        # when using a response_format of type json_object. Append a short hint
        # if the user query itself lacks it so we don't force the user to know
        # this provider-specific requirement.
        user_query = (
            query
            if 'json' in query.lower()
            else f"{query}\n\nPlease respond in valid JSON."
        )
        self.graph.invoke({'messages': [('user', user_query)]}, config)
        return self.get_agent_response(config)

    async def stream(
        self, query, sessionId, task_id
    ) -> AsyncIterable[dict[str, Any]]:
        user_query = (
            query
            if 'json' in query.lower()
            else f"{query}\n\nPlease respond in valid JSON."
        )
        inputs = {'messages': [('user', user_query)]}
        config = {'configurable': {'thread_id': sessionId}}

        logger.info(
            f'Running LanggraphPlannerAgent stream for session {sessionId} {task_id} with input {query}'
        )

        for item in self.graph.stream(inputs, config, stream_mode='values'):
            message = item['messages'][-1]
            if isinstance(message, AIMessage):
                yield {
                    'response_type': 'text',
                    'is_task_complete': False,
                    'require_user_input': False,
                    'content': message.content,
                }
                # If the model already produced a JSON asking for input, stop streaming more duplicates
                if isinstance(message.content, str) and '"status"' in message.content and 'input_required' in message.content:
                    break
        yield self.get_agent_response(config)

    def get_agent_response(self, config):
        current_state = self.graph.get_state(config)
        structured_response = current_state.values.get('structured_response')
        if structured_response and isinstance(
            structured_response, ResponseFormat
        ):
            if (
                structured_response.status == 'input_required'
                # and structured_response.content.tasks
            ):
                return {
                    'response_type': 'text',
                    'is_task_complete': False,
                    'require_user_input': True,
                    'content': structured_response.question,
                }
            if structured_response.status == 'error':
                return {
                    'response_type': 'text',
                    'is_task_complete': False,
                    'require_user_input': True,
                    'content': structured_response.question,
                }
            if structured_response.status == 'completed':
                return {
                    'response_type': 'data',
                    'is_task_complete': True,
                    'require_user_input': False,
                    'content': structured_response.content.model_dump(),
                }
        return {
            'is_task_complete': False,
            'require_user_input': True,
            'content': 'We are unable to process your request at the moment. Please try again.',
        }
