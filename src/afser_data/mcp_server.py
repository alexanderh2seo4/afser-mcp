"""Official MCP SDK, stdio transport; raw source data has no MCP tool/resource."""

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .store import Store
from .sync import SyncManager


def create_server(store: Store) -> FastMCP:
    mcp = FastMCP("AFSER private map", instructions="Anonymized active AFSER records only. Exact household addresses, names, contact details and raw source payloads are never available. Query one chapter at a time unless the user asks for all chapters. Sync stores bulk private data on the owner's local disk and returns counts only.")

    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    @mcp.tool(annotations=readonly)
    def status() -> dict:
        """Get local sync availability, latest successful update and active counts."""
        return store.status()

    @mcp.tool(annotations=readonly)
    def list_chapters() -> dict:
        """Get chapter identifiers and names, without any personal source data."""
        return store.chapters()

    @mcp.tool(annotations=readonly)
    def query_records(kind: str, chapter: str, limit: int = 50) -> dict:
        """Get active anonymous map records for sending, hopees, hostees or families.

        chapter must be an explicit chapter ID or 'all'. At most 200 results.
        Home coordinates are approximate. Hopees expose destination country only.
        """
        return store.records(kind, chapter, limit)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True, idempotentHint=True))
    def sync() -> dict:
        """Read the complete Germany source into private local storage.

        Uses the owner's authenticated source session. Returns aggregate counts
        or a safe error code; never raw payloads, names or exact home locations.
        """
        return SyncManager(store).run()

    return mcp
