"""Configuration: YAML file in the data directory, with environment overrides."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


@dataclass
class CubeConfig:
    host: str = "ihost"
    app_name: str = "ihost-bridge"
    service_host: str | None = None
    public_host: str | None = None  # address browsers use for the bridge's UI cards
    port: int = 9900


@dataclass
class CardsConfig:
    heatpump: bool = False  # optional custom tile; the normal device cards are the default experience


@dataclass
class MideaDeviceConfig:
    device_id: int
    name: str | None = None
    ip: str | None = None


@dataclass
class MideaConfig:
    enabled: bool = True
    keys_file: Path | None = None
    discover: bool = True
    devices: list[MideaDeviceConfig] = field(default_factory=list)

    def device_config(self, device_id: int) -> MideaDeviceConfig | None:
        return next((d for d in self.devices if d.device_id == device_id), None)


@dataclass
class Config:
    data_dir: Path
    cube: CubeConfig
    midea: MideaConfig
    cards: CardsConfig = field(default_factory=CardsConfig)
    log_level: str = "INFO"


def _env(name: str, default: Any) -> Any:
    value = os.environ.get(name)
    return default if value in (None, "") else value


def load(path: str | None = None) -> Config:
    default_dir = "/data" if Path("/data").is_dir() else "data"
    data_dir = Path(_env("BRIDGE_DATA_DIR", default_dir)).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    cfg_path = Path(path or _env("BRIDGE_CONFIG", data_dir / "config.yaml"))
    raw: dict[str, Any] = {}
    if cfg_path.is_file():
        raw = yaml.safe_load(cfg_path.read_text()) or {}
        log.info("Loaded config from %s", cfg_path)
    else:
        log.info("No config file at %s; using defaults and environment", cfg_path)

    c = raw.get("cube") or {}
    cube = CubeConfig(
        host=str(_env("CUBE_HOST", c.get("host", "ihost"))),
        app_name=str(_env("CUBE_APP_NAME", c.get("app_name", "ihost-bridge"))),
        service_host=_env("CUBE_SERVICE_HOST", c.get("service_host")),
        public_host=_env("CUBE_PUBLIC_HOST", c.get("public_host")),
        port=int(_env("BRIDGE_PORT", c.get("port", 9900))),
    )
    cards_raw = raw.get("cards") or {}
    cards = CardsConfig(heatpump=bool(cards_raw.get("heatpump", False)))

    m = raw.get("midea") or {}
    devices = [
        MideaDeviceConfig(device_id=int(d["device_id"]), name=d.get("name"), ip=d.get("ip"))
        for d in (m.get("devices") or [])
    ]
    keys_file = _env("MIDEA_KEYS_FILE", m.get("keys_file"))
    midea = MideaConfig(
        enabled=bool(m.get("enabled", True)),
        keys_file=Path(keys_file).expanduser() if keys_file else data_dir / "midea-devices.json",
        discover=bool(m.get("discover", True)),
        devices=devices,
    )

    level = str(_env("LOG_LEVEL", raw.get("log_level", "INFO"))).upper()
    return Config(
        data_dir=data_dir,
        cube=cube,
        midea=midea,
        cards=cards,
        log_level=level,
    )
