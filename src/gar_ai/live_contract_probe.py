from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

from .contracts import ContractProbeRecord, ContractProbeRegistry
from .factorio_udp_bridge import FactorioBridgeError, FactorioUdpBridge, FactorioUdpConfig


def _hash_mods(mods: Any) -> str | None:
    if not isinstance(mods, dict) or not mods:
        return None
    payload = json.dumps(mods, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class LiveContractProbe:
    """Read-mostly live probes for the real Factorio bridge.

    Destructive or strategically meaningful writes are intentionally not run by
    default. The optional move round-trip probe changes only the player's
    position and immediately restores it.
    """

    def __init__(
        self,
        bridge: FactorioUdpBridge,
        registry: ContractProbeRegistry,
        *,
        recipe_name: str = "iron-gear-wheel",
        technology_name: str = "automation",
    ) -> None:
        self.bridge = bridge
        self.registry = registry
        self.recipe_name = recipe_name
        self.technology_name = technology_name
        self._ping: dict[str, Any] = {}

    def _identity(self) -> tuple[str, str, str | None]:
        if not self._ping:
            self._ping = self.bridge.ping()
        return (
            str(self._ping.get("base_version") or "unknown"),
            str(self._ping.get("bridge_version") or "unknown"),
            _hash_mods(self._ping.get("active_mods")),
        )

    def _record(
        self,
        primitive: str,
        status: str,
        evidence: dict[str, Any],
    ) -> ContractProbeRecord:
        game_version, bridge_version, mod_set_hash = self._identity()
        record = ContractProbeRecord(
            primitive=primitive,
            game_version=game_version,
            bridge_version=bridge_version,
            mod_set_hash=mod_set_hash,
            status=status,
            evidence=evidence,
        )
        self.registry.record(record)
        return record

    def _run(
        self,
        primitive: str,
        fn: Callable[[], tuple[bool, dict[str, Any]]],
    ) -> ContractProbeRecord:
        try:
            ok, evidence = fn()
            return self._record(primitive, "pass" if ok else "fail", evidence)
        except Exception as exc:  # probe output must survive one failed primitive
            return self._record(
                primitive,
                "fail",
                {"error": f"{type(exc).__name__}: {exc}"},
            )

    def run_read_probes(self) -> list[ContractProbeRecord]:
        self._ping = self.bridge.ping()
        records: list[ContractProbeRecord] = []
        records.append(
            self._record(
                "ping",
                "pass",
                {
                    "reply": self._ping,
                    "local_address": self.bridge.local_address,
                },
            )
        )

        def snapshot_probe() -> tuple[bool, dict[str, Any]]:
            snap = self.bridge.snapshot()
            required = {
                "game_tick",
                "player",
                "entities",
                "power",
                "resources",
                "research",
                "threat",
            }
            missing = sorted(required - set(snap))
            player = snap.get("player") or {}
            ok = not missing and isinstance(player.get("position"), list)
            return ok, {
                "missing": missing,
                "game_tick": snap.get("game_tick"),
                "player": {
                    "index": player.get("index"),
                    "position": player.get("position"),
                    "surface": player.get("surface"),
                    "force": player.get("force"),
                },
                "entity_count": len(snap.get("entities") or []),
            }

        records.append(self._run("snapshot", snapshot_probe))

        def scan_probe() -> tuple[bool, dict[str, Any]]:
            snap = self.bridge.snapshot()
            position = (snap.get("player") or {}).get("position") or [0, 0]
            result = self.bridge.scan_area(
                (float(position[0]), float(position[1])),
                8.0,
            )
            return isinstance(result.get("entities"), list), {
                "center": result.get("center"),
                "radius": result.get("radius"),
                "entity_count": len(result.get("entities") or []),
                "game_tick": result.get("game_tick"),
            }

        records.append(self._run("scan_area", scan_probe))

        def recipe_probe() -> tuple[bool, dict[str, Any]]:
            recipe = self.bridge.query_recipe(self.recipe_name)
            return recipe is not None, {
                "name": self.recipe_name,
                "recipe": recipe,
            }

        records.append(self._run("query_recipe", recipe_probe))

        def tech_probe() -> tuple[bool, dict[str, Any]]:
            tech = self.bridge.query_technology(self.technology_name)
            return tech is not None, {
                "name": self.technology_name,
                "technology": tech,
            }

        records.append(self._run("query_technology", tech_probe))

        def reject_probe() -> tuple[bool, dict[str, Any]]:
            ack = self.bridge.act("__contract_probe_invalid_action__", {})
            return not ack.accepted, {
                "status": ack.status,
                "detail": ack.detail,
            }

        records.append(self._run("act_reject_unknown", reject_probe))
        return records

    def run_move_round_trip(self, *, delta_x: float = 0.5) -> ContractProbeRecord:
        def probe() -> tuple[bool, dict[str, Any]]:
            before = self.bridge.snapshot()
            position = (before.get("player") or {}).get("position") or [0, 0]
            x0, y0 = float(position[0]), float(position[1])
            target_x = x0 + float(delta_x)

            outward = self.bridge.act("move_to", {"x": target_x, "y": y0})
            after_out = self.bridge.snapshot()
            out_pos = (after_out.get("player") or {}).get("position") or []
            out_ok = (
                outward.accepted
                and len(out_pos) >= 2
                and abs(float(out_pos[0]) - target_x) <= 0.25
                and abs(float(out_pos[1]) - y0) <= 0.25
            )

            back = self.bridge.act("move_to", {"x": x0, "y": y0})
            after_back = self.bridge.snapshot()
            back_pos = (after_back.get("player") or {}).get("position") or []
            back_ok = (
                back.accepted
                and len(back_pos) >= 2
                and abs(float(back_pos[0]) - x0) <= 0.25
                and abs(float(back_pos[1]) - y0) <= 0.25
            )
            return out_ok and back_ok, {
                "before": [x0, y0],
                "outward_target": [target_x, y0],
                "outward_after": out_pos,
                "returned_after": back_pos,
                "outward_ack": outward.status,
                "return_ack": back.status,
            }

        return self._run("move_to", probe)


def _print_records(records: list[ContractProbeRecord]) -> None:
    for record in records:
        marker = "PASS" if record.status == "pass" else record.status.upper()
        print(f"[{marker:7}] {record.primitive}")
        if record.status != "pass":
            print(json.dumps(record.evidence, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gar-factorio-probe",
        description="Run live contract probes against the GAR Factorio UDP mod.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=34198)
    parser.add_argument("--timeout-sec", type=float, default=2.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--player-index", type=int)
    parser.add_argument("--recipe", default="iron-gear-wheel")
    parser.add_argument("--technology", default="automation")
    parser.add_argument(
        "--registry",
        default="runtime/live/contract_probes.json",
    )
    parser.add_argument(
        "--write-move-round-trip",
        action="store_true",
        help="Also move the player 0.5 tiles and immediately restore the position.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = FactorioUdpConfig(
        host=args.host,
        factorio_port=args.port,
        timeout_sec=args.timeout_sec,
        retries=args.retries,
        player_index=args.player_index,
    )
    registry = ContractProbeRegistry(Path(args.registry))
    try:
        with FactorioUdpBridge(config) as bridge:
            probe = LiveContractProbe(
                bridge,
                registry,
                recipe_name=args.recipe,
                technology_name=args.technology,
            )
            records = probe.run_read_probes()
            if args.write_move_round_trip:
                records.append(probe.run_move_round_trip())
    except FactorioBridgeError as exc:
        print(f"Factorio bridge unavailable: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(
            "Factorio bridge unavailable (socket error): "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2

    _print_records(records)
    failed = [record for record in records if record.status != "pass"]
    if failed:
        print(f"{len(failed)} probe(s) failed; see {args.registry}", file=sys.stderr)
        return 1
    print(f"All {len(records)} live probes passed. Registry: {args.registry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
