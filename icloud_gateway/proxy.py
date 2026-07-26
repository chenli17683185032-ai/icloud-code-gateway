from __future__ import annotations

import os
import socket
import sys
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


class ProxyConfigurationError(ValueError):
    pass


def _boolean(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ProxyConfigurationError("proxy required flag must be a boolean")


def _config_token(value: str, *, field: str) -> str:
    token = str(value or "")
    if any(character.isspace() or character == "#" for character in token):
        raise ProxyConfigurationError(f"proxy {field} is invalid")
    return token


@dataclass(frozen=True)
class ProxySpec:
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def requests_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        credentials = ""
        if self.username:
            encoded_username = urllib.parse.quote(self.username, safe="")
            encoded_password = urllib.parse.quote(self.password, safe="")
            credentials = f"{encoded_username}:{encoded_password}@"
        return f"{self.scheme}://{credentials}{host}:{self.port}"

    @property
    def chromium_url(self) -> str:
        if self.username:
            raise ProxyConfigurationError(
                "authenticated browser proxies require an authentication-free relay"
            )
        scheme = "socks5" if self.scheme in {"socks5", "socks5h"} else "http"
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"{scheme}://{host}:{self.port}"

    @property
    def proxychains_line(self) -> str:
        proxy_type = "socks5" if self.scheme in {"socks5", "socks5h"} else "http"
        line = f"{proxy_type} {self.host} {self.port}"
        if self.username:
            line = f"{line} {self.username} {self.password}"
        return line.rstrip()


def parse_proxy_spec(
    server: str | None,
    *,
    username: str | None = None,
    password: str | None = None,
    required: bool = False,
) -> ProxySpec | None:
    raw_server = str(server or "").strip()
    if not raw_server:
        if required:
            raise ProxyConfigurationError("proxy server is required")
        return None
    try:
        parsed = urllib.parse.urlsplit(raw_server)
        port = parsed.port
    except ValueError as exc:
        raise ProxyConfigurationError("proxy server is invalid") from exc
    scheme = parsed.scheme.casefold()
    if (
        scheme not in {"http", "socks5", "socks5h"}
        or not parsed.hostname
        or port is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ProxyConfigurationError("proxy must be HTTP or SOCKS5 with an explicit port")
    resolved_username = str(username or "").strip()
    resolved_password = str(password or "")
    if not resolved_username and parsed.username is not None:
        resolved_username = urllib.parse.unquote(parsed.username)
    if not resolved_password and parsed.password is not None:
        resolved_password = urllib.parse.unquote(parsed.password)
    host = _config_token(parsed.hostname, field="host")
    resolved_username = _config_token(resolved_username, field="username")
    resolved_password = _config_token(resolved_password, field="password")
    if resolved_password and not resolved_username:
        raise ProxyConfigurationError("proxy username is required when password is set")
    return ProxySpec(
        scheme=scheme,
        host=host,
        port=port,
        username=resolved_username,
        password=resolved_password,
    )


def proxy_from_environment(
    prefix: str,
    environment: Mapping[str, str | None] | None = None,
) -> ProxySpec | None:
    values = os.environ if environment is None else environment
    required = _boolean(values.get(f"{prefix}_REQUIRED"), default=False)
    server = values.get(f"{prefix}_SERVER") or values.get(prefix)
    return parse_proxy_spec(
        server,
        username=values.get(f"{prefix}_USERNAME"),
        password=values.get(f"{prefix}_PASSWORD"),
        required=required,
    )


def write_proxychains_config(path: str | Path, proxy: ProxySpec) -> Path:
    target = Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    content = "\n".join(
        (
            "strict_chain",
            "proxy_dns",
            "remote_dns_subnet 224",
            "tcp_read_time_out 15000",
            "tcp_connect_time_out 8000",
            "localnet 127.0.0.0/255.0.0.0",
            "localnet 10.0.0.0/255.0.0.0",
            "localnet 172.16.0.0/255.240.0.0",
            "localnet 192.168.0.0/255.255.0.0",
            "[ProxyList]",
            proxy.proxychains_line,
            "",
        )
    )
    temporary = target.with_name(f".{target.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, target)
        target.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def render_browser_proxy_config(
    path: str | Path,
    environment: Mapping[str, str | None] | None = None,
) -> ProxySpec | None:
    target = Path(path)
    proxy = proxy_from_environment("BROWSER_PROXY", environment)
    if proxy is None:
        target.unlink(missing_ok=True)
        return None
    if proxy.username:
        raise ProxyConfigurationError(
            "authenticated browser proxies require an authentication-free relay"
        )
    try:
        resolved_host = socket.gethostbyname(proxy.host)
    except OSError as exc:
        raise ProxyConfigurationError("browser proxy host cannot be resolved") from exc
    browser_proxy = ProxySpec(
        scheme=proxy.scheme,
        host=resolved_host,
        port=proxy.port,
        username=proxy.username,
        password=proxy.password,
    )
    write_proxychains_config(target, browser_proxy)
    return browser_proxy


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("usage: proxy.py OUTPUT", file=sys.stderr)
        return 2
    try:
        proxy = render_browser_proxy_config(arguments[0])
    except (OSError, ProxyConfigurationError):
        print("browser proxy configuration is invalid", file=sys.stderr)
        return 2
    if proxy is not None:
        print(proxy.chromium_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ProxyConfigurationError",
    "ProxySpec",
    "parse_proxy_spec",
    "proxy_from_environment",
    "render_browser_proxy_config",
    "write_proxychains_config",
]
