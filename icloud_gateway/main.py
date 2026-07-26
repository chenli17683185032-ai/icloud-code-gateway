from __future__ import annotations

import os

import uvicorn

from .config import Settings
from .web import create_app


def create_default_app():
    return create_app(Settings.from_environment())


def run() -> None:
    uvicorn.run(
        "icloud_gateway.main:create_default_app",
        factory=True,
        host=str(os.environ.get("ICLOUD_GATEWAY_HOST") or "0.0.0.0"),
        port=int(os.environ.get("ICLOUD_GATEWAY_PORT") or 8080),
        proxy_headers=True,
        forwarded_allow_ips=str(
            os.environ.get("ICLOUD_GATEWAY_FORWARDED_ALLOW_IPS") or "127.0.0.1"
        ),
    )


__all__ = ["create_default_app", "run"]
