#!/usr/bin/env python3
"""Publish immutable RAW packages to e2e; no device deployment capability."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class PublishError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PublishError("Platform redirects are not allowed; configure the final E2E_PLATFORM_URL")


class Client:
    def __init__(self, base, token):
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise PublishError("E2E_PLATFORM_URL must be an HTTP(S) base URL without credentials, query or fragment")
        if not token or any(c in token for c in "\r\n"):
            raise PublishError("E2E_RAW_PUBLISH_TOKEN is missing or invalid")
        self.base, self.token = base.rstrip("/"), token
        self.opener = urllib.request.build_opener(NoRedirect)

    def safe(self, message):
        return str(message).replace(self.token, "[redacted]")[:1000]

    def request(self, method, path, body=None, headers=None, missing=False):
        request = urllib.request.Request(self.base + path, data=body, method=method,
            headers={"Authorization": "Bearer " + self.token, **(headers or {})})
        try:
            with self.opener.open(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if missing and error.code == 404:
                error.close()
                return None
            try:
                message = json.loads(error.read(1 << 20)).get("error", "Request rejected")
            except (ValueError, AttributeError):
                message = "Request rejected"
            error.close()
            raise PublishError(f"HTTP {error.code}: {self.safe(message)}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise PublishError("Platform request failed: " + self.safe(error)) from None
        except (ValueError, TypeError):
            raise PublishError("Platform returned invalid JSON") from None


def value(name, default=""):
    return os.environ.get("INPUT_" + name.upper().replace("-", "_"), default).strip()


def metadata():
    file_name, url = value("package-path"), value("package-url")
    if bool(file_name) == bool(url):
        raise PublishError("Provide exactly one of package-path or package-url")
    file = Path(file_name) if file_name else None
    expected = value("sha256").lower()
    if file:
        if not file.is_file():
            raise PublishError("package-path is not a regular file")
        h = hashlib.sha256()
        with file.open("rb") as stream:
            for part in iter(lambda: stream.read(4 << 20), b""):
                h.update(part)
        actual = h.hexdigest()
        if expected and actual != expected:
            raise PublishError("Local package does not match sha256")
        expected = actual
    elif urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise PublishError("package-url must be an HTTP(S) RAW download URL")
    filename = value("filename") or (file.name if file else Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.raw", filename):
        raise PublishError("Provide a valid .raw filename")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise PublishError("URL publication requires the package sha256")
    commit = value("source-commit")
    if not commit and file:
        try:
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            pass
    commit = (commit or os.environ.get("GITHUB_SHA", "")).lower()
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    ref = value("source-ref") or os.environ.get("GITHUB_REF", "")
    run = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", repo) or repo.endswith(("/.", "/..")):
        raise PublishError("GITHUB_REPOSITORY must identify the calling repository")
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not re.fullmatch(r"[1-9][0-9]{0,19}", run) or not re.fullmatch(r"[1-9][0-9]{0,5}", attempt):
        raise PublishError("Source commit and GitHub run identity are required")
    if not ref.startswith(("refs/heads/", "refs/tags/")) or ref.endswith("/") or any(ord(c) < 32 for c in ref):
        raise PublishError("source-ref must be a full branch or tag ref")
    payload = dict(repository=repo, commit=commit, ref=ref,
        workflow=os.environ.get("GITHUB_WORKFLOW", "GitHub Actions"), run_id=run,
        run_attempt=int(attempt), filename=filename, sha256=expected)
    if value("expected-version"):
        payload["version"] = value("expected-version")
    if url:
        payload["url"] = url
    key = hashlib.sha256("\n".join([repo.lower(), run, attempt, expected, filename]).encode()).hexdigest()
    return payload, file, key


def publish(client, payload, file, key):
    lookup = "/api/raw-artifacts/publish-requests/" + key
    task = client.request("GET", lookup, missing=True)
    if task is not None:
        # POST validates that the same identity was not submitted with changed metadata.
        if file:
            encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
            return client.request("POST", "/api/raw-artifacts/publish-upload", b"", {"X-E2E-RAW-Metadata": encoded, "Content-Length": "0"})
        return client.request("POST", "/api/raw-artifacts/publish", json.dumps(payload).encode(), {"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            if file:
                encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
                with file.open("rb") as body:
                    return client.request("POST", "/api/raw-artifacts/publish-upload", body,
                        {"X-E2E-RAW-Metadata": encoded, "Content-Type": "application/octet-stream", "Content-Length": str(file.stat().st_size)})
            return client.request("POST", "/api/raw-artifacts/publish", json.dumps(payload).encode(), {"Content-Type": "application/json"})
        except PublishError as error:
            text = str(error)
            if text.startswith("HTTP 4") or attempt == 2:
                raise
            task = client.request("GET", lookup, missing=True)
            if task is not None:
                return task
            time.sleep(attempt + 1)
    raise PublishError("Unable to submit RAW publication")


def emit(outputs):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            for key, item in outputs.items():
                if "\n" in item or "\r" in item:
                    raise PublishError("Invalid output value")
                stream.write(f"{key}={item}\n")


def main():
    client = Client(value("platform-url") or os.environ.get("E2E_PLATFORM_URL", ""), value("publish-token") or os.environ.get("E2E_RAW_PUBLISH_TOKEN", ""))
    payload, file, key = metadata()
    try:
        timeout = int(value("wait-timeout", "1800"))
        interval = int(value("poll-interval", "5"))
    except ValueError:
        raise PublishError("Timeout and polling interval must be integer seconds") from None
    if not 1 <= timeout <= 10800 or not 1 <= interval <= 60:
        raise PublishError("Invalid timeout or polling interval")
    deadline = time.monotonic() + timeout
    task = publish(client, payload, file, key)
    source = task.get("source", {})
    fields = {"repo": payload["repository"], "commit": payload["commit"], "ref": payload["ref"],
        "workflow": payload["workflow"], "run_id": payload["run_id"], "run_attempt": payload["run_attempt"],
        "build_id": key, "declared_version": payload.get("version", "")}
    if source.get("kind") != "github-actions" or any(source.get(name, "") != item for name, item in fields.items()) or task.get("expected_sha256") != payload["sha256"]:
        raise PublishError("Platform task provenance does not match this GitHub build")
    task_id = task.get("id", "")
    if not re.fullmatch(r"raw-task-[A-Za-z0-9_-]+", task_id):
        raise PublishError("Platform returned an invalid task ID")
    emit({"task-id": task_id, "sha256": payload["sha256"]})
    last = None
    while True:
        state = task.get("state")
        stage = task.get("stage", state)
        if (state, stage) != last:
            print(f"RAW import {task_id}: {state} / {stage}", flush=True)
            last = state, stage
        if state == "succeeded":
            artifact = task.get("artifact_id", "")
            if not re.fullmatch(r"[0-9a-f]{64}", artifact) or artifact != payload["sha256"]:
                raise PublishError("Imported artifact does not match the requested SHA-256")
            link = client.base + "/artifacts/?" + urllib.parse.urlencode({"view": "raw", "raw_artifact": artifact})
            emit({"artifact-id": artifact, "artifact-url": link})
            summary = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary:
                with open(summary, "a", encoding="utf-8") as stream:
                    stream.write(f"RAW publication succeeded: [{payload['filename']}]({link})\n\nSHA-256: `{artifact}`\n\nTask: `{task_id}`\n")
            print("RAW publication verified: " + link)
            return
        if state == "failed":
            raise PublishError("RAW import failed: " + client.safe(task.get("error", "Unknown error")) + " (task " + task_id + ")")
        if state not in ("queued", "running"):
            raise PublishError("Unknown RAW import state")
        if time.monotonic() >= deadline:
            raise PublishError("Timed out waiting for RAW import; task " + task_id + " continues on the platform")
        time.sleep(min(interval, max(0, deadline-time.monotonic())))
        task = client.request("GET", "/api/raw-artifacts/publish-tasks/" + task_id)


if __name__ == "__main__":
    try:
        main()
    except PublishError as error:
        secret = value("publish-token") or os.environ.get("E2E_RAW_PUBLISH_TOKEN", "")
        message = str(error).replace(secret, "[redacted]") if secret else str(error)
        print("RAW publication failed: " + message, file=sys.stderr)
        sys.exit(1)
