# frostlake-connector

A high-level Python client for [Frostlake](https://frostlake.dev), built on the
[`frostlake`](https://pypi.org/project/frostlake/) PEP 249 driver.

Where the driver is a minimal DB-API surface, this package adds the conveniences an
application usually wants: `%s` and `%(name)s` binding, dict-shaped rows, multi-statement
scripts, session setup on connect, and a full exception hierarchy.

```sh
pip install frostlake-connector
```

The driver comes along as a dependency and speaks Frostlake's HTTP protocol, so no JVM is
needed on the client.

## Engine version

Requires a Frostlake engine **0.2.0 or newer**. Ask a running server which one it is with
`SELECT CURRENT_VERSION()` — every release answers it, so the check works against any engine.

The client versions independently of the engine: it speaks the HTTP protocol, not the jar,
so this is a floor rather than a lockstep pin.

## Usage

```python
import frostlake_connector

conn = frostlake_connector.connect(host="localhost", port=18082,
                                   database="MY_DB", schema="PUBLIC",
                                   warehouse="COMPUTE_WH",
                                   session_parameters={"QUERY_TAG": "ci"})
cur = conn.cursor()
cur.execute("SELECT id, name FROM people WHERE id = %s", (1,))
print(cur.fetchall())          # [(1, 'Ada')]
```

## What it covers

- `connect(**kwargs)` — `host`/`port` select the server; `role`, `warehouse`, `database`
  and `schema` become `USE` statements (in that order) and `session_parameters`/`timezone`
  become `ALTER SESSION SET`. Unrecognised keywords are accepted and ignored, so a
  configuration carried over from another warehouse still loads. Those names fold like
  unquoted SQL — `database="my_db"` selects `MY_DB` — so include the double quotes
  (`database='"my_db"'`) to reach an object whose real name is not upper case.
- **Cursors**: `execute` (returns the cursor), `executemany`, `fetchone`/`fetchmany`/
  `fetchall`, `nextset` for multi-statement results, iteration, context-manager use,
  `rowcount` from DML, and `query_id`. `DictCursor` returns dicts instead of tuples.
  A script handed to `execute` whole travels as one request, and a session runs one statement
  per request until something asks for more: `execute(sql, num_statements=n)` declares the
  count for that one call (`0` for any number), as the account's connector does, and leaves the
  session's own `MULTI_STATEMENT_COUNT` where it was, while
  `ALTER SESSION SET MULTI_STATEMENT_COUNT = n` sets it for the session; `execute_string`
  splits the script client-side instead, so it needs neither.
- **Descriptions**: `ResultMetadata(name, type_code, display_size, internal_size,
  precision, scale, is_nullable)`, where `type_code` is a numeric family code — see
  `constants.FIELD_ID_TO_NAME` — so callers can branch without parsing SQL type text.
  `internal_size` carries the column's length — characters for text, bytes for binary —
  when the server sends one, and stays `None` for other types and for engines predating
  the field. `display_size` is always `None`, as in the account's own Python client.
- **Binding**: `pyformat` by default (`%s`, `%(name)s`, `%%`), or `paramstyle="qmark"`
  for `?`. Placeholders inside string literals, quoted identifiers, comments and
  `$$…$$` bodies are left alone.
- `execute_string()` splits a script on top-level semicolons — respecting `$$…$$`
  procedure bodies — and returns one cursor per statement.
- **Transactions**: `autocommit(mode)`, `commit()`, `rollback()`. With autocommit off the
  connection stays transactional: ending one transaction opens the next. The `BEGIN` each
  transaction starts with stays owed until the engine takes it. If a setup statement or
  the `BEGIN` itself fails, the statement tried next sends it again first, so it never
  commits on its own where a `rollback()` could not reach it. A `commit()` or `rollback()`
  that fails still leaves the next statement in a transaction the next `commit()` reaches.
  A `COMMIT` the engine refuses is rolled back before the refusal is raised, and the next
  statement opens a fresh transaction. A `COMMIT` whose answer never came leaves the
  transaction open for the next statement to join. `autocommit(True)` turns autocommit on
  even when the `COMMIT` it sends fails: the error is raised, a transaction left open is
  committed on the way, and the next statement commits on its own.
- `is_closed()`, `session_id`, and connections as context managers.
- **Errors**: a PEP 249 hierarchy in `frostlake_connector.errors` — everything derives
  from `Error`, database failures from `DatabaseError`. Each carries `msg`, `errno`,
  `sqlstate`, `query_id` and `query`; engine compile errors arrive as
  `ProgrammingError(errno=1003, sqlstate="42000")`, with the engine's message text
  authoritative. `SessionLostError`, an `OperationalError`, reports a lost session (see
  below).

## Session lifetime

A connection holds one engine session, kept by the `frostlake` driver underneath. What
`connect()` sets up — `USE ROLE`, `USE WAREHOUSE`, `USE DATABASE`, `USE SCHEMA`, then an
`ALTER SESSION SET` for each of `session_parameters` and `timezone` — is the connection's
scope. It goes on before the first statement, and back on any session that replaces a lost
one. A setup statement the engine refuses (a warehouse or database that does not exist, a
parameter it does not know) stays first in line: every statement fails with that refusal,
its `query` naming the refused statement, until the engine accepts it. Nothing runs
without the rest of the setup in its place.

- **What is sent.** Once the engine has shown that it tracks sessions (its answers carry
  `newSession`, as engines from 0.1.0 do), every request that names the session also sends
  `requireSession: true`: resume this session, or refuse. An engine whose answers lack the
  field is never sent it.
- **After a lost session.** The engine forgets a session that sat idle for 30 minutes, was
  released, or went with a restart, and it refuses a request that requires it (HTTP 404)
  without running anything. When the session held nothing a fresh one would lack, the
  connection starts a fresh session, puts the scope on it, and sends the statement once
  more. A second refusal raises. When the session held an open transaction, or context
  set up with `USE`, `SET` / `UNSET`, `ALTER SESSION`, a temporary object, or a `CREATE` /
  `DROP` of a database or schema, `errors.SessionLostError` is raised instead. The
  statement did not run, and in a fresh session it would run somewhere its author did not
  intend. The connection stays usable. The next statement starts a fresh session on the
  scope, and with autocommit off it opens a transaction there first.
- **Close.** `close()`, and leaving a `with` block, release the session with
  `DELETE /api/sessions/{id}`, which rolls back a transaction left open. An engine without
  that endpoint gets a `ROLLBACK` for an open transaction instead, and keeps the session
  until its own idle expiry. Either is one request, bounded by the shorter of
  `network_timeout` and 5 seconds. `close()` never raises, and a second `close()` sends
  nothing.

## Running the tests

```sh
export JAVA_HOME=/path/to/jdk17
export FROSTLAKE_CLASSPATH="/path/to/frostlake-db.jar:<engine deps>"
python3 test/test_facade.py
```

The suite boots a real `DatabaseHttpServer` and covers connect-kwargs context
(`CURRENT_DATABASE`/`CURRENT_SCHEMA`/`CURRENT_WAREHOUSE`), binding in both paramstyles,
descriptions and type codes, `DictCursor`, `executemany`, `execute_string`, transaction
discipline, the error surface and the session lifetime. Without `FROSTLAKE_CLASSPATH` the
integration tests skip and the unit tests still run, the session scenarios among them
against a scripted stand-in engine.

Set `FL_CORPUS` to the engine's testkit directory (an absolute path) and the same run also
replays the engine's language-neutral SQL corpus through the facade (`testkit_runner.py`);
without it, that test skips:

```sh
FL_CORPUS=/path/to/frostlake/engine/src/test/resources/testkit python3 test/test_facade.py
```

## Related

- [`frostlake`](https://pypi.org/project/frostlake/) — the PEP 249 driver underneath.
- [`dbt-frostlake`](https://pypi.org/project/dbt-frostlake/) — the dbt adapter, which
  uses this client as its transport.
