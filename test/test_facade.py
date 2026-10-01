"""Facade tests. Boots a real DatabaseHttpServer from FROSTLAKE_CLASSPATH; the
integration tests skip themselves when the variable is unset.

    python3 test/test_facade.py
"""

import atexit
import http.server
import importlib.util
import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Prefer a frostlake-python checkout sitting beside this repo, so the facade is exercised
# against the working-tree driver; without one, the installed `frostlake` is used.
DRIVER = ROOT.parent / "frostlake-python"
if DRIVER.is_dir():
    sys.path.insert(0, str(DRIVER))

import frostlake_connector as sc  # noqa: E402
from frostlake_connector import DictCursor, constants, errors  # noqa: E402

PORT = None


def setUpModule():
    global PORT
    classpath = os.environ.get("FROSTLAKE_CLASSPATH")
    if not classpath:
        return
    java_home = os.environ.get("JAVA_HOME")
    java = os.path.join(java_home, "bin", "java") if java_home else "java"
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    proc = subprocess.Popen(
        [java, "-cp", classpath, "dev.frostlake.http.DatabaseHttpServer", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    atexit.register(proc.terminate)
    for _ in range(100):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=2):
                PORT = port
                return
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    raise RuntimeError("Frostlake server did not become healthy")


class BindingTest(unittest.TestCase):
    def test_pyformat_positional(self):
        from frostlake_connector import _bind_pyformat
        rendered = _bind_pyformat("SELECT '100%s', %s, %% -- t%sail", (5,))
        self.assertEqual(rendered, "SELECT '100%s', 5, % -- t%sail")

    def test_pyformat_named(self):
        from frostlake_connector import _bind_pyformat
        rendered = _bind_pyformat("SELECT %(a)s, %(b)s", {"a": "x'y", "b": 2})
        self.assertEqual(rendered, "SELECT 'x''y', 2")

    def test_split_statements(self):
        from frostlake_connector import _split_statements
        parts = _split_statements("SELECT 'a;b'; SELECT 2 -- tail;\n; SELECT 3")
        self.assertEqual(parts, ["SELECT 'a;b'", "SELECT 2 -- tail;", "SELECT 3"])


class ErrorsTest(unittest.TestCase):
    """The package defines its own exception family and raises nothing else."""

    FAMILY = ("Error", "InterfaceError", "DatabaseError", "ProgrammingError",
              "OperationalError", "IntegrityError", "InternalError", "DataError",
              "NotSupportedError")

    def test_defines_the_whole_family(self):
        for name in self.FAMILY:
            self.assertTrue(hasattr(errors, name), name)

    def test_every_class_is_our_own(self):
        # Nothing is re-exported from another database's client.
        for name in self.FAMILY:
            self.assertEqual("frostlake_connector.errors",
                             getattr(errors, name).__module__, name)

    def test_hierarchy_follows_pep_249(self):
        self.assertTrue(issubclass(errors.InterfaceError, errors.Error))
        self.assertTrue(issubclass(errors.DatabaseError, errors.Error))
        for name in ("ProgrammingError", "OperationalError", "IntegrityError",
                     "InternalError", "DataError", "NotSupportedError"):
            self.assertTrue(issubclass(getattr(errors, name), errors.DatabaseError), name)

    def test_carries_its_diagnostic_attributes(self):
        e = errors.ProgrammingError(msg="boom", errno=1003, sqlstate="42000",
                                    query_id="qid", query="SELECT 1")
        self.assertEqual("boom", e.msg)
        self.assertEqual("boom", e.raw_msg)
        self.assertEqual(1003, e.errno)
        self.assertEqual("42000", e.sqlstate)
        self.assertEqual("qid", e.query_id)
        self.assertEqual("SELECT 1", e.query)

    def test_str_includes_the_code_when_there_is_one(self):
        coded = errors.ProgrammingError(msg="bad", errno=1003, sqlstate="42000")
        self.assertEqual("001003 (42000): bad", str(coded))

    def test_str_is_plain_without_a_code(self):
        self.assertEqual("bad", str(errors.Error(msg="bad")))
        self.assertEqual("bad", str(errors.Error(msg="bad", errno=1003)))

    def test_the_package_names_no_other_database(self):
        # The package connects to Frostlake; nothing else should appear in its source.
        package = pathlib.Path(sc.__file__).resolve().parent
        for source in sorted(package.glob("*.py")):
            self.assertNotIn("snowflake", source.read_text().lower(), str(source))


class TypeCodeTest(unittest.TestCase):
    """description[i].type_code uses the connector's numeric scheme."""

    def name_for(self, data_type):
        return constants.FIELD_ID_TO_NAME[constants.type_code_for(data_type)]

    def test_fixed_point_family(self):
        for spelling in ("NUMBER", "DECIMAL", "NUMERIC", "INT", "INTEGER", "BIGINT",
                         "SMALLINT", "TINYINT", "BYTEINT", "NUMBER(38,10)"):
            self.assertEqual("FIXED", self.name_for(spelling), spelling)

    def test_approximate_family(self):
        for spelling in ("FLOAT", "FLOAT4", "FLOAT8", "DOUBLE", "DOUBLE PRECISION", "REAL"):
            self.assertEqual("REAL", self.name_for(spelling), spelling)

    def test_temporal_family(self):
        self.assertEqual("DATE", self.name_for("DATE"))
        self.assertEqual("TIME", self.name_for("TIME(9)"))
        self.assertEqual("TIMESTAMP_NTZ", self.name_for("TIMESTAMP"))
        self.assertEqual("TIMESTAMP_NTZ", self.name_for("DATETIME"))
        self.assertEqual("TIMESTAMP_NTZ", self.name_for("TIMESTAMP_NTZ"))
        self.assertEqual("TIMESTAMP_LTZ", self.name_for("TIMESTAMP_LTZ"))
        self.assertEqual("TIMESTAMP_TZ", self.name_for("TIMESTAMP_TZ"))

    def test_semi_structured_and_other_families(self):
        self.assertEqual("VARIANT", self.name_for("VARIANT"))
        self.assertEqual("OBJECT", self.name_for("OBJECT"))
        self.assertEqual("ARRAY", self.name_for("ARRAY"))
        self.assertEqual("BINARY", self.name_for("BINARY"))
        self.assertEqual("BINARY", self.name_for("VARBINARY"))
        self.assertEqual("BOOLEAN", self.name_for("BOOLEAN"))

    def test_unknown_and_empty_fall_back_to_text(self):
        self.assertEqual("TEXT", self.name_for("VARCHAR"))
        self.assertEqual("TEXT", self.name_for("GEOGRAPHY"))
        self.assertEqual("TEXT", self.name_for(None))

    def test_names_and_ids_round_trip(self):
        for field_id, name in constants.FIELD_ID_TO_NAME.items():
            self.assertEqual(field_id, constants.FIELD_NAME_TO_ID[name])


class ParamstyleTest(unittest.TestCase):

    def test_format_is_treated_as_pyformat(self):
        conn = sc.connect(host="localhost", port=1, paramstyle="format")
        self.assertEqual("pyformat", conn._paramstyle)

    def test_unsupported_paramstyle_is_rejected(self):
        self.assertRaises(errors.ProgrammingError,
                          sc.connect, host="localhost", port=1, paramstyle="numeric")


class SessionValueTest(unittest.TestCase):
    """ALTER SESSION SET renders values by type."""

    def test_booleans_are_keywords(self):
        self.assertEqual("TRUE", sc._session_value(True))
        self.assertEqual("FALSE", sc._session_value(False))

    def test_numbers_are_bare(self):
        self.assertEqual("42", sc._session_value(42))
        self.assertEqual("1.5", sc._session_value(1.5))

    def test_everything_else_is_quoted(self):
        self.assertEqual("'UTC'", sc._session_value("UTC"))
        self.assertEqual("'a''b'", sc._session_value("a'b"))


class PyformatBindingTest(unittest.TestCase):
    """The %-scanner has to leave literals, identifiers and comments alone."""

    def test_percent_escape(self):
        self.assertEqual("SELECT 100% AS pct", sc._bind_pyformat("SELECT 100%% AS pct", ()))

    def test_escape_is_not_applied_inside_a_literal(self):
        # Inside a string literal the scanner passes everything through untouched, so
        # a doubled percent stays doubled — it is data, not an escape.
        self.assertEqual("SELECT '100%%'", sc._bind_pyformat("SELECT '100%%'", ()))

    def test_placeholders_are_skipped_inside_quoted_identifiers(self):
        self.assertEqual('SELECT "c%s", 1', sc._bind_pyformat('SELECT "c%s", %s', (1,)))

    def test_placeholders_are_skipped_inside_line_comments(self):
        self.assertEqual("SELECT 1 -- %s\n", sc._bind_pyformat("SELECT %s -- %s\n", (1,)))
        self.assertEqual("SELECT 1 // %s\n", sc._bind_pyformat("SELECT %s // %s\n", (1,)))

    def test_placeholders_are_skipped_inside_block_comments(self):
        self.assertEqual("SELECT 1 /* %s */", sc._bind_pyformat("SELECT %s /* %s */", (1,)))

    def test_unterminated_block_comment(self):
        self.assertEqual("SELECT 1 /* %s", sc._bind_pyformat("SELECT %s /* %s", (1,)))

    def test_a_bare_percent_is_literal(self):
        self.assertEqual("SELECT 50% OFF", sc._bind_pyformat("SELECT 50% OFF", ()))

    def test_positional_placeholder_with_dict_parameters(self):
        self.assertRaises(errors.ProgrammingError,
                          sc._bind_pyformat, "SELECT %s", {"a": 1})

    def test_named_placeholder_with_sequence_parameters(self):
        self.assertRaises(errors.ProgrammingError,
                          sc._bind_pyformat, "SELECT %(a)s", (1,))

    def test_not_enough_positional_parameters(self):
        self.assertRaises(errors.ProgrammingError,
                          sc._bind_pyformat, "SELECT %s, %s", (1,))

    def test_missing_named_parameter(self):
        self.assertRaises(errors.ProgrammingError,
                          sc._bind_pyformat, "SELECT %(missing)s", {"a": 1})

    def test_unterminated_named_placeholder(self):
        self.assertRaises(errors.ProgrammingError,
                          sc._bind_pyformat, "SELECT %(a", {"a": 1})


class DollarQuotedTest(unittest.TestCase):
    """Procedure and UDF bodies are written as $$…$$ and are data throughout."""

    def test_placeholder_inside_a_body_is_not_bound(self):
        self.assertEqual("SELECT $$a %s b$$, 'x'",
                         sc._bind_pyformat("SELECT $$a %s b$$, %s", ("x",)))

    def test_named_placeholder_inside_a_body_is_not_bound(self):
        self.assertEqual("SELECT $$%(a)s$$, 'x'",
                         sc._bind_pyformat("SELECT $$%(a)s$$, %(a)s", {"a": "x"}))

    def test_a_procedure_body_is_not_split_on_its_semicolons(self):
        script = ("CREATE OR REPLACE PROCEDURE q() RETURNS INTEGER LANGUAGE SQL "
                  "AS $$ BEGIN LET x INTEGER := 1; RETURN x; END; $$; SELECT 1;")
        parts = sc._split_statements(script)
        self.assertEqual(2, len(parts))
        self.assertTrue(parts[0].endswith("$$"), parts[0])
        self.assertEqual("SELECT 1", parts[1])

    def test_unterminated_body_runs_to_the_end(self):
        self.assertEqual(["SELECT $$ ; unterminated"],
                         sc._split_statements("SELECT $$ ; unterminated"))


class FetchmanyZeroTest(unittest.TestCase):

    def test_zero_means_zero(self):
        cur = sc.FrostlakeCursor.__new__(sc.FrostlakeCursor)
        cur.arraysize = 5
        cur._rows = [(1,), (2,), (3,)]
        cur._pos = 0
        self.assertEqual([], cur.fetchmany(0))
        self.assertEqual(0, cur._pos)          # nothing was consumed


class GuardTranslationTest(unittest.TestCase):
    """Every driver exception has to arrive as its connector counterpart."""

    def raising(self, exception):
        def fn():
            raise exception
        return fn

    def test_compile_errors_carry_the_connector_code(self):
        import frostlake
        with self.assertRaises(errors.ProgrammingError) as caught:
            sc._guard(self.raising(frostlake.ProgrammingError("SQL compilation error: nope")))
        self.assertEqual(1003, caught.exception.errno)
        self.assertEqual("42000", caught.exception.sqlstate)

    def test_other_programming_errors_carry_no_compile_code(self):
        # Only engine compile errors get the 1003/42000 stamp; a client-side binding
        # failure is still a ProgrammingError but without that code.
        import frostlake
        with self.assertRaises(errors.ProgrammingError) as caught:
            sc._guard(self.raising(frostlake.ProgrammingError("not enough parameters")))
        self.assertNotEqual(1003, getattr(caught.exception, "errno", None))
        self.assertNotEqual("42000", getattr(caught.exception, "sqlstate", None))

    def test_operational_and_interface_errors(self):
        import frostlake
        self.assertRaises(errors.OperationalError,
                          sc._guard, self.raising(frostlake.OperationalError("down")))
        self.assertRaises(errors.InterfaceError,
                          sc._guard, self.raising(frostlake.InterfaceError("closed")))

    def test_a_lost_session_keeps_a_class_of_its_own(self):
        import frostlake
        with self.assertRaises(errors.SessionLostError) as caught:
            sc._guard(self.raising(frostlake.SessionLostError("gone")), query="SELECT 1")
        self.assertIsInstance(caught.exception, errors.OperationalError)
        self.assertEqual("frostlake_connector.errors", type(caught.exception).__module__)
        self.assertEqual("SELECT 1", caught.exception.query)

    def test_any_other_driver_error_becomes_a_database_error(self):
        import frostlake
        for cls in (frostlake.DataError, frostlake.IntegrityError,
                    frostlake.InternalError, frostlake.NotSupportedError):
            self.assertRaises(errors.DatabaseError, sc._guard, self.raising(cls("x")))

    def test_a_successful_call_passes_its_value_through(self):
        self.assertEqual(7, sc._guard(lambda: 7))


class SplitStatementsTest(unittest.TestCase):

    def test_semicolons_inside_literals_do_not_split(self):
        self.assertEqual(["SELECT 'a;b'"], sc._split_statements("SELECT 'a;b'"))

    def test_semicolons_inside_identifiers_do_not_split(self):
        self.assertEqual(['SELECT "a;b"'], sc._split_statements('SELECT "a;b"'))

    def test_semicolons_inside_comments_do_not_split(self):
        # Statements come back stripped, so the comment's trailing newline is gone.
        self.assertEqual(["SELECT 1 -- ;"], sc._split_statements("SELECT 1 -- ;\n"))
        self.assertEqual(["SELECT 1 // ;"], sc._split_statements("SELECT 1 // ;\n"))
        self.assertEqual(["SELECT 1 /* ; */"], sc._split_statements("SELECT 1 /* ; */"))

    def test_unterminated_block_comment_runs_to_the_end(self):
        self.assertEqual(["SELECT 1 /* ;"], sc._split_statements("SELECT 1 /* ;"))

    def test_trailing_statement_without_a_semicolon(self):
        self.assertEqual(["SELECT 1", "SELECT 2"], sc._split_statements("SELECT 1; SELECT 2"))

    def test_blank_statements_are_dropped(self):
        self.assertEqual(["SELECT 1"], sc._split_statements(";;  SELECT 1 ;; "))

    def test_empty_script(self):
        self.assertEqual([], sc._split_statements("   "))


class FacadeTest(unittest.TestCase):
    def setUp(self):
        if PORT is None:
            self.skipTest("FROSTLAKE_CLASSPATH not set")

    def bootstrap(self, database):
        conn = sc.connect(host="127.0.0.1", port=PORT)
        cur = conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE %s" % database)
        cur.execute("USE DATABASE %s" % database)
        return conn, cur

    def test_session_id_and_is_closed(self):
        conn, cur = self.bootstrap("fac_state_db")
        self.assertFalse(conn.is_closed())
        cur.execute("SELECT 1")
        self.assertTrue(conn.session_id)          # assigned once the server answers
        conn.close()
        self.assertTrue(conn.is_closed())

    def test_role_and_timezone_kwargs(self):
        conn, cur = self.bootstrap("fac_role_db")
        cur.execute("CREATE ROLE IF NOT EXISTS fac_role")
        conn.close()
        conn2 = sc.connect(host="127.0.0.1", port=PORT, database="fac_role_db",
                           role="fac_role", timezone="UTC")
        cur2 = conn2.cursor()
        cur2.execute("SELECT CURRENT_ROLE() AS r")
        self.assertEqual([("FAC_ROLE",)], cur2.fetchall())
        conn2.close()

    def test_a_quoted_database_kwarg_reaches_a_lower_case_object(self):
        # The names in connect() fold like unquoted SQL, so a database whose real name
        # is lower case is reachable only by passing the quotes along with it.
        conn, cur = self.bootstrap("fac_quoting_db")
        cur.execute('CREATE OR REPLACE DATABASE "fac_lower_db"')
        conn.close()
        conn2 = sc.connect(host="127.0.0.1", port=PORT, database='"fac_lower_db"')
        cur2 = conn2.cursor()
        cur2.execute("SELECT CURRENT_DATABASE() AS d")
        # 0.1.0 keeps the quoted name's case; 0.0.7 upper-cased it on the way in and
        # looked names up case-insensitively, so either answer means the USE landed.
        self.assertIn(cur2.fetchall(), ([("fac_lower_db",)], [("FAC_LOWER_DB",)]))
        conn2.close()

    def test_semi_structured_values_come_back_as_their_json_text(self):
        # The client inherits the driver's rule: a semi-structured cell is the JSON text
        # of the value, and its type_code names the family it came from.
        conn, cur = self.bootstrap("fac_variant_db")
        cur.execute("CREATE OR REPLACE TABLE v (j VARIANT, o OBJECT, a ARRAY)")
        cur.execute("""INSERT INTO v SELECT PARSE_JSON('{"a":1}'),
                       OBJECT_CONSTRUCT('b', 2), ARRAY_CONSTRUCT(1, 2)""")
        cur.execute("SELECT j, o, a FROM v")
        self.assertEqual(["VARIANT", "OBJECT", "ARRAY"],
                         [constants.FIELD_ID_TO_NAME[d.type_code] for d in cur.description])
        self.assertEqual([('{"a":1}', '{"b":2}', "[1,2]")], cur.fetchall())
        conn.close()

    def test_description_reports_text_and_binary_lengths(self):
        conn, cur = self.bootstrap("fac_len_db")
        cur.execute("CREATE OR REPLACE TABLE s (t VARCHAR(20), b BINARY(10), n NUMBER(12,2))")
        cur.execute("SELECT t, b, n FROM s")
        sizes = [(d.display_size, d.internal_size) for d in cur.description]
        if sizes[0][1] is None:
            # An engine predating the wire's length field sends none at all.
            self.assertEqual([(None, None)] * 3, sizes)
        else:
            # internal_size carries the length; display_size is None throughout, which is
            # the shape the account's own Python client reports.
            self.assertEqual([(None, 20), (None, 10), (None, None)], sizes)
        conn.close()

    def test_cursor_fetch_mechanics(self):
        conn, cur = self.bootstrap("fac_fetch_db")
        cur.execute("SELECT 1 AS n UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4")
        self.assertEqual([(1,)], cur.fetchmany())        # arraysize defaults to 1
        self.assertEqual([(2,), (3,)], cur.fetchmany(2))
        self.assertEqual([(4,)], cur.fetchmany(10))
        self.assertEqual([], cur.fetchmany(1))
        self.assertIsNone(cur.fetchone())                # exhausted
        conn.close()

    def test_cursor_iteration_and_close(self):
        conn, cur = self.bootstrap("fac_iter_db")
        cur.execute("SELECT 1 AS n UNION ALL SELECT 2")
        self.assertEqual([(1,), (2,)], list(cur))
        self.assertTrue(cur.close())
        self.assertIsNone(cur.description)
        conn.close()

    def test_cursor_and_connection_context_managers(self):
        conn, _ = self.bootstrap("fac_ctx_mgr_db")
        with conn.cursor() as cur:
            cur.execute("SELECT 7 AS n")
            self.assertEqual([(7,)], cur.fetchall())
        conn.close()

        with sc.connect(host="127.0.0.1", port=PORT) as scoped:
            scoped_cur = scoped.cursor()
            scoped_cur.execute("SELECT 8 AS n")
            self.assertEqual([(8,)], scoped_cur.fetchall())
        self.assertTrue(scoped.is_closed())

    def test_autocommit_to_the_same_value_is_a_noop(self):
        conn, cur = self.seeded("fac_ac_noop_db")
        conn.autocommit(True)            # already on
        cur.execute("INSERT INTO t VALUES (9)")
        self.assertEqual(4, self.count(cur))
        conn.close()

    def test_dict_cursor_via_execute_string(self):
        conn, cur = self.bootstrap("fac_dict_script_db")
        cursors = conn.execute_string("SELECT 1 AS a; SELECT 2 AS b;",
                                      cursor_class=DictCursor)
        self.assertEqual([{"A": 1}], cursors[0].fetchall())
        self.assertEqual([{"B": 2}], cursors[1].fetchall())
        conn.close()

    def test_description_reports_real_nullability(self):
        conn, cur = self.bootstrap("fac_null_db")
        cur.execute("CREATE OR REPLACE TABLE n (req INTEGER NOT NULL, opt INTEGER)")
        cur.execute("SELECT req, opt FROM n")
        self.assertEqual([False, True], [d.is_nullable for d in cur.description])
        conn.close()

    def test_nextset_walks_a_multi_statement_execute(self):
        conn, cur = self.bootstrap("fac_nextset_db")
        # A script handed to execute whole travels as one request, and a session
        # runs one statement per request until it asks for more. (execute_string
        # splits client-side, so it never needs this.)
        cur.execute("ALTER SESSION SET MULTI_STATEMENT_COUNT = 0")
        cur.execute("SELECT 1 AS a; SELECT 2 AS b; SELECT 3 AS c;")
        collected = [cur.fetchall()]
        while cur.nextset():
            collected.append(cur.fetchall())
        self.assertEqual([[(1,)], [(2,)], [(3,)]], collected)
        self.assertIsNone(cur.nextset())
        conn.close()

    def test_nextset_is_none_for_a_single_statement(self):
        conn, cur = self.bootstrap("fac_nextset1_db")
        cur.execute("SELECT 1 AS a")
        self.assertIsNone(cur.nextset())
        conn.close()

    def test_user_defined_function_is_callable(self):
        conn, cur = self.bootstrap("fac_udf_db")
        cur.execute("CREATE OR REPLACE FUNCTION double_it(a INTEGER) "
                    "RETURNS INTEGER AS $$ a * 2 $$")
        cur.execute("SELECT double_it(%s) AS doubled", (21,))
        self.assertEqual([(42,)], cur.fetchall())
        conn.close()

    def test_function_used_across_a_table(self):
        conn, cur = self.bootstrap("fac_udf2_db")
        cur.execute("CREATE TABLE nums (n INTEGER)")
        cur.executemany("INSERT INTO nums VALUES (%s)", [(1,), (2,), (3,)])
        cur.execute("CREATE OR REPLACE FUNCTION squared(a INTEGER) "
                    "RETURNS INTEGER AS $$ a * a $$")
        cur.execute("SELECT n, squared(n) AS sq FROM nums ORDER BY n")
        self.assertEqual([(1, 1), (2, 4), (3, 9)], cur.fetchall())
        conn.close()

    def test_function_and_procedure_in_one_script(self):
        # Both bodies are dollar-quoted, so execute_string must not split them.
        conn, cur = self.bootstrap("fac_udf3_db")
        cursors = conn.execute_string("""
            CREATE OR REPLACE FUNCTION shout(v VARCHAR) RETURNS VARCHAR
                AS $$ UPPER(v) || '!' $$;
            CREATE OR REPLACE PROCEDURE greet(v VARCHAR) RETURNS VARCHAR LANGUAGE SQL
                AS $$ BEGIN RETURN shout(v); END; $$;
            SELECT shout('hi') AS loud;
        """)
        self.assertEqual(3, len(cursors))
        self.assertEqual([("HI!",)], cursors[2].fetchall())
        cur.execute("CALL greet(%s)", ("hey",))
        self.assertEqual([("HEY!",)], cur.fetchall())
        conn.close()

    def test_errors_name_the_statement_that_failed(self):
        conn, cur = self.bootstrap("fac_qid_db")
        with self.assertRaises(errors.ProgrammingError) as caught:
            cur.execute("SELECT * FROM no_such_table_here")
        self.assertIn("no_such_table_here", caught.exception.query or "")
        self.assertTrue(caught.exception.query_id)
        self.assertEqual(cur.query_id, caught.exception.query_id)
        conn.close()

    def test_query_id_is_set_even_when_the_statement_fails(self):
        conn, cur = self.bootstrap("fac_qid2_db")
        try:
            cur.execute("SELECT nonsense FROM nowhere")
        except errors.Error:
            pass
        self.assertTrue(cur.query_id)
        conn.close()

    def test_procedure_body_survives_execute_string(self):
        conn, cur = self.bootstrap("fac_proc_db")
        cursors = conn.execute_string("""
            CREATE OR REPLACE PROCEDURE tag(x INTEGER) RETURNS INTEGER LANGUAGE SQL
            AS $$ BEGIN LET y INTEGER := x; RETURN y; END; $$;
            CALL tag(4);
        """)
        self.assertEqual(2, len(cursors))
        self.assertEqual([(4,)], cursors[1].fetchall())
        conn.close()

    def test_dict_cursor_on_a_ddl_statement(self):
        # Engines before 0.1.0 answer DDL with no result set (description None, no
        # rows); 0.1.0 answers with a one-row status grid, as live does. Either way a
        # DictCursor hands back what it got rather than fall over.
        conn, _ = self.bootstrap("fac_dict_ddl_db")
        cur = conn.cursor(DictCursor)
        cur.execute("CREATE OR REPLACE TABLE t (a INTEGER)")
        rows = cur.fetchall()
        if cur.description is None:
            self.assertEqual([], rows)
        else:
            self.assertEqual(["status"], [column[0] for column in cur.description])
            self.assertEqual(1, len(rows))
            self.assertIn("status", rows[0])
        conn.close()

    def test_binding_errors_surface_as_programming_errors(self):
        conn = sc.connect(host="127.0.0.1", port=PORT, paramstyle="qmark")
        cur = conn.cursor()
        self.assertRaises(errors.ProgrammingError, cur.execute, "SELECT ?, ?", (1,))
        conn.close()

    def test_execute_string_can_skip_returning_cursors(self):
        conn, cur = self.bootstrap("fac_noret_db")
        self.assertEqual([], conn.execute_string("SELECT 1; SELECT 2;",
                                                 return_cursors=False))
        conn.close()

    def test_unreachable_server_is_an_operational_error(self):
        conn = sc.connect(host="127.0.0.1", port=1, network_timeout=2)
        cur = conn.cursor()
        self.assertRaises(errors.OperationalError, cur.execute, "SELECT 1")

    def test_using_a_closed_connection_is_an_interface_error(self):
        conn, cur = self.bootstrap("fac_closed_db")
        conn.close()
        self.assertRaises(errors.InterfaceError, cur.execute, "SELECT 1")

    def seeded(self, database):
        """A table with three rows, in a fresh database."""
        conn, cur = self.bootstrap(database)
        cur.execute("CREATE OR REPLACE TABLE t (a INTEGER)")
        cur.execute("INSERT INTO t VALUES (1), (2), (3)")
        return conn, cur

    def count(self, cur):
        cur.execute("SELECT COUNT(*) FROM t")
        return cur.fetchone()[0]

    def test_autocommit_off_then_rollback_restores(self):
        # The engine only rolls back what an explicit BEGIN opened, so autocommit(False)
        # has to start a transaction rather than just clear a flag.
        conn, cur = self.seeded("fac_tx_rollback_db")
        conn.autocommit(False)
        cur.execute("DELETE FROM t")
        self.assertEqual(0, self.count(cur))
        conn.rollback()
        self.assertEqual(3, self.count(cur))
        conn.close()

    def test_autocommit_off_then_commit_persists(self):
        conn, cur = self.seeded("fac_tx_commit_db")
        conn.autocommit(False)
        cur.execute("INSERT INTO t VALUES (4)")
        conn.commit()
        self.assertEqual(4, self.count(cur))
        conn.rollback()          # nothing left to undo
        self.assertEqual(4, self.count(cur))
        conn.close()

    def test_transaction_restarts_after_each_boundary(self):
        # With autocommit off the connector keeps you transactional: the statements
        # after a rollback belong to a new transaction, which can itself roll back.
        conn, cur = self.seeded("fac_tx_restart_db")
        conn.autocommit(False)
        cur.execute("DELETE FROM t")
        conn.rollback()
        cur.execute("DELETE FROM t WHERE a = 1")
        self.assertEqual(2, self.count(cur))
        conn.rollback()
        self.assertEqual(3, self.count(cur))
        conn.close()

    def test_autocommit_back_on_commits_and_persists(self):
        conn, cur = self.seeded("fac_tx_reenable_db")
        conn.autocommit(False)
        cur.execute("INSERT INTO t VALUES (4)")
        conn.autocommit(True)            # commits what was open
        cur.execute("INSERT INTO t VALUES (5)")
        conn.rollback()                  # a no-op while autocommitting
        self.assertEqual(5, self.count(cur))
        conn.close()

    def test_connect_with_autocommit_false(self):
        conn, cur = self.seeded("fac_tx_kwarg_db")
        conn.close()
        conn2 = sc.connect(host="127.0.0.1", port=PORT, database="fac_tx_kwarg_db",
                           autocommit=False)
        cur2 = conn2.cursor()
        cur2.execute("DELETE FROM t")
        self.assertEqual(0, self.count(cur2))
        conn2.rollback()
        self.assertEqual(3, self.count(cur2))
        conn2.close()

    def test_connect_kwargs_apply_context(self):
        conn, cur = self.bootstrap("fac_ctx_db")
        cur.execute("CREATE SCHEMA IF NOT EXISTS fac_ctx_db.s2")
        cur.execute("CREATE WAREHOUSE IF NOT EXISTS fac_wh")
        conn.close()
        conn2 = sc.connect(
            account="ignored",
            user="ignored",
            password="ignored",
            host="127.0.0.1",
            port=PORT,
            database="fac_ctx_db",
            schema="s2",
            warehouse="fac_wh",
            session_parameters={"QUERY_TAG": "dbt-facade-test"},
            client_session_keep_alive=True,
        )
        cur2 = conn2.cursor()
        cur2.execute("SELECT CURRENT_DATABASE() AS d, CURRENT_SCHEMA() AS s, CURRENT_WAREHOUSE() AS w")
        self.assertEqual(cur2.fetchall(), [("FAC_CTX_DB", "S2", "FAC_WH")])
        conn2.close()

    def test_cursor_roundtrip_and_description(self):
        conn, cur = self.bootstrap("fac_cur_db")
        cur.execute("CREATE TABLE people (id INTEGER, name VARCHAR, score FLOAT, ok BOOLEAN)")
        cur.execute(
            "INSERT INTO people VALUES (%s, %s, %s, %s), (%s, %s, %s, %s)",
            (1, "Ada O'Hara \\ Byron", 9.5, True, 2, "Grace", 8.25, False),
        )
        self.assertEqual(cur.rowcount, 2)
        cur.execute("SELECT id, name, score, ok FROM people WHERE id = %s", (1,))
        self.assertEqual(cur.fetchall(), [(1, "Ada O'Hara \\ Byron", 9.5, True)])
        self.assertIsNotNone(cur.query_id)
        names = [d.name for d in cur.description]
        codes = [d.type_code for d in cur.description]
        self.assertEqual(names, ["ID", "NAME", "SCORE", "OK"])
        self.assertEqual(
            codes,
            [
                sc.constants.FIELD_NAME_TO_ID["FIXED"],
                sc.constants.FIELD_NAME_TO_ID["TEXT"],
                sc.constants.FIELD_NAME_TO_ID["REAL"],
                sc.constants.FIELD_NAME_TO_ID["BOOLEAN"],
            ],
        )
        conn.close()

    def test_dict_cursor(self):
        conn, cur = self.bootstrap("fac_dict_db")
        cur.execute("CREATE TABLE t (a INTEGER, b VARCHAR)")
        cur.execute("INSERT INTO t VALUES (1, 'x')")
        dcur = conn.cursor(DictCursor)
        dcur.execute("SELECT a, b FROM t")
        self.assertEqual(dcur.fetchall(), [{"A": 1, "B": "x"}])
        conn.close()

    def test_qmark_paramstyle(self):
        conn = sc.connect(host="127.0.0.1", port=PORT, paramstyle="qmark")
        cur = conn.cursor()
        cur.execute("CREATE OR REPLACE DATABASE fac_qm_db")
        cur.execute("USE DATABASE fac_qm_db")
        cur.execute("CREATE TABLE q (a INTEGER)")
        cur.execute("INSERT INTO q VALUES (?)", (7,))
        cur.execute("SELECT a FROM q WHERE a = ?", (7,))
        self.assertEqual(cur.fetchall(), [(7,)])
        conn.close()

    def test_executemany_accumulates_rowcount(self):
        conn, cur = self.bootstrap("fac_many_db")
        cur.execute("CREATE TABLE seeds (a INTEGER, b VARCHAR)")
        cur.executemany("INSERT INTO seeds VALUES (%s, %s)", [(1, "x"), (2, "y"), (3, "z")])
        self.assertEqual(cur.rowcount, 3)
        conn.close()

    def test_execute_string(self):
        conn, cur = self.bootstrap("fac_es_db")
        cursors = conn.execute_string(
            "CREATE TABLE es (a INTEGER); INSERT INTO es VALUES (1); SELECT 'x;y' AS v"
        )
        self.assertEqual(len(cursors), 3)
        self.assertEqual(cursors[1].rowcount, 1)
        self.assertEqual(cursors[2].fetchall(), [("x;y",)])
        conn.close()

    def test_error_shape(self):
        conn, cur = self.bootstrap("fac_err_db")
        with self.assertRaises(errors.ProgrammingError) as ctx:
            cur.execute("SELECT FROM nowhere")
        e = ctx.exception
        self.assertIn("SQL compilation error", e.msg)
        self.assertEqual(e.errno, 1003)
        self.assertEqual(e.sqlstate, "42000")
        self.assertIsInstance(e, errors.DatabaseError)
        conn.close()

    def test_transactions_dbt_style(self):
        conn, cur = self.bootstrap("fac_tx_db")
        cur.execute("CREATE TABLE acc (n INTEGER)")
        cur.execute("INSERT INTO acc VALUES (1)")
        conn.autocommit(False)
        cur.execute("BEGIN")
        cur.execute("INSERT INTO acc VALUES (2)")
        conn.rollback()
        conn.autocommit(True)
        cur.execute("SELECT COUNT(*) AS n FROM acc")
        self.assertEqual(cur.fetchall(), [(1,)])

        conn.autocommit(False)
        cur.execute("BEGIN")
        cur.execute("INSERT INTO acc VALUES (3)")
        conn.commit()
        conn.autocommit(True)
        cur.execute("SELECT COUNT(*) AS n FROM acc")
        self.assertEqual(cur.fetchall(), [(2,)])
        conn.close()


class MultiStatementCountTest(unittest.TestCase):
    """`cursor.execute(..., num_statements=N)`, the account's connector spelling."""

    def setUp(self):
        if PORT is None:
            self.skipTest("FROSTLAKE_CLASSPATH not set")
        self.conn = sc.connect(host="127.0.0.1", port=PORT)

    def tearDown(self):
        self.conn.close()

    def test_a_declared_pack_runs(self):
        cur = self.conn.cursor()
        cur.execute("SELECT 1 AS a; SELECT 2 AS b;", num_statements=2)
        self.assertEqual([(1,)], cur.fetchall())

    def test_zero_takes_any_number(self):
        cur = self.conn.cursor()
        cur.execute("SELECT 1 AS a; SELECT 2 AS b; SELECT 3 AS c;", num_statements=0)
        self.assertEqual([(1,)], cur.fetchall())

    def test_the_session_count_is_untouched(self):
        cur = self.conn.cursor()
        cur.execute("SELECT 1 AS a; SELECT 2 AS b;", num_statements=2)
        # The count travelled with that request; the session still refuses an undeclared pack.
        enforced = True
        try:
            cur.execute("SELECT 1 AS a; SELECT 2 AS b;")
        except errors.ProgrammingError:
            pass
        else:
            enforced = False
        # Either way the connection is still usable for an ordinary statement.
        cur.execute("SELECT 9 AS a")
        self.assertEqual([(9,)], cur.fetchall())
        if not enforced:
            # An engine predating the statement-count rule accepts any pack, so it has
            # nothing to refuse here. Skip rather than pass: the refusal is the point.
            self.skipTest("this engine does not enforce the statement count")

    def test_a_bad_count_is_refused(self):
        cur = self.conn.cursor()
        self.assertRaises(errors.ProgrammingError,
                          cur.execute, "SELECT 1", None, -1)


class MultiStatementCountWireTest(unittest.TestCase):
    """What `num_statements` puts on the wire — no server of the engine's needed."""

    BODIES = []

    @classmethod
    def setUpClass(cls):
        bodies = cls.BODIES

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                bodies.append(json.loads(self.rfile.read(length).decode("utf-8")))
                body = b'{"success": true, "sessionId": "s1", "resultSets": []}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                pass

        cls.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        del self.BODIES[:]
        self.conn = sc.connect(host="127.0.0.1", port=self.server.server_address[1])

    def test_asking_for_nothing_sends_no_field(self):
        self.conn.cursor().execute("SELECT 1")
        self.assertNotIn("multiStatementCount", self.BODIES[-1])

    def test_a_count_rides_on_the_request(self):
        self.conn.cursor().execute("SELECT 1; SELECT 2;", num_statements=2)
        self.assertEqual(2, self.BODIES[-1]["multiStatementCount"])
        self.assertEqual(["SELECT 1; SELECT 2;"], [b["sql"] for b in self.BODIES])

    def test_zero_is_an_answer_and_is_sent(self):
        self.conn.cursor().execute("SELECT 1; SELECT 2;", num_statements=0)
        self.assertEqual(0, self.BODIES[-1]["multiStatementCount"])


# -- session lifetime ---------------------------------------------------------------

class ScriptedEngine(object):
    """A stand-in engine on a local socket: it answers each request from a script and
    records what it was sent. A request the script did not expect is recorded as such and
    answered with a body that is not an engine's."""

    def __init__(self):
        self.requests = []
        self.unscripted = []
        self._script = []
        engine = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                engine._handle(self)

            do_POST = do_GET
            do_DELETE = do_GET

            def log_message(self, fmt, *args):
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._server.block_on_close = False
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()
        self.port = self._server.server_address[1]

    def reply(self, body, status=200):
        self._script.append((status, json.dumps(body)))

    def reply_text(self, text, status):
        """Answer with a body that is not JSON, the way a proxy in the way does."""
        self._script.append((status, text))

    def drop(self):
        """Close the socket without answering."""
        self._script.append((None, ""))

    @property
    def pending(self):
        return len(self._script)

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def _handle(self, handler):
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        body = json.loads(raw.decode("utf-8")) if raw else None
        self.requests.append((handler.command, handler.path, body))
        if self._script:
            status, text = self._script.pop(0)
        else:
            self.unscripted.append((handler.command, handler.path, body))
            status, text = 500, "<html>unscripted</html>"
        if status is None:
            handler.close_connection = True
            return
        payload = text.encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    def statements(self):
        return [body["sql"] for _, path, body in self.requests if path == "/api/execute"]

    def executes(self):
        return [body for _, path, body in self.requests if path == "/api/execute"]


def _status():
    return {"columns": [{"name": "status", "dataType": "VARCHAR"}],
            "rows": [["Statement executed successfully."]], "rowCount": 1, "updateCount": -1}


def _number(name, value):
    return {"columns": [{"name": name, "dataType": "NUMBER", "precision": 38, "scale": 0}],
            "rows": [[value]], "rowCount": 1, "updateCount": -1}


def _answer(session_id, started, *sets):
    return {"success": True, "sessionId": session_id, "newSession": started,
            "errorMessage": None, "resultSets": list(sets)}


def _gone(session_id):
    return {"success": False, "sessionId": None, "newSession": False,
            "errorMessage": "Session '%s' does not exist or has expired." % session_id,
            "resultSets": []}


_RELEASED = {"success": True, "sessionId": None, "newSession": False, "resultSets": []}

# What connect() below sets up, in the order the connection applies it.
_SETUP = ["USE ROLE R1", "USE WAREHOUSE W1", "USE DATABASE APP", "USE SCHEMA PUBLIC",
          "ALTER SESSION SET QUERY_TAG = 'tagged'"]


class SessionLifetimeTest(unittest.TestCase):
    """The connection's session over a scripted engine: no engine of the real kind needed."""

    def setUp(self):
        self.engine = ScriptedEngine()

    def tearDown(self):
        self.engine.stop()

    def connect(self, **extra):
        return sc.connect(host="127.0.0.1", port=self.engine.port, network_timeout=5,
                          role="R1", warehouse="W1", database="APP", schema="PUBLIC",
                          session_parameters={"QUERY_TAG": "tagged"}, **extra)

    def set_up(self, session_id):
        self.engine.reply(_answer(session_id, True, _status()))
        for _ in _SETUP[1:]:
            self.engine.reply(_answer(session_id, False, _status()))

    def opened(self, **extra):
        """A connection that has run SELECT 1 on session s1, after its setup."""
        self.set_up("s1")
        self.engine.reply(_answer("s1", False, _number("N", 1)))
        conn = self.connect(**extra)
        cur = conn.cursor()
        cur.execute("SELECT 1 AS N")
        return conn, cur

    def assert_all_answered(self):
        self.assertEqual([], self.engine.unscripted)
        self.assertEqual(0, self.engine.pending)

    def assert_session_lost(self, call, *args):
        # Caught as the PEP 249 class first, so a client that raises something else shows
        # what it raised.
        with self.assertRaises(errors.OperationalError) as caught:
            call(*args)
        self.assertIsInstance(caught.exception, errors.SessionLostError)
        return caught.exception

    def test_the_session_is_required_once_the_engine_has_said_it_can(self):
        self.opened()
        bodies = self.engine.executes()
        self.assertNotIn("requireSession", bodies[0])
        for body in bodies[1:]:
            self.assertEqual("s1", body["sessionId"])
            self.assertIs(True, body["requireSession"])
        self.assertEqual(_SETUP + ["SELECT 1 AS N"], self.engine.statements())

    def test_a_lost_session_comes_back_on_the_connect_setup_and_the_statement_is_resent(self):
        conn, cur = self.opened()
        self.engine.reply(_gone("s1"), status=404)
        self.set_up("s2")
        self.engine.reply(_answer("s2", False, _number("N", 2)))
        cur.execute("SELECT 2 AS N")
        self.assertEqual([(2,)], cur.fetchall())
        # The setup is the connection's own, not the caller's context, so it is what the
        # fresh session gets: the statement is not refused over it.
        self.assertEqual(_SETUP + ["SELECT 1 AS N", "SELECT 2 AS N"] + _SETUP + ["SELECT 2 AS N"],
                         self.engine.statements())
        self.assertEqual("s2", conn.session_id)
        self.assert_all_answered()

    def test_a_lost_transaction_is_reported_and_the_next_statement_starts_over(self):
        conn, cur = self.opened()
        conn.autocommit(False)
        self.engine.reply(_answer("s1", False, _status()))
        self.engine.reply(_answer("s1", False, _number("number of rows inserted", 1)))
        cur.execute("INSERT INTO t VALUES (1)")
        self.engine.reply(_gone("s1"), status=404)
        error = self.assert_session_lost(cur.execute, "INSERT INTO t VALUES (2)")
        self.assertIn("transaction", error.msg)
        self.assertEqual("INSERT INTO t VALUES (2)", error.query)
        self.assertFalse(conn.is_closed())
        # A fresh session on the setup, and with autocommit off, a transaction on it.
        self.set_up("s2")
        self.engine.reply(_answer("s2", False, _status()))
        self.engine.reply(_answer("s2", False, _number("N", 3)))
        cur.execute("SELECT 3 AS N")
        self.assertEqual(_SETUP + ["BEGIN", "SELECT 3 AS N"], self.engine.statements()[9:])
        self.assert_all_answered()

    def test_a_lost_session_after_a_use_is_reported(self):
        conn, cur = self.opened()
        self.engine.reply(_answer("s1", False, _status()))
        cur.execute("USE SCHEMA OTHER")
        self.engine.reply(_gone("s1"), status=404)
        error = self.assert_session_lost(cur.execute, "SELECT * FROM t")
        self.assertIn("context", error.msg)
        self.assertEqual(_SETUP + ["SELECT 1 AS N", "USE SCHEMA OTHER", "SELECT * FROM t"],
                         self.engine.statements())
        self.assert_all_answered()

    def test_a_refused_setup_statement_is_the_one_named(self):
        self.engine.reply(_answer("s1", True, _status()))
        self.engine.reply({"success": False, "sessionId": "s1", "newSession": False,
                           "errorMessage": "Warehouse 'W1' does not exist or not authorized.",
                           "resultSets": []})
        conn = self.connect()
        with self.assertRaises(errors.ProgrammingError) as caught:
            conn.cursor().execute("SELECT 1")
        self.assertEqual("USE WAREHOUSE W1", caught.exception.query)
        self.assert_all_answered()

    def test_a_refused_setup_statement_keeps_failing_instead_of_falling_through(self):
        missing = {"success": False, "sessionId": "s1", "newSession": False,
                   "errorMessage": "SQL compilation error:\n"
                                   "Warehouse 'W1' does not exist or not authorized.",
                   "resultSets": []}
        self.engine.reply(_answer("s1", True, _status()))
        self.engine.reply(missing)
        self.engine.reply(missing)
        conn = self.connect()
        cur = conn.cursor()
        with self.assertRaises(errors.ProgrammingError) as first:
            cur.execute("SELECT 1")
        with self.assertRaises(errors.ProgrammingError) as second:
            cur.execute("SELECT CURRENT_WAREHOUSE()")
        # Neither statement reached the engine, nor did the setup past the refused USE:
        # each met the refused statement first. USE ROLE took, and is not sent again.
        self.assertEqual(["USE ROLE R1", "USE WAREHOUSE W1", "USE WAREHOUSE W1"],
                         self.engine.statements())
        for caught in (first, second):
            self.assertEqual("USE WAREHOUSE W1", caught.exception.query)
        # Once the engine takes it (the warehouse was created meanwhile), the rest of the
        # setup follows, and then the statement.
        for _ in _SETUP[1:]:
            self.engine.reply(_answer("s1", False, _status()))
        self.engine.reply(_answer("s1", False, _number("N", 1)))
        cur.execute("SELECT 1 AS N")
        self.assertEqual([(1,)], cur.fetchall())
        self.assertEqual(["USE ROLE R1"] + ["USE WAREHOUSE W1"] * 3 + _SETUP[2:]
                         + ["SELECT 1 AS N"], self.engine.statements())
        self.assert_all_answered()

    def test_closing_releases_the_session_once(self):
        conn, cur = self.opened()
        self.engine.reply(_RELEASED)
        conn.close()
        self.assertEqual(("DELETE", "/api/sessions/s1"), self.engine.requests[-1][:2])
        self.assertTrue(conn.is_closed())
        count = len(self.engine.requests)
        conn.close()
        self.assertEqual(count, len(self.engine.requests))
        self.assertRaises(errors.InterfaceError, cur.execute, "SELECT 1")
        self.assert_all_answered()

    def test_leaving_a_with_block_releases_the_session(self):
        self.set_up("s1")
        self.engine.reply(_answer("s1", False, _number("N", 1)))
        self.engine.reply(_RELEASED)
        with self.connect() as conn:
            conn.cursor().execute("SELECT 1 AS N")
        self.assertEqual("DELETE", self.engine.requests[-1][0])
        self.assert_all_answered()

    def test_closing_never_raises(self):
        for script in (lambda: self.engine.reply(_gone("s1"), status=404),
                       lambda: self.engine.reply({"error": "Method not allowed"}, status=405),
                       self.engine.drop):
            conn, cur = self.opened()
            script()
            conn.close()
            self.assertEqual("DELETE", self.engine.requests[-1][0])
            self.assert_all_answered()
        conn, cur = self.opened()
        self.engine.stop()
        conn.close()


class OwedBeginTest(unittest.TestCase):
    """With autocommit off, the BEGIN a statement is owed stays owed until it took."""

    def setUp(self):
        self.engine = ScriptedEngine()

    def tearDown(self):
        self.engine.stop()

    def connect(self, **extra):
        return sc.connect(host="127.0.0.1", port=self.engine.port, network_timeout=5,
                          role="R1", warehouse="W1", database="APP", schema="PUBLIC",
                          session_parameters={"QUERY_TAG": "tagged"}, **extra)

    def assert_all_answered(self):
        self.assertEqual([], self.engine.unscripted)
        self.assertEqual(0, self.engine.pending)

    def test_a_refused_setup_statement_leaves_the_begin_owed(self):
        self.engine.reply(_answer("s1", True, _status()))
        self.engine.reply({"success": False, "sessionId": "s1", "newSession": False,
                           "errorMessage": "SQL compilation error:\n"
                                           "Warehouse 'W1' does not exist or not authorized.",
                           "resultSets": []})
        conn = self.connect(autocommit=False)
        cur = conn.cursor()
        self.assertRaises(errors.ProgrammingError, cur.execute, "INSERT INTO t VALUES (1)")
        for _ in _SETUP[1:] + ["BEGIN"]:
            self.engine.reply(_answer("s1", False, _status()))
        self.engine.reply(_answer("s1", False, _number("number of rows inserted", 1)))
        self.engine.reply(_answer("s1", False, _status()))
        cur.execute("INSERT INTO t VALUES (1)")
        conn.rollback()
        # Once the setup took, the INSERT ran inside the BEGIN it was owed, where the
        # rollback() reached it.
        self.assertEqual(_SETUP[:2] + _SETUP[1:]
                         + ["BEGIN", "INSERT INTO t VALUES (1)", "ROLLBACK"],
                         self.engine.statements())
        insert = self.engine.executes()[-2]
        self.assertIs(False, insert["autoCommit"])
        self.assert_all_answered()

    def test_a_failed_begin_stays_owed(self):
        failures = (
            (lambda: self.engine.reply({"success": False, "sessionId": "s1", "newSession": False,
                                        "errorMessage": "SQL compilation error:\nnot now",
                                        "resultSets": []}), errors.ProgrammingError),
            (lambda: self.engine.reply_text("<html><body>502 Bad Gateway</body></html>", 502),
             errors.OperationalError),
            (self.engine.drop, errors.OperationalError),
        )
        for script, error in failures:
            for _ in _SETUP:
                self.engine.reply(_answer("s1", False, _status()))
            conn = self.connect()
            cur = conn.cursor()
            conn.autocommit(False)
            script()
            self.assertRaises(error, cur.execute, "INSERT INTO t VALUES (1)")
            self.engine.reply(_answer("s1", False, _status()))
            self.engine.reply(_answer("s1", False, _number("number of rows inserted", 1)))
            self.engine.reply(_answer("s1", False, _status()))
            cur.execute("INSERT INTO t VALUES (1)")
            conn.commit()
            self.assertEqual(_SETUP + ["BEGIN", "BEGIN", "INSERT INTO t VALUES (1)", "COMMIT"],
                             self.engine.statements(), error.__name__)
            self.assert_all_answered()
            del self.engine.requests[:]


class AutocommitBackOnTest(unittest.TestCase):
    """autocommit(True) leaves the connection committing each statement on its own,
    whatever the COMMIT it sends meets."""

    def setUp(self):
        self.engine = ScriptedEngine()

    def tearDown(self):
        self.engine.stop()

    def connect(self):
        return sc.connect(host="127.0.0.1", port=self.engine.port, network_timeout=5,
                          role="R1", warehouse="W1", database="APP", schema="PUBLIC",
                          session_parameters={"QUERY_TAG": "tagged"})

    def test_a_failed_commit_turning_autocommit_on_leaves_the_next_statement_committing(self):
        ok = _answer("s1", False, _status())
        failures = (
            # A refused COMMIT is followed by a ROLLBACK of the transaction nobody chose.
            (lambda: (self.engine.reply({"success": False, "sessionId": "s1",
                                         "newSession": False,
                                         "errorMessage": "SQL compilation error:\nnot now",
                                         "resultSets": []}), self.engine.reply(ok)),
             errors.ProgrammingError, False, ["ROLLBACK"]),
            # A COMMIT whose answer never came leaves the transaction open, and turning
            # autocommit on commits what is open: the COMMIT goes out once more.
            (lambda: (self.engine.drop(), self.engine.reply(ok)),
             errors.OperationalError, False, ["COMMIT"]),
            (lambda: self.engine.reply(_gone("s1"), status=404), errors.SessionLostError,
             True, []),
        )
        for fail, error, lost, after in failures:
            for _ in _SETUP + ["BEGIN"]:
                self.engine.reply(_answer("s1", False, _status()))
            self.engine.reply(_answer("s1", False, _number("number of rows inserted", 1)))
            conn = self.connect()
            cur = conn.cursor()
            conn.autocommit(False)
            cur.execute("INSERT INTO t VALUES (1)")
            fail()
            self.assertRaises(error, conn.autocommit, True)
            # The next statement commits on its own: no BEGIN ahead of it, autocommit on.
            # After a lost session it runs on a fresh one, behind the setup.
            again = _SETUP if lost else []
            for _ in again:
                self.engine.reply(_answer("s2", False, _status()))
            self.engine.reply(_answer("s2" if lost else "s1", False,
                                      _number("number of rows inserted", 1)))
            try:
                cur.execute("INSERT INTO t VALUES (2)")
            except errors.Error:
                pass  # what went out, checked next, says why
            self.assertEqual(_SETUP + ["BEGIN", "INSERT INTO t VALUES (1)", "COMMIT"] + after
                             + again + ["INSERT INTO t VALUES (2)"],
                             self.engine.statements(), error.__name__)
            self.assertIs(True, self.engine.executes()[-1]["autoCommit"], error.__name__)
            self.assertEqual([], self.engine.unscripted)
            self.assertEqual(0, self.engine.pending)
            del self.engine.requests[:]


class FailedCommitTest(unittest.TestCase):
    """With autocommit off, a commit() or rollback() that failed still leaves the next
    statement inside a transaction the next commit() reaches."""

    def setUp(self):
        self.engine = ScriptedEngine()

    def tearDown(self):
        self.engine.stop()

    def in_transaction(self):
        for _ in _SETUP + ["BEGIN"]:
            self.engine.reply(_answer("s1", False, _status()))
        self.engine.reply(_answer("s1", False, _number("number of rows inserted", 1)))
        conn = sc.connect(host="127.0.0.1", port=self.engine.port, network_timeout=5,
                          role="R1", warehouse="W1", database="APP", schema="PUBLIC",
                          session_parameters={"QUERY_TAG": "tagged"})
        cur = conn.cursor()
        conn.autocommit(False)
        cur.execute("INSERT INTO t VALUES (1)")
        return conn, cur

    def refused(self, message):
        return {"success": False, "sessionId": "s1", "newSession": False,
                "errorMessage": "SQL compilation error:\n" + message, "resultSets": []}

    def test_the_statement_after_a_failed_commit_or_rollback_is_committed_by_the_next_commit(self):
        ok = _answer("s1", False, _status())
        cases = (
            # what ends the transaction, how it fails, what the driver sends after it,
            # whether the next INSERT opens a fresh transaction, and on which session
            ("commit", lambda: (self.engine.reply(self.refused("not now")), self.engine.reply(ok)),
             errors.ProgrammingError, ["ROLLBACK"], True, "s1"),
            ("commit", self.engine.drop, errors.OperationalError, [], False, "s1"),
            ("commit", lambda: self.engine.reply(_gone("s1"), status=404),
             errors.SessionLostError, [], True, "s2"),
            ("rollback", lambda: self.engine.reply(self.refused("no")), errors.ProgrammingError,
             [], True, "s1"),
        )
        for ending, fail, error, after, opens_one, session_id in cases:
            conn, cur = self.in_transaction()
            fail()
            self.assertRaises(error, getattr(conn, ending))
            again = _SETUP if session_id != "s1" else []
            for _ in again + (["BEGIN"] if opens_one else []):
                self.engine.reply(_answer(session_id, False, _status()))
            self.engine.reply(_answer(session_id, False, _number("number of rows inserted", 1)))
            self.engine.reply(_answer(session_id, False, _status()))
            try:
                cur.execute("INSERT INTO t VALUES (2)")
                conn.commit()
            except errors.Error:
                pass  # what went out, checked next, says why
            self.assertEqual(_SETUP + ["BEGIN", "INSERT INTO t VALUES (1)", ending.upper()] + after
                             + again + (["BEGIN"] if opens_one else [])
                             + ["INSERT INTO t VALUES (2)", "COMMIT"],
                             self.engine.statements(), "%s %s" % (ending, error.__name__))
            self.assertEqual([], self.engine.unscripted)
            self.assertEqual(0, self.engine.pending)
            del self.engine.requests[:]


class SessionLifetimeLiveTest(unittest.TestCase):
    """The same against the engine: a session released out of band, and close()."""

    def setUp(self):
        if PORT is None:
            self.skipTest("FROSTLAKE_CLASSPATH not set")
        # Asked over the wire rather than of the client: an engine that answers newSession
        # also takes requireSession and releases sessions on DELETE.
        probe = self.post({"sql": "SELECT 1"})
        if "newSession" not in probe:
            self.skipTest("this engine predates requireSession and session release")
        self.release_out_of_band(probe["sessionId"])

    def base(self):
        return "http://127.0.0.1:%d" % PORT

    def post(self, payload):
        request = urllib.request.Request(self.base() + "/api/execute",
                                         data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def active_sessions(self):
        with urllib.request.urlopen(self.base() + "/api/sessions", timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))["activeSessions"]

    def release_out_of_band(self, session_id):
        request = urllib.request.Request(self.base() + "/api/sessions/" + session_id,
                                         method="DELETE")
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(200, response.status)

    def set_up(self, database):
        admin = sc.connect(host="127.0.0.1", port=PORT)
        cur = admin.cursor()
        cur.execute("CREATE OR REPLACE DATABASE %s" % database)
        cur.execute("CREATE SCHEMA IF NOT EXISTS %s.s2" % database)
        cur.execute("CREATE WAREHOUSE IF NOT EXISTS fac_lost_wh")
        cur.execute("CREATE OR REPLACE TABLE %s.s2.t (a INTEGER)" % database)
        admin.close()
        return sc.connect(host="127.0.0.1", port=PORT, database=database, schema="s2",
                          warehouse="fac_lost_wh", timezone="Asia/Tokyo")

    def test_a_session_released_out_of_band_comes_back_on_the_connect_setup(self):
        conn = self.set_up("fac_lost_db")
        cur = conn.cursor()
        cur.execute("SELECT 1")
        lost = conn.session_id
        self.release_out_of_band(lost)
        cur.execute("SELECT CURRENT_DATABASE() AS d, CURRENT_SCHEMA() AS s, "
                    "CURRENT_WAREHOUSE() AS w")
        self.assertEqual([("FAC_LOST_DB", "S2", "FAC_LOST_WH")], cur.fetchall())
        cur.execute("SHOW PARAMETERS LIKE 'TIMEZONE' IN SESSION")
        self.assertEqual("Asia/Tokyo", cur.fetchone()[1])
        self.assertNotEqual(lost, conn.session_id)
        conn.close()

    def test_a_session_released_with_a_transaction_open_is_reported(self):
        conn = self.set_up("fac_losttx_db")
        conn.autocommit(False)
        cur = conn.cursor()
        cur.execute("INSERT INTO t VALUES (1)")
        self.release_out_of_band(conn.session_id)
        with self.assertRaises(errors.OperationalError) as caught:
            cur.execute("INSERT INTO t VALUES (2)")
        self.assertIsInstance(caught.exception, errors.SessionLostError)
        # The release rolled the first row back, and the second never ran.
        conn.autocommit(True)
        cur.execute("SELECT COUNT(*) FROM t")
        self.assertEqual([(0,)], cur.fetchall())
        conn.close()

    def test_closing_releases_the_session(self):
        conn = sc.connect(host="127.0.0.1", port=PORT)
        conn.cursor().execute("SELECT 1")
        before = self.active_sessions()
        conn.close()
        self.assertEqual(before - 1, self.active_sessions())


class RefusedSetupLiveTest(unittest.TestCase):
    """A USE from connect() that the engine refuses, against the engine."""

    def setUp(self):
        if PORT is None:
            self.skipTest("FROSTLAKE_CLASSPATH not set")

    def test_a_refused_warehouse_keeps_failing_instead_of_falling_through(self):
        admin = sc.connect(host="127.0.0.1", port=PORT)
        admin_cur = admin.cursor()
        admin_cur.execute("CREATE OR REPLACE DATABASE fac_refused_db")
        admin_cur.execute("DROP WAREHOUSE IF EXISTS fac_late_wh")
        conn = sc.connect(host="127.0.0.1", port=PORT, warehouse="fac_late_wh",
                          database="fac_refused_db")
        cur = conn.cursor()
        # The USE WAREHOUSE is refused, and stays in line: nothing runs without it, and the
        # USE DATABASE after it is not skipped over either.
        for sql in ("SELECT 1", "SELECT CURRENT_WAREHOUSE()"):
            with self.assertRaises(errors.ProgrammingError) as caught:
                cur.execute(sql)
            self.assertEqual("USE WAREHOUSE fac_late_wh", caught.exception.query)
        # Once the warehouse exists, the next statement applies the rest of the setup.
        admin_cur.execute("CREATE WAREHOUSE fac_late_wh")
        admin.close()
        cur.execute("SELECT CURRENT_WAREHOUSE() AS w, CURRENT_DATABASE() AS d")
        self.assertEqual([("FAC_LATE_WH", "FAC_REFUSED_DB")], cur.fetchall())
        conn.close()

    def test_with_autocommit_off_a_refused_setup_leaves_the_transaction_owed(self):
        admin = sc.connect(host="127.0.0.1", port=PORT)
        admin_cur = admin.cursor()
        admin_cur.execute("CREATE OR REPLACE DATABASE fac_owed_db")
        conn = sc.connect(host="127.0.0.1", port=PORT, database="fac_owed_db",
                          schema="s_late", autocommit=False)
        cur = conn.cursor()
        # USE SCHEMA is refused while the schema does not exist yet.
        self.assertRaises(errors.ProgrammingError, cur.execute, "SELECT 1")
        admin_cur.execute("CREATE SCHEMA fac_owed_db.s_late")
        admin_cur.execute("CREATE TABLE fac_owed_db.s_late.t (a INTEGER)")
        cur.execute("INSERT INTO t VALUES (1)")
        # The INSERT ran inside the transaction autocommit off owed it, so a second session
        # does not see the row until commit(), and then does.
        admin_cur.execute("SELECT COUNT(*) FROM fac_owed_db.s_late.t")
        self.assertEqual([(0,)], admin_cur.fetchall())
        conn.commit()
        admin_cur.execute("SELECT COUNT(*) FROM fac_owed_db.s_late.t")
        self.assertEqual([(1,)], admin_cur.fetchall())
        conn.close()
        admin.close()


@unittest.skipUnless(os.environ.get("FL_CORPUS"),
                     "set FL_CORPUS to frostlake's engine/src/test/resources/testkit"
                     " to replay the testkit corpus")
class TestkitCorpusTest(unittest.TestCase):
    """The engine's testkit corpus, replayed through the facade by testkit_runner.py."""

    def test_corpus_replays_without_failures(self):
        # Loaded by path: the driver checkout ahead of ROOT on sys.path has a runner of the
        # same name.
        spec = importlib.util.spec_from_file_location("testkit_runner",
                                                      ROOT / "testkit_runner.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        runner.suites_directory()   # an FL_CORPUS without suites fails here, before any engine
        if not (os.environ.get("FROSTLAKE_CLASSPATH") or os.environ.get("FROSTLAKE_URL")):
            self.skipTest("no engine (set FROSTLAKE_CLASSPATH or FROSTLAKE_URL)")
        # The runner prints its tally and first failures, and returns 1 on a failed or
        # errored case.
        self.assertEqual(0, runner.main(["--backend", "connector"]))


if __name__ == "__main__":
    unittest.main(verbosity=1)
