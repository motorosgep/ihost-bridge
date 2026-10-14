"""Entry point: `python -m bridge [--config path]`."""

from __future__ import annotations

import argparse
import asyncio
import logging

from bridge.app import BridgeApp
from bridge.config import load


def main() -> None:
    parser = argparse.ArgumentParser(prog="ihost-bridge")
    parser.add_argument("--config", help="path to config.yaml (default: <data dir>/config.yaml)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load(args.config)
    logging.getLogger().setLevel("DEBUG" if cfg.log_level == "TRACE" else cfg.log_level)
    # midea-local is chatty at DEBUG (every packet); keep it at INFO unless explicitly asked for
    logging.getLogger("midealocal").setLevel(logging.DEBUG if cfg.log_level == "TRACE" else max(logging.INFO, logging.getLogger().level))

    try:
        asyncio.run(BridgeApp(cfg).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
