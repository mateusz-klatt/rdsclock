"""Browser dashboard (``recon --web``): HTTP server, defences and CLI wiring."""

import http.client
import json
import socket
import sys
import threading
import time

import pytest

import rdsclock.cli as cli
import rdsclock.web as web
from tests.fake_receiver import MARKUP_PS, receiver_command

RESCAN = {"X-Requested-By": "rdsclock"}


class _Running:
    """A WebDashboard serving on an ephemeral loopback port from a thread."""

    def __init__(self, command: list[str]) -> None:
        self.dashboard = web.WebDashboard(command, host="127.0.0.1", port=0)
        self.port = self.dashboard.server.server_address[1]
        self.thread = threading.Thread(target=self.dashboard.serve_forever, daemon=True)

    def __enter__(self) -> "_Running":
        self.dashboard.start()
        self.thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.dashboard.server.shutdown()
        self.dashboard.close()
        self.thread.join(5)

    def request(self, method: str, path: str, headers: dict[str, str] | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(
                method, path, headers={"Host": f"127.0.0.1:{self.port}", **(headers or {})}
            )
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def state(self) -> dict:
        status, _, body = self.request("GET", "/api/state")
        assert status == 200
        return json.loads(body)

    def wait_for(self, condition, timeout_s: float = 15.0) -> None:
        deadline = time.monotonic() + timeout_s
        while not condition(self.state()):
            assert time.monotonic() < deadline, "the dashboard did not reach the expected state"
            time.sleep(0.05)


@pytest.fixture
def running(tmp_path):
    with _Running(receiver_command(tmp_path)) as server:
        yield server


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("127.0.0.1", True),
        ("127.8.9.1", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("localhost", True),
        ("192.168.5.1", False),
        ("::", False),
        ("rdsclock.local", False),
    ],
)
def test_is_loopback(address, expected):
    assert web.is_loopback(address) is expected


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1:8080", True),
        ("[::1]:8080", True),
        ("LOCALHOST:8080", True),
        ("192.168.5.1:8080", True),
        ("rdsclock.local:8080", True),
        ("attacker.example", False),
        ("", False),
        (None, False),
    ],
)
def test_host_allowed(host, expected):
    assert web.host_allowed(host, "rdsclock.local") is expected


class TestWebDashboard:
    def test_page_and_assets_carry_strict_headers(self, running):
        for path, kind in (
            ("/", "text/html"),
            ("/app.js", "text/javascript"),
            ("/app.css", "text/css"),
        ):
            status, headers, body = running.request("GET", path)
            assert status == 200
            assert headers["Content-Type"].startswith(kind)
            assert "default-src 'none'" in headers["Content-Security-Policy"]
            assert headers["X-Content-Type-Options"] == "nosniff"
            assert body
        assert b'<script src="app.js" defer></script>' in running.request("GET", "/")[2]
        assert running.request("GET", "/missing")[0] == 404

    def test_state_follows_the_receiver(self, running):
        running.wait_for(lambda state: state["consensus"] is not None)
        state = running.state()
        assert [row["cells"][0] for row in state["rows"]] == ["102.40", "98.80", "107.50"]
        assert state["rows"][2]["cells"][1] == MARKUP_PS
        assert state["rows"][2]["status"] == "OUTLIER"
        assert state["consensus"]["trust_level"] == "HIGH"
        assert state["columns"][-1] == "Status"
        assert state["note"].endswith("not a calibrated UTC error.")
        assert state["can_rescan"] is True
        assert "Receiver ready: tuner type 5, 29 gain steps." in state["log"]

    def test_event_stream_sends_snapshots(self, running):
        connection = http.client.HTTPConnection("127.0.0.1", running.port, timeout=10)
        try:
            connection.request("GET", "/events")
            response = connection.getresponse()
            assert response.status == 200
            assert response.getheader("Content-Type").startswith("text/event-stream")
            line = response.fp.readline()
            assert line.startswith(b"data: ")
            assert "status" in json.loads(line[len(b"data: ") :])
        finally:
            connection.close()

    def test_foreign_host_names_are_refused(self, running):
        assert running.request("GET", "/api/state", {"Host": "attacker.example:8080"})[0] == 421
        assert (
            running.request("POST", "/api/rescan", {"Host": "attacker.example", **RESCAN})[0] == 421
        )
        assert running.request("GET", "/", {"Host": f"localhost:{running.port}"})[0] == 200

    def test_rescan_needs_the_header_and_the_same_origin(self, running):
        running.wait_for(lambda state: state["can_rescan"])
        assert running.request("POST", "/api/rescan")[0] == 403
        foreign = {**RESCAN, "Origin": "http://attacker.example"}
        assert running.request("POST", "/api/rescan", foreign)[0] == 403
        same = {**RESCAN, "Origin": f"http://127.0.0.1:{running.port}"}
        status, _, body = running.request("POST", "/api/rescan", same)
        assert status == 202
        assert json.loads(body) == {
            "message": "Rescan requested; it starts after the current cycle."
        }
        running.wait_for(lambda state: state["status"].startswith("ACQUISITION #2"))
        assert running.request("POST", "/api/other", RESCAN)[0] == 404

    def test_rescans_only_from_this_machine(self, running, monkeypatch):
        running.wait_for(lambda state: state["consensus"] is not None)
        monkeypatch.setattr(web, "is_loopback", lambda address: False)
        assert running.state()["can_rescan"] is False
        status, _, body = running.request("POST", "/api/rescan", RESCAN)
        assert (status, json.loads(body)) == (
            403,
            {"error": "Rescans are accepted only from this machine."},
        )

    def test_rescan_before_the_receiver_is_ready_conflicts(self):
        with _Running([sys.executable, "-I", "-c", "import time; time.sleep(30)"]) as server:
            status, _, body = server.request("POST", "/api/rescan", RESCAN)
        assert (status, json.loads(body)) == (
            409,
            {"error": "The receiver is not ready for a rescan."},
        )

    def test_receiver_failure_is_shown(self, tmp_path):
        with _Running(receiver_command(tmp_path, "fail")) as server:
            server.wait_for(lambda state: state["phase"] == "STOPPED")
            log = server.state()["log"]
        assert "Receiver process exited with status 1." in log
        assert "  rdsclock: [Errno 111] Connection refused" in log
        assert server.dashboard.failed

    def test_stream_ending_without_a_stop_is_a_failure(self, tmp_path):
        with _Running(receiver_command(tmp_path, "truncated")) as server:
            server.wait_for(lambda state: state["phase"] == "STOPPED")
            log = server.state()["log"]
        assert "The receiver stream ended without recon_stopped." in log
        assert server.dashboard.failed

    def test_missing_receiver_command_is_reported(self, tmp_path):
        with _Running([str(tmp_path / "missing-rdsclock")]) as server:
            log = server.state()["log"]
        assert log[0].startswith("Could not start the receiver")
        assert server.dashboard.failed

    def test_url_names_a_reachable_address(self, tmp_path):
        command = receiver_command(tmp_path)
        for host, prefix in (("127.0.0.1", "http://127.0.0.1:"), ("0.0.0.0", "http://127.0.0.1:")):
            dashboard = web.WebDashboard(command, host=host, port=0)
            try:
                assert dashboard.url.startswith(prefix)
            finally:
                dashboard.server.server_close()
        if not socket.has_ipv6:
            pytest.skip("IPv6 is unavailable")
        try:
            dashboard = web.WebDashboard(command, host="::1", port=0)
        except OSError:
            pytest.skip("IPv6 loopback is unavailable")
        try:
            assert dashboard.url.startswith("http://[::1]:")
        finally:
            dashboard.server.server_close()


