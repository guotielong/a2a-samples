import json
import logging
import uuid

from collections.abc import AsyncIterable
from enum import Enum
from uuid import uuid4

import httpx
import networkx as nx

from a2a.client.client import ClientConfig
from a2a.client.client_factory import ClientFactory
from a2a.client.errors import A2AClientHTTPError
from a2a.types import (
    AgentCard,
    Message,
    Part,
    TextPart,
    Role,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
)
from a2a_mcp.common.utils import get_mcp_server_config
from a2a_mcp.mcp import client


logger = logging.getLogger(__name__)


class Status(Enum):
    """Represents the status of a workflow and its associated node."""

    READY = 'READY'
    RUNNING = 'RUNNING'
    COMPLETED = 'COMPLETED'
    PAUSED = 'PAUSED'
    INITIALIZED = 'INITIALIZED'


class WorkflowNode:
    """Represents a single node in a workflow graph.

    Each node encapsulates a specific task to be executed, such as finding an
    agent or invoking an agent's capabilities. It manages its own state
    (e.g., READY, RUNNING, COMPLETED, PAUSED) and can execute its assigned task.

    """

    def __init__(
        self,
        task: str,
        node_key: str | None = None,
        node_label: str | None = None,
    ):
        self.id = str(uuid.uuid4())
        self.node_key = node_key
        self.node_label = node_label
        self.task = task
        self.results = None
        self.state = Status.READY

    async def get_planner_resource(self) -> AgentCard | None:
        logger.info(f'Getting resource for node {self.id}')
        config = get_mcp_server_config()
        async with client.init_session(
            config.host, config.port, config.transport
        ) as session:
            response = await client.find_resource(
                session, 'resource://agent_cards/planner_agent'
            )
            data = json.loads(response.contents[0].text)
            return AgentCard(**data['agent_card'][0])

    async def find_agent_for_task(self) -> AgentCard | None:
        logger.info(f'Find agent for task - {self.task}')
        config = get_mcp_server_config()
        async with client.init_session(
            config.host, config.port, config.transport
        ) as session:
            result = await client.find_agent(session, self.task)
            agent_card_json = json.loads(result.content[0].text)
            logger.debug(f'Found agent {agent_card_json} for task {self.task}')
            return AgentCard(**agent_card_json)

    async def run_node(
        self,
        query: str,
        task_id: str,
        context_id: str,
    ) -> AsyncIterable[dict[str, any]]:
        logger.info(f'Executing node {self.id}')
        agent_card = None
        if self.node_key == 'planner':
            agent_card = await self.get_planner_resource()
        else:
            agent_card = await self.find_agent_for_task()
        if not agent_card:
            logger.error('No agent card resolved for node; aborting execution')
            return
        logger.info(
            'Invoking downstream agent name=%s url=%s node_key=%s task_snippet=%s',
            getattr(agent_card, 'name', 'UNKNOWN'),
            getattr(agent_card, 'url', 'UNKNOWN'),
            self.node_key,
            (query[:80] + '…') if query and len(query) > 80 else query,
        )
        # Use trust_env=False so local intra-process calls (localhost) are NOT routed via any
        # corporate/system HTTP proxies (e.g., Privoxy) which were injecting HTML error pages
        # and breaking SSE (Content-Type became text/html).
        async with httpx.AsyncClient(trust_env=False) as httpx_client:
            # Use new A2A client API
            config = ClientConfig(streaming=True, httpx_client=httpx_client)
            factory = ClientFactory(config)
            a2a_client = factory.create(agent_card)

            # Build message using new API
            message = Message(
                messageId=str(uuid4()),
                role=Role.user,
                parts=[Part(root=TextPart(text=query))],
            )

            try:
                async for event in a2a_client.send_message(message):
                    # event is either (Task, UpdateEvent) | Message
                    if isinstance(event, tuple):
                        task, update = event
                        if update is None:
                            # Initial Task object - don't yield, just log
                            logger.info(f'Task {task.id} status: {task.status.state}')
                        else:
                            if isinstance(update, TaskStatusUpdateEvent):
                                # Check for input_required state
                                if update.status.state == TaskState.input_required:
                                    msg_txt = "Need more information"
                                    if update.status.message and update.status.message.parts:
                                        part0 = update.status.message.parts[0].root
                                        msg_txt = getattr(part0, "text", None) or getattr(part0, "data", None) or msg_txt
                                    
                                    yield {
                                        'response_type': 'text',
                                        'is_task_complete': False,
                                        'require_user_input': True,
                                        'content': msg_txt,
                                        'task_id': task.id,
                                    }
                                else:
                                    # Working state or other status
                                    msg_txt = "Processing..."
                                    if update.status.message and update.status.message.parts:
                                        part0 = update.status.message.parts[0].root
                                        msg_txt = getattr(part0, "text", None) or getattr(part0, "data", None) or msg_txt
                                    
                                    yield {
                                        'response_type': 'text',
                                        'is_task_complete': False,
                                        'require_user_input': False,
                                        'content': msg_txt,
                                        'task_id': task.id,
                                    }
                            elif isinstance(update, TaskArtifactUpdateEvent):
                                # Save artifact and yield completion
                                artifact = update.artifact
                                self.results = artifact
                                
                                # Extract content from artifact
                                content = "Task completed"
                                if artifact.parts:
                                    part0 = artifact.parts[0].root
                                    if hasattr(part0, 'data'):
                                        content = part0.data
                                    elif hasattr(part0, 'text'):
                                        content = part0.text
                                
                                yield {
                                    'response_type': 'data' if hasattr(artifact.parts[0].root, 'data') else 'text',
                                    'is_task_complete': True,
                                    'require_user_input': False,
                                    'content': content,
                                    'task_id': task.id,
                                    'artifact': artifact,  # Keep artifact for orchestrator
                                }
                            else:
                                # Other update types - yield as working
                                yield {
                                    'response_type': 'text',
                                    'is_task_complete': False,
                                    'require_user_input': False,
                                    'content': f"Update: {update}",
                                    'task_id': task.id,
                                }
                    else:
                        # Final Message response - treat as completion
                        content = "Task completed"
                        if hasattr(event, 'parts') and event.parts:
                            part0 = event.parts[0].root
                            content = getattr(part0, "text", None) or getattr(part0, "data", None) or content
                        
                        yield {
                            'response_type': 'text',
                            'is_task_complete': True,
                            'require_user_input': False,
                            'content': content,
                        }
            except A2AClientHTTPError as e:
                logger.error('A2A client error: %s', e)
                raise


