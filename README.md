# Claude at home

A FastAPI chat service that fronts Claude behind a Claude-style web UI, with JWT auth,
per-user chat history, token budgets, an admin panel, and pluggable backends for the model,
chat storage and telemetry. Every external dependency has a local stub, so the whole product
can be developed and tested on a laptop with no credentials and no token spend.

`HOW-IT-WORKS.md` explains the architecture. This file explains how to run it.

## Quick start (local, all stubs)

```bash
uv sync --group dev
cp .env.example .env
uv run uvicorn app.main:app --reload --port 8765
open http://127.0.0.1:8765
tail -f data/telemetry.log        # in another terminal
```

`uv run pytest` runs the end-to-end tests; `uvx ruff check app tests` and `uvx ruff format --check app tests` lint and format-check (config in `pyproject.toml`).

### Test credentials

Seeded on first start when the user table is empty (`APP_SEED_USERS`; set it to `[]` to disable).

| Username | Password | Role  |
|----------|----------|-------|
| `admin`  | `admin`  | admin |
| `alice`  | `alice`  | user  |
| `bob`    | `bob`    | user  |

Admins get an **Admin** entry in the sidebar: users (add, remove, set token budgets), all
chats (read-only view of every user's history with timestamps), and usage (tokens per user
plus totals across the userbase). Regular users only see and write their own conversations.

## Running modes

The service has three independent switches. Each accepts a stub and a production value, and
they can be mixed freely.

| Switch | Values | Default |
|--------|--------|---------|
| `APP_LLM_PROVIDER` | `stub`, `anthropic`, `azure` | `stub` |
| `APP_CHAT_STORE` | `sqlite`, `elastic` | `sqlite` |
| `APP_TELEMETRY` | `file`, `elastic_apm`, `none` | `file` |

With `file`, `APP_TELEMETRY_FILE` is a path to append to (default `./data/telemetry.log`,
for `tail -f`) or `/dev/stderr` to write into the process output, which is what the Docker
image does so telemetry lands in `docker logs`.

The three configurations below are the ones most people want.

### Mode 1: local development with stubs

Nothing outside the repository is contacted. This is what `.env.example` gives you.

```dotenv
APP_LLM_PROVIDER=stub
APP_CHAT_STORE=sqlite
APP_TELEMETRY=file
APP_SQLITE_PATH=./data/app.db
APP_TELEMETRY_FILE=./data/telemetry.log
```

The stub streams a fixed answer after a random thinking delay, at a randomised token rate
with occasional stalls. Tune it to the behaviour you want to test:

| Variable | Meaning | Default |
|----------|---------|---------|
| `APP_STUB_LAG_MIN_S` / `APP_STUB_LAG_MAX_S` | thinking lead time range, seconds | `0.8` / `3.0` |
| `APP_STUB_TOKENS_PER_S_MIN` / `APP_STUB_TOKENS_PER_S_MAX` | nominal output rate range; one draw per reply, exponential jitter per token | `15` / `60` |
| `APP_STUB_STALL_PROBABILITY` | chance per token of a 0.3 to 1.2 second pause | `0.03` |

Set the lag to `0` and the rate to something large when you want fast iteration on the UI
rather than realistic pacing. The telemetry file shows every request as a transaction with
its spans and metrics; `APP_TELEMETRY_FILE_FORMAT=jsonl` makes it `jq`-friendly.

### Mode 2: local development against a real model

Keep SQLite and the telemetry file, switch only the provider. This is for checking that
prompts, streaming and thinking display behave with the real thing.

Anthropic API:

```dotenv
APP_LLM_PROVIDER=anthropic
APP_LLM_MODEL=claude-opus-5
ANTHROPIC_API_KEY=sk-ant-...        # or run `ant auth login` and leave this unset
```

Azure AI (Claude on Microsoft Foundry):

```dotenv
APP_LLM_PROVIDER=azure
APP_LLM_MODEL=claude-opus-5
APP_AZURE_RESOURCE=my-foundry-resource
APP_AZURE_API_KEY=...
```

Both providers stream with adaptive thinking and `display: "summarized"`, so the UI's
thinking phase is populated. The Anthropic provider also enables server-side refusal
fallbacks (`fallbacks: "default"`), which re-run a refused request on a fallback model
inside the same call; Foundry does not offer that feature so the Azure provider omits it.
`APP_LLM_MAX_TOKENS` (default `64000`) caps the reply length.

