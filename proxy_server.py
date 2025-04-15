# proxy_server_simple.py
import asyncio
import sys
import logging
import traceback
import shutil
from typing import Any, List, Optional, Sequence, Dict, AsyncIterator, Iterable
from contextlib import asynccontextmanager, AsyncExitStack
import base64

# --- MCP Core Imports ---
from mcp import (
    ClientSession,
    StdioServerParameters,
    McpError,
)
from mcp.types import (
    Tool as MCPTool,
    TextContent, ImageContent, EmbeddedResource, # No AudioContent
    CallToolResult as DownstreamCallToolResult,
    ListToolsResult as DownstreamListToolsResult,
    Resource as MCPResource, # Needed if proxying resources
    ListResourcesResult as DownstreamListResourcesResult, # Needed if proxying resources
    Prompt as MCPPrompt, # Needed if proxying prompts
    ListPromptsResult as DownstreamListPromptsResult, # Needed if proxying prompts
    GetPromptResult, # Needed if proxying prompts
    ErrorData,
    ServerCapabilities, ClientCapabilities, Implementation,
    InitializeRequest, InitializedNotification,
    # Request types are now implicitly handled by decorators
    ServerResult, # Still potentially needed if decorators don't auto-wrap
    AnyUrl
)
# Low-level helper type needed only if proxying resources later
# from mcp.server.lowlevel.helper_types import ReadResourceContents

# --- MCP SDK Components ---
from mcp.client.stdio import stdio_client
# Use the low-level server directly
from mcp.server.lowlevel import Server as UpstreamMCPServer
from mcp.server.stdio import stdio_server as upstream_stdio_server
from mcp.server.models import InitializationOptions # For upstream run config

