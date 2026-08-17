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

Requires a Frostlake engine **0.0.7 or newer**. Ask a running server which one it is with
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
  configuration carried over from another warehouse still loads.
- **Cursors**: `execute` (returns the cursor), `executemany`, `fetchone`/`fetchmany`/
  `fetchall`, `nextset` for multi-statement results, iteration, context-manager use,
  `rowcount` from DML, and `query_id`. `DictCursor` returns dicts instead of tuples.
- **Descriptions**: `ResultMetadata(name, type_code, display_size, internal_size,
  precision, scale, is_nullable)`, where `type_code` is a numeric family code — see
  `constants.FIELD_ID_TO_NAME` — so callers can branch without parsing SQL type text.
- **Binding**: `pyformat` by default (`%s`, `%(name)s`, `%%`), or `paramstyle="qmark"`
  for `?`. Placeholders inside string literals, quoted identifiers, comments and
  `$$…$$` bodies are left alone.
- `execute_string()` splits a script on top-level semicolons — respecting `$$…$$`
  procedure bodies — and returns one cursor per statement.
- **Transactions**: `autocommit(mode)`, `commit()`, `rollback()`. With autocommit off the
  connection stays transactional: ending one transaction opens the next.
- `is_closed()`, `session_id`, and connections as context managers.
- **Errors**: a PEP 249 hierarchy in `frostlake_connector.errors` — everything derives
  from `Error`, database failures from `DatabaseError`. Each carries `msg`, `errno`,
  `sqlstate`, `query_id` and `query`; engine compile errors arrive as
  `ProgrammingError(errno=1003, sqlstate="42000")`, with the engine's message text
  authoritative.

## Running the tests

```sh
export JAVA_HOME=/path/to/jdk17
export FROSTLAKE_CLASSPATH="/path/to/frostlake-db.jar:<engine deps>"
python3 test/test_facade.py
```

The suite boots a real `DatabaseHttpServer` and covers connect-kwargs context
(`CURRENT_DATABASE`/`CURRENT_SCHEMA`/`CURRENT_WAREHOUSE`), binding in both paramstyles,
descriptions and type codes, `DictCursor`, `executemany`, `execute_string`, transaction
discipline and the error surface. Without `FROSTLAKE_CLASSPATH` the integration tests skip
and the unit tests still run.

## Related

- [`frostlake`](https://pypi.org/project/frostlake/) — the PEP 249 driver underneath.
- [`dbt-frostlake`](https://pypi.org/project/dbt-frostlake/) — the dbt adapter, which
  uses this client as its transport.
