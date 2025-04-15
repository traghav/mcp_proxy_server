# proxy_server_multi_final.py
import asyncio
import sys
import logging
import traceback
import shutil
from typing import Any, List, Optional, Sequence, Dict, Tuple, AsyncIterator, Iterable
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
    TextContent, ImageContent, EmbeddedResource,
    CallToolResult as DownstreamCallToolResult,
    ListToolsResult as DownstreamListToolsResult,
    Resource as MCPResource,
    ListResourcesResult as DownstreamListResourcesResult,
    Prompt as MCPPrompt,
    ListPromptsResult as DownstreamListPromptsResult,
    GetPromptResult,
    ErrorData,
    # Import Base ServerCapabilities and nested classes
    ServerCapabilities,
    ToolsCapability,
    ResourcesCapability,
    PromptsCapability,
    LoggingCapability,
    # CompletionsCapability, # Removed as per previous step
    # --- End nested class imports ---
    ClientCapabilities, Implementation,
    InitializeRequest, InitializedNotification,
    ServerResult,
    AnyUrl
)
# --- MCP SDK Components ---
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel import Server as UpstreamMCPServer
from mcp.server.stdio import stdio_server as upstream_stdio_server
from mcp.server.models import InitializationOptions

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d - %(name)s - %(levelname)s - [%(threadName)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("MCPMultiProxy")
logging.getLogger("mcp").setLevel(logging.WARNING)
logging.getLogger("anyio").setLevel(logging.WARNING)


# --- Downstream Server Configurations ---
# Assign unique IDs for routing
DOWNSTREAM_SERVERS_CONFIG = {
    "weather": {
        "display_name": "Weather Server",
        "command": "npx",
        "args": ["-y", "@h1deya/mcp-server-weather"],
        "env": None
    },
    "git_ingest": {
        "display_name": "Git Ingest Server",
        "command": "uvx",
        "args": ["--from", "git+https://github.com/adhikasp/mcp-git-ingest", "mcp-git-ingest"],
        "env": None
    }
}

# --- Global State ---
downstream_sessions: Dict[str, ClientSession] = {}
tool_routing_map: Dict[str, Tuple[str, str]] = {}
combined_downstream_capabilities = ServerCapabilities() # Initialize empty

# --- Upstream Server Instance ---
upstream_server = UpstreamMCPServer(name="MCPMultiProxy_TempName")

# --- Upstream Server Handlers ---

@upstream_server.list_tools()
async def handle_upstream_list_tools() -> List[MCPTool]:
    """Combines tools from all connected downstream servers."""
    logger.info("[UPSTREAM_HANDLER] Received tools/list request.")
    combined_tools: List[MCPTool] = []
    tool_routing_map.clear() # Clear map before repopulating

    list_tasks = []
    valid_server_ids = list(downstream_sessions.keys())
    for server_id in valid_server_ids:
        session = downstream_sessions.get(server_id)
        if session:
            # Check if downstream server actually supports tools before listing
            if combined_downstream_capabilities.tools: # Use combined caps check
                 list_tasks.append(session.list_tools())
            else:
                 logger.debug(f"Skipping tool list for '{server_id}' as it doesn't support tools.")
        else:
             logger.warning(f"[UPSTREAM_HANDLER] Session for '{server_id}' disappeared during list_tools.")

    if not list_tasks:
        logger.warning("[UPSTREAM_HANDLER] No active downstream sessions support tools.")
        return []

    results = await asyncio.gather(*list_tasks, return_exceptions=True)

    current_routing_map: Dict[str, Tuple[str, str]] = {} # Build locally first
    for i, result in enumerate(results):
        # Need to map result back to server_id reliably even if some fail
        # Find the corresponding server_id by assuming results are in order of tasks
        task_index = i
        original_task_server_id = None
        processed_sessions = 0
        for sid in valid_server_ids:
             session = downstream_sessions.get(sid)
             if session and combined_downstream_capabilities.tools: # Only count sessions we tried to list from
                 if processed_sessions == task_index:
                     original_task_server_id = sid
                     break
                 processed_sessions += 1

        if not original_task_server_id:
             logger.error(f"Could not map list_tools result index {i} back to a server ID.")
             continue # Skip this result

        server_id = original_task_server_id

        if isinstance(result, DownstreamListToolsResult):
            logger.debug(f"[UPSTREAM_HANDLER] Got {len(result.tools)} tools from '{server_id}'.")
            for tool in result.tools:
                original_name = tool.name
                prefixed_name = f"{server_id}:{original_name}"
                current_routing_map[prefixed_name] = (server_id, original_name)

                # --- FIXED LINE ---
                # Create the prefixed tool WITHOUT annotations
                prefixed_tool = MCPTool(
                    name=prefixed_name,
                    description=f"[{DOWNSTREAM_SERVERS_CONFIG[server_id]['display_name']}] {tool.description}",
                    inputSchema=tool.inputSchema
                    # annotations=tool.annotations # REMOVED THIS LINE
                )
                # --- END FIX ---

                combined_tools.append(prefixed_tool)
        elif isinstance(result, Exception):
            logger.error(f"[UPSTREAM_HANDLER] Error listing tools from '{server_id}': {result}", exc_info=isinstance(result, McpError))
        else:
            logger.error(f"[UPSTREAM_HANDLER] Unexpected result type when listing tools from '{server_id}': {type(result)}")

    # Atomically update the global map
    tool_routing_map.update(current_routing_map)
    logger.info(f"[UPSTREAM_HANDLER] Returning {len(combined_tools)} combined tools upstream. Map updated.")
    return combined_tools

@upstream_server.call_tool()
async def handle_upstream_call_tool(name: str, arguments: Dict[str, Any]) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
    """Routes tool call to the correct downstream server."""
    logger.info(f"[UPSTREAM_HANDLER] Received tools/call request for '{name}' with args: {arguments}")

    routing_info = tool_routing_map.get(name)
    if not routing_info:
        logger.error(f"[UPSTREAM_HANDLER] Unknown tool requested: '{name}'. Routing map keys: {list(tool_routing_map.keys())}")
        # Attempt to refresh the tool map in case it's stale
        logger.info("[UPSTREAM_HANDLER] Attempting to refresh tool list before failing call.")
        await handle_upstream_list_tools()
        routing_info = tool_routing_map.get(name) # Try again
        if not routing_info:
             logger.error(f"[UPSTREAM_HANDLER] Tool '{name}' still not found after refresh.")
             raise McpError(error=ErrorData(code=-32601, message=f"Tool '{name}' not found."))

    server_id, original_name = routing_info
    session = downstream_sessions.get(server_id)

    if not session:
        logger.error(f"[UPSTREAM_HANDLER] Downstream session for '{server_id}' unavailable for tool '{name}'.")
        raise McpError(error=ErrorData(code=-32000, message=f"Proxy Error: Downstream connection for '{server_id}' lost"))

    downstream_call_successful = False
    try:
        logger.debug(f"[UPSTREAM_HANDLER] Forwarding tool '{original_name}' to downstream '{server_id}'...")
        args_to_send = arguments or {}
        result: DownstreamCallToolResult = await session.call_tool(original_name, args_to_send)
        downstream_call_successful = True
        logger.info(f"[UPSTREAM_HANDLER] Received result for tool '{original_name}' from '{server_id}'. isError={result.isError}. Forwarding upstream as '{name}'.")
        if result.isError:
             error_text = f"Downstream tool '{server_id}:{original_name}' reported an error"
             if result.content:
                 first_content = result.content[0]
                 content_text = ""
                 if isinstance(first_content, TextContent) and first_content.text:
                     content_text = first_content.text
                 elif isinstance(first_content, str):
                     content_text = first_content
                 if content_text:
                     error_text += f": {content_text[:200]}{'...' if len(content_text) > 200 else ''}"

             raise McpError(error=ErrorData(code=-32001, message=error_text))
        return result.content
    except McpError as e:
        if downstream_call_successful:
            logger.warning(f"[UPSTREAM_HANDLER] Downstream tool '{server_id}:{original_name}' returned an error result: {e.error.message}")
        else:
            logger.error(f"[UPSTREAM_HANDLER] MCPError proxying call_tool '{original_name}' to '{server_id}': {e.error.message}")
        raise McpError(error=ErrorData(code=e.error.code, message=f"Proxy Error (->{server_id}): {e.error.message}", data=e.error.data))
    except Exception as e:
        logger.exception(f"[UPSTREAM_HANDLER] Unexpected Python error during call_tool proxy for '{original_name}' to '{server_id}'.")
        raise McpError(error=ErrorData(code=-32603, message=f"Internal Proxy Error calling tool '{server_id}:{original_name}': {type(e).__name__}"))


# --- Helper to connect to one downstream server ---
async def connect_to_downstream(server_id: str, config: Dict[str, Any], stack: AsyncExitStack) -> Optional[ServerCapabilities]:
    """Connects to a single downstream server and returns its capabilities."""
    global downstream_sessions
    logger.info(f"--- Connecting to downstream server: '{server_id}' ({config['display_name']}) ---")
    cmd = config['command']
    args = config['args']
    env = config['env']

    cmd_path = shutil.which(cmd) or cmd
    # Simplified check for common globally installed tools
    if cmd_path == cmd and cmd not in ['npx', 'uvx', 'python', 'node', 'java']:
        if not shutil.which(cmd):
            logger.warning(f"Downstream command '{cmd}' for '{server_id}' not found in PATH.")
        else:
             logger.info(f"Using downstream command path for '{server_id}': '{shutil.which(cmd)}'")
             cmd_path = shutil.which(cmd) # Use the found path
    elif cmd_path != cmd:
         logger.info(f"Using downstream command path for '{server_id}': '{cmd_path}'")
    else:
         logger.info(f"Using globally available command '{cmd}' for '{server_id}'.")


    try:
        server_params = StdioServerParameters(command=cmd_path, args=args, env=env)
        stdio_transport = await stack.enter_async_context(stdio_client(server_params))
        read_ds, write_ds = stdio_transport
        # Store the session immediately after context entry
        session = ClientSession(read_ds, write_ds)
        downstream_sessions[server_id] = session
        # Enter session context *after* storing it globally
        await stack.enter_async_context(session)
        logger.info(f"Downstream ClientSession context entered and stored for '{server_id}'.")


        init_result = await session.initialize()
        info = init_result.serverInfo
        logger.info(f"Downstream '{server_id}' initialized: {info.name if info else 'N/A'} v{info.version if info else 'N/A'} (Caps: {init_result.capabilities})")
        return init_result.capabilities
    except Exception as e:
        logger.error(f"Failed to connect or initialize downstream server '{server_id}': {type(e).__name__} - {e}", exc_info=False) # Less verbose logging for connection errors
        # Clean up session if it was stored but failed init
        if server_id in downstream_sessions:
            del downstream_sessions[server_id]
            logger.info(f"Removed failed session for '{server_id}'.")
        return None

# --- Main Execution Function ---
async def main():
    global upstream_server, combined_downstream_capabilities, tool_routing_map
    logger.info("--- Starting Multi MCP Proxy ---")

    async with AsyncExitStack() as stack:
        # --- 1. Connect to all Downstream Servers Concurrently ---
        connect_tasks = []
        server_ids = list(DOWNSTREAM_SERVERS_CONFIG.keys())
        for server_id in server_ids:
            config = DOWNSTREAM_SERVERS_CONFIG[server_id]
            # Pass the stack down for context management
            connect_tasks.append(connect_to_downstream(server_id, config, stack))

        logger.info(f"Attempting to connect to {len(server_ids)} downstream servers...")
        capability_results = await asyncio.gather(*connect_tasks)

        # --- 2. Process Connection Results & Combine Capabilities ---
        successful_connections = 0
        # Reset combined capabilities before merging
        combined_downstream_capabilities = ServerCapabilities(
             tools=None, resources=None, prompts=None, logging=None,
             experimental=None
        )

        for i, caps in enumerate(capability_results):
            server_id = server_ids[i]
            if caps is not None:
                successful_connections += 1
                # --- CORRECTED Capability Merging ---
                if caps.tools:
                    if combined_downstream_capabilities.tools is None:
                        combined_downstream_capabilities.tools = ToolsCapability()
                    combined_downstream_capabilities.tools.listChanged = combined_downstream_capabilities.tools.listChanged or caps.tools.listChanged
                if caps.resources:
                    if combined_downstream_capabilities.resources is None:
                        combined_downstream_capabilities.resources = ResourcesCapability()
                    combined_downstream_capabilities.resources.listChanged = combined_downstream_capabilities.resources.listChanged or caps.resources.listChanged
                    combined_downstream_capabilities.resources.subscribe = combined_downstream_capabilities.resources.subscribe or caps.resources.subscribe
                if caps.prompts:
                    if combined_downstream_capabilities.prompts is None:
                        combined_downstream_capabilities.prompts = PromptsCapability()
                    combined_downstream_capabilities.prompts.listChanged = combined_downstream_capabilities.prompts.listChanged or caps.prompts.listChanged
                if caps.logging:
                     if combined_downstream_capabilities.logging is None:
                         combined_downstream_capabilities.logging = LoggingCapability()
                # CompletionsCapability removed here
                if caps.experimental:
                     combined_downstream_capabilities.experimental = caps.experimental or combined_downstream_capabilities.experimental

            else:
                 logger.error(f"Downstream server '{server_id}' failed to connect or initialize.")

        logger.info(f"Successfully connected to {successful_connections}/{len(server_ids)} downstream servers.")
        logger.info(f"Combined Downstream Capabilities: {combined_downstream_capabilities}")

        if successful_connections == 0:
            logger.critical("No downstream servers connected. Exiting.")
            return

        # --- 3. Populate Tool Routing Map (Initial) ---
        logger.info("Populating initial tool routing map...")
        try:
            # Only call if tools are supported by *any* downstream server
            if combined_downstream_capabilities.tools:
                 await handle_upstream_list_tools()
                 logger.info(f"Initial tool routing map populated with {len(tool_routing_map)} entries.")
            else:
                 logger.info("Skipping initial tool map population as no downstream server supports tools.")
        except Exception:
             logger.exception("Error during initial population of tool routing map.")


        # --- 4. Finalize Upstream Server Configuration ---
        proxy_server_name = f"MultiProxy({','.join(downstream_sessions.keys())})"
        upstream_server.name = proxy_server_name
        logger.info(f"Upstream server name set to: '{proxy_server_name}'")

        # --- 5. Start Upstream Server Loop ---
        logger.info(f"Starting upstream server '{proxy_server_name}' on stdio...")
        async with upstream_stdio_server() as (read_us, write_us):
            logger.info("Upstream transport established. Running upstream server run loop...")
            init_options = InitializationOptions(
                 server_name=upstream_server.name,
                 server_version="1.0-multiproxy-final", # Updated version string
                 capabilities=combined_downstream_capabilities # Advertise combined caps
            )
            await upstream_server.run(read_us, write_us, init_options, raise_exceptions=False)
        logger.info("Upstream server run loop finished normally.")


    # --- 6. Cleanup (Handled by AsyncExitStack) ---
    logger.info("Proxy main async function finished. AsyncExitStack handles cleanup.")
    # Clear global state after stack unwinds
    downstream_sessions.clear()
    tool_routing_map.clear()


# --- Script Entry Point ---
if __name__ == "__main__":
    exit_code = 0
    try:
        asyncio.run(main())
        logger.info("Proxy main function completed.")
    except KeyboardInterrupt:
         logger.info("Proxy stopped by user (KeyboardInterrupt).")
    except Exception as e:
        logger.critical(f"Proxy process stopped due to unhandled exception: {type(e).__name__}", exc_info=True)
        exit_code = 1
    finally:
        logger.info(f"Proxy process exiting with code {exit_code}.")
        sys.exit(exit_code)