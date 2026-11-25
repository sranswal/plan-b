"""Verify the generated server independently of the model."""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("server", nargs="?", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--tasks", action="store_true", help="Check the Tasks service on port 8001")
    mode.add_argument("--google", action="store_true", help="Check live Google availability on port 8000")
    args = parser.parse_args()
    service = "tasks" if args.tasks else "context"
    default_server = Path(__file__).resolve().parents[1] / "services" / service / "synapse_auto_server.py"
    if args.google and args.server is None:
        parser.error("Pass --google and the Context server path printed by Synapse (such as services/context/synapse_auto_server_5.py).")
    server = (args.server or default_server).resolve()
    if not server.is_file():
        parser.error(f"Generated server file does not exist: {server}")
    port = 8001 if args.tasks else 8000
    expected = {"list_tasks": "tasks", "list_routines": "routines"} if args.tasks else {"list_commitments": "commitments"}
    arguments = {}
    if args.google:
        start = datetime.now(ZoneInfo("Asia/Kolkata")).replace(microsecond=0)
        arguments = {"start": start.isoformat(), "end": (start + timedelta(days=2)).isoformat()}
        expected = {"list_calendar_range": "commitments", "get_calendar_availability": "occupied"}
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(server)],
        env={"ENDPOINT_BASE_URL": f"http://127.0.0.1:{port}"},
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            if missing := expected.keys() - {tool.name for tool in tools.tools}:
                raise RuntimeError(f"Generated server is missing tools: {sorted(missing)}")
            for tool, collection in expected.items():
                result = await session.call_tool(tool, arguments)
                if result.isError:
                    if tool == "list_commitments" and "422 Client Error" in result.model_dump_json():
                        raise RuntimeError("The fixture check received HTTP 422. For Google mode, rerun with --google and the generated Context server path.")
                    raise RuntimeError(result.model_dump_json())
                data = result.structuredContent
                if data is None:
                    data = json.loads(next(part.text for part in result.content if part.type == "text"))
                records = data.get(collection)
                if not isinstance(records, list) or (not args.google and not records):
                    raise RuntimeError(f"{tool} did not return the expected {collection} list")
                if args.google and tool == "list_calendar_range" and not data.get("google_window", {}).get("revision"):
                    raise RuntimeError("Calendar range did not return a Google revision. Check the Context provider setting.")
                if tool == "list_commitments" and not any(
                    record["id"] == "cal-001" and record["ends_at"] == "2026-11-23T18:15:00+05:30"
                    for record in records
                ):
                    raise RuntimeError("Expected calendar fixture was missing")
                print(f"{service}/{tool}: {len(records)} record(s)")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except* RuntimeError as errors:
        def report(group):
            for error in group.exceptions:
                if isinstance(error, BaseExceptionGroup):
                    report(error)
                else:
                    print(f"MCP check failed: {error}", file=sys.stderr)

        report(errors)
        sys.exit(1)
