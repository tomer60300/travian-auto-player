# Resumable Cross-Device Operations

Long-running operations (oasis raid, farm-list run, auto-scout, build
queue, farm builder) must survive iOS Safari going to background, page
reloads, bfcache restores, and cross-device control. A user who starts a
3-hour build queue from their iPhone needs to be able to monitor it from
their laptop, and a Stop pressed on the laptop must reach the op even
though the laptop didn't start it.

This document describes the protocol contract and client behavior. The
implementation lives in:

- `src/travian_api/operation_manager.py` — server-side op runner
- `src/travian_api/web/execution_sessions.py` — message buffer + fan-out
- `src/travian_api/web/routes/exec_sessions.py` — `/ws/sessions/{id}/stream`
- `src/travian_api/web/ws/_resumable.py` — starter-WS history-replay helper
- `frontend/src/hooks/useResumableOperation.js` — React hook

## Why this exists

The naive design — one starter WebSocket per op, op tied to that WS — has
three failure modes Travian players hit constantly:

1. **iOS Safari background** kills WebSockets within ~30s of the tab
   leaving the foreground. When the user returns, the op is gone.
2. **bfcache restore** brings back the page from snapshot WITHOUT firing
   `visibilitychange`, so a reattach handler keyed on visibility never
   runs.
3. **Cross-device control** — the user wants to start an op from their
   phone and stop it from their desk. With one WS, only the originating
   device can stop.

The resumable-ops design decouples the operation's lifetime from any
single WebSocket. Ops live in a server-side registry; any client with a
JWT and the `session_id` can subscribe, replay history, tail live
output, and request stop.

## Server protocol

### Registry

`ExecutionSessionManager` keeps:

- A 2000-message ring buffer per session (sized to cover most multi-hour
  ops without truncation).
- A `dict[sub_id → asyncio.Queue]` of live subscribers per session. New
  messages fan out to every queue; full queues drop the oldest entry to
  make room.
- `status: "running" | "disconnected"`. `disconnected` means the op
  coroutine has terminated; the session sticks around for 24h so a
  late-arriving subscriber can still read history and the terminal
  message.

### Operation lifecycle

`operation_manager.start(user_id, label, session_type, session_label, session, coro, require_unique_label=False, require_unique_extras=())`:

1. Atomic uniqueness check — `require_unique_label` rejects a duplicate
   start (returns None) so the WS handler can reply `already_running`
   with the existing `session_id`.
2. Create `ExecutionSession`; expose `session_id` to the starter WS.
3. Wrap the coro in `_run()` which translates every termination to a
   terminal `operation_complete` message:
   - `coro` returned + `should_stop()` was set → status=`stopped`
   - `coro` returned + tail buffer has `{"type":"error","fatal":true}` → status=`failed`
   - `coro` returned otherwise → status=`completed`
   - `coro` raised `CancelledError` → status=`stopped`
   - `coro` raised any other exception → status=`failed` + a fatal error frame is pushed first
4. After the terminal frame, `mark_disconnected()` flips status and pushes
   the `None` sentinel to every subscriber queue (which causes the stream
   handler to send `{type:"session_ended"}` and close).

The terminal message is ALWAYS pushed BEFORE `mark_disconnected`, so a
late-attaching subscriber that catches up via history sees the terminal
frame in-band rather than just `session_ended`.

### Stream WebSocket

`WS /ws/sessions/{session_id}/stream?token=<JWT>` accepts any number of
concurrent subscribers per session. Per connection:

```
{type: "session_meta", id, session_type, label, status, created_at}
{type: "history", messages: [...]}
↓ live ↓
{type: "message", data: <original op frame>}
{type: "message", data: ...}
...
{type: "session_ended"}                  ← when mark_disconnected fires
```

Plus error envelopes:

```
{type: "error", message: "Session not found or expired"}   ← code 4004
{type: "error", message: "Access denied"}                  ← code 4003
```

The WS also reads incoming frames; `{action:"stop"}` from any subscriber
calls `operation_manager.request_stop(session_id)`. `_listen_for_stop` is
a separate task that runs alongside the streaming loop so a stop request
doesn't have to wait for the next outgoing frame to be processed.

### Starter WS protocol

Per-feature starter WSs (`/ws/oasis-raider`, `/ws/farm/run/{id}`, etc.)
emit:

```
{type: "session_init", session_id: "<hex>"}    ← capture this
↓ op-specific frames ↓
{type: "operation_complete", status: "completed"|"stopped"|"failed"}
```

Or, when `require_unique_label` rejects a duplicate start:

```
{type: "already_running", session_id: "<existing-hex>", message: "..."}
```

The starter WS also accepts `{action:"stop"}` and forwards to
`operation_manager.request_stop`, so a client connected to the starter
can stop without first reaching for the session-stream.

## Client protocol — `useResumableOperation`

```js
const op = useResumableOperation('oasis-raider', {
  onMessage: (data) => { /* ... */ },
  onStatusChange: (status) => { /* ... */ },
})

op.start('/ws/oasis-raider', config)   // start a fresh op
op.stop()                              // stop the running op
op.sessionId                           // current session_id (or null)
op.status                              // 'idle'|'connecting'|'running'|'reconnecting'|'completed'|'stopped'|'failed'
```

### Storage

`localStorage[`resumableOp:<opType>`]` holds the active `session_id`.
On mount, the hook reads it and immediately subscribes to
`/ws/sessions/{id}/stream` (status starts as `reconnecting`). On a
terminal frame the key is cleared.

