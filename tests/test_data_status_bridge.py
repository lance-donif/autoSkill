"""Bound status reads use the real service and its live WAL without probing caller write access."""
import base64
import contextlib
import io
import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

from test_bridge import Sandbox

from autoskill import cli, data_runtime, data_store, db, paths, runtime_identity


class BoundDataStatusTests(Sandbox):
    def token(self, port):
        value = {"version": 1, "workspace": str(self.workspace), "port": port, "host": "dsh"}
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    def invoke(self, *arguments, **environment):
        output, error = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"AUTOSKILL_BRIDGE": "auto", **environment}), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = cli.main(list(arguments))
        return code, json.loads(output.getvalue()), error.getvalue()

    def test_bound_status_reads_service_wal_without_opening_the_callers_database(self):
        port = self.start_service()
        database = self.workspace / ".local/autoskill.db"
        with contextlib.closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            identity = json.dumps(runtime_identity.current())
            connection.execute("INSERT INTO runs(id,project_id,engine,status,created_at,pid,owner_identity) VALUES(?,?,?,?,?,?,?)",
                               ("wal-live", "fixture", "playwright", "Running", db.now(), os.getpid(), identity))
            connection.commit()
            self.assertGreater(Path(database.as_posix() + "-wal").stat().st_size, 0)
            blocked = sqlite3.OperationalError("unable to open database file")
            with patch.object(data_runtime, "activities", side_effect=blocked), \
                    patch.object(paths, "restricted", side_effect=AssertionError("A bound status read must not create write probes")):
                code, result, error = self.invoke("--runtime-context=" + self.token(port), "data", "status")
            self.assertEqual(code, 0, (result, error))
            self.assertEqual(result["root"], str(self.workspace))
            self.assertTrue(any(item.get("id") == "wal-live" for item in result["activity"]), result)
            connection.execute("UPDATE runs SET status='Cancelled',pid=NULL WHERE id='wal-live'")
            connection.commit()

    def test_environment_bound_status_also_uses_the_service(self):
        port = self.start_service()
        with patch.object(data_store, "status", side_effect=AssertionError("Caller must not read an external runtime database")):
            code, result, error = self.invoke("data", "status", AUTOSKILL_RUNTIME_CONTEXT=self.token(port))
        self.assertEqual(code, 0, (result, error))
        self.assertEqual(result["root"], str(self.workspace))

    def test_unbound_status_stays_read_only_and_does_not_create_a_data_root(self):
        self.assertFalse(self.workspace.exists())
        with patch.object(cli, "delegate", side_effect=AssertionError("An unbound status read stays local")), \
                patch.object(paths, "restricted", side_effect=AssertionError("Status must not create write probes")):
            code, result, error = self.invoke("data", "status", AUTOSKILL_RUNTIME_CONTEXT="")
        self.assertEqual(code, 0, (result, error))
        self.assertFalse(result["initialized"])
        self.assertFalse(self.workspace.exists())

    def test_explicit_bridge_off_keeps_the_local_status_contract(self):
        with patch.object(cli, "delegate", side_effect=AssertionError("Explicit bridge off must remain local")):
            code, result, error = self.invoke("--runtime-context=" + self.token(54321), "data", "status", AUTOSKILL_BRIDGE="off")
        self.assertEqual(code, 0, (result, error))
        self.assertFalse(result["initialized"])
        self.assertFalse(self.workspace.exists())