Because usage is now real, lower the budget while you experiment so a mistake in a loop
cannot run away:

```dotenv
APP_DEFAULT_TOKEN_BUDGET=50000
APP_BUDGET_PERIOD=day
```

### Mode 3: production

Real model, Elasticsearch for chat history, Elastic APM for telemetry, and the development
defaults turned off.

```bash
uv sync --extra elastic --extra apm --no-dev
```

```dotenv
# model
APP_LLM_PROVIDER=anthropic                 # or azure, with APP_AZURE_RESOURCE / APP_AZURE_API_KEY
APP_LLM_MODEL=claude-opus-5
ANTHROPIC_API_KEY=...                      # from your secret store, not a .env file

# chat history
APP_CHAT_STORE=elastic
APP_ELASTIC_URL=https://es.internal:9200
APP_ELASTIC_API_KEY=...
APP_ELASTIC_INDEX_PREFIX=chat              # indices become chat-conversations, chat-messages, chat-usage

# telemetry
APP_TELEMETRY=elastic_apm
APP_APM_SERVER_URL=https://apm.internal:8200
APP_APM_SECRET_TOKEN=...                   # or APP_APM_API_KEY
APP_APM_SERVICE_NAME=claude-at-home
APP_APM_ENVIRONMENT=production

# auth
APP_JWT_SECRET=<at least 32 random bytes>
APP_JWT_TTL_MINUTES=60
APP_SEED_USERS=[]                          # never seed default passwords in production

# budgets
APP_DEFAULT_TOKEN_BUDGET=500000
APP_BUDGET_PERIOD=month
APP_ENFORCE_TOKEN_BUDGET=true

# behaviour
APP_SYSTEM_PROMPT="You are a helpful assistant."
APP_SQLITE_PATH=/var/lib/claude-at-home/users.db   # users still live in SQLite, see below
```

Run under a process manager with several uvicorn workers, or one worker per container:

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

Production notes:

- **Streaming through a proxy.** Responses set `X-Accel-Buffering: no`. If you terminate
  TLS at nginx or a cloud load balancer, confirm response buffering is off for
  `/api/conversations/*/messages`, or users will see nothing until the reply completes.
  Set idle timeouts above your longest expected model turn.
- **Users.** The user table stays in SQLite in every mode. For a single instance that is
  fine on a persistent volume; for multiple instances, replace `SqliteUserStore` with a
  store backed by your identity provider or shared database. It is a five-method protocol.
- **First admin.** With seeding disabled, create the first admin by seeding once with a
  strong password (`APP_SEED_USERS=[{"username":"ops","password":"...","is_admin":true}]`)
  and then removing the variable, or by inserting into the users table directly using
  `app.auth.hash_password`.
- **Secrets.** Nothing in this repository reads secrets from anywhere but the environment.
  Inject them from your secret manager; do not ship a `.env` file.
- **Not yet in this repository:** refresh tokens, rate limiting, and CSRF protection beyond
  bearer-token auth. The Elasticsearch store and the APM backend are written against the
  current client libraries and smoke-tested offline, but have not been run against live
  clusters here; the SQLite store and file telemetry are covered by the tests.

## Running in Docker

The image is a two-stage build: uv installs the locked dependencies (including the
`elastic` and `apm` extras) into a virtualenv, which is copied into a slim Python image
that runs as a non-root user. One image serves every mode; configuration is entirely
environment variables, and state (`users.db`, the SQLite chat store, the telemetry file)
lives under the `/data` volume.

### Stub mode with compose

```bash
docker compose up --build
open http://127.0.0.1:8765
docker compose logs -f app          # server log and telemetry lines together
```

`compose.yaml` reads `.env` if present, so the same file that drives `uv run` drives the
container. It pins the SQLite path to the `/data` volume and telemetry to `/dev/stderr`, so
state survives restarts and telemetry goes wherever your container logs go.

### Plain docker

```bash
docker build -t claude-at-home .

# Mode 1: stubs
docker run --rm -p 8765:8000 -v claude-data:/data claude-at-home

# Mode 2: real model, everything else local
docker run --rm -p 8765:8000 -v claude-data:/data \
  -e APP_LLM_PROVIDER=anthropic -e ANTHROPIC_API_KEY=sk-ant-... \
  claude-at-home

# Mode 3: production backends
docker run -d --name claude-at-home -p 8000:8000 -v claude-data:/data \
  -e APP_LLM_PROVIDER=anthropic -e ANTHROPIC_API_KEY=... \
  -e APP_CHAT_STORE=elastic -e APP_ELASTIC_URL=https://es.internal:9200 -e APP_ELASTIC_API_KEY=... \
  -e APP_TELEMETRY=elastic_apm -e APP_APM_SERVER_URL=https://apm.internal:8200 -e APP_APM_SECRET_TOKEN=... \
  -e APP_APM_ENVIRONMENT=production \
  -e APP_JWT_SECRET=... -e APP_SEED_USERS='[]' \
  claude-at-home
```

