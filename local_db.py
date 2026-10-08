import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "profile.db")

PROFILE_FIELDS = [
    "name",
    "age",
    "gender",
    "state",
    "district",
    "caste_category",
    "annual_income",
    "land_ownership",
    "occupation",
    "education_level",
    "family_size",
    "bpl_status",
    "disability",
    "has_bank_account",
    "aadhaar_linked",
    "mobile_number",
    "updated_at",
]


def _get_connection() -> sqlite3.Connection:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _get_connection() as conn:
        conn.execute(
            """
            create table if not exists user_profile (
                session_id text primary key,
                name text,
                age integer,
                gender text,
                state text,
                district text,
                caste_category text,
                annual_income integer,
                land_ownership text,
                occupation text,
                education_level text,
                family_size integer,
                bpl_status boolean,
                disability boolean,
                has_bank_account boolean,
                aadhaar_linked boolean,
                mobile_number text,
                updated_at text
            )
            """
        )
        existing_columns = {row["name"] for row in conn.execute("pragma table_info(user_profile)").fetchall()}
        if "mobile_number" not in existing_columns:
            conn.execute("alter table user_profile add column mobile_number text")
        conn.execute(
            """
            create table if not exists chat_messages (
                id integer primary key autoincrement,
                session_id text not null,
                role text not null,
                content text not null,
                created_at text not null
            )
            """
        )
        conn.execute(
            """
            create table if not exists bookmarked_schemes (
                session_id text not null,
                scheme_id text not null,
                scheme_name text not null,
                deadline text,
                created_at text not null,
                primary key (session_id, scheme_id)
            )
            """
        )
        conn.execute(
            """
            create table if not exists chat_sessions (
                session_id text primary key,
                title text not null default 'New Chat',
                created_at text not null,
                updated_at text not null
            )
            """
        )


def get_profile(session_id: str) -> Optional[Dict[str, Any]]:
    init_db()
    with _get_connection() as conn:
        row = conn.execute(
            "select * from user_profile where session_id = ?",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def upsert_profile(session_id: str, **fields: Any) -> None:
    init_db()
    fields = {k: v for k, v in fields.items() if k in PROFILE_FIELDS and k != "updated_at"}

    fields["updated_at"] = datetime.now(timezone.utc).isoformat()

    with _get_connection() as conn:
        existing = conn.execute(
            "select session_id from user_profile where session_id = ?",
            (session_id,),
        ).fetchone()

        if existing:
            set_clause = ", ".join([f"{key} = ?" for key in fields.keys()])
            values = list(fields.values()) + [session_id]
            conn.execute(
                f"update user_profile set {set_clause} where session_id = ?",
                values,
            )
        else:
            columns = ["session_id"] + list(fields.keys())
            placeholders = ", ".join(["?"] * len(columns))
            values = [session_id] + list(fields.values())
            conn.execute(
                f"insert into user_profile ({', '.join(columns)}) values ({placeholders})",
                values,
            )


def update_profile(session_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    upsert_profile(session_id, **fields)
    profile = get_profile(session_id)
    if not profile:
        raise ValueError("profile could not be saved")
    return profile


def get_missing_fields(session_id: str) -> List[str]:
    profile = get_profile(session_id)
    missing = []
    excluded_fields = {"updated_at"}

    if not profile:
        return [f for f in PROFILE_FIELDS if f not in excluded_fields]

    for field in PROFILE_FIELDS:
        if field in excluded_fields:
            continue
        if profile.get(field) is None:
            missing.append(field)

    return missing


def _ensure_session(session_id: str, conn: sqlite3.Connection) -> None:
    """Create a chat_sessions row if one does not already exist."""
    existing = conn.execute(
        "select session_id from chat_sessions where session_id = ?",
        (session_id,),
    ).fetchone()
    if not existing:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "insert into chat_sessions (session_id, title, created_at, updated_at) values (?, ?, ?, ?)",
            (session_id, "New Chat", now, now),
        )


def save_chat_message(session_id: str, role: str, content: str) -> None:
    if role not in {"user", "assistant"} or not content.strip():
        return
    init_db()
    with _get_connection() as conn:
        _ensure_session(session_id, conn)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "insert into chat_messages (session_id, role, content, created_at) values (?, ?, ?, ?)",
            (session_id, role, content.strip(), now),
        )
        conn.execute(
            "update chat_sessions set updated_at = ? where session_id = ?",
            (now, session_id),
        )
        # Auto-title: if this is the first user message, set title from it
        if role == "user":
            row = conn.execute(
                "select title from chat_sessions where session_id = ?",
                (session_id,),
            ).fetchone()
            if row and row["title"] == "New Chat":
                words = content.strip().split()
                title = " ".join(words[:8])
                if len(title) > 60:
                    title = title[:57] + "..."
                conn.execute(
                    "update chat_sessions set title = ? where session_id = ?",
                    (title, session_id),
                )


