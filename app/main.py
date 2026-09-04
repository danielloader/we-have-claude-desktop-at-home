import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import hash_password
from .config import Settings
from .llm.factory import build_llm
from .routes import admin, auth, chat
from .storage.base import UserStore
from .storage.factory import build_stores
from .telemetry.factory import build_telemetry
from .telemetry.traced_store import trace_chat_store, trace_user_store

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class AppInfo(BaseModel):
    provider: str
    model: str
    chat_store: str
    telemetry: str


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    telemetry = build_telemetry(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        user_store, chat_store = build_stores(settings)
        await user_store.init()
        await chat_store.init()
        await seed_users(user_store, settings)
        app.state.user_store = trace_user_store(user_store, telemetry, "sqlite")
        app.state.chat_store = trace_chat_store(chat_store, telemetry, settings.chat_store)
        app.state.llm = build_llm(settings)
        log.info(
            "provider=%s model=%s chat_store=%s telemetry=%s",
            app.state.llm.name, app.state.llm.model, settings.chat_store, telemetry.name,
        )
        yield
        await chat_store.close()
        await user_store.close()
        await telemetry.close()

    app = FastAPI(title="Claude at home", lifespan=lifespan)
    app.state.settings = settings
    app.state.telemetry = telemetry
    telemetry.install(app)
    app.include_router(auth.router)
    app.include_router(chat.router)
    app.include_router(admin.router)

    @app.get("/api/info", response_model=AppInfo)
    async def info() -> AppInfo:
        return AppInfo(
            provider=app.state.llm.name,
            model=app.state.llm.model,
            chat_store=settings.chat_store,
            telemetry=telemetry.name,
        )

    # Mounted last so the API routes above win.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


async def seed_users(users: UserStore, settings: Settings) -> None:
    if await users.count() > 0 or not settings.seed_users:
        return
    for seed in settings.seed_users:
        await users.create_user(seed.username, hash_password(seed.password), seed.is_admin)
    log.warning(
        "Seeded %d users with default passwords (%s). Change them before exposing this service.",
        len(settings.seed_users),
        ", ".join(s.username for s in settings.seed_users),
    )


app = create_app()
