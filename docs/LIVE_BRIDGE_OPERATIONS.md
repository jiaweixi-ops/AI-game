# Live bridge operations

Operational contract for running the controller against a **real** Factorio
instance through `factorio-mod/gar-ai-bridge` + `src/gar_ai/factorio_udp_bridge.py`.

This document exists because the bridge cannot work in every game state, and the
failure mode is silent: the UDP socket stays bound, `helpers.recv_udp()` returns
without error, and the controller simply sees timeouts. That is a Factorio
behaviour, not a controller bug.

## The one rule that matters

`recv_udp` only dispatches packets **while the game update loop is running**.
The Factorio runtime API states it explicitly:

> UDP socket when enabled requests 256KB of receive buffer from the operating
> system. If there is more data than this between two subsequent calls of this
> method, data will be lost. **That also applies to periods when the game is
> paused or is being saved as in those case the game update is not happening.**

The mod polls with `script.on_nth_tick(1, helpers.recv_udp)`. If the game does
not tick, the poll does not run, and the controller gets no answer.

### Game states and bridge behaviour

| State | Receives UDP | Notes |
| --- | --- | --- |
| Normal gameplay, window focused | yes | reference case |
| Normal gameplay, window in background | yes | no need to keep the window focused |
| Modal dialog open (settings, graphics options, ESC menu, mod list) | **no** | pauses the simulation |
| Game paused | **no** | |
| Save in progress | **no** | |
| During map load | **no** | the socket is opened only once the map is running |
| Main menu (no save loaded) | **no** | the mod has no game state yet |

If the controller reports repeated `TimeoutError` / "bridge did not answer",
**first check the game is actually simulating** before suspecting the code.

## Required launch configuration

The Lua UDP API must be enabled per instance:

```text
--enable-lua-udp=34198
```

Two ways to get it right:

- **Steam launch options** (recommended — survives the automatic restart
  Factorio performs when mods change). Steam → Factorio → Properties → Launch
  Options.
- Command line, e.g.
  `Factorio.exe --enable-lua-udp=34198 --load-game "<save.zip>"`.

Verify from the log — the socket line only carries an address once a map is
running:

```text
Info UDPSocket.cpp:38: Opening socket at (IP ADDR:({127.0.0.1:34198}))   # good
Info UDPSocket.cpp:44: Opening socket                                     # no --enable-lua-udp
```

## Do not run a second UDP-consuming mod

Any other enabled mod that also calls `helpers.recv_udp()` competes for the same
inbound datagrams. Symptom: the controller's requests are consumed by the other
mod, which replies with its own protocol, while the GAR mod reports
`packets_seen = 0`.

Keep exactly one bridge mod enabled per profile.

## Built-in diagnostics

The mod tracks its own health and exposes it three ways.

**In game** — chat command:

```text
/gar-ai-diag
```

```text
[gar-ai-bridge] packets_seen=6 handled=6 bad_json=0 bad_proto=0 handler_errors=0 recv_ack=2296 recv_errors=0 init_tick=0 now_tick=2296
```

**Every `ping` response** carries the same counters under `result.diag`.

**Factorio log** — first five polls are logged at startup, plus each handled
packet, e.g.:

```text
Script @__gar-ai-bridge__/control.lua:41: [gar-ai-bridge] poll #1 at tick 0
Script @__gar-ai-bridge__/control.lua:41: [gar-ai-bridge] packet 6 handled op=ping
```

### Reading the counters

| Observation | Meaning |
| --- | --- |
| `recv_ack` growing, equals `now_tick` | polling loop healthy, one call per tick |
| `recv_ack` static while you expect traffic | the game is not ticking (see the table above) |
| `recv_errors > 0` | `recv_udp` raised; `last_error` holds the message |
| `packets_seen` static after a known send | datagrams are not reaching the mod — check for a competing mod |
| `packets_seen` growing, `handled` not | a handler error; `last_error` holds the message |
| `game_tick` static across two pings | the simulation is paused |

## Why `pcall` wraps the receive path

`helpers.recv_udp()` and the per-request dispatch both run inside `pcall`. An
unexpected error in a single operation must not kill the tick handler that polls
for packets — an unprotected error there would permanently silence the bridge.
On failure the mod replies with `accepted = false` and a `handler error: ...`
reason instead of going quiet.

## Windows: unreachable localhost port

Sending to a localhost UDP port with no listener makes Windows answer with ICMP
port-unreachable, which surfaces as `ConnectionResetError` (WinError 10054) on
the next `recvfrom()` rather than a timeout. The Python bridge treats that as a
normal retryable "no answer yet" condition and converts it into a
`FactorioBridgeError` with an actionable message; `gar-factorio-probe` exits `2`
instead of printing a traceback.

## Known limitation

The controller is blind during pause/save/modal states. Work that must survive
those windows relies on the existing retry, budget and safe-stop behaviour —
there is no queuing that survives a pause on the Lua side.
