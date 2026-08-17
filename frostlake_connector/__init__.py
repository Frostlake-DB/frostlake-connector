"""A high-level client for Frostlake, built on the Frostlake Python driver.

Where the ``frostlake`` driver is a minimal PEP 249 surface, this package adds the
conveniences an application usually wants:

    import frostlake_connector
    conn = frostlake_connector.connect(host="localhost", port=18082, database="MY_DB")
    cur = conn.cursor()
    cur.execute("SELECT id FROM people WHERE id = %s", (1,))
    print(cur.fetchall())

Covered: connect() with a broad keyword surface (unknown keywords are accepted and
ignored), pyformat (%s / %(name)s / %%) and qmark binding, cursors with named
ResultMetadata descriptions / rowcount / query_id, DictCursor, execute_string for
multi-statement scripts, autocommit()/commit()/rollback(), a full errors module, and
role/warehouse/database/schema/session_parameters applied on connect.

Transport is the ``frostlake`` PEP 249 driver (frostlake-python), which must be
importable alongside this package.
"""

import uuid as _uuid
from collections import namedtuple as _namedtuple

import frostlake as _frostlake

from . import constants
from . import errors
from .errors import (  # noqa: F401  (re-exported for `except frostlake_connector.X`)
    DatabaseError,
    DataError,
    Error,
    IntegrityError,
    InterfaceError,
    InternalError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
)

__version__ = "0.2.0"
apilevel = "2.0"
threadsafety = 2
paramstyle = "pyformat"

ResultMetadata = _namedtuple(
    "ResultMetadata",
    ["name", "type_code", "display_size", "internal_size", "precision", "scale", "is_nullable"],
)


def connect(**kwargs):
    """Open a connection.

    The keywords that matter are host/port (plus database, schema, warehouse, role,
    autocommit, session_parameters, paramstyle, timezone). Anything else — account,
    user, password, authenticator, application, ... — is accepted and ignored, so a
    configuration carried over from another warehouse still loads.
    """
    return FrostlakeConnection(**kwargs)


class FrostlakeConnection(object):
    def __init__(self, **kwargs):
        host = kwargs.get("host") or "localhost"
        port = int(kwargs.get("port") or 18082)
        timeout = kwargs.get("network_timeout") or kwargs.get("socket_timeout") or 300
        self._paramstyle = kwargs.get("paramstyle") or paramstyle
        if self._paramstyle == "format":
            self._paramstyle = "pyformat"
        if self._paramstyle not in ("pyformat", "qmark"):
            raise errors.ProgrammingError(msg="unsupported paramstyle %r" % self._paramstyle)
        self._inner = _frostlake.connect(host=host, port=port, timeout=timeout)
        # The engine only treats statements as transactional after an explicit BEGIN, so
        # autocommit-off is carried here and the BEGIN is issued lazily in _run() — after
        # the USE/ALTER SESSION statements queued below have gone out.
        self._autocommit = bool(kwargs.get("autocommit", True))
        self._inner.autocommit = True
        self._begin_pending = not self._autocommit

        self.role = kwargs.get("role")
        self.warehouse = kwargs.get("warehouse")
        self.database = kwargs.get("database")
        self.schema = kwargs.get("schema")

        self._pending = []
        if self.role:
            self._pending.append("USE ROLE " + _quote_ident(self.role))
        if self.warehouse:
            self._pending.append("USE WAREHOUSE " + _quote_ident(self.warehouse))
        if self.database:
            self._pending.append("USE DATABASE " + _quote_ident(self.database))
        if self.schema:
            self._pending.append("USE SCHEMA " + _quote_ident(self.schema))
        session_parameters = dict(kwargs.get("session_parameters") or {})
        if kwargs.get("timezone"):
            session_parameters.setdefault("TIMEZONE", kwargs["timezone"])
        for key, value in session_parameters.items():
            self._pending.append(
                "ALTER SESSION SET %s = %s" % (_quote_ident(str(key)), _session_value(value))
            )

    # -- public surface ------------------------------------------------------

    @property
    def session_id(self):
        return self._inner._session_id

    def cursor(self, cursor_class=None):
        cls = cursor_class or FrostlakeCursor
        return cls(self)

    def is_closed(self):
        return self._inner._closed

    def close(self):
        self._inner.close()

    def commit(self):
        _guard(self._inner.commit)
        self._transaction_ended()

    def rollback(self):
        _guard(self._inner.rollback)
        self._transaction_ended()

    def autocommit(self, mode):
        """Turn autocommit on or off.

        With it off, statements run in a transaction that commit()/rollback() control;
        turning it back on commits whatever is open. Merely clearing the driver's flag
        would not do it — without a BEGIN the engine commits each statement as it goes,
        and a later rollback() would have nothing to undo.
        """
        wanted = bool(mode)
        if wanted == self._autocommit:
            return
        self._autocommit = wanted
        if wanted:
            _guard(self._inner.commit)
            self._inner.autocommit = True
            self._begin_pending = False
        else:
            self._begin_pending = True

    def _transaction_ended(self):
        """With autocommit off the connection stays transactional, so closing one
        transaction arms the next."""
        if not self._autocommit:
            self._inner.autocommit = True
            self._begin_pending = True

    def execute_string(self, sql_text, remove_comments=False, return_cursors=True, cursor_class=None):
        """Execute a semicolon-separated script; returns the per-statement cursors."""
        del remove_comments  # statements pass through verbatim, comments included
        cursors = []
        for statement in _split_statements(sql_text):
            cur = self.cursor(cursor_class)
            cur.execute(statement)
            cursors.append(cur)
        return cursors if return_cursors else []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # -- internal ------------------------------------------------------------

    def _run(self, sql, query_id=None):
        while self._pending:
            pending = self._pending.pop(0)
            _guard(self._inner._execute, pending, query=pending)
        if self._begin_pending:
            self._begin_pending = False
            _guard(self._inner.begin)
        return _guard(self._inner._execute, sql, query=sql, query_id=query_id)


