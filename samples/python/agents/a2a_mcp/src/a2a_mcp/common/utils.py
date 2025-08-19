import logging
import os

from a2a_mcp.common.types import ServerConfig


logger = logging.getLogger(__name__)


def init_api_key():
    """Initialize API keys for providers (DashScope / OpenAI compatible).

    Priority:
    1. If DASHSCOPE_API_KEY present, proceed.
    2. Else if OPENAI_API_KEY present, proceed.
    Raise if neither key is present.
    """
    dashscope_key = os.getenv('DASHSCOPE_API_KEY')
    openai_key = os.getenv('OPENAI_API_KEY')

    if not dashscope_key and not openai_key:
        logger.error('Neither DASHSCOPE_API_KEY nor OPENAI_API_KEY is set')
        raise ValueError('Missing DASHSCOPE_API_KEY or OPENAI_API_KEY')
    if dashscope_key:
        logger.info('Using DashScope key')
    elif openai_key:
        logger.info('Using OpenAI key')


def config_logging():
    """Configure basic logging."""
    log_level = (
        os.getenv('A2A_LOG_LEVEL') or os.getenv('FASTMCP_LOG_LEVEL') or 'INFO'
    ).upper()
    logging.basicConfig(level=getattr(logging, log_level, logging.INFO))


def config_logger(logger):
    """Logger specific config, avoiding clutter in enabling all loggging."""
    # TODO: replace with env
    logger.setLevel(logging.INFO)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


def get_mcp_server_config() -> ServerConfig:
    """Get the MCP server configuration."""
    return ServerConfig(
        host='localhost',
        port=10100,
        transport='sse',
        url='http://localhost:10100/sse',
    )
