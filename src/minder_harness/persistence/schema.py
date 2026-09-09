"""Ordered, additive migrations. Unknown future versions are never downgraded."""

MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, body TEXT NOT NULL)",
        """CREATE TABLE runs (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            status TEXT NOT NULL, body TEXT NOT NULL)""",
        """CREATE UNIQUE INDEX one_active_run ON runs(session_id)
            WHERE status IN ('queued', 'running', 'cancel_requested')""",
        """CREATE TABLE messages (
            ordinal INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            run_id TEXT REFERENCES runs(id), accepted INTEGER NOT NULL, body TEXT NOT NULL)""",
        """CREATE TABLE events (
            run_id TEXT NOT NULL REFERENCES runs(id), sequence INTEGER NOT NULL,
            id TEXT UNIQUE NOT NULL, body TEXT NOT NULL, PRIMARY KEY(run_id, sequence))""",
        """CREATE TABLE attempts (
            id TEXT PRIMARY KEY, execution_id TEXT NOT NULL,
            run_id TEXT NOT NULL REFERENCES runs(id), attempt INTEGER NOT NULL,
            scope TEXT NOT NULL, call_key TEXT NOT NULL, retry_safe INTEGER NOT NULL,
            state TEXT NOT NULL, outcome_unknown INTEGER NOT NULL,
            call_json TEXT NOT NULL, result_json TEXT,
            UNIQUE(execution_id, attempt))""",
        "CREATE INDEX attempt_guard ON attempts(scope, call_key, outcome_unknown)",
    ),
    (
        """CREATE TABLE task_states (
            session_id TEXT PRIMARY KEY REFERENCES sessions(id), body TEXT NOT NULL)""",
        """INSERT INTO task_states(session_id, body)
            SELECT id, '{"schema_version":1,"revision":0,"objective":null,
            "constraints":[],"observations":[],"open_questions":[]}' FROM sessions""",
    ),
    (
        "CREATE INDEX messages_by_session ON messages(session_id)",
        "CREATE INDEX runs_by_session ON runs(session_id)",
        "PRAGMA optimize",
    ),
)
