from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agentweb.connector import chrome_executable
from agentweb.sdk import AgentWebError
from agentweb.storage import StatePaths
from agentweb.web_runtime import WebRuntime


try:
    chrome_executable()
    CHROME_AVAILABLE = True
except AgentWebError:
    CHROME_AVAILABLE = False


pytestmark = pytest.mark.skipif(
    not CHROME_AVAILABLE, reason="Chrome or Chromium is required for web runtime tests"
)


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/frame":
            body = (
                b"<button onclick=\"this.textContent='clicked'\">Frame action</button>"
            )
        elif self.path == "/delayed":
            body = b"""
            <div id="app"></div>
            <script>
              setTimeout(() => {
                document.querySelector('#app').innerHTML = '<div data-ng-click="choose()">Rendered later</div>';
              }, 400);
            </script>
            """
        elif self.path == "/reactive":
            body = b"""
            <input id="mode" type="radio" name="mode">
            <input id="query" placeholder="Query">
            <button id="go" disabled>Go</button>
            <script>
              document.querySelector('#mode').addEventListener('click', () => {
                document.querySelector('#query').dataset.active = 'true';
              });
              document.querySelector('#query').addEventListener('keyup', (event) => {
                document.querySelector('#go').disabled =
                  !(event.target.dataset.active && event.target.value.length >= 3);
              });
            </script>
            """
        elif self.path == "/captcha":
            body = (
                b"<p>"
                + b"long page content " * 500
                + b"Type the characters you see in the image below</p>"
                + b'<input name="captcha_value">'
            )
        else:
            body = (
                b"""
            <input placeholder="Main input">
            <div id="host"></div>
            <iframe src="/frame"></iframe>
            <iframe src="http://localhost:%d/frame"></iframe>
            <script>
              const root = document.querySelector('#host').attachShadow({mode:'open'});
              root.innerHTML = '<input placeholder="Shadow input">';
            </script>
            """
                % self.server.server_port
            )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


def test_web_runtime_handles_frames_shadow_dom_filters_and_tabs(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    web = WebRuntime(
        StatePaths(tmp_path),
        site="fixture",
        profile="test",
        base_url=url,
        allowed_domains=["127.0.0.1"],
        root=tmp_path / "private-authoring-runtime",
    )
    try:
        page = web.start()["page"]
        assert (tmp_path / "private-authoring-runtime" / "browser-launch.log").is_file()
        # Chromium may expose a third empty execution context for the blocked
        # cross-origin fixture frame. The main document and allowed same-origin
        # frame must always be present; extra empty contexts are harmless.
        assert page["frame_count"] >= 2
        assert page["control_count_total"] == 3
        frame = web.inspect(query="Frame action", limit=1, max_text_chars=500)["page"]
        assert frame["control_count"] == 1
        ref = frame["controls"][0]["ref"]
        changed = web.action(
            [{"action": "click", "ref": ref}],
            inspect_query="clicked",
            inspect_limit=1,
        )["page"]
        assert changed["controls"][0]["text"] == "clicked"
        delayed = web.action(
            [{"action": "goto", "url": url + "delayed"}],
        )["page"]
        assert delayed["controls"][0]["text"] == "Rendered later"
        reactive = web.action(
            [{"action": "goto", "url": url + "reactive"}],
        )["page"]
        assert len(reactive["controls"]) == 3
        enabled = web.action(
            [
                {
                    "action": "check",
                    "ref": "stale-radio-ref",
                    "target": {
                        "tag": "input",
                        "type": "radio",
                        "name": "mode",
                        "value": "on",
                    },
                },
                {
                    "action": "type",
                    "ref": "stale-input-ref",
                    "target": {"tag": "input", "text": "Query"},
                    "text": "tax",
                },
            ],
            inspect_query="Go",
        )["page"]
        assert enabled["controls"][0]["disabled"] is False
        challenge = web.action(
            [{"action": "goto", "url": url + "captcha"}],
        )["page"]
        assert challenge["human_interaction"] == {
            "required": True,
            "reason": "CAPTCHA or human-verification challenge",
            "instruction": "Call web_start with visible=true and ask the user to complete this step; then continue through AgentWeb.",
        }
        web.action([{"action": "goto", "url": url + "reactive"}])
        waited = web.action([{"action": "wait", "ms": 1}], inspect_after=False)
        assert waited["results"][0]["result"] == {"waited_ms": 1}
        reactive = web.inspect(query="Query", limit=1)["page"]
        filled = web.action(
            [
                {
                    "action": "fill",
                    "ref": reactive["controls"][0]["ref"],
                    "text": "tax",
                }
            ],
            inspect_limit=3,
        )["page"]
        assert any(control["value"] == "tax" for control in filled["controls"])
        with pytest.raises(AgentWebError, match="requires a text field"):
            web.action(
                [{"action": "type", "ref": "stale-input-ref"}],
                inspect_after=False,
            )
        captured = web.action(
            [{"action": "goto", "url": url + "frame"}],
            capture_name="frame-navigation",
        )["capture"]
        assert captured["redacted"] is True
        assert captured["request_count"] >= 1
        assert captured["response_bodies_captured"] >= 1
        assert captured["endpoint_count"] >= 1
        assert captured["recipe_draft"]["steps"]
        trace = json.loads(Path(captured["path"]).read_text())
        assert trace["kind"] == "agentweb_redacted_network_trace"
        assert trace["schema_version"] == 2
        assert trace["redaction"]["response_bodies"] == "shape_only"
        assert any(request["url"].endswith("/frame") for request in trace["requests"])
        opened = web.new_tab(url)
        assert web.tabs()["count"] == 2
        assert web.close_tab(opened["target_id"])["remaining_tabs"] == 1
        with pytest.raises(AgentWebError, match="confirm=true"):
            web.close_tab()
    finally:
        web.stop()
        server.shutdown()
        server.server_close()
