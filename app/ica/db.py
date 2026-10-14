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