Any variable from the "Running modes" section can be passed with `-e`, or collected in a
file and passed with `--env-file`. Inside the container the defaults are:

| Variable | Container default | Why |
|----------|-------------------|-----|
| `APP_SQLITE_PATH` | `/data/app.db` | on the volume, survives restarts |
| `APP_TELEMETRY_FILE` | `/dev/stderr` | telemetry lines appear in `docker logs` alongside uvicorn's; set `/data/telemetry.log` to tail a file instead |
| port | `8000` | map with `-p host:8000` |

The container runs one uvicorn worker. Scale by running more containers behind a load
balancer rather than more workers in one container; with the SQLite user store that means
sharing the `/data` volume or, better, replacing the user store (see production notes).
The health check hits `/api/info`, which also reports the active provider, store and
telemetry backend, so `docker inspect` or `curl :8000/api/info` confirms the mode.

## Layout

```
app/
  main.py            app factory, lifespan wiring, user seeding
  config.py          pydantic-settings; everything is APP_* env vars
  auth.py            scrypt password hashing, HS256 JWT issue/verify
  budget.py          per-user token budgets over a day/month/all-time window
  deps.py            FastAPI dependencies (current user, admin guard, stores, provider, telemetry)
  routes/
    auth.py          POST /api/auth/login, GET /api/auth/me, GET /api/auth/me/usage
    chat.py          conversations CRUD + POST .../messages (server-sent events)
    admin.py         users CRUD and budgets, all conversations, usage report
  llm/
    base.py          LLMProvider protocol + provider-neutral StreamEvent
    stub.py          fixed response, random lag, randomised token rate, occasional stalls
    anthropic_provider.py  Claude via the Anthropic API or Azure AI (Microsoft Foundry)
    factory.py       picks a provider from APP_LLM_PROVIDER
  storage/
    base.py          UserStore and ChatStore protocols
    sqlite.py        aiosqlite implementation of both
    elastic.py       Elasticsearch implementation of ChatStore
    factory.py       picks stores from APP_CHAT_STORE
  telemetry/
    base.py          Telemetry protocol: request transactions, spans, counters, gauges
    file.py          stub: one line per transaction/span/metric appended to a tailable file
    elastic_apm.py   Elastic APM agent: Starlette middleware, custom spans, custom metrics
    traced_store.py  proxy that wraps every store method in a `store.<method>` span
    factory.py       picks a backend from APP_TELEMETRY
  static/index.html  the UI (vanilla JS, no build step)
tests/test_app.py    end-to-end tests through the ASGI app with the stub provider
Dockerfile           two-stage uv build, non-root, /data volume
compose.yaml         stub mode in a container, reads .env
```

## API summary

| Method and path | Who | Purpose |
|-----------------|-----|---------|
| `POST /api/auth/login` | anyone | exchange username and password for a JWT |
| `GET /api/auth/me` | user | the signed-in user |
| `GET /api/auth/me/usage` | user | tokens used, budget and remaining for the current period |
| `GET/POST /api/conversations` | user | list or create own conversations |
| `GET/DELETE /api/conversations/{id}` | user | read or delete an owned conversation |
| `POST /api/conversations/{id}/messages` | user | send a message; streams the reply as SSE |
| `GET/POST /api/admin/users` | admin | list or create users (optionally with a budget) |
| `PATCH/DELETE /api/admin/users/{id}` | admin | change a user's budget, or remove them |
| `GET /api/admin/conversations` | admin | every conversation with its owner |
| `GET /api/admin/conversations/{id}` | admin | read any conversation |
| `GET /api/admin/usage` | admin | per-user usage and budgets plus userbase totals |
| `GET /api/info` | anyone | which provider, model, store and telemetry are active |

SSE events on the message stream, in order: `user_message`, `thinking_start`,
`thinking_delta`…, `thinking_stop`, `text_delta`…, then `done` (with the persisted
assistant message and token counts) or `error`.
