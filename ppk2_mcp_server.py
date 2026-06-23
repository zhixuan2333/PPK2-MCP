#!/usr/bin/env python3
"""
PPK2 MCP server
===============

A Model Context Protocol (stdio) server that lets an MCP client — Claude Code,
Claude Desktop, Cursor, etc. — drive a Nordic **Power Profiler Kit II (PPK2)**:
set its mode/voltage, power a device-under-test (DUT), and take current/power
measurements with summarised statistics.

It is a thin, stateful wrapper around the `ppk2_api` library. The PPK2 talks
over a serial line, and a serial line has a single owner, so this server holds
the port open for its whole lifetime and serialises every tool call behind one
lock. That makes the MCP server *the* owner of the PPK2 — the client talks to
the device only through these tools.

Serial port resolution (first match wins):
  1. the `port` argument passed to a tool
  2. the PPK2_PORT environment variable
  3. autodetection via ppk2_api.list_devices()

Tools
-----
  ppk2_status      – connection state, mode, voltage, DUT power, available ports
  ppk2_configure   – open the port, read calibration, set meter mode + voltage
  ppk2_power       – turn DUT power ON or OFF
  ppk2_measure     – sample current for N seconds, return summary statistics
  ppk2_logic       – capture the 8 digital channels (logic analyser), per-channel stats
  ppk2_disconnect  – release the serial port

Run standalone:  python3 ppk2_mcp_server.py
"""

from __future__ import annotations

import os
import time
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from ppk2_api.ppk2_api import PPK2_API, PPK2_Modes

# --- voltage limits, mirrored from ppk2_api so we can validate before opening ---
VDD_LOW_MV = 800
VDD_HIGH_MV = 5000

mcp = FastMCP("ppk2")


# --------------------------------------------------------------------------- #
# Connection state (one device, one lock, held for the server's lifetime)
# --------------------------------------------------------------------------- #
@dataclass
class Ppk2State:
    lock: threading.Lock = field(default_factory=threading.Lock)
    dev: Optional[PPK2_API] = None
    port: Optional[str] = None
    mode: Optional[str] = None          # "AMPERE_MODE" | "SOURCE_MODE"
    voltage_mv: Optional[int] = None
    dut_power: str = "OFF"              # last commanded DUT power state

    def close(self) -> None:
        if self.dev is not None:
            try:
                # best effort: stop sampling and drop DUT power before releasing
                try:
                    self.dev.stop_measuring()
                except Exception:
                    pass
                try:
                    self.dev.toggle_DUT_power("OFF")
                except Exception:
                    pass
                self.dev.ser.close()
            except Exception:
                pass
        self.dev = None
        self.port = None
        self.mode = None
        self.voltage_mv = None
        self.dut_power = "OFF"


STATE = Ppk2State()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _resolve_candidates(port: Optional[str]) -> list[str]:
    """Ordered ports to try: explicit arg > PPK2_PORT env > autodetected.

    The PPK2 exposes two CDC serial interfaces but only the control one answers
    `get_modifiers()`, so autodetection returns *all* candidates (lowest-numbered
    first, which is usually the control port) and the caller probes each.
    """
    if port:
        return [port]
    env_port = os.environ.get("PPK2_PORT")
    if env_port:
        return [env_port]
    try:
        found = PPK2_API.list_devices()
    except Exception:
        found = []
    return sorted(found)


def _resolve_port(port: Optional[str]) -> Optional[str]:
    """First candidate port (for status/display); None if nothing is found."""
    candidates = _resolve_candidates(port)
    return candidates[0] if candidates else None


def _available_ports() -> list[str]:
    try:
        return PPK2_API.list_devices()
    except Exception:
        return []


def _open(port: str) -> PPK2_API:
    """Open the serial port and load the device's calibration modifiers."""
    if not os.path.exists(port):
        raise RuntimeError(
            f"Serial port '{port}' does not exist. Plug in the PPK2 and check the "
            f"path (e.g. `ls /dev/cu.usbmodem*` on macOS). "
            f"Available ports now: {_available_ports() or 'none'}"
        )
    # ppk2_api forces baudrate internally; pass a generous read timeout so a
    # missing/half-open bridge fails fast instead of blocking forever.
    dev = PPK2_API(port, timeout=1, write_timeout=1, exclusive=True)
    ok = dev.get_modifiers()
    if not ok:
        try:
            dev.ser.close()
        except Exception:
            pass
        raise RuntimeError(
            f"Opened '{port}' but could not read PPK2 calibration metadata. "
            f"Is a real PPK2 on the other end of the bridge, and is it the "
            f"control interface (the lower-numbered usbmodem port)?"
        )
    return dev


