# How it works

This document explains the shape of the service: why every external dependency sits behind
an interface, how a chat message flows through the async streaming path, how conversations
are stored and resumed, and how the telemetry layer serves both a laptop and a production
cluster. It is written for someone who will extend or operate the service, not for someone
trying to run it for the first time (see `README.md` for that).

## 1. Three seams, each with a stub and a production implementation

The service depends on three things it does not own: a language model, somewhere to keep
chat history, and somewhere to send traces and metrics. Each is expressed as a small
`typing.Protocol` and chosen at startup from configuration.

| Seam | Protocol | Local / test implementation | Production implementation |
|------|----------|-----------------------------|---------------------------|
| Model | `app/llm/base.py` `LLMProvider` | `StubProvider` | `AnthropicProvider` (Anthropic API or Azure AI / Foundry) |
| Chat history | `app/storage/base.py` `ChatStore` | `SqliteChatStore` | `ElasticChatStore` |
| Observability | `app/telemetry/base.py` `Telemetry` | `FileTelemetry` | `ElasticApmTelemetry` |

A `factory.py` next to each protocol reads `Settings` and returns one implementation. The
rest of the application only ever sees the protocol. The FastAPI app factory in
`app/main.py` wires the chosen instances onto `app.state`, and route handlers receive them
through the dependency aliases in `app/deps.py` (`LLMDep`, `ChatStoreDep`, `TelemetryDep`).

### Why protocols rather than base classes

`Protocol` is structural: an implementation does not import or inherit anything from the
core, so a new backend can live in its own package with its own optional dependencies. This
is why `elasticsearch` and `elastic-apm` are optional extras and are imported lazily inside
the factory branch that needs them. A laptop running the stubs installs neither.

### What this buys for local development

- **No credentials, no cost.** The default configuration touches nothing outside the
  repository: SQLite in `./data`, a stub model, telemetry to a file.
- **Deterministic tests.** `tests/test_app.py` runs the real ASGI application end to end,
  including the SSE stream, with the stub's lag set to zero. The tests exercise the same
  route code that talks to Claude in production; only the leaf implementation differs.
- **Realistic timing without a model.** The stub is not a canned HTTP fixture. It emits the
  same event sequence a real provider does (thinking, then text, then usage) with random
  lead time and randomised pacing, so the UI's loading states, cancellation behaviour and
  timeouts are exercised the way they will be in production.

### What this buys for production

- **Swapping a backend is configuration, not a code change.** `APP_LLM_PROVIDER=azure`
  and `APP_CHAT_STORE=elastic` select different leaves; nothing above them changes.
- **Provider quirks stay in the provider.** The Anthropic API supports server-side refusal
  fallbacks; Foundry does not. That difference is expressed once, in the two classmethods
  that build `AnthropicProvider`, and the chat route never learns about it.
- **The seams are the natural places to add cross-cutting behaviour.** The tracing proxy in
  `app/telemetry/traced_store.py` wraps any `ChatStore` or `UserStore` without knowing
  which one it is. A retry wrapper, a cache, or a read-replica router would be built the
  same way.

## 2. The streaming path

A chat turn is a single HTTP request that stays open until the model has finished. Nothing
is buffered end to end: bytes leave the server as soon as the provider yields them.

```
browser ──POST /api/conversations/{id}/messages──▶ FastAPI route
                                                     │ auth, ownership, budget check
                                                     │ persist user message
                                                     ▼
                                          provider.stream(turns)  ── async generator
                                                     │ StreamEvent objects
                                                     ▼
                                          events() async generator ── formats SSE
                                                     │
◀────────── text/event-stream, one event per chunk ──┘
```

### Provider-neutral events

`LLMProvider.stream()` is an async generator of `StreamEvent` values with a small closed set
of types: `thinking_start`, `thinking_delta`, `thinking_stop`, `text_delta`, `done`. The
`done` event carries token usage and the stop reason. This is the whole contract between a
provider and the rest of the service. The Anthropic implementation maps the SDK's
`content_block_start` / `content_block_delta` / `content_block_stop` events onto it; the
stub generates it directly.

Keeping this set small is deliberate. Tool use, citations and images would be added as new
event types, and every consumer (the SSE route today, perhaps a websocket or a batch job
later) handles them by matching on `type`.

### Server-sent events over a POST

The response is `text/event-stream` produced by a `StreamingResponse` wrapping an async
generator. SSE was chosen over websockets because a chat turn is a request/response pair
with a long response, which is exactly what SSE models, and because it needs no extra
infrastructure at a load balancer. The browser cannot use `EventSource` for a POST with a
bearer token, so `index.html` reads the response body with `fetch` and parses the
`event:` / `data:` framing itself (about fifteen lines).