class FrostlakeCursor(object):
    arraysize = 1

    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self.rowcount = -1
        self.query_id = None
        self.sqlstate = None
        self._rows = []
        self._pos = 0
        self._inner = None

    # -- execution -----------------------------------------------------------

    def execute(self, command, params=None, **kwargs):
        del kwargs  # timeout/_bind_stage/... accepted and ignored
        # Stamp the id before running, so a statement that fails is identifiable too.
        self.query_id = _uuid.uuid4().hex
        self.sqlstate = None
        if params is not None:
            if self.connection._paramstyle == "qmark":
                command = _guard(_frostlake._substitute, command, params,
                                 query=command, query_id=self.query_id)
            else:
                command = _bind_pyformat(command, params)
        out = self.connection._run(command, query_id=self.query_id)
        self._load(out)
        return self

    def executemany(self, command, seq_of_params):
        total = 0
        for params in seq_of_params:
            self.execute(command, params)
            if self.rowcount > 0:
                total += self.rowcount
        self.rowcount = total
        return self

    # -- fetching ------------------------------------------------------------

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchmany(self, size=None):
        # size=0 means zero rows; only an omitted size falls back to arraysize.
        n = self.arraysize if size is None else size
        out = self._rows[self._pos:self._pos + n]
        self._pos += len(out)
        return out

    def fetchall(self):
        out = self._rows[self._pos:]
        self._pos = len(self._rows)
        return out

    def close(self):
        self._rows = []
        self.description = None
        return True

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # -- result loading ------------------------------------------------------

    def _load(self, out):
        self._inner = _frostlake.Cursor(self.connection._inner)
        self._inner._load(out)
        self._absorb()

    def nextset(self):
        """Step to the next result set of a multi-statement execute(); None at the end."""
        if self._inner is None or self._inner.nextset() is None:
            return None
        self._absorb()
        return True

    def _absorb(self):
        inner = self._inner
        self.rowcount = inner.rowcount
        self.description = None
        if inner.description is not None:
            self.description = [
                ResultMetadata(
                    name=d[0],
                    type_code=constants.type_code_for(d[1]),
                    display_size=None,
                    internal_size=None,
                    precision=d[4],
                    scale=d[5],
                    # d[6] is the server's nullability; only assume nullable when it
                    # did not say.
                    is_nullable=True if d[6] is None else bool(d[6]),
                )
                for d in inner.description
            ]
        self._rows = self._shape(inner._rows)
        self._pos = 0

    def _shape(self, rows):
        return rows


class DictCursor(FrostlakeCursor):
    def _shape(self, rows):
        if self.description is None:
            return rows
        names = [d.name for d in self.description]
        return [dict(zip(names, row)) for row in rows]


# -- helpers -----------------------------------------------------------------


