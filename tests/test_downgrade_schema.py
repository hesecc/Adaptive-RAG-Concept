"""Rollback support: the schema downgrade must make the PREVIOUS release's writes work again."""

from contextlib import closing
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

from adaptive_rag.app import RagApplication
from adaptive_rag.auth import AccountStore
from adaptive_rag.config import Principal, Settings
from adaptive_rag.domain import IngestRequest


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
spec = importlib.util.spec_from_file_location("rag_downgrade", DEPLOY / "downgrade_schema.py")
downgrade_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(downgrade_module)

# The previous release (1f2df52) writes with these exact positional statements.
PREVIOUS_DOCUMENT = "INSERT OR IGNORE INTO documents VALUES (?,?,?,0)"
PREVIOUS_REVISION = "INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?,?)"
PREVIOUS_ACCOUNT = "INSERT OR IGNORE INTO accounts VALUES (?,?,?,?,?,?,?)"
PREVIOUS_COLUMNS = {
    "documents": ["tenant_id", "document_id", "allowed_subjects", "revoked"],
    "revisions": ["tenant_id", "document_id", "revision_id", "version", "processing_fingerprint",
                  "content_hash", "raw_key", "metadata_json", "payload_hash"],
    "accounts": ["account_id", "username", "tenant_id", "admin", "password_salt", "password_hash", "created_at"],
}


class DowngradeSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name) / "data"
        self.application = RagApplication(Settings(data_dir=self.data))
        generation = None  # publishing is compare-and-swap on the current generation
        for document_id, domains in (("ops-guide", ("operations",)), ("fin-report", ("finance",)), ("legacy", ())):
            result = self.application.ingest(IngestRequest("demo", document_id, "v1", f"# {document_id}\nbody text", data_domains=domains))
            generation = self.application.publish("demo", result.job_id, generation).generation
        AccountStore(self.data, "company").create(
            Principal("company", "admin", admin=True), "alice", "account-password", ("operations", "finance"))

    def run_downgrade(self, check_only=False):
        with redirect_stderr(io.StringIO()) as progress:
            exported = downgrade_module.downgrade(self.data, check_only)
        return exported, progress.getvalue()

    def columns(self, database, table):
        with closing(sqlite3.connect(self.data / database)) as connection:
            return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]

    def test_export_captures_every_classification_before_anything_is_removed(self):
        exported, _ = self.run_downgrade()
        documents = {row["document_id"]: row["data_domains"] for row in exported["documents"]}
        self.assertEqual(documents, {"ops-guide": ["operations"], "fin-report": ["finance"], "legacy": []})
        accounts = {row["username"]: (row["admin"], row["data_domains"]) for row in exported["accounts"]}
        self.assertEqual(accounts["alice"], (False, ["finance", "operations", "public"]))
        self.assertTrue(accounts["ragadmin"][0])

    def test_columns_return_to_exactly_the_previous_shape_and_order(self):
        # A freshly created current database has these columns inside CREATE TABLE, an upgraded one
        # has them appended; either way the previous release's positional INSERT needs this order.
        self.run_downgrade()
        self.assertEqual(self.columns("knowledge.sqlite3", "documents"), PREVIOUS_COLUMNS["documents"])
        self.assertEqual(self.columns("knowledge.sqlite3", "revisions"), PREVIOUS_COLUMNS["revisions"])
        self.assertEqual(self.columns("accounts.sqlite3", "accounts"), PREVIOUS_COLUMNS["accounts"])

    def test_previous_release_writes_succeed_and_land_in_the_right_columns(self):
        before = self.columns("knowledge.sqlite3", "documents")
        self.assertIn("data_domains", before)
        with closing(sqlite3.connect(self.data / "knowledge.sqlite3")) as connection:
            with self.assertRaisesRegex(sqlite3.OperationalError, "values"):
                connection.execute(PREVIOUS_DOCUMENT, ("demo", "x", "[]"))
        with closing(sqlite3.connect(self.data / "accounts.sqlite3")) as connection:
            with self.assertRaisesRegex(sqlite3.OperationalError, "values"):
                connection.execute(PREVIOUS_ACCOUNT, ("id-1", "bob", "company", 0, "salt", "hash", "2026-01-01"))
        self.run_downgrade()
        with closing(sqlite3.connect(self.data / "knowledge.sqlite3")) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                connection.execute(PREVIOUS_DOCUMENT, ("demo", "rollback-doc", '["alice"]'))
                connection.execute(PREVIOUS_REVISION, ("demo", "rollback-doc", "rev-1", "v9", "fp", "ch", "a" * 64, '{"k":1}', "ph"))
            self.assertEqual(connection.execute(
                "SELECT allowed_subjects,revoked FROM documents WHERE document_id='rollback-doc'").fetchone(), ('["alice"]', 0))
            self.assertEqual(connection.execute(
                "SELECT version,processing_fingerprint,content_hash,raw_key,metadata_json,payload_hash FROM revisions "
                "WHERE revision_id='rev-1'").fetchone(), ("v9", "fp", "ch", "a" * 64, '{"k":1}', "ph"))
        with closing(sqlite3.connect(self.data / "accounts.sqlite3")) as connection:
            with connection:
                connection.execute(PREVIOUS_ACCOUNT, ("id-1", "bob", "company", 0, "salt", "hash", "2026-01-01"))
            self.assertEqual(connection.execute("SELECT username,tenant_id,admin,password_salt,password_hash,created_at "
                                                "FROM accounts WHERE username='bob'").fetchone(),
                             ("bob", "company", 0, "salt", "hash", "2026-01-01"))

    def test_existing_data_survives_and_the_current_release_can_upgrade_again(self):
        before = {}
        with closing(sqlite3.connect(self.data / "knowledge.sqlite3")) as connection:
            before["documents"] = connection.execute("SELECT tenant_id,document_id,allowed_subjects,revoked FROM documents ORDER BY 2").fetchall()
            before["revisions"] = connection.execute("SELECT revision_id,payload_hash FROM revisions ORDER BY 1").fetchall()
        self.run_downgrade()
        with closing(sqlite3.connect(self.data / "knowledge.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT tenant_id,document_id,allowed_subjects,revoked FROM documents ORDER BY 2").fetchall(), before["documents"])
            self.assertEqual(connection.execute("SELECT revision_id,payload_hash FROM revisions ORDER BY 1").fetchall(), before["revisions"])
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
        # Upgrading again re-adds the columns fail-closed: documents unclassified, accounts public-only.
        upgraded = RagApplication(Settings(data_dir=self.data))
        with closing(sqlite3.connect(self.data / "knowledge.sqlite3")) as connection:
            self.assertEqual({r[0] for r in connection.execute("SELECT data_domains FROM documents")}, {"[]"})
        accounts = AccountStore(self.data, "company")
        self.assertEqual(accounts.login("alice", "account-password")[1].principal.data_domains, ("public",))
        self.assertIsNone(accounts.login("ragadmin", "ragadmin123")[1].principal.data_domains)
        self.assertIsNotNone(upgraded)

    def test_second_run_is_a_no_op_and_reports_it(self):
        self.run_downgrade()
        exported, progress = self.run_downgrade()
        self.assertEqual(exported, {"documents": [], "accounts": []})
        self.assertEqual(progress.count("none (already previous shape)"), 2)

    def test_check_mode_changes_nothing(self):
        before = (self.data / "knowledge.sqlite3").read_bytes(), (self.data / "accounts.sqlite3").read_bytes()
        exported, progress = self.run_downgrade(check_only=True)
        self.assertEqual(len(exported["documents"]), 3)
        self.assertIn("columns to drop", progress)
        self.assertIn("data_domains", self.columns("knowledge.sqlite3", "documents"))
        self.assertIn("data_domains", self.columns("accounts.sqlite3", "accounts"))
        self.assertEqual(before, ((self.data / "knowledge.sqlite3").read_bytes(), (self.data / "accounts.sqlite3").read_bytes()))

    def test_command_line_prints_only_the_export_on_stdout(self):
        result = subprocess.run([sys.executable, str(DEPLOY / "downgrade_schema.py"), str(self.data), "--check"],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(json.loads(result.stdout)), ["accounts", "documents"])
        self.assertIn("columns to drop", result.stderr)
        bad = subprocess.run([sys.executable, str(DEPLOY / "downgrade_schema.py")], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(bad.returncode, 0)
        unknown = subprocess.run([sys.executable, str(DEPLOY / "downgrade_schema.py"), str(self.data), "--force"],
                                 capture_output=True, text=True, timeout=60)
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn("data_domains", self.columns("accounts.sqlite3", "accounts"))

    def test_missing_database_is_skipped_and_symlink_is_never_followed(self):
        empty = Path(self.temporary.name) / "empty"
        empty.mkdir()
        with redirect_stderr(io.StringIO()) as progress:
            exported = downgrade_module.downgrade(empty)
        self.assertEqual(exported, {"documents": [], "accounts": []})
        self.assertEqual(progress.getvalue().count("not present, skipped"), 2)
        linked = Path(self.temporary.name) / "linked"
        linked.mkdir()
        try:
            (linked / "knowledge.sqlite3").symlink_to(self.data / "knowledge.sqlite3")
        except OSError:
            self.skipTest("symlinks need privileges on this platform")
        with redirect_stderr(io.StringIO()):
            downgrade_module.downgrade(linked)
        self.assertIn("data_domains", self.columns("knowledge.sqlite3", "documents"))


if __name__ == "__main__":
    unittest.main()
