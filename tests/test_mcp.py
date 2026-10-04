import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from afser_data.mcp_server import create_server
from afser_data.store import Store


def test_official_mcp_exposes_only_anonymous_tools(tmp_path):
    server = create_server(Store(tmp_path))
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == {"status", "list_chapters", "query_records", "sync"}
    for tool in tools:
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.readOnlyHint is (tool.name != "sync")
        assert tool.annotations.openWorldHint is (tool.name == "sync")
        if tool.name == "sync":
            assert tool.annotations.idempotentHint is True
    result = asyncio.run(server.call_tool("status", {}))
    assert result is not None
    assert not asyncio.run(server.list_resources())


def test_real_stdio_protocol_initializes_and_reads_sanitized_status(tmp_path):
    async def connect():
        params = StdioServerParameters(command=sys.executable, args=["-m", "afser_data.cli", "--data-dir", str(tmp_path), "mcp"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == {"status", "list_chapters", "query_records", "sync"}
                result = await session.call_tool("status", {})
                assert not result.isError
                payload = json.loads(result.content[0].text)
                assert payload["ready"] is False
                assert sum(payload["counts"].values()) == 0

    asyncio.run(connect())
