"""Regression tests for destructive API and lazer database write guards.

Everything here is deliberately local and side-effect free: the download root is a
temporary directory, the import helper is faked, and no osu!lazer is touched.

Run:  python tests/test_server_safety.py
"""
from __future__ import annotations

import io
import os
import sys
import time
import unittest
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import server

REPO = Path(__file__).resolve().parent.parent


def _clear_jobs() -> None:
    with server.JOBS._lock:
        server.JOBS._jobs.clear()


def wait_for_job(job_id: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = server.JOBS.get(job_id) or {}
        if job.get("status") not in ("pending", "running"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s: {server.JOBS.get(job_id)}")


class RequestBodyTests(unittest.TestCase):
    """A request body must never be silently turned into an empty dict."""

    @staticmethod
    def make_handler(body: bytes, content_type: str = "application/json") -> server.Handler:
        handler = object.__new__(server.Handler)
        handler.headers = Message()
        handler.headers["Content-Length"] = str(len(body))
        handler.headers["Content-Type"] = content_type
        handler.rfile = io.BytesIO(body)
        return handler

    def test_empty_post_body_is_rejected(self) -> None:
        handler = self.make_handler(b"")
        with self.assertRaisesRegex(ValueError, "required"):
            handler._body()

    def test_malformed_json_is_rejected(self) -> None:
        handler = self.make_handler(b"{not json}")
        with self.assertRaisesRegex(ValueError, "valid JSON"):
            handler._body()

    def test_non_json_content_type_is_rejected(self) -> None:
        handler = self.make_handler(b'{"scope":"all"}', "text/plain")
        with self.assertRaisesRegex(ValueError, "application/json"):
            handler._body()

    def test_json_array_is_rejected(self) -> None:
        handler = self.make_handler(b"[]")
        with self.assertRaisesRegex(ValueError, "JSON object"):
            handler._body()

    def test_a_valid_object_comes_through(self) -> None:
        handler = self.make_handler(b'{"scope": "all"}')
        self.assertEqual(handler._body(), {"scope": "all"})


class OriginTests(unittest.TestCase):
    """Cross-origin POSTs at a loopback server are refused; curl keeps working."""

    def test_posts_without_an_origin_are_allowed(self) -> None:
        self.assertTrue(server._origin_allowed(None, "127.0.0.1:8765", 8765))

    def test_same_origin_is_allowed(self) -> None:
        self.assertTrue(server._origin_allowed("http://127.0.0.1:8765", "127.0.0.1:8765", 8765))
        self.assertTrue(server._origin_allowed("http://localhost:8765", "127.0.0.1:8765", 8765))

    def test_foreign_and_null_origins_are_refused(self) -> None:
        for origin in ("http://evil.example", "https://osucollector.com", "null", "http://127.0.0.1:9999"):
            with self.subTest(origin=origin):
                self.assertFalse(server._origin_allowed(origin, "127.0.0.1:8765", 8765))

    def test_a_rebound_page_cannot_borrow_its_own_hostname(self) -> None:
        """A DNS-rebound page is same-origin in the browser and brings its own name in
        *both* headers — matching Origin against Host must not be enough."""
        self.assertFalse(server._origin_allowed("http://evil.example:8765", "evil.example:8765", 8765))

    def test_a_non_loopback_host_header_is_refused(self) -> None:
        self.assertFalse(server._origin_allowed(None, "evil.example:8765", 8765))

    def test_a_loopback_host_header_is_case_insensitive(self) -> None:
        self.assertTrue(server._origin_allowed(None, "LOCALHOST:8765", 8765))
        self.assertTrue(server._origin_allowed(None, "127.0.0.1:8765", 8765))


class DeleteApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name) / "collections"
        self.root.mkdir(parents=True)
        self._settings = patch.dict(server.SETTINGS, {"download_dir": str(self.root)})
        self._settings.start()
        self.addCleanup(self._settings.stop)
        self.addCleanup(_clear_jobs)
        self.addCleanup(self._tmp.cleanup)

    def _folder(self, name: str) -> Path:
        folder = self.root / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "1.osz").write_bytes(b"PK\x03\x04" + b"x" * 64)
        return folder

    def test_delete_requires_an_explicit_scope(self) -> None:
        code, payload = server.api_post("/api/library/delete", {"dry_run": True})
        self.assertEqual(code, 400)
        self.assertIn("scope", payload["error"])

    def test_dry_run_reports_without_deleting(self) -> None:
        folder = self._folder("coll-1")
        code, payload = server.api_post("/api/library/delete", {"scope": "all", "dry_run": True})
        self.assertEqual((code, payload["removed"]), (200, 1))
        self.assertTrue(folder.exists())
        self.assertTrue((folder / "1.osz").exists())

    def test_dry_run_reports_the_bytes_a_real_delete_would_reclaim(self) -> None:
        folder = self._folder("coll-1")
        stray = self.root / "stray.bin"
        stray.write_bytes(b"y" * 512)
        expected = (folder / "1.osz").stat().st_size + 512
        code, payload = server.api_post("/api/library/delete", {"scope": "all", "dry_run": True})
        self.assertEqual(code, 200)
        self.assertEqual(payload["freed"], expected)

    def test_a_file_inside_the_root_is_not_a_collection_folder(self) -> None:
        stray = self.root / "stray.bin"
        stray.write_bytes(b"x")
        code, payload = server.api_post("/api/library/delete", {"scope": "all", "folder": str(stray)})
        self.assertEqual(code, 400)
        self.assertIn("collection folder", payload["error"])
        self.assertTrue(stray.exists(), "refused requests must not delete anything")

    def test_a_collection_folder_that_is_already_gone_is_a_no_op(self) -> None:
        code, payload = server.api_post("/api/library/delete", {"scope": "all", "folder": str(self.root / "nope")})
        self.assertEqual((code, payload["removed"]), (200, 0))

    def test_the_download_root_itself_is_not_a_collection_folder(self) -> None:
        code, payload = server.api_post("/api/library/delete", {"scope": "all", "folder": str(self.root)})
        self.assertEqual(code, 403)
        self.assertIn("collection folder", payload["error"])

    def test_a_running_job_blocks_deleting_that_collection(self) -> None:
        folder = self._folder("coll-1")
        server.JOBS.new("download", folder=str(folder))
        code, payload = server.api_post("/api/library/delete", {"scope": "all", "folder": str(folder)})
        self.assertEqual(code, 409)
        self.assertIn("still running", payload["error"])
        self.assertTrue(folder.exists())

    def test_deleting_one_collection_leaves_the_root_and_siblings_alone(self) -> None:
        first, second = self._folder("coll-1"), self._folder("coll-2")
        stray = self.root / "stray.txt"
        stray.write_bytes(b"x")

        code, payload = server.api_post("/api/library/delete", {"scope": "all", "folder": str(first)})
        self.assertEqual((code, payload["removed"]), (200, 1))
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertTrue(stray.exists(), "a single-collection delete must not sweep the root")

        code, payload = server.api_post("/api/library/delete", {"scope": "all"})
        self.assertEqual(code, 200)
        self.assertFalse(second.exists())
        self.assertFalse(stray.exists())

    def test_the_write_route_also_refuses_the_root_itself(self) -> None:
        code, _ = server.api_post("/api/collection/write", {"folder": str(self.root)})
        self.assertEqual(code, 403)


