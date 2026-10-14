from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from . import config

SCHEMA = Path(__file__).with_name("schema.sql")


def connect():
    # autocommit: every statement stands alone. Nothing in the MVP needs
    # multi-statement transactions, and it avoids forgotten-commit bugs.
    return psycopg.connect(config.DATABASE_URL, row_factory=dict_row, autocommit=True)


def init(conn):
    conn.execute(SCHEMA.read_text())


def hashtag_budget_used(conn):
    row = conn.execute(
        "SELECT count(DISTINCT tag) AS n FROM hashtag_queries "
        "WHERE queried_at > now() - interval '7 days'"
    ).fetchone()
    return row["n"]


def hashtag_in_window(conn, tag):
    return conn.execute(
        "SELECT 1 FROM hashtag_queries WHERE tag = %s "
        "AND queried_at > now() - interval '7 days' LIMIT 1",
        (tag,),
    ).fetchone() is not None


class Run:
    """Records a job run in the runs table: `with Run(conn, "ingest") as run:`
    then run.add(key, n) / run.set(key, value). Saves even if the job crashes."""

    def __init__(self, conn, job):
        self.conn, self.job, self.summary = conn, job, {}

    def __enter__(self):
        self.id = self.conn.execute(
            "INSERT INTO runs (job) VALUES (%s) RETURNING id", (self.job,)
        ).fetchone()["id"]
        return self

    def add(self, key, n=1):
        self.summary[key] = self.summary.get(key, 0) + n

    def set(self, key, value):
        self.summary[key] = value

    def __exit__(self, exc_type, exc, tb):
        from psycopg.types.json import Jsonb
        self.conn.execute(
            "UPDATE runs SET finished_at = now(), summary = %s, error = %s WHERE id = %s",
            (Jsonb(self.summary), f"{exc_type.__name__}: {exc}" if exc else None, self.id),
        )
        self.conn.execute("DELETE FROM runs WHERE started_at < now() - interval '14 days'")
        return False