def _ensure_connected(port: Optional[str]) -> PPK2_API:
    """Return a live device, probing/(re)opening the port if needed."""
    candidates = _resolve_candidates(port)
    if not candidates:
        raise RuntimeError(
            "No PPK2 port given, PPK2_PORT is unset, and autodetection found no "
            "device. Pass `port`, set PPK2_PORT, or plug in the PPK2."
        )
    # Reuse the existing connection unless the caller asked for a different port.
    if STATE.dev is not None and (not port or port == STATE.port):
        return STATE.dev

    STATE.close()
    errors: list[str] = []
    for cand in candidates:
        try:
            STATE.dev = _open(cand)
            STATE.port = cand
            return STATE.dev
        except Exception as e:  # not the control interface, or busy — try next
            errors.append(f"{cand}: {e}")
    raise RuntimeError(
        "Could not open any PPK2 candidate port. Tried:\n  "
        + "\n  ".join(errors)
    )


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@mcp.tool()
def ppk2_status() -> dict[str, Any]:
    """Report PPK2 connection and configuration state.

    Returns whether a device is connected, the resolved/candidate serial port,
    the active meter mode, the configured source/reference voltage, the last
    commanded DUT power state, and any serial ports that look like a PPK2.
    Safe to call any time; never opens the port.
    """
    with STATE.lock:
        return {
            "connected": STATE.dev is not None,
            "port": STATE.port or _resolve_port(None),
            "port_exists": (lambda p: bool(p) and os.path.exists(p))(
                STATE.port or _resolve_port(None)
            ),
            "mode": STATE.mode,
            "voltage_mv": STATE.voltage_mv,
            "dut_power": STATE.dut_power,
            "available_ports": _available_ports(),
            "env_PPK2_PORT": os.environ.get("PPK2_PORT"),
        }


@mcp.tool()
def ppk2_configure(
    mode: str = "ampere",
    voltage_mv: int = 3300,
    port: Optional[str] = None,
) -> dict[str, Any]:
    """Open the PPK2 and configure it for measurement.

    Opens the serial port (if not already open), reads the device calibration,
    selects the meter mode, and sets the voltage. Call this before `ppk2_measure`.

    Args:
        mode: "ampere" — the PPK2 measures current drawn from an *external*
              supply (it does not power the DUT). `voltage_mv` is still required
              and is used for the gain/offset calibration, so set it to your
              external rail voltage.
              "source" — the PPK2 *sources* `voltage_mv` to the DUT and measures
              the current it draws. Use `ppk2_power("ON")` to enable the output.
        voltage_mv: Source/reference voltage in millivolts (800–5000).
        port: Serial port override; defaults to PPK2_PORT or autodetection.

    Returns the resulting status.
    """
    mode_norm = mode.strip().lower()
    if mode_norm in ("ampere", "ampere_mode", "amp", "current"):
        mode_const = PPK2_Modes.AMPERE_MODE
    elif mode_norm in ("source", "source_mode", "src"):
        mode_const = PPK2_Modes.SOURCE_MODE
    else:
        raise ValueError(f"mode must be 'ampere' or 'source', got {mode!r}")

    if not (VDD_LOW_MV <= voltage_mv <= VDD_HIGH_MV):
        raise ValueError(
            f"voltage_mv must be {VDD_LOW_MV}–{VDD_HIGH_MV} mV, got {voltage_mv}"
        )

    with STATE.lock:
        dev = _ensure_connected(port)
        if mode_const == PPK2_Modes.AMPERE_MODE:
            dev.use_ampere_meter()
        else:
            dev.use_source_meter()
        STATE.mode = mode_const
        dev.set_source_voltage(voltage_mv)
        STATE.voltage_mv = voltage_mv
        return {
            "connected": True,
            "port": STATE.port,
            "mode": STATE.mode,
            "voltage_mv": STATE.voltage_mv,
            "dut_power": STATE.dut_power,
            "note": (
                "Source mode: call ppk2_power('ON') to enable the output rail."
                if mode_const == PPK2_Modes.SOURCE_MODE
                else "Ampere mode: PPK2 measures current from your external supply."
            ),
        }


@mcp.tool()
def ppk2_power(state: str) -> dict[str, Any]:
    """Turn the DUT power output ON or OFF.

    In source mode this enables/disables the voltage the PPK2 sources to the DUT.
    In ampere mode it connects/disconnects the PPK2's internal switch in the
    current path. Requires `ppk2_configure` to have been called first.

    Args:
        state: "ON" or "OFF".
    """
    s = state.strip().upper()
    if s not in ("ON", "OFF"):
        raise ValueError(f"state must be 'ON' or 'OFF', got {state!r}")
    with STATE.lock:
        if STATE.dev is None:
            raise RuntimeError("Not connected. Call ppk2_configure first.")
        STATE.dev.toggle_DUT_power(s)
        STATE.dut_power = s
        return {"dut_power": STATE.dut_power}


