from ..config import Settings
from .base import ChatStore, UserStore
from .sqlite import SqliteChatStore, SqliteUserStore


def build_stores(settings: Settings) -> tuple[UserStore, ChatStore]:
    # Users always live in SQLite here; swap in an IdP-backed store when there is one.
    user_store = SqliteUserStore(settings.sqlite_path)
    match settings.chat_store:
        case "sqlite":
            chat_store: ChatStore = SqliteChatStore(settings.sqlite_path)
        case "elastic":
            from .elastic import ElasticChatStore

            chat_store = ElasticChatStore(
                settings.elastic_url,
                api_key=settings.elastic_api_key,
                index_prefix=settings.elastic_index_prefix,
            )
        case other:
            raise ValueError(f"unknown chat store {other!r}")
    return user_store, chat_store
