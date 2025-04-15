#!/usr/bin/env python3

import sys
import logging
from mcp.server.fastmcp import FastMCP

# --- Basic Logging Setup (to stderr) ---
# Ensure logs go to stderr, leaving stdout for MCP JSON-RPC messages
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr, # Explicitly direct logs to stderr
    format='%(asctime)s - SERVER - %(levelname)s - %(message)s'
)
logger = logging.getLogger("EchoServer")

# --- MCP Server Initialization ---
# Use FastMCP for simpler tool definition using type hints and docstrings
mcp = FastMCP(
    name="echo-server", # The name of this server implementation
    version="1.0.0"    # The version of this server implementation
)

# --- Tool Definition ---
@mcp.tool()
async def echo(message: str) -> str:
    """
    Echoes back the received message prepended with 'You sent: '.

    Args:
        message: The string message to echo back.
    """
    logger.info(f"Received message to echo: '{message}'")
    response = f"You sent: {message}"
    logger.info(f"Sending echo response: '{response}'")
    # FastMCP handles wrapping the return string in the correct MCP content structure
    return response

# --- Main Execution Block ---
if __name__ == "__main__":
    logger.info("Starting Echo MCP Server on stdio...")
    try:
        # Run the server using stdio transport
        mcp.run(transport='stdio')
    except KeyboardInterrupt:
        logger.info("Echo server stopped by user.")
    except Exception as e:
        # Log any unexpected errors during runtime
        logger.exception("Echo server encountered an unhandled exception.")
    finally:
        logger.info("Echo MCP Server shutdown complete.")