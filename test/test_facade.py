"""Facade tests. Boots a real DatabaseHttpServer from FROSTLAKE_CLASSPATH; the
integration tests skip themselves when the variable is unset.

    python3 test/test_facade.py
"""

import atexit
import os
import pathlib
import socket
import subprocess
import sys
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

    def test_dict_cursor_on_a_statement_with_no_result_set(self):
        # DDL leaves description None; a DictCursor has no names to zip against and
        # must hand back what it got rather than fall over.
        conn, _ = self.bootstrap("fac_dict_ddl_db")
        cur = conn.cursor(DictCursor)
        cur.execute("CREATE OR REPLACE TABLE t (a INTEGER)")
        self.assertIsNone(cur.description)
        self.assertEqual([], cur.fetchall())
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


if __name__ == "__main__":
    unittest.main(verbosity=1)