class HostGuardTests(unittest.TestCase):
    """Reads are loopback-only as well: a rebound page must not even read local metadata."""

    def test_loopback_hosts_are_served(self) -> None:
        for host in (None, "", "127.0.0.1:8765", "localhost:8765", "LOCALHOST:8765", "[::1]:8765"):
            with self.subTest(host=host):
                self.assertTrue(server._host_allowed(host))

    def test_everything_else_is_refused(self) -> None:
        for host in ("evil.example:8765", "127.0.0.1.evil.example:8765", "192.168.1.5:8765", "2130706433:8765"):
            with self.subTest(host=host):
                self.assertFalse(server._host_allowed(host))


class StaticFileTests(unittest.TestCase):
    """The static route serves the UI and nothing else — no path escapes."""

    def test_files_inside_the_web_directory_are_served(self) -> None:
        self.assertIsNotNone(server._static_file("index.html"))
        self.assertIsNotNone(server._static_file("app.js"))
        self.assertIsNotNone(server._static_file("style.css"))

    def test_escapes_are_refused(self) -> None:
        for rel in (
            "../README.md",
            "..\\..\\README.md",
            "../../../../Windows/win.ini",
            "web/../README.md",
            "C:/Windows/win.ini",
            "//127.0.0.1/share/web.js",
            "",
        ):
            with self.subTest(rel=rel):
                self.assertIsNone(server._static_file(rel))


class FinalizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name) / "collections"
        self.root.mkdir(parents=True)
        self._settings = patch.dict(
            server.SETTINGS, {"download_dir": str(self.root), "collection_mode": "database"}
        )
        self._settings.start()
        self.addCleanup(self._settings.stop)
        self.addCleanup(_clear_jobs)
        self.addCleanup(self._tmp.cleanup)

    def test_a_failed_archive_is_kept_even_when_paths_have_different_shapes(self) -> None:
        """The helper reports resolved absolute paths; a relative download folder must
        not stop the app recognising a failed archive (it used to delete it anyway)."""
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            folder = Path("coll-1")
            folder.mkdir()
            taken, failed = folder / "1.osz", folder / "2.osz"
            for f in (taken, failed):
                f.write_bytes(b"PK\x03\x04" + b"x" * 2048)
            # the pipeline runs while the cwd is this temp root (that is the point),
            # but the assertions below run after it is restored — track absolute paths
            taken_abs, failed_abs = taken.resolve(), failed.resolve()

            def fake_import(paths, **kwargs):
                return {
                    "requested": 2,
                    "imported": 1,
                    "failed": 1,
                    "seconds": 0.1,
                    "errors": ["2.osz: ArgumentException: boom"],
                    "failed_files": [str(failed.resolve())],
                    "sets_before": 1,
                    "sets_after": 2,
                }

            with patch.object(server.lazerdb, "import_beatmaps", fake_import), patch.object(
                server.lazer, "is_running", lambda: False
            ):
                job = server.start_finalize(str(folder))
                done = wait_for_job(job["id"])
        finally:
            os.chdir(cwd)

        self.assertIn(done["status"], ("done", "partial"), done)
        self.assertTrue(failed_abs.exists(), "an archive that failed to import must stay on disk")
        self.assertFalse(taken_abs.exists(), "an archive the importer took must not stay on disk")
        self.assertTrue(any("kept 2.osz" in line for line in done["log"]), done["log"])

    def test_writing_the_collection_closes_the_game_first(self) -> None:
        folder = self.root / "coll-write"
        folder.mkdir()
        (folder / "collection.db").write_bytes(b"db")
        events: list[str] = []

        def fake_close(*args, **kwargs):
            events.append("close")
            return True, "closed"

        def fake_write(db_path, *args, **kwargs):
            events.append("write")
            return {"changed": {"c": 1}, "backup": None, "unchanged": False}

        with patch.object(server.lazer, "is_running", lambda: True), patch.object(
            server.lazer, "close_lazer", fake_close
        ), patch.object(server.lazerdb, "write_collection", fake_write):
            job = server.start_finalize(str(folder))
            done = wait_for_job(job["id"])

        self.assertEqual(events, ["close", "write"], done)
        self.assertIn(done["status"], ("done", "partial"), done)

    def test_collection_write_is_refused_while_the_game_runs_and_autoclose_is_off(self) -> None:
        folder = self.root / "coll-write"
        folder.mkdir()
        (folder / "collection.db").write_bytes(b"db")
        calls: list[str] = []

        def fake_write(*args, **kwargs):
            calls.append("write")
            return {}

        with patch.dict(server.SETTINGS, {"close_lazer_before_import": False}), patch.object(
            server.lazer, "is_running", lambda: True
        ), patch.object(server.lazerdb, "write_collection", fake_write):
            job = server.start_finalize(str(folder))
            done = wait_for_job(job["id"])

        self.assertEqual(calls, [], "the realm must not be written while the game is running")
        self.assertEqual(done["status"], "partial", done)
        self.assertTrue(any("turned off" in line for line in done["log"]), done["log"])


class WriteRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name) / "collections"
        self.folder = self.root / "coll-1"
        self.folder.mkdir(parents=True)
        (self.folder / "collection.db").write_bytes(b"db")
        self._settings = patch.dict(server.SETTINGS, {"download_dir": str(self.root)})
        self._settings.start()
        self.addCleanup(self._settings.stop)
        self.addCleanup(_clear_jobs)
        self.addCleanup(self._tmp.cleanup)

    def test_route_refuses_while_the_game_runs_and_autoclose_is_off(self) -> None:
        calls: list[str] = []

        def fake_write(*args, **kwargs):
            calls.append("write")
            return {}

        with patch.dict(server.SETTINGS, {"close_lazer_before_import": False}), patch.object(
            server.lazer, "is_running", lambda: True
        ), patch.object(server.lazerdb, "write_collection", fake_write):
            code, payload = server.api_post("/api/collection/write", {"folder": str(self.folder)})

        self.assertEqual(code, 409, payload)
        self.assertEqual(calls, [])

    def test_route_closes_the_game_and_writes_when_allowed(self) -> None:
        events: list[str] = []

        def fake_close(*args, **kwargs):
            events.append("close")
            return True, "closed"

        def fake_write(*args, **kwargs):
            events.append("write")
            return {"changed": {"c": 1}, "backup": None}

        with patch.object(server.lazer, "is_running", lambda: True), patch.object(
            server.lazer, "close_lazer", fake_close
        ), patch.object(server.lazerdb, "write_collection", fake_write):
            code, payload = server.api_post("/api/collection/write", {"folder": str(self.folder)})

        self.assertEqual(code, 200, payload)
        self.assertEqual(events, ["close", "write"])

    def test_route_writes_without_touching_a_closed_game(self) -> None:
        events: list[str] = []

        def fake_close(*args, **kwargs):
            events.append("close")
            return True, "closed"

        def fake_write(*args, **kwargs):
            events.append("write")
            return {"changed": {"c": 1}, "backup": None}

        with patch.object(server.lazer, "is_running", lambda: False), patch.object(
            server.lazer, "close_lazer", fake_close
        ), patch.object(server.lazerdb, "write_collection", fake_write):
            code, _ = server.api_post("/api/collection/write", {"folder": str(self.folder)})

        self.assertEqual(code, 200)
        self.assertEqual(events, ["write"])


class UiTextTests(unittest.TestCase):
    """The UI must describe what the app actually does (direct import, game closed)."""

    def test_the_ui_does_not_promise_in_game_steps(self) -> None:
        js = (REPO / "app" / "web" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("import screen", js)
        self.assertNotIn("land live", js)
        self.assertIn("closed automatically", js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