# --- Logging Setup ---
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s.%(msecs)03d - %(name)s - %(levelname)s - [%(threadName)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("MCPProxySimple")
logging.getLogger("mcp").setLevel(logging.INFO)
logging.getLogger("anyio").setLevel(logging.WARNING)

# --- Global variable to hold the downstream session ---
downstream_session_global: Optional[ClientSession] = None

# --- Upstream Server Instance ---
# Create this globally so we can attach handlers using decorators
upstream_server = UpstreamMCPServer(name="MCPProxySimple_TempName")

# --- Upstream Server Handlers (Registered with Decorators) ---

@upstream_server.list_tools()
async def handle_upstream_list_tools() -> List[MCPTool]: # Return the list directly
    """Handler for when the upstream client asks for tools."""
    logger.info("[UPSTREAM_HANDLER] Received tools/list request.")
    if not downstream_session_global:
        logger.error("[UPSTREAM_HANDLER] Downstream session unavailable for list_tools.")
        # How to return error from decorator? Raise McpError.
        raise McpError(error=ErrorData(code=-32000, message="Proxy Error: Downstream connection lost"))

    try:
        logger.debug("[UPSTREAM_HANDLER] Forwarding tools/list to downstream...")
        result: DownstreamListToolsResult = await downstream_session_global.list_tools()
        logger.info(f"[UPSTREAM_HANDLER] Received {len(result.tools)} tools from downstream. Forwarding upstream.")
        # Decorator likely handles wrapping in ServerResult, return the inner list
        return result.tools
    except McpError as e:
         logger.error(f"[UPSTREAM_HANDLER] McpError proxying list_tools: {e.error.message}")
         raise # Let the server framework handle McpError
    except Exception as e:
        logger.exception("[UPSTREAM_HANDLER] Unexpected error proxying list_tools.")
        raise McpError(error=ErrorData(code=-32603, message=f"Internal Proxy Error during list_tools: {e}"))

@upstream_server.call_tool()
async def handle_upstream_call_tool(name: str, arguments: Dict[str, Any]) -> Sequence[TextContent | ImageContent | EmbeddedResource]: # Return content list directly
    """Handler for when the upstream client calls a tool."""
    logger.info(f"[UPSTREAM_HANDLER] Received tools/call request for '{name}' with args: {arguments}")
    if not downstream_session_global:
        logger.error(f"[UPSTREAM_HANDLER] Downstream session unavailable for call_tool '{name}'.")
        raise McpError(error=ErrorData(code=-32000, message="Proxy Error: Downstream connection lost"))

    downstream_call_successful = False
    try:
        logger.debug(f"[UPSTREAM_HANDLER] Forwarding tools/call '{name}' to downstream...")
        args_to_send = arguments or {}
        result: DownstreamCallToolResult = await downstream_session_global.call_tool(name, args_to_send)
        downstream_call_successful = True
        logger.info(f"[UPSTREAM_HANDLER] Received result for tool '{name}' from downstream. isError={result.isError}. Forwarding upstream.")
        if result.isError:
             error_text = f"Downstream tool '{name}' reported an error"
             if result.content and isinstance(result.content[0], TextContent):
                 error_text += f": {result.content[0].text}"
             # Raise McpError for the framework to turn into an error *result*
             raise McpError(error=ErrorData(code=-32001, message=error_text))
        # Decorator likely handles wrapping in ServerResult(CallToolResult(...)), return content list
        return result.content
    except McpError as e:
        if downstream_call_successful: # Error was in the downstream result itself
            logger.warning(f"[UPSTREAM_HANDLER] Downstream tool '{name}' returned an error result: {e.error.message}")
        else: # Error was in the communication / proxy layer
            logger.error(f"[UPSTREAM_HANDLER] MCPError proxying call_tool '{name}': {e.error.message}")
        raise # Re-raise McpError for framework handling
    except Exception as e:
        logger.exception(f"[UPSTREAM_HANDLER] Unexpected Python error during call_tool proxy for '{name}'.")
        raise McpError(error=ErrorData(code=-32603, message=f"Internal Proxy Error calling tool '{name}': {e}"))


# --- Main Execution Function ---

async def main(downstream_cmd: str, downstream_args: List[str]):
    global downstream_session_global, upstream_server
    logger.info("--- Starting Simple MCP Proxy ---")

    downstream_cmd_path = shutil.which(downstream_cmd) or downstream_cmd
    if downstream_cmd_path == downstream_cmd and not shutil.which(downstream_cmd):
         logger.warning(f"Downstream command '{downstream_cmd}' not found in PATH.")
    else:
         logger.info(f"Using downstream command path: '{downstream_cmd_path}'")

    async with AsyncExitStack() as stack:
        downstream_supports_tools = False
        try:
            # 1. Connect Downstream
            logger.info(f"Connecting to downstream: {downstream_cmd_path} {' '.join(downstream_args)}")
            server_params = StdioServerParameters(command=downstream_cmd_path, args=downstream_args)
            stdio_transport = await stack.enter_async_context(stdio_client(server_params))
            read_ds, write_ds = stdio_transport
            downstream_session_global = await stack.enter_async_context(ClientSession(read_ds, write_ds))
            logger.info("Downstream ClientSession context entered.")

            init_result = await downstream_session_global.initialize()
            downstream_name = init_result.serverInfo.name if init_result.serverInfo else "downstream"
            logger.info(f"Downstream connected and initialized: {downstream_name} (Caps: {init_result.capabilities})")
            downstream_supports_tools = init_result.capabilities and init_result.capabilities.tools is not None

            # 2. Finalize Upstream Server Configuration (Name & Handlers)
            proxy_server_name = f"SimpleProxy->{downstream_name}"
            upstream_server.name = proxy_server_name # Set the final name
            logger.info(f"Upstream server name set to: '{proxy_server_name}'")

            # Handler registration happens via decorators above, just log capability
            if not downstream_supports_tools:
                logger.warning("Downstream server does not support tools. Tool handlers will not be active.")

            # 3. Start Upstream Server Loop
            logger.info(f"Starting upstream server '{proxy_server_name}' on stdio...")
            async with upstream_stdio_server() as (read_us, write_us):
                logger.info("Upstream transport established. Running upstream server run loop...")
                init_options = InitializationOptions(
                     server_name=upstream_server.name,
                     server_version="1.0-simpleproxy",
                     # Advertise ONLY tools capability IF downstream supports it
                     capabilities=ServerCapabilities(tools={}) if downstream_supports_tools else ServerCapabilities()
                )
                # Pass the globally defined upstream_server instance
                await upstream_server.run(read_us, write_us, init_options, raise_exceptions=False)
            logger.info("Upstream server run loop finished normally.")

        except McpError as mcp_err:
             logger.critical(f"MCPError during proxy setup or run: {mcp_err.error.message}", exc_info=False)
             raise
        except Exception:
            logger.critical("Fatal error during proxy operation.", exc_info=True)
            raise
        finally:
            logger.info("Proxy main async function exiting. Cleaning up downstream via AsyncExitStack...")
            downstream_session_global = None # Clear global ref

# --- Script Entry Point ---
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <downstream_command> [downstream_args...]")
        print("\nExample:")
        print(f"  python {sys.argv[0]} npx -y @h1deya/mcp-server-weather")
        sys.exit(1)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    exit_code = 0
    try:
        asyncio.run(main(cmd, args))
        logger.info("Proxy main function completed.")
    except KeyboardInterrupt:
         logger.info("Proxy stopped by user (KeyboardInterrupt).")
    except Exception as e:
        logger.critical(f"Proxy process stopped due to unhandled exception: {type(e).__name__}", exc_info=True)
        exit_code = 1
    finally:
        logger.info(f"Proxy process exiting with code {exit_code}.")
        # Cleanup is now handled by AsyncExitStack in main()
        sys.exit(exit_code)