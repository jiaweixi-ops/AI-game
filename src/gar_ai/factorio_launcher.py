from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .factorio_udp_bridge import FactorioBridgeError, FactorioUdpBridge, FactorioUdpConfig

MOD_NAME = "gar-ai-bridge"
DEFAULT_PORT = 34198


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def source_mod_dir() -> Path:
    path = repo_root() / "factorio-mod" / MOD_NAME
    if not path.exists():
        raise FileNotFoundError(f"Factorio mod source not found: {path}")
    return path


def factorio_user_dir() -> Path:
    override = os.getenv("FACTORIO_USER_DIR")
    if override:
        return Path(override).expanduser()
    system = platform.system().lower()
    if system == "windows":
        appdata = os.getenv("APPDATA")
        if not appdata:
            raise RuntimeError("APPDATA is not set; pass --mods-dir")
        return Path(appdata) / "Factorio"
    if system == "darwin":
        return Path.home() / "Library" / "Application Support" / "factorio"
    return Path.home() / ".factorio"


def default_mods_dir() -> Path:
    return factorio_user_dir() / "mods"


def _candidate_factorio_exes() -> list[Path]:
    candidates: list[Path] = []
    env = os.getenv("FACTORIO_EXE")
    if env:
        candidates.append(Path(env))

    if platform.system().lower() == "windows":
        roots = [
            Path(os.getenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)")),
            Path(os.getenv("PROGRAMFILES", r"C:\Program Files")),
        ]
        for root in roots:
            candidates.append(root / "Steam" / "steamapps" / "common" / "Factorio" / "bin" / "x64" / "factorio.exe")
        for drive in "CDEFGHI":
            candidates.append(Path(f"{drive}:\\SteamLibrary\\steamapps\\common\\Factorio\\bin\\x64\\factorio.exe"))
            candidates.append(Path(f"{drive}:\\Steam\\steamapps\\common\\Factorio\\bin\\x64\\factorio.exe"))
    else:
        for command in ("factorio",):
            found = shutil.which(command)
            if found:
                candidates.append(Path(found))
        candidates.extend(
            [
                Path.home() / ".steam" / "steam" / "steamapps" / "common" / "Factorio" / "bin" / "x64" / "factorio",
                Path.home() / ".local" / "share" / "Steam" / "steamapps" / "common" / "Factorio" / "bin" / "x64" / "factorio",
            ]
        )
    return candidates


def find_factorio_exe(explicit: str | Path | None = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return path
        raise FileNotFoundError(f"Factorio executable not found: {path}")
    for candidate in _candidate_factorio_exes():
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "Could not auto-detect Factorio. Pass --factorio-exe or set FACTORIO_EXE."
    )


def detect_factorio_major(exe: Path) -> str:
    try:
        result = subprocess.run(
            [str(exe), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return "2.0"
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    match = re.search(r"(?:Version:\s*)?(\d+)\.(\d+)\.\d+", text)
    if not match:
        return "2.0"
    return f"{match.group(1)}.{match.group(2)}"


def install_mod(
    *,
    mods_dir: Path,
    factorio_major: str,
) -> Path:
    source = source_mod_dir()
    destination = mods_dir / MOD_NAME
    mods_dir.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)

    info_path = destination / "info.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    info["factorio_version"] = factorio_major
    info_path.write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination


def launch_factorio(
    exe: Path,
    *,
    udp_port: int,
    save: str | Path | None = None,
    extra_args: list[str] | None = None,
) -> subprocess.Popen:
    command = [str(exe), f"--enable-lua-udp={int(udp_port)}"]
    if save:
        command.extend(["--load-game", str(Path(save).expanduser())])
    command.extend(extra_args or [])
    return subprocess.Popen(command)


def wait_for_bridge(
    *,
    port: int,
    timeout_sec: float,
    player_index: int | None = None,
) -> dict:
    deadline = time.monotonic() + timeout_sec
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with FactorioUdpBridge(
                FactorioUdpConfig(
                    factorio_port=port,
                    timeout_sec=1.0,
                    retries=1,
                    player_index=player_index,
                )
            ) as bridge:
                return bridge.ping()
        except (FactorioBridgeError, OSError) as exc:
            last_error = exc
            time.sleep(1.0)
    raise FactorioBridgeError(
        "Factorio started but the GAR bridge did not answer before timeout. "
        "Make sure a save is loaded and the gar-ai-bridge mod is enabled. "
        f"Last error: {last_error}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gar-factorio-launch",
        description="Install the GAR bridge mod, start Factorio with Lua UDP enabled, and verify connectivity.",
    )
    parser.add_argument("--factorio-exe")
    parser.add_argument("--mods-dir")
    parser.add_argument("--save", help="Optional save to load directly with --load-game")
    parser.add_argument("--udp-port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--player-index", type=int)
    parser.add_argument("--bridge-timeout-sec", type=float, default=120.0)
    parser.add_argument("--install-only", action="store_true")
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("factorio_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        exe = find_factorio_exe(args.factorio_exe)
        major = detect_factorio_major(exe)
        mods_dir = Path(args.mods_dir).expanduser() if args.mods_dir else default_mods_dir()
        installed = install_mod(mods_dir=mods_dir, factorio_major=major)
        print(f"Installed {MOD_NAME} -> {installed}")
        print(f"Detected Factorio executable: {exe}")
        print(f"Factorio mod compatibility: {major}")

        if args.install_only:
            return 0

        process = launch_factorio(
            exe,
            udp_port=args.udp_port,
            save=args.save,
            extra_args=list(args.factorio_args or []),
        )
        print(f"Factorio started (pid={process.pid}) with UDP port {args.udp_port}")
        if args.no_wait:
            return 0

        reply = wait_for_bridge(
            port=args.udp_port,
            timeout_sec=args.bridge_timeout_sec,
            player_index=args.player_index,
        )
        print("GAR Factorio bridge is LIVE:")
        print(json.dumps(reply, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (FileNotFoundError, FactorioBridgeError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
