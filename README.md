# AI-game — Factorio Autonomous Agent Lite

Current code target: **V0 live-bridge bring-up + V0.5 Engineering Freeze implementation**.

> The LLM chooses the future; deterministic code moves reality toward that future.

## Status discipline

Implementation state and acceptance state are deliberately separate:

- V0/V0.5 Python kernel: **IMPLEMENTED / MOCK_VERIFIED**
- real Factorio localhost UDP Bridge + Lua Mod: **IMPLEMENTED / CI_VERIFIED**
- live GameBridge connection: **RUNNABLE, REQUIRES LOCAL FACTORIO PROCESS**
- real current-save V0 acceptance: **NOT YET LIVE_VERIFIED**
- real smelting V0.5 acceptance: **NOT YET LIVE_VERIFIED**

Passing unit tests does not count as a live Factorio release acceptance. The next release gate is a real save running the included Mod and Live Contract Probe.

## Real Factorio bridge

This repository now contains both sides of the real bridge:

```text
Python controller
  -> src/gar_ai/factorio_udp_bridge.py
  -> localhost UDP / JSON protocol v1.0
  -> factorio-mod/gar-ai-bridge/control.lua
  -> Factorio runtime Lua API
```

The Mod exposes the existing `GameBridge` contract:

- `snapshot`
- `scan_area`
- `query_recipe`
- `query_technology`
- `act`

The first live action set is:

- `move_to`
- `ensure_item` (hand crafting only; no free item injection)
- `place_entity` (consumes an item before creating the entity)
- `transfer`
- `set_recipe`
- `start_research`

UDP retries reuse the same operation id and the Mod caches recent operation results, preventing duplicate side effects when a response packet is lost.

### The bridge is silent while the game is not simulating

`recv_udp` only dispatches packets while the game update loop runs. During a
pause, a save, a map load, an open modal dialog (settings, ESC menu), or on the
main menu the socket stays bound but **no packets are delivered**, and the
controller sees plain timeouts. This is documented Factorio behaviour, not a
controller bug. In-game diagnostics are available through `/gar-ai-diag`.

Do not enable a second UDP-consuming mod alongside `gar-ai-bridge` — it competes
for the same inbound datagrams.

See [`docs/LIVE_BRIDGE_OPERATIONS.md`](docs/LIVE_BRIDGE_OPERATIONS.md) for the
full state table, the required launch configuration, and how to read the
counters.

## Fastest Windows bring-up

From the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\start-factorio-live.ps1
```

That script:

1. installs the Python package in editable mode;
2. installs `gar-ai-bridge` into the Factorio mods directory;
3. detects the installed Factorio major version and patches the copied Mod manifest accordingly;
4. starts Factorio with `--enable-lua-udp=34198`;
5. waits for a real save to load and the Lua Mod to answer;
6. runs the read-only Live Contract Probe.

To load a specific save directly:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\start-factorio-live.ps1 `
  -Save "$env:APPDATA\Factorio\saves\your-save.zip"
```

To also verify a reversible real write, move the player 0.5 tiles and restore the original position:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\start-factorio-live.ps1 -ProbeMove
```

If Factorio is installed outside the common Steam paths:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\start-factorio-live.ps1 `
  -FactorioExe "D:\SteamLibrary\steamapps\common\Factorio\bin\x64\factorio.exe"
```

## Manual bridge bring-up

Install/launch Factorio and wait for a live save:

```bash
gar-factorio-launch --save /path/to/save.zip
```

Run the real contract probe without any AI API key:

```bash
gar-factorio-probe
```

Run the reversible write probe too:

```bash
gar-factorio-probe --write-move-round-trip
```

The probe registry is written to:

```text
runtime/live/contract_probes.json
```

## Starting the AI controller against the real game

After the bridge probe passes, configure an OpenAI-compatible model endpoint:

```powershell
$env:GAR_AI_ENDPOINT="https://your-provider.example/v1/chat/completions"
$env:GAR_AI_MODEL="your-model"
$env:GAR_AI_API_KEY="..."
$env:GAR_FACTORIO_UDP_PORT="34198"

gar-ai --bridge-factory gar_ai.factorio_udp_bridge:create_bridge --runtime-dir runtime/live
```

Or let the PowerShell helper start the AI after the live probe:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\start-factorio-live.ps1 -StartAI
```

The API key is **not required** to prove that Python and the real game are connected. It is only required when starting the strategic AI controller.

## V0 trusted kernel

The repository keeps the V0 guarantees: ACK + fresh-state verified writes, desired-state/idempotent tasks, bounded replan, Watchdog, incident bundles, safe AI fallback and event-driven control.

## V0-RC2 runtime hardening

- `gar-ai` CLI / `python -m gar_ai` is the controller entry point;
- runtime SAFE_HOLD semantics are enforced inside `ControllerLoop.step()`, so custom `while ctl.step()` drivers cannot bypass them;
- AI/provider `safe_stop` enters SAFE_HOLD without terminating the 24/7 process;
- `ControllerLoop.resume()` forces a `user_resume` strategic resynchronization;
- CLI installs separate shutdown and resume signals: `SIGINT/SIGTERM` stop cleanly, while `SIGUSR1` (POSIX) or `SIGBREAK` (Windows when available) resumes SAFE_HOLD;
- optional `--duration-sec` provides a clean supervised-run stop path;
- Keeper in-memory action history is bounded and configurable with `--action-log-limit`, while full history stays in JSONL;
- Global Batch Budget remains persistent but writes are batched by action/time thresholds, with forced flushes at batch checkpoints and terminal transitions;
- graceful shutdown fsyncs runtime state files;
- V0.5 smelting verification still requires a positive real product-rate threshold.

## V0.5 implemented infrastructure

- schema/version contracts for tools, Digest, TaskSpec, prompts, Blueprint IR and Contract Probes;
- unified error semantics and policies;
- tick/timestamp freshness metadata;
- Global + Task + Batch + Atomic budget layering;
- persistent area locks and material reservations;
- non-blocking async verification (`pending -> verified/timeout/failed`);
- operation journal / transaction boundary for batch work;
- structured failure fingerprints and duplicate-recovery rejection support;
- replan rate guard;
- formal runtime metrics;
- versioned Blueprint IR;
- deterministic Site Planner;
- parameterized 8/16/24/48-furnace smelting layouts;
- Batch Executor with checkpoints, partial-failure accounting, lock/reservation release and final production verification.

## Runtime control

- `SIGINT` / `SIGTERM`: graceful shutdown.
- `SIGUSR1` on POSIX: leave SAFE_HOLD and resume with a fresh `user_resume` sync.
- `SIGBREAK` on Windows when available: same resume behavior.
- embedding code can always call `ControllerLoop.resume()` directly.

## Safety behavior

A failed batch does **not** automatically demolish player construction. It records a partial transaction, releases unused reservations/area locks, emits structured evidence, and expects desired-state reconciliation/replan.

An AI/provider failure does not terminate the 24/7 controller. The controller enters `SAFE_HOLD`, continues heartbeat/state availability, and waits for an explicit resume.

## Tests

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```

## Current release gate

Do not expand to V1 yet.

1. run `gar-factorio-launch` against a real Factorio installation;
2. load a real current save with the included Mod enabled;
3. pass `gar-factorio-probe` read probes;
4. pass the reversible `move_to` live probe;
5. complete one non-prewritten V0 objective on the current save;
6. force a blocked condition and prove replan/recovery;
7. restart mid-task and prove desired-state reconciliation;
8. only then run the V0.5 real smelting acceptance: site selection, reservation, batch construction, power and mandatory production-rate verification.
