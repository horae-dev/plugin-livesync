# SPDX-License-Identifier: Apache-2.0
"""The plugin, as the core's plugin host calls it: over stdio, and through the SDK's harness."""

import sys
from pathlib import Path
from typing import Any

import anyio
from horae_sdk.testing import Replayed, call, read_result
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from horae_livesync import COMMAND, NAME, build, status

TOOL = f"{NAME}.status"


async def serve_and_list() -> tuple[set[str], Replayed]:
    # `COMMAND` without its interpreter: run the module with the test's own Python.
    server = StdioServerParameters(command=sys.executable, args=COMMAND[1:])
    async with Client(server, read_timeout_seconds=30) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        result = read_result(await client.call_tool(TOOL, {}))
    return tools, result


def test_serves_over_stdio() -> None:
    tools, result = anyio.run(serve_and_list)
    assert TOOL in tools
    # No approved paths.read: the tool says what it needs.
    assert result.error is not None
    assert "paths.read" in result.error


def run(paths: dict[str, Any], **params: Any) -> Replayed:
    return anyio.run(lambda: call(build(), TOOL, params, paths=paths))


def test_reports_a_live_sidecar(tmp_path: Path) -> None:
    doc = status.new_status("1.0.46-patch2-cli")
    doc["process"]["state"] = "live"
    doc["remote"]["ok"] = True
    doc["replication"].update(pending=0, last_ok_at=status.now_iso())
    status.write(tmp_path, doc)
    result = run({"read": [str(tmp_path)]})
    assert result.intents == []
    assert result.data["healthy"] is True
    assert result.data["live"] is True
    assert result.data["cli_version"] == "1.0.46-patch2-cli"


def test_thresholds_default_to_the_sidecars(tmp_path: Path) -> None:
    doc = status.new_status("v", {"probe_interval_s": 20, "max_age_s": 60, "max_lag_s": 90})
    status.write(tmp_path, doc)
    data = run({"read": [str(tmp_path)]}).data
    assert (data["max_age_s"], data["max_lag_s"]) == (60, 90)
    data = run({"read": [str(tmp_path)]}, max_age_s=600, max_lag_s=30).data
    assert (data["max_age_s"], data["max_lag_s"]) == (600, 30)
    too_short = run({"read": [str(tmp_path)]}, max_age_s=59)
    assert too_short.error is not None
    assert "at least 3 probe intervals (60 s)" in too_short.error


def test_reports_problems_as_data(tmp_path: Path) -> None:
    assert run({"read": [str(tmp_path)]}).data["problems"] == ["status_missing"]
    (tmp_path / status.STATUS_FILE).write_text("not json")
    assert run({"read": [str(tmp_path)]}).data["problems"] == ["status_invalid"]


def test_refuses_bad_parameters(tmp_path: Path) -> None:
    result = run({"read": [str(tmp_path)]}, max_lag_s=0)
    assert result.error is not None
    assert "max_lag_s" in result.error
    two = run({"read": [str(tmp_path), str(tmp_path / "x")]})
    assert two.error is not None
    assert "exactly one" in two.error
