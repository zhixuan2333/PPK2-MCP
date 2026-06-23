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
- [uv](https://docs.astral.sh/uv/) (the installer below will fetch it if missing)
- Python ≥ 3.10 (uv will fetch one if needed)
- [Claude Code](https://claude.com/claude-code) CLI (optional, for auto-registration)

## One-shot install

```bash
git clone https://github.com/zhixuan2333/PPK2-MCP && cd PPK2-MCP
./install.sh          # installs uv, syncs deps, registers the MCP server
./install.sh --run    # ...and immediately launches Claude with a test prompt
```

`install.sh` registers the `ppk2` server with the Claude Code CLI (`claude mcp
add`, user scope). The serial port is **autodetected** — no path to configure.
Then in Claude:

> Use the ppk2 MCP tools to check the PPK2: call `ppk2_status`, then configure
> source mode at 3.3V, power the DUT on, measure current for 2 seconds, capture
> the logic channels for 1 second, and finally power off and disconnect.

## Manual setup

```bash
uv sync     # create .venv and install dependencies from uv.lock
```

Run the server standalone (speaks MCP over stdio, so this is mostly a smoke
test — Ctrl-C to exit). The PPK2 port is autodetected; override with `PPK2_PORT`
if needed:

```bash
uv run ppk2_mcp_server.py
# or pin a port:  PPK2_PORT=/dev/cu.usbmodemXXXX uv run ppk2_mcp_server.py
```

`.mcp.json` in this repo also registers the server for any Claude Code session
opened in this directory (autodetected port, no edits needed). Approve the
project server (or run `/mcp`) and confirm it shows **connected**.

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
