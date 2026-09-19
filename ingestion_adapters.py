"""
Ingestion adapters for a genuinely LIVE event source, as opposed to
main.py's default of loading a complete file upfront.

We don't know the exact wire protocol the Prepathon's real replay
harness uses (the dataset README only specifies pacing/time-boundary
*semantics* in replay_config.json, not a transport) so this module
covers the three most likely delivery shapes for a "replay server":

  1. stdin        -- the replay tool pipes newline-delimited JSON into
                      this process's stdin as it "arrives"
                      (`python replay_tool.py | python main.py --stdin`)
  2. growing file -- the replay tool appends lines to a file over time
                      (classic `tail -f` pattern)
  3. TCP socket   -- the replay tool runs a server that streams
                      newline-delimited JSON to connected clients

All three are plain generators that `yield` one event dict at a time, in
true arrival order (NOT pre-sorted, since a live source can't be sorted
ahead of time) -- main.py's `Watermark` buffer is what handles ordering
downstream, exactly as it does for the batch file case.

If the real harness uses something else entirely (a provided Python
callback API, a message queue, a webhook), wrap that source in a
generator with the same shape (`yield json.loads(line)` per arriving
event) and pass it to `Orchestrator.run_stream(...)` directly -- nothing
else in the pipeline needs to change, since agents/StateBoard/etc only
ever see one event dict at a time regardless of where it came from.
"""
import json
import socket
import sys
import time
from typing import Iterator


def iter_stdin_jsonl() -> Iterator[dict]:
    """Blocks on stdin, yielding one event per line as it arrives.
    Use when the replay tool pipes directly into this process:
        python replay_tool.py | python main.py --stdin
    """
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        yield json.loads(line)


def iter_growing_file(path: str, poll_interval: float = 0.2, stop_after_idle: float = None) -> Iterator[dict]:
    """Tails a file that the replay tool is appending to, yielding each
    new complete line as it shows up. `stop_after_idle` (seconds), if
    set, stops iterating after that long with no new lines -- otherwise
    this runs forever (Ctrl-C to stop), which is appropriate for a
    genuinely open-ended live feed.
    """
    last_activity = time.time()
    with open(path, "r") as f:
        while True:
            line = f.readline()
            if line:
                line = line.strip()
                if line:
                    last_activity = time.time()
                    yield json.loads(line)
            else:
                if stop_after_idle is not None and (time.time() - last_activity) > stop_after_idle:
                    return
                time.sleep(poll_interval)


def iter_tcp_jsonl(host: str, port: int) -> Iterator[dict]:
    """Connects to a TCP server streaming newline-delimited JSON events
    (the common shape for a simple replay server) and yields each one as
    it arrives. Reconnects are NOT handled -- add retry/backoff here if
    the real harness's server can drop connections mid-stream.
    """
    with socket.create_connection((host, port)) as sock:
        buf = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if line:
                    yield json.loads(line.decode("utf-8"))
