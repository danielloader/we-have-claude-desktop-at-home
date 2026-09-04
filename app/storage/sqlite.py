from datetime import datetime
from pathlib import Path

import aiosqlite

from ..models import Conversation, Message, UsageRecord, UsageSummary, User, UserWithSecret, new_id, now

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    token_budget INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS conversations_user ON conversations(user_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    thinking TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, created_at);
CREATE TABLE IF NOT EXISTS usage (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_user ON usage(user_id);
"""


class _SqliteBase:
    def __init__(self, path: str) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(_SCHEMA)
        await self._migrate()
        await self._db.commit()

    async def _migrate(self) -> None:
        async with self.db.execute("PRAGMA table_info(users)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "token_budget" not in cols:
            await self.db.execute("ALTER TABLE users ADD COLUMN token_budget INTEGER")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "store not initialised"
        return self._db


class SqliteUserStore(_SqliteBase):
    async def count(self) -> int:
        async with self.db.execute("SELECT COUNT(*) FROM users") as cur:
            (n,) = await cur.fetchone()
        return n

    async def create_user(
        self, username: str, password_hash: str, is_admin: bool, token_budget: int | None = None
    ) -> User:
        user = User(id=new_id(), username=username, is_admin=is_admin, token_budget=token_budget, created_at=now())
        await self.db.execute(
            "INSERT INTO users (id, username, password_hash, is_admin, token_budget, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (user.id, user.username, password_hash, int(is_admin), token_budget, _ts(user.created_at)),
        )
        await self.db.commit()
        return user

    async def set_token_budget(self, user_id: str, token_budget: int | None) -> bool:
        cur = await self.db.execute("UPDATE users SET token_budget=? WHERE id=?", (token_budget, user_id))
        await self.db.commit()
        return cur.rowcount > 0

    async def get_user(self, user_id: str) -> User | None:
        async with self.db.execute("SELECT * FROM users WHERE id=?", (user_id,)) as cur:
            row = await cur.fetchone()
        return _user(row) if row else None

    async def get_by_username(self, username: str) -> UserWithSecret | None:
        async with self.db.execute("SELECT * FROM users WHERE username=?", (username,)) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return UserWithSecret(**_user(row).model_dump(), password_hash=row["password_hash"])

    async def list_users(self) -> list[User]:
        async with self.db.execute("SELECT * FROM users ORDER BY created_at") as cur:
            return [_user(r) for r in await cur.fetchall()]

    async def delete_user(self, user_id: str) -> bool:
        cur = await self.db.execute("DELETE FROM users WHERE id=?", (user_id,))
        await self.db.commit()
        return cur.rowcount > 0


class SqliteChatStore(_SqliteBase):
    async def create_conversation(self, user_id: str, title: str) -> Conversation:
        ts = now()
        conv = Conversation(id=new_id(), user_id=user_id, title=title, created_at=ts, updated_at=ts)
        await self.db.execute(
            "INSERT INTO conversations (id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
            (conv.id, conv.user_id, conv.title, _ts(ts), _ts(ts)),
        )
        await self.db.commit()
        return conv

    async def list_conversations(self, user_id: str) -> list[Conversation]:
        async with self.db.execute(
            "SELECT * FROM conversations WHERE user_id=? ORDER BY updated_at DESC", (user_id,)
        ) as cur:
            return [_conversation(r) for r in await cur.fetchall()]

    async def list_all_conversations(self) -> list[Conversation]:
        async with self.db.execute("SELECT * FROM conversations ORDER BY updated_at DESC") as cur:
            return [_conversation(r) for r in await cur.fetchall()]

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        async with self.db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)) as cur:
            row = await cur.fetchone()
        return _conversation(row) if row else None

    async def touch_conversation(self, conversation_id: str, *, title: str | None = None) -> None:
        if title is None:
            await self.db.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?", (_ts(now()), conversation_id)
            )
        else:
            await self.db.execute(
                "UPDATE conversations SET updated_at=?, title=? WHERE id=?",
                (_ts(now()), title, conversation_id),
            )
        await self.db.commit()

    async def delete_conversation(self, conversation_id: str) -> None:
        await self.db.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
        await self.db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
        await self.db.commit()

    async def append_message(self, message: Message) -> None:
        await self.db.execute(
            "INSERT INTO messages (id, conversation_id, user_id, role, content, thinking, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                message.id,
                message.conversation_id,
                message.user_id,
                message.role,
                message.content,
                message.thinking,
                _ts(message.created_at),
            ),
        )
        await self.db.commit()

    async def list_messages(self, conversation_id: str) -> list[Message]:
        async with self.db.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at, rowid", (conversation_id,)
        ) as cur:
            return [_message(r) for r in await cur.fetchall()]

    async def record_usage(self, record: UsageRecord) -> None:
        await self.db.execute(
            "INSERT INTO usage (id, user_id, conversation_id, message_id, provider, model,"
            " input_tokens, output_tokens, latency_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                record.id,
                record.user_id,
                record.conversation_id,
                record.message_id,
                record.provider,
                record.model,
                record.input_tokens,
                record.output_tokens,
                record.latency_ms,
                _ts(record.created_at),
            ),
        )
        await self.db.commit()

    async def usage_summary(self, *, since: datetime | None = None) -> list[UsageSummary]:
        async with self.db.execute(
            "SELECT user_id, COUNT(*) AS messages, SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens FROM usage WHERE created_at >= ? GROUP BY user_id",
            (_since(since),),
        ) as cur:
            return [UsageSummary(**dict(r)) for r in await cur.fetchall()]

    async def usage_for_user(self, user_id: str, *, since: datetime | None = None) -> UsageSummary:
        async with self.db.execute(
            "SELECT COUNT(*) AS messages, COALESCE(SUM(input_tokens),0) AS input_tokens,"
            " COALESCE(SUM(output_tokens),0) AS output_tokens FROM usage WHERE user_id=? AND created_at >= ?",
            (user_id, _since(since)),
        ) as cur:
            row = await cur.fetchone()
        return UsageSummary(user_id=user_id, **dict(row))


def _ts(dt: datetime) -> str:
    return dt.isoformat()


def _since(since: datetime | None) -> str:
    # ISO-8601 strings in UTC compare correctly as text; "" sorts before everything.
    return since.isoformat() if since else ""


def _user(row: aiosqlite.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        is_admin=bool(row["is_admin"]),
        token_budget=row["token_budget"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _conversation(row: aiosqlite.Row) -> Conversation:
    return Conversation(
        id=row["id"],
        user_id=row["user_id"],
        title=row["title"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _message(row: aiosqlite.Row) -> Message:
    return Message(
        id=row["id"],
        conversation_id=row["conversation_id"],
        user_id=row["user_id"],
        role=row["role"],
        content=row["content"],
        thinking=row["thinking"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )
