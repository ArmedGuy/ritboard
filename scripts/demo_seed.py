"""Fill a demo workspace over MCP, the way an agent would.

    uv run python scripts/demo_seed.py [http://127.0.0.1:8765/mcp]
"""

import asyncio
import sys

from mcp import Client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/mcp"


def out(res):
    if res.is_error:
        raise SystemExit(res.content[0].text)
    return res.structured_content


async def main():
    async with Client(URL) as c:
        ws = out(await c.call_tool("create_workspace", {"name": "Demo: training stall", "first_board_name": "Overview"}))
        w = ws["workspace"]["id"]
        board = ws["boards"][0]["id"]
        call = lambda tool, **kw: c.call_tool(tool, {"workspace": w, **kw})  # noqa: E731

        folder = out(await call("add_finding", title="Profiling", kind="folder"))
        a = out(await call(
            "add_finding", title="Allreduce stalls at step 4k", category="network", source="profiler-agent",
            parent_id=folder["id"], board_id=board, x=80, y=80, w=460, h=260,
            content="## What we saw\n- NCCL allreduce p99 jumps from **4 ms** to **38 ms**\n- Only on nodes 12–15\n- Starts after checkpoint write\n\n`NCCL_DEBUG=INFO` shows ring rebuilds.",
        ))
        b = out(await call(
            "add_finding", title="Per-node bandwidth", kind="csv", category="network", source="profiler-agent",
            parent_id=folder["id"], board_id=board, x=620, y=80, w=420, h=230,
            content="node,GB/s before,GB/s after\n11,182,180\n12,181,61\n13,183,58\n14,180,64\n15,182,59",
        ))
        cpt = out(await call(
            "add_finding", title="Checkpoint writer", category="storage", source="code-agent", board_id=board,
            x=80, y=420, w=460, h=230,
            content="Checkpoint writes go through the same NIC as NCCL on nodes 12–15.\n\n| node | NIC for storage |\n|---|---|\n| 0–11 | mlx5_1 |\n| 12–15 | **mlx5_0** |",
        ))
        chart = out(await call(
            "add_finding", title="Step time", kind="html", category="perf", source="analysis-agent", board_id=board,
            x=620, y=380, w=640, h=360,
            content="""<svg viewBox="0 0 600 300" style="width:100%;height:100%;font:12px system-ui">
<line x1="40" y1="260" x2="580" y2="260" stroke="#999"/><line x1="40" y1="20" x2="40" y2="260" stroke="#999"/>
<polyline fill="none" stroke="#00798c" stroke-width="2.5" points="40,200 100,198 160,201 220,199 280,197 300,120 340,110 400,115 460,108 520,112 580,109"/>
<line x1="290" y1="20" x2="290" y2="260" stroke="#d1495b" stroke-dasharray="4 3"/>
<text x="296" y="34" fill="#d1495b">checkpoint</text><text x="44" y="16">step time (ms)</text></svg>""",
        ))
        fix = out(await call(
            "add_finding", title="Proposed fix", category="action", source="you", board_id=board, x=1340, y=240, w=380, h=200,
            content="Pin checkpoint traffic to `mlx5_1` on nodes 12–15.\n\n- [ ] Change `CKPT_IFACE`\n- [ ] Rerun 6k steps",
        ))
        await call("create_board", name="Timeline", width=7680, height=2160)
        print("workspace:", w)


asyncio.run(main())