@mcp.tool()
def ppk2_measure(
    duration_seconds: float = 1.0,
    settle_ms: int = 200,
    include_series: bool = False,
    series_points: int = 100,
) -> dict[str, Any]:
    """Sample current for a fixed duration and return summary statistics.

    Starts continuous sampling, discards an initial settling window, collects
    samples for `duration_seconds`, stops, and computes statistics. Requires a
    prior `ppk2_configure`. The PPK2 streams ~100k samples/s, so raw samples are
    never returned by default — only aggregates (and an optional downsampled
    series).

    Args:
        duration_seconds: How long to collect samples (after settling). 0.05–60.
        settle_ms: Initial data discarded before timing starts, to skip the
            switch-on transient. Default 200 ms.
        include_series: If true, also return a downsampled current series.
        series_points: Target number of points in the downsampled series.

    Returns current statistics in microamps (µA), plus average power (µW),
    charge (µC) and energy (µJ) when a voltage is configured. `samples` is the
    raw sample count; `sample_rate_hz` is the achieved rate.
    """
    if not (0.05 <= duration_seconds <= 60):
        raise ValueError("duration_seconds must be between 0.05 and 60")
    if include_series and series_points < 1:
        raise ValueError("series_points must be >= 1 when include_series is true")

    with STATE.lock:
        dev = STATE.dev
        if dev is None:
            raise RuntimeError("Not connected. Call ppk2_configure first.")
        if STATE.voltage_mv is None or STATE.mode is None:
            raise RuntimeError("Not configured. Call ppk2_configure first.")

        samples: list[float] = []
        dev.start_measuring()
        try:
            # Settling window: read and discard so the start-up spike and any
            # stale buffer don't skew the stats.
            settle_end = time.monotonic() + max(0, settle_ms) / 1000.0
            while time.monotonic() < settle_end:
                dev.get_data()
                time.sleep(0.005)

            t0 = time.monotonic()
            t_end = t0 + duration_seconds
            while time.monotonic() < t_end:
                raw = dev.get_data()
                if raw:
                    s, _digital = dev.get_samples(raw)
                    samples.extend(s)
                else:
                    time.sleep(0.002)
            # Drain whatever is already buffered so we use the full window.
            raw = dev.get_data()
            if raw:
                s, _digital = dev.get_samples(raw)
                samples.extend(s)
            elapsed = time.monotonic() - t0
        finally:
            dev.stop_measuring()
            dev.get_data()  # flush so the next command doesn't choke on stale bytes

    n = len(samples)
    if n == 0:
        raise RuntimeError(
            "No samples received. Is the DUT powered (ppk2_power('ON')) and the "
            "serial bridge healthy?"
        )

    mean = sum(samples) / n
    smin = min(samples)
    smax = max(samples)
    var = sum((x - mean) ** 2 for x in samples) / n
    std = var ** 0.5

    result: dict[str, Any] = {
        "samples": n,
        "duration_s": round(elapsed, 4),
        "sample_rate_hz": round(n / elapsed) if elapsed > 0 else None,
        "current_uA": {
            "mean": round(mean, 4),
            "min": round(smin, 4),
            "max": round(smax, 4),
            "std": round(std, 4),
        },
        "mode": STATE.mode,
        "voltage_mv": STATE.voltage_mv,
        "dut_power": STATE.dut_power,
    }

    # Power/energy need a voltage; we always have one once configured.
    if STATE.voltage_mv is not None:
        v = STATE.voltage_mv / 1000.0  # volts
        power_uW = mean * v            # µA * V = µW
        result["power_uW"] = {
            "mean": round(power_uW, 4),
            "min": round(smin * v, 4),
            "max": round(smax * v, 4),
        }
        result["charge_uC"] = round(mean * elapsed, 4)          # µA * s
        result["energy_uJ"] = round(power_uW * elapsed, 4)      # µW * s
        result["charge_uAh"] = round(mean * elapsed / 3600.0, 6)

    if include_series:
        result["series_uA"] = _downsample(samples, series_points)

    return result