The route emits a `user_message` event first, before the model is called, so the UI can
confirm persistence and update the conversation title immediately. Every subsequent
provider event is forwarded as it arrives. `done` is sent only after the assistant message
and usage record are committed, so a client that sees `done` knows the turn is durable.

### Performance notes

- **Everything on the hot path is `async`.** The SQLite driver is `aiosqlite`, the
  Anthropic client is `AsyncAnthropic`, and Elasticsearch uses `AsyncElasticsearch`. One
  uvicorn worker can hold many open streams because a stream that is waiting on the model
  costs a coroutine, not a thread.
- **No per-token database writes.** The user message is written once before streaming and
  the assistant message once after. Tokens are accumulated in memory in a list and joined
  at the end. Writing on every delta would multiply database load by the number of tokens
  for no benefit.
- **Backpressure is natural.** The route's generator only pulls the next provider event
  after the previous SSE frame has been handed to the ASGI server. A slow client slows its
  own stream and nothing else.
- **Proxy buffering is disabled explicitly.** The response sets `Cache-Control: no-cache`
  and `X-Accel-Buffering: no`. Without the latter, nginx and some cloud load balancers
  buffer the whole response and the user sees nothing until the model finishes.
- **The stub costs nothing but sleeps.** Its pacing uses `asyncio.sleep`, so a hundred
  concurrent stub streams still run on one worker. Its inter-token delays are drawn from
  an exponential distribution around a per-response rate, which is what a real token
  stream looks like, rather than a fixed interval.
- **Large `max_tokens` needs streaming.** The Anthropic SDK refuses non-streaming requests
  it estimates could exceed its HTTP timeout. Using the stream helper avoids that entirely
  and gives the user output as it is generated.
- **Where to look when it is slow.** Every request produces a `llm.stream` span and a set
  of `store.<method>` spans under the same transaction (see section 4). If the transaction
  is long and `llm.stream` is nearly all of it, the model is slow; if the store spans add
  up, the database is.

### Failure handling in a stream

Once the first byte of a streaming response has been sent, the HTTP status can no longer
change. The route therefore catches exceptions inside the generator and emits an `error`
event with a generic message, logging the real exception server side. The UI renders the
error in place of the reply. Budget exhaustion and authorisation failures are checked
before the stream starts, so they are still ordinary 429 and 404 responses.

## 3. Chat storage and resuming a conversation

### The store interface

`ChatStore` deals in three record types: `Conversation` (id, owner, title, timestamps),
`Message` (role, content, optional thinking text, timestamp, and the owning user id), and
`UsageRecord` (tokens, provider, model and latency for one assistant reply). The interface
is deliberately narrow: create, list, get, touch and delete conversations; append and list
messages; record usage and aggregate it.

Two things are kept out of the store on purpose:

- **Authorisation.** The store returns whatever it is asked for; the route compares the
  conversation's `user_id` with the JWT `sub` claim and returns 404 on mismatch so that
  conversation ids cannot be probed. Admin read-only access uses separate admin routes over
  the same store methods.
- **Users.** The chat store only knows opaque user ids. Usernames are joined in at the route
  layer from the `UserStore`. This keeps the Elasticsearch documents free of anything that
  would need to change when a user is renamed, and lets users eventually come from an
  identity provider without touching chat storage.

### SQLite and Elasticsearch

The SQLite implementation is four tables with indexes on `(user_id, updated_at)` and
`(conversation_id, created_at)`, WAL mode, and ISO-8601 timestamps stored as text. It is
the reference implementation and is what the test suite runs against.

The Elasticsearch implementation maps the same three record types onto three indices with
explicit mappings (`keyword` for ids, `date` for timestamps, `text` for content) so that
term queries and range aggregations behave. Usage summaries are a `terms` aggregation on
`user_id` with `sum` sub-aggregations, which is how the admin usage report stays one round
trip regardless of history size. Writes use `refresh="wait_for"` so that a list issued
straight after a write sees the document; drop that for higher write throughput if
read-your-writes is not required.

### Resuming

The service is stateless between requests. There is no in-memory conversation object and no
session affinity. Resuming a conversation is the normal path, not a special one:

1. The UI lists conversations for the user and loads one with `GET /api/conversations/{id}`.
2. When the user sends a message, the route loads every stored message for that
   conversation, converts them to alternating `user` and `assistant` turns, appends the new
   user message, and passes the whole list to the provider.
3. The provider sends that list as the `messages` array of one API call.

This is the same model Claude Code uses locally: the transcript on disk is the state, and
each turn replays it. Any instance of the service can serve any turn of any conversation,
which is what makes horizontal scaling and rolling deploys uneventful.

Two consequences worth knowing:

