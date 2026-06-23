# PPK2 MCP server

A [Model Context Protocol](https://modelcontextprotocol.io) (stdio) server that
lets an MCP client — Claude Code, Claude Desktop, Cursor — drive a Nordic
**Power Profiler Kit II (PPK2)** over its USB serial port: set mode/voltage,
power a device-under-test (DUT), measure current/power/energy, and capture the
8 digital channels as a logic analyser.

The server holds the serial port open for its whole lifetime and serialises
every tool call behind one lock, so it is *the* single owner of the PPK2 — the
client talks to the device only through these tools.

## Requirements

- A PPK2 connected over USB
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Python ≥ 3.10 (uv will fetch one if needed)

## Setup

```bash
uv sync        # create .venv and install dependencies from uv.lock
```

Find your PPK2's serial port:

```bash
ls /dev/cu.usbmodem*     # macOS — the lower-numbered port is the control interface
ls /dev/ttyACM*          # Linux
```

Run the server standalone (it speaks MCP over stdio, so this is mostly a smoke
test — Ctrl-C to exit):

```bash
PPK2_PORT=/dev/cu.usbmodemXXXX uv run ppk2_mcp_server.py
```

## Use with Claude Code

`.mcp.json` in this repo registers the server for Claude Code. Update the
`--directory` path and `PPK2_PORT` to match your machine:

```json
{
  "mcpServers": {
    "ppk2": {
      "command": "uv",
      "args": ["run", "--directory", "/abs/path/to/PPK2-MCP", "ppk2_mcp_server.py"],
      "env": { "PPK2_PORT": "/dev/cu.usbmodemXXXX" }
    }
  }
}
```

Open Claude Code in this directory; it picks up `.mcp.json` automatically.
Approve the project server (or run `/mcp`) and confirm it shows **connected**.

## Tools

| Tool | What it does |
|------|--------------|
| `ppk2_status`     | Connection state, mode, voltage, DUT power, available ports. Never opens the port. |
| `ppk2_configure`  | Open the port, read calibration, set meter mode (`ampere`/`source`) + voltage. |
| `ppk2_power`      | Turn DUT power output `ON`/`OFF`. |
| `ppk2_measure`    | Sample current for N seconds → summary stats (current/power/charge/energy). |
| `ppk2_logic`      | Capture the 8 digital channels (D0–D7) → per-channel duty, edges, activity. |
| `ppk2_disconnect` | Release the serial port. |

### Modes

- **ampere** — the PPK2 measures current drawn from an *external* supply (it does
  not power the DUT). `voltage_mv` is still used for the gain/offset calibration,
  so set it to your external rail voltage.
- **source** — the PPK2 *sources* `voltage_mv` to the DUT and measures the
  current it draws. Use `ppk2_power("ON")` to enable the output rail.

### Serial port resolution

First match wins: a tool's explicit `port` arg → `PPK2_PORT` env var →
autodetection via `ppk2_api.list_devices()`.

## Notes

- A serial line has a single owner — only one process can hold the PPK2 at a
  time. Stop other PPK2 tools (e.g. nRF Connect Power Profiler) before using this.
- Unconnected digital pins float and read a constant/noisy level; drive them from
  your DUT to see real logic activity.
