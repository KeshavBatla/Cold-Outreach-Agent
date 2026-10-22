from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from sqlalchemy.engine import make_url


@contextmanager
def postgres_checkpointer(database_url: str) -> Iterator[PostgresSaver]:
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql"):
        raise RuntimeError("Durable workflow workers require a PostgreSQL database URL.")
    key = os.getenv("LANGGRAPH_AES_KEY")
    if key is None or len(key.encode("utf-8")) not in {16, 24, 32}:
        raise RuntimeError("Set LANGGRAPH_AES_KEY to a private 16, 24, or 32 byte key.")
    connection_url = url.set(drivername="postgresql").render_as_string(hide_password=False)
    serializer = EncryptedSerializer.from_pycryptodome_aes(key=key.encode("utf-8"))
    with psycopg.connect(
        connection_url,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
    ) as connection:
        checkpointer = PostgresSaver(connection, serde=serializer)
        checkpointer.setup()
        yield checkpointer
