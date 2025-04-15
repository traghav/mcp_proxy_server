# test_client.py
import asyncio
import sys
import traceback
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
# Import specific types needed for checking results if necessary
from mcp.types import ListToolsResult, CallToolResult, TextContent, McpError

async def run_test():
    # Command to run *your proxy*
    proxy_command = sys.executable # Path to your python executable in the venv
    # --- IMPORTANT: Use the absolute path to your proxy script ---
    proxy_script = "/Users/raghav/dev/personal/mcp/proxy_server/proxy_server.py" # <--- CHANGE THIS TO YOUR ACTUAL ABSOLUTE PATH
    # -----------------------------------------------------------
    downstream_command = "npx"
    downstream_args = ["-y", "@h1deya/mcp-server-weather"]

    params = StdioServerParameters(
        command=proxy_command,
        args=[proxy_script, downstream_command] + downstream_args
    )

    print(f"Connecting to proxy: {proxy_command} {' '.join(params.args)}")

    try:
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                print("Initializing client session with proxy...")
                init_result = await session.initialize()
                print(f"Proxy initialized: {init_result.serverInfo}")
                print(f"Proxy capabilities: {init_result.capabilities}")

                if init_result.capabilities and init_result.capabilities.tools:
                    print("\nListing tools via proxy...")
                    tools_result = await session.list_tools()
                    print(f"Tools found: {[t.name for t in tools_result.tools]}")

                    if tools_result.tools:
                        print("\n--- Calling Tools ---")
                        for tool in tools_result.tools:
                            tool_name = tool.name
                            tool_args = {}

                            # --- Provide correct arguments based on tool name ---
                            if tool_name == 'get_alerts':
                                tool_args = {"state": "CA"} # Example: California
                                print(f"Calling tool: {tool_name} with args: {tool_args}")
                            elif tool_name == 'get_forecast':
                                tool_args = {"latitude": 37.7749, "longitude": -122.4194} # Example: San Francisco
                                print(f"Calling tool: {tool_name} with args: {tool_args}")
                            else:
                                print(f"Skipping unknown tool: {tool_name}")
                                continue
                            # -----------------------------------------------------

                            try:
                                call_result: CallToolResult = await session.call_tool(tool_name, tool_args)
                                print(f"\nResult for {tool_name}:")
                                if call_result.isError:
                                    print("  Error: True")
                                else:
                                    print("  Error: False")
                                for content in call_result.content:
                                    if isinstance(content, TextContent):
                                        print(f"  Content: {content.text[:200]}...") # Print first 200 chars
                                    else:
                                         print(f"  Content Type: {type(content)}")
                            except McpError as call_err:
                                print(f"\nError calling {tool_name}: {call_err.error.message}")
                            except Exception as call_exc:
                                print(f"\nUnexpected error calling {tool_name}:")
                                traceback.print_exc()

                    else:
                        print("No tools found via proxy.")
                else:
                    print("Proxy does not advertise tool capability.")

    except Exception as e:
        print(f"\n--- TEST CLIENT ERROR ---")
        traceback.print_exc()
        print(f"-------------------------")


if __name__ == "__main__":
    # Ensure the absolute path is set correctly above!
    if "/ABSOLUTE/PATH/TO/" in proxy_script:
         print("ERROR: Please replace '/ABSOLUTE/PATH/TO/proxy_server_simple.py' with the actual path in test_client.py")
         sys.exit(1)
    asyncio.run(run_test())