The `opType` namespaces sessions per feature, so a user can run an oasis
raid AND a build queue concurrently without one's reattach overwriting
the other.

### Status state machine

```
idle  ──start()──>  connecting  ──session_init──>  running
                                                  │
                                                  ↓
                                       (terminal: completed|stopped|failed)
                                                  ↑
mount  ──hasSessionId──>  reconnecting  ──session_meta(running)──┘
visibilitychange/pageshow ──┘
```

Status transitions are intentionally narrow:

- `onReconnected` does NOT flip to `running` — it stays `reconnecting`
  until `session_meta` confirms the server-side session is still live.
  Otherwise a stop pressed in the gap could fire on a session that
  completed during the disconnect.
- `session_meta` with non-running status clears `pendingStopRef` (no
  point sending a stop for a session that's already terminal).

### Stop semantics

`stop()` is idempotent and works in three windows:

1. **Status `running` + WS open** → send `{action:"stop"}` immediately.
2. **Status `connecting` or `reconnecting`** → set `pendingStopRef`; the
   drain points are the starter `session_init` handler (which fires when
   the op confirms server-side) and the session-stream `session_meta`
   handler (gated on `data.status === "running"`).
3. **Status terminal (`completed`/`failed`/`stopped`/`idle`)** → no-op
   that clears `pendingStopRef` defensively.

When `stop()` is called with no live socket but a stored `session_id`
(e.g. user tapped Stop seconds after page mount before subscribe
completed), the hook actively reaches the op via
`subscribeToExisting()` so the queued stop drains as soon as the new
stream attaches.

### History replay deduplication

Every server-side message carries a `ts` (added by
`ExecutionSessionManager.push`). The hook tracks `lastSeenTsRef` so a
reconnect that replays the full ring buffer doesn't refire toasts, log
appends, or completion handlers for messages the page already saw.

The same `lastSeenTsRef` covers starter-WS-delivered messages too, so a
later session-stream reconnect after Safari resume doesn't double-deliver
the early frames.

### Mount/unmount safety

- `mountedRef` flips to false on cleanup. Starter `onClose` and the
  consumer's `onMessageRef` calls are gated on it so a queued WS frame
  delivered after unmount doesn't poke React state on a dead component.
- `setStatusBoth` and the `setSessionId` half of `persistSessionId`
  short-circuit after unmount (statusRef and localStorage updates still
  run — terminal handlers MUST be able to clear stored session_id even
  if the component unmounted, otherwise the next mount resumes a dead
  session).

### Auto-reconnect interaction

`createWebSocket(reconnect: true)` in `frontend/src/ws.js` drives
session-stream reconnect with `wait_random_exponential` backoff. The
hook's terminal handlers MUST call `closeWs()` (via `closeIfStillCurrent`)
so the captured stream path doesn't keep reopening after the server
sends `session_ended` and closes — that loop produced the
"Stop-button-does-nothing" symptom because each reconnect's
`session_meta` arrived with `status="disconnected"` and cleared the
queued stop.

`closeIfStillCurrent()` checks `wsHandleRef.current === myHandle` before
closing so a sync reattach inside the same terminal frame doesn't close
the brand-new socket.

## Failure modes the design handles

| Scenario | What happens |
|---|---|
| iOS Safari background → return | `pageshow` (or `visibilitychange`) fires, hook reattaches, history replays, dedupe filter drops the prior frames. |
| bfcache restore | Same as above — `pageshow` with `persisted=true` triggers reattach. |
| Page reload mid-op | localStorage holds session_id; mount effect subscribes immediately, status starts as `reconnecting`. |
| Cross-device stop | Laptop pressing Stop opens its own session-stream and sends `{action:"stop"}`; phone's stream sees the resulting `operation_complete{status:"stopped"}` and updates UI. |
| Network drop mid-op | createWebSocket auto-reconnects; on next `session_meta` we either confirm running (drain pending stop) or see terminal (clear it). |
| Server restart | session is gone; stream sends `{type:"error","Session not found or expired"}` with code 4004; hook clears stored session_id, sets `failed`. |
| 24h TTL expiration | Same as server restart from client's perspective. |
| User tries to start a duplicate op | Starter WS sends `already_running` with the existing session_id; hook reattaches via `subscribeToExisting()`. |

## Testing

- `tests/test_operation_manager.py` covers atomic uniqueness, terminal
  status classification, FAILED-on-fatal, captcha-stop cleanup, and the
  KeyedLock interactions.
- Manual iPhone test plan: start a long op, switch to WhatsApp / lock
  screen, return → `reconnecting → running`, no duplicate logs.

## Trade-offs and constraints

- **24h TTL** — a session that's been idle longer than this is gone.
  Long-running auto-builders that exceed 24h between checkpoints would
  need a different design (we don't have any today).
- **Server-local registry** — fanning out a session across multiple
  backend processes would require a pub/sub layer (Redis or similar).
  Current target is single-process self-hosted.
- **Ring buffer truncation** — at 2000 messages per session, an op that
  spams logs faster than that loses early history. Verbose modes should
  be used sparingly during long ops.
- **Stop is best-effort** — `request_stop` flips `should_stop()`; the
  coro must check it. A coro stuck in a long network call won't react
  until the call returns. Per-feature ops poll `should_stop()` between
  natural checkpoints.
