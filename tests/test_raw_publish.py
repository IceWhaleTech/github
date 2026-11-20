import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

SOURCE = Path(__file__).parents[1] / "actions/publish-raw/publish.py"
spec = importlib.util.spec_from_file_location("raw_publish", SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, value, code=200):
        data = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        fixture = self.server.fixture
        fixture["requests"].append((self.command, self.path))
        if not self.headers.get("Authorization") == "Bearer rawpub_test-secret":
            return self.reply({"error": "unauthorized"}, 401)
        if "publish-requests" in self.path:
            return self.reply(fixture["task"], 200) if fixture["task"] else self.reply({"error": "missing"}, 404)
        fixture["polls"] += 1
        task = dict(fixture["task"])
        task.update(state=fixture["state"], artifact_id=fixture["sha"], error="package verification failed")
        self.reply(task)

    def do_POST(self):
        fixture = self.server.fixture
        fixture["requests"].append((self.command, self.path))
        if fixture.get("redirect"):
            self.send_response(307)
            self.send_header("Location", fixture["redirect"])
            self.end_headers()
            return
        data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path.endswith("publish-upload"):
            metadata = json.loads(base64.urlsafe_b64decode(self.headers["X-E2E-RAW-Metadata"] + "==="))
            if data:
                fixture["uploads"].append(data)
        else:
            metadata = json.loads(data)
        fixture["metadata"] = metadata
        fixture["sha"] = metadata["sha256"]
        source = {"kind":"github-actions","repo":metadata["repository"],"commit":metadata["commit"],"ref":metadata["ref"],"workflow":metadata["workflow"],"run_id":metadata["run_id"],"run_attempt":metadata["run_attempt"],"declared_version":metadata.get("version","")}
        source["build_id"]=hashlib.sha256("\n".join([metadata["repository"].lower(),metadata["run_id"],str(metadata["run_attempt"]),metadata["sha256"],metadata["filename"]]).encode()).hexdigest()
        fixture["task"] = {"id": "raw-task-fixture", "state": "queued", "stage": "queued", "source":source,"expected_sha256":metadata["sha256"]}
        if fixture.get("disconnect"):
            fixture["disconnect"] = False
            self.connection.close()
            return
        self.reply(fixture["task"], 202)


class RawPublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.file = self.root / "app.raw"
        self.file.write_bytes(b"isolated raw fixture data")
        self.fixture = dict(task=None, requests=[], uploads=[], polls=0, state="succeeded")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.fixture = self.fixture
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.env = dict(INPUT_PLATFORM_URL=f"http://127.0.0.1:{self.server.server_port}", INPUT_PUBLISH_TOKEN="rawpub_test-secret",
            INPUT_PACKAGE_PATH=str(self.file), INPUT_SOURCE_COMMIT="a" * 40,
            GITHUB_REPOSITORY="Example/App", GITHUB_REF="refs/tags/v1.2.3", GITHUB_WORKFLOW="Build app",
            GITHUB_SHA="b" * 40, GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="2", INPUT_POLL_INTERVAL="1",
            GITHUB_OUTPUT=str(self.root / "outputs"), GITHUB_STEP_SUMMARY=str(self.root / "summary"))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def run_client(self):
        with patch.dict(os.environ, self.env, clear=True), patch.object(module.time, "sleep", lambda _: None), patch("sys.stdout", new_callable=io.StringIO) as output:
            module.main()
            return output.getvalue()

    def test_file_upload_records_actual_commit_and_verified_outputs(self):
        output = self.run_client()
        self.assertEqual(self.fixture["uploads"], [self.file.read_bytes()])
        self.assertEqual(self.fixture["metadata"]["commit"], "a" * 40)
        self.assertEqual(self.fixture["metadata"]["run_attempt"], 2)
        self.assertEqual(self.fixture["metadata"]["ref"], "refs/tags/v1.2.3")
        self.assertEqual(self.fixture["polls"], 1)
        self.assertIn("artifact-id=" + hashlib.sha256(self.file.read_bytes()).hexdigest(), (self.root / "outputs").read_text())
        self.assertNotIn("rawpub_test-secret", output + (self.root / "summary").read_text())

    def test_existing_request_does_not_upload_the_file_again(self):
        self.run_client()
        self.run_client()
        self.assertEqual(len(self.fixture["uploads"]), 1)

    def test_network_disconnect_recovers_accepted_task_without_duplicate_upload(self):
        self.fixture["disconnect"] = True
        self.run_client()
        self.assertEqual(len(self.fixture["uploads"]), 1)
        self.assertGreaterEqual(sum("publish-requests" in path for _, path in self.fixture["requests"]), 2)

    def test_url_mode_submits_metadata_without_downloading_package(self):
        self.env.update(INPUT_PACKAGE_PATH="", INPUT_PACKAGE_URL="https://packages.invalid/app.raw?signature=private", INPUT_SHA256="c" * 64)
        self.run_client()
        self.assertEqual(self.fixture["uploads"], [])
        self.assertEqual(self.fixture["metadata"]["url"], self.env["INPUT_PACKAGE_URL"])
        self.assertNotIn("version", self.fixture["metadata"])

    def test_failed_import_does_not_emit_success(self):
        self.fixture["state"] = "failed"
        with self.assertRaisesRegex(module.PublishError, "package verification failed"):
            self.run_client()
        self.assertNotIn("artifact-id", (self.root / "outputs").read_text())
        self.assertFalse((self.root / "summary").exists())

    def test_redirect_is_rejected(self):
        self.fixture["redirect"] = "https://other.invalid/collect"
        with self.assertRaisesRegex(module.PublishError, "redirects are not allowed"):
            self.run_client()

    def test_input_validation_happens_before_submission(self):
        self.env.update(INPUT_SHA256="0" * 64)
        with self.assertRaisesRegex(module.PublishError, "does not match"):
            self.run_client()
        self.assertEqual(self.fixture["requests"], [])

    def test_timeout_keeps_task_id_for_diagnosis(self):
        self.fixture["state"] = "running"
        self.env["INPUT_WAIT_TIMEOUT"] = "1"
        clock = [0]
        def advance(seconds): clock[0] += seconds
        with patch.dict(os.environ, self.env, clear=True), patch.object(module.time, "monotonic", lambda: clock[0]), patch.object(module.time, "sleep", advance), patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaisesRegex(module.PublishError, "task raw-task-fixture continues"):
                module.main()
        self.assertIn("task-id=raw-task-fixture", (self.root / "outputs").read_text())


if __name__ == "__main__":
    unittest.main()
