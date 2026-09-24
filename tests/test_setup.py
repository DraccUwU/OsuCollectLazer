"""First-run setup: settings migration, the wizard's state, and helper installation.

Runs as a plain script (`python tests/test_setup.py`) like the other suites here.
Everything is local and side-effect free: settings point at temp files, downloads come
from a faked GitHub, and nothing touches lazer's own data.
"""
from __future__ import annotations

import http.server
import io
import json
import socket
import sys
import threading
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app import config, launcher, lazerdb, server, setup, version  # noqa: E402


class FakeResponse:
    """Minimal stand-in for urllib's response object (context manager + read)."""

    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def release_json(*names: str, tag: str = "v1.2.3") -> bytes:
    return json.dumps(
        {
            "tag_name": tag,
            "assets": [
                {"name": name, "browser_download_url": f"https://example.invalid/{name}"} for name in names
            ],
        }
    ).encode("utf-8")


def helper_zip(entries: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    entries = entries or {"LazerDb.exe": b"MZ fake helper"}
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, blob in entries.items():
            archive.writestr(name, blob)
    return buffer.getvalue()


class VersionTests(unittest.TestCase):
    def test_the_release_workflow_publishes_what_the_wizard_downloads(self) -> None:
        workflow = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn(version.HELPER_ASSET, workflow)
        self.assertIn(version.APP_ASSET, workflow)
        self.assertIn(version.__version__, (REPO / "app" / "version.py").read_text(encoding="utf-8"))

    def test_the_api_url_follows_the_repo_constant(self) -> None:
        self.assertIn(version.REPO, version.LATEST_RELEASE_API)
        self.assertTrue(version.LATEST_RELEASE_API.startswith("https://"))


class SettingsMigrationTests(unittest.TestCase):
    """An install that predates the wizard must never be sent back to it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "settings.json"
        self._saved = config.SETTINGS_PATH
        config.SETTINGS_PATH = self.path

    def tearDown(self) -> None:
        config.SETTINGS_PATH = self._saved
        self._tmp.cleanup()

    def test_a_brand_new_install_has_setup_pending(self) -> None:
        self.assertFalse(self.path.exists())
        self.assertFalse(config.load_settings()["setup_complete"])

    def test_an_existing_settings_file_counts_as_set_up(self) -> None:
        self.path.write_text(json.dumps({"concurrency": 7}), encoding="utf-8")
        settings = config.load_settings()
        self.assertTrue(settings["setup_complete"])
        self.assertEqual(settings["concurrency"], 7)

    def test_saving_settings_does_not_resurrect_the_wizard(self) -> None:
        self.path.write_text(json.dumps({"concurrency": 7}), encoding="utf-8")
        config.save_settings({"concurrency": 9})
        self.assertTrue(json.loads(self.path.read_text(encoding="utf-8"))["setup_complete"])
        self.assertTrue(config.load_settings()["setup_complete"])

    def test_an_explicit_flag_wins(self) -> None:
        self.path.write_text(json.dumps({"setup_complete": False}), encoding="utf-8")
        self.assertFalse(config.load_settings()["setup_complete"])


class SetupStateTests(unittest.TestCase):
    def test_state_has_everything_the_wizard_renders(self) -> None:
        state = setup.state()
        for key in ("version", "setup_complete", "lazer", "helper", "helper_asset", "releases_url", "dotnet", "settings"):
            self.assertIn(key, state)
        self.assertEqual(state["version"], version.__version__)
        self.assertEqual(state["helper_asset"], version.HELPER_ASSET)
        self.assertIn("install_dir", state["helper"])
        self.assertIn("can_build", state["dotnet"])

    def test_a_missing_helper_is_reported_rather_than_raised(self) -> None:
        with mock.patch.object(lazerdb, "helper_path", return_value=None):
            helper = setup.state()["helper"]
        self.assertFalse(helper["available"])
        self.assertIn("not installed", helper["reason"])


class HelperInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name) / "LazerDb"
        self._saved = lazerdb.helper_install_dir
        lazerdb.helper_install_dir = lambda: self.target  # type: ignore[assignment]

    def tearDown(self) -> None:
        lazerdb.helper_install_dir = self._saved  # type: ignore[assignment]
        self._tmp.cleanup()

    def _fake_urlopen(self, payloads: list[FakeResponse]):
        calls = []

        def fake(request, timeout=None):
            calls.append(request.full_url)
            return payloads[len(calls) - 1]

        return fake, calls

    def test_the_published_helper_is_downloaded_and_unpacked(self) -> None:
        fake, calls = self._fake_urlopen([FakeResponse(release_json(version.HELPER_ASSET)), FakeResponse(helper_zip())])
        with mock.patch.object(setup.urllib.request, "urlopen", fake):
            result = setup.download_helper()
        self.assertEqual(result["method"], "download")
        self.assertEqual(result["tag"], "v1.2.3")
        self.assertTrue((self.target / "LazerDb.exe").is_file())
        self.assertEqual((self.target / "LazerDb.exe").read_bytes(), b"MZ fake helper")
        self.assertIn(version.LATEST_RELEASE_API, calls[0])
        self.assertTrue(calls[1].endswith(version.HELPER_ASSET))

    def test_a_release_without_the_asset_says_so(self) -> None:
        fake, _ = self._fake_urlopen([FakeResponse(release_json(version.APP_ASSET))])
        with mock.patch.object(setup.urllib.request, "urlopen", fake):
            with self.assertRaises(RuntimeError) as caught:
                setup.download_helper()
        self.assertIn(version.HELPER_ASSET, str(caught.exception))

    def test_no_release_at_all_points_at_building_instead(self) -> None:
        import urllib.error

        def fake(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

        with mock.patch.object(setup.urllib.request, "urlopen", fake):
            with self.assertRaises(RuntimeError) as caught:
                setup.download_helper()
        self.assertIn("no published release", str(caught.exception))

    def test_a_zip_without_the_helper_is_refused(self) -> None:
        fake, _ = self._fake_urlopen([FakeResponse(release_json(version.HELPER_ASSET)), FakeResponse(helper_zip({"readme.txt": b"hi"}))])
        with mock.patch.object(setup.urllib.request, "urlopen", fake):
            with self.assertRaises(RuntimeError) as caught:
                setup.download_helper()
        self.assertIn("LazerDb.exe", str(caught.exception))
        self.assertFalse(self.target.exists())

    def test_auto_falls_back_to_building_when_the_download_fails(self) -> None:
        built = {"method": "build", "path": "x", "log": ["dotnet build"]}
        with mock.patch.object(setup, "download_helper", side_effect=RuntimeError("no internet")), mock.patch.object(
            setup, "build_helper", return_value=dict(built)
        ):
            result = setup.install_helper("auto")
        self.assertEqual(result["method"], "build")
        self.assertTrue(any("no internet" in line for line in result["log"]))
        self.assertIn("status", result)

    def test_an_unknown_method_is_a_value_error(self) -> None:
        with self.assertRaises(ValueError):
            setup.install_helper("magic")


class FinishTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._saved = config.SETTINGS_PATH
        config.SETTINGS_PATH = self.root / "settings.json"

    def tearDown(self) -> None:
        config.SETTINGS_PATH = self._saved
        self._tmp.cleanup()

    def test_finishing_saves_the_choices_and_creates_the_folder(self) -> None:
        target = self.root / "downloads" / "here"
        saved = setup.apply_settings({"download_dir": str(target), "no_video": False, "nonsense": True})
        self.assertTrue(saved["setup_complete"])
        self.assertEqual(saved["no_video"], False)
        self.assertNotIn("nonsense", saved)
        self.assertTrue(target.is_dir(), "the wizard creates the folder it was given")
        on_disk = json.loads(config.SETTINGS_PATH.read_text(encoding="utf-8"))
        self.assertTrue(on_disk["setup_complete"])
        self.assertEqual(Path(on_disk["download_dir"]), target)

    def test_finishing_twice_is_harmless(self) -> None:
        setup.apply_settings({"no_video": True})
        again = setup.apply_settings({"no_video": True})
        self.assertTrue(again["setup_complete"])


class SetupRouteTests(unittest.TestCase):
    """The wizard talks to these three routes; they must stay strict but friendly."""

    def setUp(self) -> None:
        from app import server

        self.server = server

    def test_the_state_route_answers(self) -> None:
        code, payload = self.server.api_get("/api/setup", {})
        self.assertEqual(code, 200)
        self.assertIn("helper", payload)
        self.assertIn("lazer", payload)

    def test_a_bad_picker_kind_is_refused(self) -> None:
        code, payload = self.server.api_post("/api/setup/pick", {"kind": "elevator"})
        self.assertEqual(code, 400)
        self.assertIn("folder", payload["error"])

    def test_an_unknown_helper_method_is_refused(self) -> None:
        code, payload = self.server.api_post("/api/setup/helper", {"method": "wish"})
        self.assertEqual(code, 400)
        self.assertIn("wish", payload["error"])

    def test_a_failed_install_reports_409_not_a_traceback(self) -> None:
        with mock.patch.object(self.server.setup, "install_helper", side_effect=RuntimeError("no helper for you")):
            code, payload = self.server.api_post("/api/setup/helper", {"method": "download"})
        self.assertEqual(code, 409)
        self.assertEqual(payload["error"], "no helper for you")

    def test_status_reports_the_version_and_the_setup_flag(self) -> None:
        code, payload = self.server.api_get("/api/status", {})
        self.assertEqual(code, 200)
        self.assertEqual(payload["version"], version.__version__)
        self.assertIn("setup_complete", payload)


class LauncherTests(unittest.TestCase):
    """The exe's entry point: the port it probes has to be the port it would use."""

    def test_cli_port_reads_both_spellings_and_ignores_junk(self):
        self.assertEqual(launcher._cli_port(["--no-browser", "--port", "9001"]), 9001)
        self.assertEqual(launcher._cli_port(["--port=9002"]), 9002)
        self.assertIsNone(launcher._cli_port(["--no-browser"]))
        self.assertIsNone(launcher._cli_port(["--port", "not-a-port"]))
        self.assertIsNone(launcher._cli_port(["--port"]))

    def _free_port(self) -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def test_the_probe_recognises_a_running_copy_and_nothing_else(self):
        """_running_url answers only on a real OsuCollectLazer /api/status."""
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertEqual(launcher._running_url(port), f"http://127.0.0.1:{port}/")
            self.assertIsNone(launcher._running_url(self._free_port()))
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
