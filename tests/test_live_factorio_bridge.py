import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from gar_ai.factorio_launcher import install_mod
from gar_ai.factorio_udp_bridge import (
    FactorioBridgeError,
    FactorioUdpBridge,
    FactorioUdpConfig,
)
from gar_ai.live_contract_probe import LiveContractProbe
from gar_ai.contracts import ContractProbeRegistry


class _FakeFactorioUdpServer:
    def __init__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.socket.settimeout(0.1)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.seen = {}
        self.state = {
            "game_tick": 100,
            "game_version": "2.0.77",
            "bridge_version": "0.1.0",
            "player": {
                "index": 1,
                "position": [10.0, 20.0],
                "surface": "nauvis",
                "force": "player",
                "inventory": {"iron-plate": 10},
            },
            "entities": [],
            "power": {"margin": None, "status": "unknown"},
            "resources": {
                "iron": {"stock": 10, "rate": None},
                "copper": {"stock": 0, "rate": None},
                "coal": {"stock": 0, "rate": None},
            },
            "research": {
                "technology": None,
                "progress": 0.0,
                "unit_count": None,
                "science_supply": {},
                "state": "idle",
            },
            "threat": {"level": "low"},
        }

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=1)
        self.socket.close()

    def _response(self, request, accepted=True, result=None, error=None):
        return {
            "protocol_version": "1.0",
            "bridge_version": "0.1.0",
            "request_id": request.get("request_id"),
            "operation_id": request.get("operation_id"),
            "type": "response",
            "accepted": accepted,
            "game_tick": self.state["game_tick"],
            "result": result or {},
            "error": error,
        }

    def _handle(self, request):
        operation_id = request.get("operation_id")
        if operation_id in self.seen:
            return self.seen[operation_id]

        op = request.get("op")
        payload = request.get("payload") or {}
        if op == "ping":
            response = self._response(
                request,
                result={
                    "game_tick": self.state["game_tick"],
                    "bridge_version": "0.1.0",
                    "base_version": "2.0.77",
                    "mod_version": "0.1.0",
                    "active_mods": {"base": "2.0.77", "gar-ai-bridge": "0.1.0"},
                },
            )
        elif op == "snapshot":
            response = self._response(request, result=self.state)
        elif op == "scan_area":
            response = self._response(
                request,
                result={
                    "center": payload.get("center"),
                    "radius": payload.get("radius"),
                    "entities": [],
                    "game_tick": self.state["game_tick"],
                },
            )
        elif op == "query_recipe":
            response = self._response(
                request,
                result={
                    "recipe": {
                        "name": payload.get("name"),
                        "enabled": True,
                        "ingredients": [],
                        "products": [],
                    },
                    "game_tick": self.state["game_tick"],
                },
            )
        elif op == "query_technology":
            response = self._response(
                request,
                result={
                    "technology": {
                        "name": payload.get("name"),
                        "enabled": True,
                        "researched": False,
                    },
                    "game_tick": self.state["game_tick"],
                },
            )
        elif op == "act":
            action = payload.get("action")
            params = payload.get("params") or {}
            if action == "move_to":
                self.state["game_tick"] += 1
                self.state["player"]["position"] = [
                    float(params["x"]),
                    float(params["y"]),
                ]
                response = self._response(request, result={"position": self.state["player"]["position"]})
            else:
                response = self._response(request, accepted=False, error=f"unsupported action: {action}")
        else:
            response = self._response(request, accepted=False, error=f"unsupported op: {op}")

        self.seen[operation_id] = response
        return response

    def _run(self):
        while not self.stop_event.is_set():
            try:
                data, address = self.socket.recvfrom(65535)
            except socket.timeout:
                continue
            request = json.loads(data.decode("utf-8"))
            response = self._handle(request)
            self.socket.sendto(json.dumps(response).encode("utf-8"), address)


class LiveFactorioBridgeTests(unittest.TestCase):
    def setUp(self):
        self.server = _FakeFactorioUdpServer().start()
        self.bridge = FactorioUdpBridge(
            FactorioUdpConfig(
                factorio_port=self.server.port,
                timeout_sec=0.5,
                retries=2,
            )
        )

    def tearDown(self):
        self.bridge.close()
        self.server.close()

    def test_real_udp_game_bridge_contract(self):
        ping = self.bridge.ping()
        self.assertEqual(ping["bridge_version"], "0.1.0")

        snapshot = self.bridge.snapshot()
        self.assertEqual(snapshot["player"]["position"], [10.0, 20.0])

        scan = self.bridge.scan_area((10.0, 20.0), 8)
        self.assertEqual(scan["radius"], 8.0)

        recipe = self.bridge.query_recipe("iron-gear-wheel")
        self.assertEqual(recipe["name"], "iron-gear-wheel")

        tech = self.bridge.query_technology("automation")
        self.assertEqual(tech["name"], "automation")

        ack = self.bridge.act("move_to", {"x": 10.5, "y": 20.0})
        self.assertTrue(ack.accepted)
        self.assertEqual(self.bridge.snapshot()["player"]["position"], [10.5, 20.0])

        rejected = self.bridge.act("not-real", {})
        self.assertFalse(rejected.accepted)

    def test_live_contract_probe_records_read_and_move_checks(self):
        with tempfile.TemporaryDirectory() as td:
            registry = ContractProbeRegistry(Path(td) / "probes.json")
            probe = LiveContractProbe(self.bridge, registry)
            records = probe.run_read_probes()
            records.append(probe.run_move_round_trip())

            self.assertTrue(all(record.status == "pass" for record in records))
            self.assertEqual(len(registry.load()), len(records))
            self.assertEqual(self.bridge.snapshot()["player"]["position"], [10.0, 20.0])

    def test_factorio_mod_installer_patches_major_version(self):
        with tempfile.TemporaryDirectory() as td:
            destination = install_mod(
                mods_dir=Path(td),
                factorio_major="2.1",
            )
            info = json.loads((destination / "info.json").read_text(encoding="utf-8"))
            self.assertEqual(info["factorio_version"], "2.1")
            self.assertTrue((destination / "control.lua").is_file())


class UnreachableBridgeTests(unittest.TestCase):
    """A silent Factorio side must never leak a raw socket exception.

    On Windows, sending to a localhost UDP port with no listener triggers ICMP
    port-unreachable, which surfaces as ConnectionResetError (WinError 10054) on
    the next recvfrom() rather than a timeout. The bridge has to retry and then
    report an actionable FactorioBridgeError.
    """

    class _ResetSocket:
        def __init__(self):
            self.sendto_calls = 0
            self.timeouts = []

        def bind(self, _address):
            return None

        def settimeout(self, value):
            self.timeouts.append(value)

        def sendto(self, _payload, _destination):
            self.sendto_calls += 1
            return len(_payload)

        def recvfrom(self, _size):
            raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。")

        def close(self):
            return None

    def test_connection_reset_is_retried_and_wrapped(self):
        fake = self._ResetSocket()
        bridge = FactorioUdpBridge(
            FactorioUdpConfig(factorio_port=34198, timeout_sec=1.0, retries=2)
        )
        bridge._socket.close()
        bridge._socket = fake

        with self.assertRaises(FactorioBridgeError) as cm:
            bridge.ping()

        self.assertEqual(fake.sendto_calls, 2)
        message = str(cm.exception)
        self.assertIn("--enable-lua-udp=34198", message)
        self.assertIn("load a save", message)
        self.assertIn("ConnectionResetError", message)


if __name__ == "__main__":
    unittest.main()