class TestRunWeb:
    def test_serves_until_interrupted(self, tmp_path, monkeypatch, capsys):
        def interrupted(self):
            raise KeyboardInterrupt

        monkeypatch.setattr(web.WebDashboard, "serve_forever", interrupted)
        assert web.run_web(receiver_command(tmp_path), "127.0.0.1", 0) == 0
        err = capsys.readouterr().err
        assert "Dashboard at http://127.0.0.1:" in err
        assert "warning" not in err

    def test_warns_when_reachable_from_the_network(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(web.WebDashboard, "serve_forever", lambda self: None)
        assert web.run_web(receiver_command(tmp_path), "0.0.0.0", 0) == 0
        assert "warning: the dashboard has no authentication" in capsys.readouterr().err

    def test_failed_receiver_sets_the_exit_status(self, tmp_path, monkeypatch):
        def until_failed(self):
            deadline = time.monotonic() + 15
            while not self.failed and time.monotonic() < deadline:
                time.sleep(0.05)

        monkeypatch.setattr(web.WebDashboard, "serve_forever", until_failed)
        assert web.run_web(receiver_command(tmp_path, "fail"), "127.0.0.1", 0) == 1

    def test_busy_port_is_reported(self, tmp_path, capsys):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            assert web.run_web(receiver_command(tmp_path), "127.0.0.1", port) == 2
        assert f"cannot listen on 127.0.0.1:{port}" in capsys.readouterr().err


class TestReconWebCli:
    def test_child_runs_jsonl_and_the_server_gets_the_address(self, monkeypatch):
        calls: list[tuple[list[str], str, int]] = []
        monkeypatch.setattr(
            web, "run_web", lambda command, host, port: calls.append((command, host, port)) or 0
        )
        assert cli.main(["recon", "--web", "--web-port=9000", "--start=98"]) == 0
        ((command, host, port),) = calls
        assert command[:5] == [sys.executable, "-m", "rdsclock", "recon", "--jsonl"]
        assert "--start=98.0" in command
        assert (host, port) == ("127.0.0.1", 9000)

    def test_offline_replay_is_rejected(self, tmp_path, capsys):
        assert cli.main(["recon", "--web", "--from-dir", str(tmp_path)]) == 2
        assert "--from-dir" in capsys.readouterr().err

    def test_web_and_jsonl_are_exclusive(self, capsys):
        with pytest.raises(SystemExit) as exc:
            cli.main(["recon", "--web", "--jsonl"])
        assert exc.value.code == 2
