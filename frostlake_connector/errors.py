"""The exception surface raised by this package.

The hierarchy follows PEP 249: every error derives from ``Error``, and the database
errors derive from ``DatabaseError``, so a single ``except`` can catch a family.
Each carries ``msg``, ``errno``, ``sqlstate``, ``query_id`` and ``query`` so a handler
can tell an engine compile error from a client-side one.
"""


class Error(Exception):
    def __init__(self, msg=None, errno=0, sqlstate=None, query_id=None, query=None):
        self.msg = msg
        self.raw_msg = msg
        self.errno = errno
        self.sqlstate = sqlstate
        self.query_id = query_id
        self.query = query
        super().__init__(self.msg)

    def __str__(self):
        if self.errno and self.sqlstate:
            return "%06d (%s): %s" % (self.errno, self.sqlstate, self.msg)
        return str(self.msg)


class InterfaceError(Error):
    """Something wrong with the connection itself rather than the statement."""


class DatabaseError(Error):
    """Anything the engine rejected."""


class ProgrammingError(DatabaseError):
    """Bad SQL, bad parameters, or a statement issued at the wrong time."""


class OperationalError(DatabaseError):
    """The server could not be reached, or dropped the request."""


class IntegrityError(DatabaseError):
    """A constraint was violated."""


class InternalError(DatabaseError):
    """The engine reported a fault of its own."""


class DataError(DatabaseError):
    """A value was out of range or the wrong shape for its column."""


class NotSupportedError(DatabaseError):
    """A feature the engine does not implement."""