@mcp.tool()
def ppk2_logic(
    duration_seconds: float = 1.0,
    settle_ms: int = 100,
    channels: Optional[list[int]] = None,
    include_series: bool = False,
    series_points: int = 100,
) -> dict[str, Any]:
    """Capture the PPK2's 8 digital logic channels (D0–D7) — a logic analyser.

    The PPK2 samples 8 digital inputs alongside current at ~100k samples/s.
    This starts sampling, discards a settling window, collects digital states
    for `duration_seconds`, and returns per-channel statistics: the fraction of
    time each channel was high, the number of edges (level transitions), and the
    first/last observed level. Requires a prior `ppk2_configure`.

    Note: unconnected digital pins float and may read a constant or noisy level;
    drive them from your DUT to see real activity.

    Args:
        duration_seconds: Capture window after settling. 0.05–60.
        settle_ms: Initial samples discarded before timing starts. Default 100 ms.
        channels: Which channels (0–7) to report. Default all eight.
        include_series: If true, also return a downsampled level series per channel
            (each point is the fraction high over that block, 0.0–1.0).
        series_points: Target number of points in each downsampled series.

    Returns sample count, achieved sample rate, and a `channels` map keyed by
    channel index.
    """
    if not (0.05 <= duration_seconds <= 60):
        raise ValueError("duration_seconds must be between 0.05 and 60")
    chans = list(range(8)) if channels is None else sorted(set(channels))
    for ch in chans:
        if not (0 <= ch <= 7):
            raise ValueError(f"channels must be in 0–7, got {ch}")
    if include_series and series_points < 1:
        raise ValueError("series_points must be >= 1 when include_series is true")

    with STATE.lock:
        dev = STATE.dev
        if dev is None or STATE.mode is None:
            raise RuntimeError("Not configured. Call ppk2_configure first.")

        bits: list[int] = []
        dev.start_measuring()
        try:
            settle_end = time.monotonic() + max(0, settle_ms) / 1000.0
            while time.monotonic() < settle_end:
                dev.get_data()
                time.sleep(0.005)

            t0 = time.monotonic()
            t_end = t0 + duration_seconds
            while time.monotonic() < t_end:
                raw = dev.get_data()
                if raw:
                    _s, digital = dev.get_samples(raw)
                    bits.extend(digital)
                else:
                    time.sleep(0.002)
            raw = dev.get_data()
            if raw:
                _s, digital = dev.get_samples(raw)
                bits.extend(digital)
            elapsed = time.monotonic() - t0
        finally:
            dev.stop_measuring()
            dev.get_data()  # flush stale bytes

    n = len(bits)
    if n == 0:
        raise RuntimeError(
            "No digital samples received. Is the serial link healthy?"
        )

    # Single pass: per-channel high count and edge count.
    high = {ch: 0 for ch in chans}
    edges = {ch: 0 for ch in chans}
    prev: Optional[int] = None
    for b in bits:
        for ch in chans:
            if (b >> ch) & 1:
                high[ch] += 1
        if prev is not None:
            x = prev ^ b
            if x:
                for ch in chans:
                    if (x >> ch) & 1:
                        edges[ch] += 1
        prev = b

    first, last = bits[0], bits[-1]
    out_channels: dict[str, Any] = {}
    for ch in chans:
        ch_high = high[ch]
        entry: dict[str, Any] = {
            "high_fraction": round(ch_high / n, 4),
            "duty_pct": round(100.0 * ch_high / n, 2),
            "edges": edges[ch],
            "first_level": (first >> ch) & 1,
            "last_level": (last >> ch) & 1,
            "activity": "toggling" if edges[ch] > 0 else (
                "high" if (first >> ch) & 1 else "low"
            ),
        }
        if include_series:
            entry["series"] = _downsample(
                [(b >> ch) & 1 for b in bits], series_points
            )
        out_channels[str(ch)] = entry

    return {
        "samples": n,
        "duration_s": round(elapsed, 4),
        "sample_rate_hz": round(n / elapsed) if elapsed > 0 else None,
        "channels": out_channels,
        "mode": STATE.mode,
        "voltage_mv": STATE.voltage_mv,
        "dut_power": STATE.dut_power,
    }


@mcp.tool()
def ppk2_disconnect() -> dict[str, Any]:
    """Release the serial port (stops sampling and drops DUT power first).

    Use this to hand the port to another process, or to recover from a wedged
    connection — the next tool call will reopen it.
    """
    with STATE.lock:
        was = STATE.dev is not None
        STATE.close()
        return {"disconnected": was}


def _downsample(samples: list[float], target: int) -> list[float]:
    """Block-average `samples` down to about `target` points."""
    n = len(samples)
    if target >= n:
        return [round(x, 4) for x in samples]
    block = n / target
    out: list[float] = []
    i = 0.0
    while int(i) < n:
        start = int(i)
        end = min(int(i + block), n)
        if end <= start:
            end = start + 1
        chunk = samples[start:end]
        out.append(round(sum(chunk) / len(chunk), 4))
        i += block
    return out


if __name__ == "__main__":
    try:
        mcp.run()  # stdio transport
    finally:
        STATE.close()