def _guard(fn, *args, **context):
    """Run a driver call, translating its PEP 249 exceptions into this package's own
    error classes so callers only ever have one family to catch.

    `query` and `query_id` name the statement that failed, so a caught error says which
    one it was rather than only what went wrong.
    """
    query = context.get("query")
    query_id = context.get("query_id")
    try:
        return fn(*args)
    except _frostlake.ProgrammingError as e:
        message = str(e)
        if message.startswith("SQL compilation error"):
            raise errors.ProgrammingError(msg=message, errno=1003, sqlstate="42000",
                                          query=query, query_id=query_id) from e
        raise errors.ProgrammingError(msg=message, query=query, query_id=query_id) from e
    except _frostlake.OperationalError as e:
        raise errors.OperationalError(msg=str(e), query=query, query_id=query_id) from e
    except _frostlake.InterfaceError as e:
        raise errors.InterfaceError(msg=str(e), query=query, query_id=query_id) from e
    except _frostlake.Error as e:
        raise errors.DatabaseError(msg=str(e), query=query, query_id=query_id) from e


def _quote_ident(name):
    return _frostlake._quote_ident(name)


def _session_value(value):
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return _frostlake._encode_string(str(value))


def _bind_pyformat(sql, params):
    """Inline %s / %(name)s placeholders (%% escapes a percent), skipping string
    literals, quoted identifiers and comments — same scanner as the driver."""
    named = isinstance(params, dict)
    positional = None if named else list(params)
    out = []
    next_param = 0
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = _frostlake._skip_string(sql, i)
            out.append(sql[i:j])
            i = j
        elif ch == '"':
            j = _frostlake._skip_quoted(sql, i, '"')
            out.append(sql[i:j])
            i = j
        elif ch == "-" and sql.startswith("--", i):
            j = _frostlake._skip_line(sql, i)
            out.append(sql[i:j])
            i = j
        elif ch == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            j = n if end < 0 else end + 2
            out.append(sql[i:j])
            i = j
        elif ch == "/" and sql.startswith("//", i):
            j = _frostlake._skip_line(sql, i)
            out.append(sql[i:j])
            i = j
        elif ch == "$" and sql.startswith("$$", i):
            # A $$…$$ body (procedure/UDF source) is data start to finish.
            j = _frostlake._skip_dollar_quoted(sql, i)
            out.append(sql[i:j])
            i = j
        elif ch == "%":
            if sql.startswith("%%", i):
                out.append("%")
                i += 2
            elif sql.startswith("%s", i):
                if named:
                    raise errors.ProgrammingError(msg="%s placeholder with dict parameters")
                if next_param >= len(positional):
                    raise errors.ProgrammingError(msg="not enough parameters for placeholders")
                out.append(_format(positional[next_param]))
                next_param += 1
                i += 2
            elif sql.startswith("%(", i):
                end = sql.find(")s", i + 2)
                if end < 0:
                    raise errors.ProgrammingError(msg="unterminated %(name)s placeholder")
                key = sql[i + 2:end]
                if not named:
                    raise errors.ProgrammingError(msg="%(name)s placeholder with sequence parameters")
                if key not in params:
                    raise errors.ProgrammingError(msg="missing parameter %r" % key)
                out.append(_format(params[key]))
                i = end + 2
            else:
                out.append(ch)
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _format(value):
    return _guard(_frostlake._format_literal, value)


def _split_statements(sql_text):
    """Split a script on top-level semicolons, respecting literals and comments."""
    statements = []
    start = 0
    i = 0
    n = len(sql_text)
    while i < n:
        ch = sql_text[i]
        if ch == "'":
            i = _frostlake._skip_string(sql_text, i)
        elif ch == '"':
            i = _frostlake._skip_quoted(sql_text, i, '"')
        elif ch == "-" and sql_text.startswith("--", i):
            i = _frostlake._skip_line(sql_text, i)
        elif ch == "/" and sql_text.startswith("/*", i):
            end = sql_text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif ch == "/" and sql_text.startswith("//", i):
            i = _frostlake._skip_line(sql_text, i)
        elif ch == "$" and sql_text.startswith("$$", i):
            # Procedure and UDF bodies are full of semicolons; splitting on them would
            # tear a CREATE PROCEDURE into fragments.
            i = _frostlake._skip_dollar_quoted(sql_text, i)
        elif ch == ";":
            statement = sql_text[start:i].strip()
            if statement:
                statements.append(statement)
            i += 1
            start = i
        else:
            i += 1
    tail = sql_text[start:].strip()
    if tail:
        statements.append(tail)
    return statements