- **Cost grows with conversation length** because the whole history is resent. With a real
  provider the mitigation is prompt caching on the stable prefix (system prompt plus older
  turns) and, for very long threads, truncation or server-side compaction. Both belong in
  the provider implementation, behind the same `stream()` signature.
- **Thinking is stored but not replayed.** The assistant's summarised thinking is saved on
  the message for display, but only the visible text is sent back as history. For a
  text-only chat this is correct; if tool use is added, the tool-use and tool-result blocks
  would need to be stored and replayed intact.

### Usage attribution

Every `Message` and `UsageRecord` carries the user id taken from the verified JWT, never
from the request body. Token budgets (`app/budget.py`) are computed by aggregating usage
records for the current period, so a budget check is one store call and needs no separate
counter that could drift.

## 4. Telemetry: the same instrumentation locally and in production

`Telemetry` is the third seam. It has four operations: a request transaction (installed as
middleware), a span context manager, a counter and a gauge, plus a hook to attach the
authenticated user to the current transaction. The application is instrumented once
against that interface:

| Where | What is recorded |
|-------|------------------|
| Middleware | one transaction per HTTP request, named by route template, with status, duration and user |
| `deps.current_user` | user id and username attached to the transaction as soon as the JWT is verified |
| `routes/chat.py` | `llm.stream` span around the provider call; gauges `llm.time_to_first_token_ms`, `llm.duration_ms`; counters `llm.requests`, `llm.input_tokens`, `llm.output_tokens`, all labelled by provider and model; counter `budget.rejected` labelled by user |
| `telemetry/traced_store.py` | a `store.<method>` span around every async method of both stores, labelled by backend |

The store proxy deserves a note. It is a small `__getattr__` wrapper that intercepts any
coroutine method and runs it inside a span. It does not know which store it wraps or which
methods exist, so adding a method to the protocol instruments it automatically, and the
same proxy will wrap a future store without change.

### The file stub

`FileTelemetry` appends one line per transaction, span or metric to a file, line-buffered so
`tail -f data/telemetry.log` shows each line as it happens. Point it at `/dev/stderr` (the
Docker image's default) and the same lines go to the process output, where a container
runtime or log shipper already collects them; no volume or sidecar is needed. Its middleware is pure ASGI (not
Starlette's `BaseHTTPMiddleware`) so that the transaction's duration covers the entire
streamed response and so that the request id, kept in a `contextvars.ContextVar`, is
visible inside the streaming generator. Spans and metrics print that id, so one request can
be isolated with `grep`.

Why this is worth having rather than just logging:

- It answers the same questions as the production backend. "Was the slow request slow in
  the model or in the database?" is visible from the span durations in the file, using the
  identical instrumentation calls that will feed APM.
- It has no moving parts. There is no agent, no collector, no container to run, and no
  network failure mode. `APP_TELEMETRY_FILE_FORMAT=jsonl` turns it into a `jq`-friendly
  stream for ad hoc analysis.
- It is testable. A test asserts that a chat request produces a transaction line with the
  right route, status and user, and that the expected spans and metrics share its id. That
  test guards the instrumentation itself, which the APM backend then simply transports.

### The Elastic APM backend

`ElasticApmTelemetry` maps the same four operations onto the Elastic APM Python agent:

- The agent's Starlette middleware produces transactions named by route, captures
  unhandled exceptions, and propagates W3C trace context headers, so requests from an
  upstream service that already carries a `traceparent` join the same distributed trace.
- `span()` uses `elasticapm.async_capture_span` with the span type set to `external` for
  the model call and `db` for stores, which is what makes APM's service map and the
  "time spent by span type" breakdown correct.
- Counters and gauges are registered on a custom metric set, so they are collected on the
  agent's normal metrics interval and appear as `llm.*` and `budget.*` fields in the
  `metrics-apm.app.*` data stream, labelled by provider, model or user and ready for
  Kibana dashboards or alerts.
- The agent auto-instruments httpx. The outbound call to the Anthropic or Foundry API
  therefore appears as a child span of `llm.stream` with its own duration and status,
  with no code in this repository.

Both backends implement the same protocol, so switching from the file to APM is one
environment variable. The instrumentation, and the test that guards it, do not change.

## 5. Configuration and startup

All settings are `APP_*` environment variables read by `pydantic-settings`
(`app/config.py`), with a `.env` file for local convenience. Startup, in
`create_app()` and its lifespan:

1. Build the telemetry backend and install its middleware (this must happen before the app
   serves its first request).
2. Build the stores, initialise them (create tables or indices), and seed users if the user
   table is empty.
3. Wrap the stores in the tracing proxy and build the LLM provider.
4. On shutdown, close stores and flush the telemetry backend.

The app factory takes an optional `Settings` so tests construct the application with a
temporary database and zero stub latency rather than reading the environment.