class WorkflowGraph:
    """Represents a graph of workflow nodes."""

    def __init__(self) -> None:
        self.graph = nx.DiGraph()
        self.nodes = {}
        self.latest_node = None
        self.node_type = None
        self.state = Status.INITIALIZED
        self.paused_node_id = None

    def add_node(self, node) -> None:
        logger.info(f'Adding node {node.id}')
        self.graph.add_node(node.id, query=node.task)
        self.nodes[node.id] = node
        self.latest_node = node.id

    def add_edge(self, from_node_id: str, to_node_id: str) -> None:
        if from_node_id not in self.nodes or to_node_id not in self.nodes:
            raise ValueError('Invalid node IDs')

        self.graph.add_edge(from_node_id, to_node_id)

    async def run_workflow(
        self, start_node_id: str | None = None
    ) -> AsyncIterable[dict[str, any]]:
        logger.info('Executing workflow graph')
        if not start_node_id or start_node_id not in self.nodes:
            start_nodes = [n for n, d in self.graph.in_degree() if d == 0]
        else:
            start_nodes = [self.nodes[start_node_id].id]

        applicable_graph = set()

        for node_id in start_nodes:
            applicable_graph.add(node_id)
            applicable_graph.update(nx.descendants(self.graph, node_id))

        complete_graph = list(nx.topological_sort(self.graph))
        sub_graph = [n for n in complete_graph if n in applicable_graph]
        logger.info(f'Sub graph {sub_graph} size {len(sub_graph)}')
        self.state = Status.RUNNING
        # Alternative is to loop over all nodes, but we only need the connected nodes.
        for node_id in sub_graph:
            node = self.nodes[node_id]
            node.state = Status.RUNNING
            query = self.graph.nodes[node_id].get('query')
            task_id = self.graph.nodes[node_id].get('task_id')
            context_id = self.graph.nodes[node_id].get('context_id')
            async for chunk in node.run_node(query, task_id, context_id):
                # When the workflow node is paused, do not yield any chunks
                # but, let the loop complete.
                if node.state != Status.PAUSED:
                    # Handle the response format
                    if isinstance(chunk, dict):
                        # Check for input required state
                        if chunk.get('require_user_input', False):
                            node.state = Status.PAUSED
                            self.state = Status.PAUSED
                            self.paused_node_id = node.id
                        # Check for task completion
                        elif chunk.get('is_task_complete', False):
                            # Store any artifact if present
                            if 'artifact' in chunk:
                                node.results = chunk['artifact']
                    yield chunk
            if self.state == Status.PAUSED:
                break
            if node.state == Status.RUNNING:
                node.state = Status.COMPLETED
        if self.state == Status.RUNNING:
            self.state = Status.COMPLETED

    def set_node_attribute(self, node_id, attribute, value) -> None:
        nx.set_node_attributes(self.graph, {node_id: value}, attribute)

    def set_node_attributes(self, node_id, attr_val) -> None:
        nx.set_node_attributes(self.graph, {node_id: attr_val})

    def is_empty(self) -> bool:
        return self.graph.number_of_nodes() == 0