def get_chat_history(session_id: str, limit: int = 20) -> List[Dict[str, str]]:
    init_db()
    with _get_connection() as conn:
        rows = conn.execute(
            "select role, content from chat_messages where session_id = ? order by id desc limit ?",
            (session_id, limit),
        ).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


def get_all_chat_history(limit: int = 200) -> List[Dict[str, Any]]:
    with _get_connection() as conn:
        rows = conn.execute(
            "select session_id, role, content, created_at from chat_messages order by id desc limit ?",
            (limit,),
        ).fetchall()
    # Group by session_id but keep chronological order within groups
    sessions = {}
    for row in reversed(rows):
        sid = row["session_id"]
        if sid not in sessions:
            sessions[sid] = []
        sessions[sid].append({
            "role": row["role"],
            "content": row["content"],
            "created_at": row["created_at"]
        })
    # Convert dict to a list of sessions ordered by latest activity
    session_list = [
        {"session_id": sid, "messages": msgs}
        for sid, msgs in sessions.items()
    ]
    # Reverse so the most recent sessions appear first
    session_list.reverse()
    return session_list


def get_all_sessions() -> List[Dict[str, Any]]:
    """Return all chat sessions ordered by most recent first."""
    init_db()
    with _get_connection() as conn:
        rows = conn.execute(
            "select session_id, title, created_at, updated_at from chat_sessions order by updated_at desc"
        ).fetchall()
    return [dict(row) for row in rows]


def rename_session(session_id: str, title: str) -> None:
    init_db()
    with _get_connection() as conn:
        conn.execute(
            "update chat_sessions set title = ? where session_id = ?",
            (title, session_id),
        )


def list_bookmarks(session_id: str) -> List[Dict[str, Any]]:
    init_db()
    with _get_connection() as conn:
        rows = conn.execute(
            "select scheme_id, scheme_name, deadline, created_at from bookmarked_schemes where session_id = ? order by created_at desc",
            (session_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_bookmark(session_id: str, scheme_id: str, scheme_name: str, deadline: Optional[str] = None) -> None:
    init_db()
    with _get_connection() as conn:
        conn.execute(
            "insert or replace into bookmarked_schemes (session_id, scheme_id, scheme_name, deadline, created_at) values (?, ?, ?, ?, ?)",
            (session_id, scheme_id, scheme_name, deadline, datetime.now(timezone.utc).isoformat()),
        )


def delete_bookmark(session_id: str, scheme_id: str) -> None:
    init_db()
    with _get_connection() as conn:
        conn.execute("delete from bookmarked_schemes where session_id = ? and scheme_id = ?", (session_id, scheme_id))


def deadline_notification_sent(session_id: str, scheme_id: str, deadline: str) -> bool:
    with _get_connection() as conn:
        row = conn.execute(
            "select 1 from deadline_notifications where session_id = ? and scheme_id = ? and deadline = ?",
            (session_id, scheme_id, deadline),
        ).fetchone()
    return row is not None


def record_deadline_notification(session_id: str, scheme_id: str, deadline: str) -> None:
    with _get_connection() as conn:
        conn.execute(
            "insert or ignore into deadline_notifications (session_id, scheme_id, deadline, sent_at) values (?, ?, ?, ?)",
            (session_id, scheme_id, deadline, datetime.now(timezone.utc).isoformat()),
        )
