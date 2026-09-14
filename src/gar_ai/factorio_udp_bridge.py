from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from .types import Ack

PROTOCOL_VERSION = "1.0"
DEFAULT_FACTORIO_UDP_PORT = 34198


class FactorioBridgeError(RuntimeError):
    """Raised when the live Factorio UDP bridge cannot satisfy a request."""


@dataclass(slots=True)
class FactorioUdpConfig:
    host: str = "127.0.0.1"
    factorio_port: int = DEFAULT_FACTORIO_UDP_PORT
    bind_host: str = "127.0.0.1"
    bind_port: int = 0
    timeout_sec: float = 2.0
    retries: int = 3
    player_index: int | None = None
    ensure_item_wait_sec: float = 20.0
    poll_interval_sec: float = 0.25

    @classmethod
    def from_env(cls) -> "FactorioUdpConfig":
        player = os.getenv("GAR_FACTORIO_PLAYER_INDEX")
        return cls(
            host=os.getenv("GAR_FACTORIO_HOST", "127.0.0.1"),
            factorio_port=int(
                os.getenv("GAR_FACTORIO_UDP_PORT", str(DEFAULT_FACTORIO_UDP_PORT))
            ),
            bind_host=os.getenv("GAR_FACTORIO_BIND_HOST", "127.0.0.1"),
            bind_port=int(os.getenv("GAR_FACTORIO_REPLY_PORT", "0")),
            timeout_sec=float(os.getenv("GAR_FACTORIO_TIMEOUT_SEC", "2.0")),
            retries=max(1, int(os.getenv("GAR_FACTORIO_RETRIES", "3"))),
            player_index=int(player) if player else None,
            ensure_item_wait_sec=float(
                os.getenv("GAR_FACTORIO_ENSURE_ITEM_WAIT_SEC", "20.0")
            ),
            poll_interval_sec=float(
                os.getenv("GAR_FACTORIO_POLL_INTERVAL_SEC", "0.25")
            ),
        )


class FactorioUdpBridge:
    """Real localhost UDP implementation of the V0 ``GameBridge`` contract.

    The Factorio mod receives packets through ``helpers.recv_udp`` and replies to
    this socket's source port. Requests are serialized through one lock so stale
    datagrams cannot be consumed by concurrent callers. Retries reuse both the
    request id and operation id; the Lua side deduplicates operation ids.
    """

    def __init__(self, config: FactorioUdpConfig | None = None) -> None:
        self.config = config or FactorioUdpConfig.from_env()
        if self.config.factorio_port <= 0 or self.config.factorio_port > 65535:
            raise ValueError("factorio_port must be in 1..65535")
        if self.config.timeout_sec <= 0:
            raise ValueError("timeout_sec must be > 0")
        if self.config.retries <= 0:
            raise ValueError("retries must be > 0")

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind((self.config.bind_host, self.config.bind_port))
        self._socket.settimeout(self.config.timeout_sec)
        self._lock = threading.Lock()
        self._closed = False
        self._last_meta: dict[str, Any] = {}

    @property
    def local_address(self) -> tuple[str, int]:
        host, port = self._socket.getsockname()[:2]
        return str(host), int(port)

    @property
    def last_meta(self) -> dict[str, Any]:
        return dict(self._last_meta)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._socket.close()

    def __enter__(self) -> "FactorioUdpBridge":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _payload(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        result = dict(payload or {})
        if self.config.player_index is not None:
            result.setdefault("player_index", self.config.player_index)
        return result

    def _request(
        self,
        op: str,
        payload: Mapping[str, Any] | None = None,
        *,
        operation_id: str | None = None,
        require_accepted: bool = True,
    ) -> dict[str, Any]:
        if self._closed:
            raise FactorioBridgeError("bridge is closed")

        request_id = str(uuid.uuid4())
        operation_id = operation_id or str(uuid.uuid4())
        request = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "operation_id": operation_id,
            "type": "request",
            "op": op,
            "payload": self._payload(payload),
        }
        encoded = json.dumps(request, separators=(",", ":")).encode("utf-8")
        destination = (self.config.host, self.config.factorio_port)

        with self._lock:
            for attempt in range(1, self.config.retries + 1):
                try:
                    self._socket.sendto(encoded, destination)
                    deadline = time.monotonic() + self.config.timeout_sec
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise socket.timeout
                        self._socket.settimeout(remaining)
                        data, address = self._socket.recvfrom(65535)
                        if address[0] not in {
                            self.config.host,
                            "127.0.0.1",
                            "::1",
                        }:
                            continue
                        try:
                            response = json.loads(data.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        if not isinstance(response, dict):
                            continue
                        if response.get("request_id") != request_id:
                            continue
                        if response.get("protocol_version") != PROTOCOL_VERSION:
                            raise FactorioBridgeError(
                                "Factorio bridge protocol version mismatch: "
                                f"{response.get('protocol_version')!r}"
                            )
                        self._last_meta = {
                            "game_tick": response.get("game_tick"),
                            "bridge_version": response.get("bridge_version"),
                            "operation_id": response.get("operation_id"),
                        }
                        if require_accepted and response.get("accepted") is not True:
                            raise FactorioBridgeError(
                                str(response.get("error") or "Factorio bridge rejected request")
                            )
                        return response
                except socket.timeout:
                    if attempt >= self.config.retries:
                        raise FactorioBridgeError(
                            "Factorio UDP bridge timed out. Start Factorio with "
                            f"--enable-lua-udp={self.config.factorio_port} and enable "
                            "the gar-ai-bridge mod."
                        )
                finally:
                    self._socket.settimeout(self.config.timeout_sec)

        raise FactorioBridgeError("Factorio UDP request failed")

    def ping(self) -> dict[str, Any]:
        response = self._request("ping")
        return dict(response.get("result") or {})

    def snapshot(self) -> dict[str, Any]:
        response = self._request("snapshot")
        result = response.get("result")
        if not isinstance(result, dict):
            raise FactorioBridgeError("snapshot response did not contain an object")
        return dict(result)

    def scan_area(
        self,
        center: tuple[float, float],
        radius: float,
    ) -> dict[str, Any]:
        response = self._request(
            "scan_area",
            {"center": [float(center[0]), float(center[1])], "radius": float(radius)},
        )
        return dict(response.get("result") or {})

    def query_recipe(self, name: str) -> dict[str, Any] | None:
        response = self._request("query_recipe", {"name": str(name)})
        result = dict(response.get("result") or {})
        recipe = result.get("recipe")
        return dict(recipe) if isinstance(recipe, dict) else None

    def query_technology(self, name: str) -> dict[str, Any] | None:
        response = self._request("query_technology", {"name": str(name)})
        result = dict(response.get("result") or {})
        technology = result.get("technology")
        return dict(technology) if isinstance(technology, dict) else None

    def act(self, action: str, params: Mapping[str, Any]) -> Ack:
        operation_id = str(uuid.uuid4())
        try:
            response = self._request(
                "act",
                {"action": str(action), "params": dict(params)},
                operation_id=operation_id,
                require_accepted=False,
            )
        except FactorioBridgeError as exc:
            return Ack(status="rejected", action_id=operation_id, detail=str(exc))

        accepted = response.get("accepted") is True
        detail = response.get("error")
        result = response.get("result")
        if detail is None and isinstance(result, dict):
            detail = json.dumps(result, ensure_ascii=False, sort_keys=True)

        if accepted and action == "ensure_item":
            item = str(params.get("item", ""))
            target = int(params.get("count", 0) or 0)
            if item and target > 0:
                deadline = time.monotonic() + self.config.ensure_item_wait_sec
                while time.monotonic() < deadline:
                    try:
                        state = self.snapshot()
                    except FactorioBridgeError:
                        break
                    current = int(
                        (state.get("player") or {})
                        .get("inventory", {})
                        .get(item, 0)
                        or 0
                    )
                    if current >= target:
                        detail = f"inventory reached target: {current}/{target}"
                        break
                    time.sleep(self.config.poll_interval_sec)

        return Ack(
            status="accepted" if accepted else "rejected",
            action_id=operation_id,
            detail=str(detail) if detail is not None else None,
        )


def create_bridge() -> FactorioUdpBridge:
    """Factory used by ``gar-ai --bridge-factory gar_ai.factorio_udp_bridge:create_bridge``."""

    return FactorioUdpBridge(FactorioUdpConfig.from_env())
