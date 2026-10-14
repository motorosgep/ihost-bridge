"""Run only the history recorder: `python -m bridge.recorder_cli --host <gateway> --token <token>`.

Useful for development, or for homes without Midea appliances that just want
history for Butter. The add-on runs the recorder inside the bridge instead.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

import aiohttp
from aiohttp import web

from bridge.core.recorder import Recorder
from bridge.cube.client import CubeClient
from bridge.history_api import history_routes


async def serve(host: str, token: str | None, data_dir: Path, port: int, app_name: str, snapshot: float) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(force_close=True)) as session:
        client = CubeClient(session, host, app_name, data_dir)
        if token:
            client.token, client.token_valid = token, True
        if not client.token:
            print("No token: press Allow on the gateway's console within five minutes")
            if not await client.wait_for_token():
                raise SystemExit("timed out waiting for the token")
        recorder = Recorder(client, session, data_dir / "history.sqlite", snapshot_interval=snapshot)
        app = web.Application()
        app["recorder"] = recorder
        app.add_routes(history_routes())
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        logging.getLogger(__name__).info("History API on port %d, recording from %s", port, host)
        task = asyncio.create_task(recorder.run(stop))
        await stop.wait()
        task.cancel()
        await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(prog="ihost-bridge recorder")
    parser.add_argument("--host", default="ihost")
    parser.add_argument("--token", help="gateway access token (otherwise <data dir>/cube_token.json or the Allow flow)")
    parser.add_argument("--data-dir", default="data-recorder")
    parser.add_argument("--port", type=int, default=9901)
    parser.add_argument("--app-name", default="ihost-bridge")
    parser.add_argument("--snapshot", type=float, default=900.0, help="seconds between full snapshots")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(serve(args.host, args.token, Path(args.data_dir), args.port, args.app_name, args.snapshot))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
