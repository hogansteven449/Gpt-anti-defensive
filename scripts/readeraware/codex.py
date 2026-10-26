from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any

from .util import ValidationError, check_no_credentials, digest, json_write, read_json, redact, text_write, utc_now

DISABLE_FEATURES = ("multi_agent", "shell_tool", "shell_snapshot", "unified_exec", "hooks", "plugins", "apps", "browser_use", "computer_use", "image_generation", "in_app_browser", "view_image", "code_mode")
REQUIRED_FLAGS = ("--json", "--output-schema", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check", "--sandbox", "--output-last-message")
TRANSIENT = re.compile(r"^Reconnecting\.\.\.\s+\d+/\d+\s+\((?:exceeded retry limit, last status:\s*429 Too Many Requests|request timed out)\)$")


class BackendError(RuntimeError):
    def __init__(self, message: str, *, metadata: dict | None = None):
        super().__init__(redact(message))
        self.metadata = metadata or {}


def local_selection() -> dict:
    """Read only two non-secret settings, never auth or the rest of config."""
    path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"model": None, "reasoning_effort": None}
    return {"model": data.get("model"), "reasoning_effort": data.get("model_reasoning_effort")}


def resolve_codex(executable: str | None = None) -> str:
    found = shutil.which(executable or "codex")
    if not found:
        raise BackendError("Codex CLI was not found. Install/sign in to Codex, or pass --codex-bin with its native executable path.")
    if Path(found).suffix.lower() in (".cmd", ".bat"):
        raise BackendError("Use --codex-bin with the native codex.exe path, not a Windows shell wrapper.")
    return str(Path(found).resolve())


def _inspection(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, shell=False,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def inspect_cli(executable: str | None = None, *, auth: bool = False) -> dict:
    exe = resolve_codex(executable)
    version = _inspection([exe, "--version"])
    help_result = _inspection([exe, "exec", "--help"])
    if version.returncode or help_result.returncode:
        raise BackendError("Codex CLI version/help inspection failed")
    missing = [flag for flag in REQUIRED_FLAGS if flag not in help_result.stdout]
    if missing:
        raise BackendError("Codex CLI lacks required options: " + ", ".join(missing))
    features = _inspection([exe, "features", "list"])
    if features.returncode:
        raise BackendError("Codex feature inspection failed; refusing to guess isolation flags")
    names = {line.split()[0] for line in features.stdout.splitlines() if line.split()}
    result = {"executable": exe, "executable_sha256": digest(Path(exe)), "version": version.stdout.strip(),
              "required_flags_available": True, "disabled_features": [f for f in DISABLE_FEATURES if f in names],
              "skip_host_skill_discovery_available": "skip_host_skill_discovery" in names,
              "local_selection": local_selection()}
    if auth:
        status = _inspection([exe, "login", "status"])
        # This CLI command reports status, not credential storage contents.
        result["login_status_exit_code"] = status.returncode
        lines = [line for line in (status.stdout + status.stderr).splitlines() if "logged in" in line.lower() or "not logged" in line.lower()]
        result["login_status"] = redact("\n".join(lines)) or "CLI did not report a recognized login status"
    return result


def parse_event_stream(stdout: str) -> tuple[list[dict], dict | None, str | None, list[str], list[str]]:
    """Keep visible event metadata only; never persist reasoning text/raw streams."""
    events, usage, reported_model, forbidden, errors = [], None, None, [], []
    for line in stdout.splitlines():
        try:
            raw = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type", "unknown"))
        event: dict[str, Any] = {"type": kind}
        if isinstance(raw.get("thread_id"), str):
            event["thread_id"] = raw["thread_id"]
        if isinstance(raw.get("model"), str):
            reported_model = raw["model"]
            event["model"] = reported_model
        item = raw.get("item")
        if isinstance(item, dict):
            item_type = str(item.get("type", "unknown"))
            event["item_type"] = item_type
            if isinstance(item.get("id"), str):
                event["item_id"] = item["id"]
            # CLI startup diagnostics are item-level errors and can precede a
            # successful turn. They are neither tool calls nor fatal turn errors.
            # Top-level error/turn.failed events remain fatal below.
            if item_type == "error":
                message = item.get("message") or item.get("error")
                if isinstance(message, dict):
                    message = message.get("message")
                if message is not None:
                    event["diagnostic"] = redact(str(message))
            elif item_type not in {"agent_message", "reasoning", "plan", "todo_list"}:
                forbidden.append(item_type)
        if kind == "turn.completed" and isinstance(raw.get("usage"), dict):
            usage = {k: v for k, v in raw["usage"].items() if type(v) in (int, float)}
            event["usage"] = usage
        if kind in {"error", "turn.failed"}:
            message = raw.get("message") or raw.get("error") or "Codex turn failed"
            if isinstance(message, dict):
                message = message.get("message", "Codex turn failed")
            safe = redact(str(message))
            errors.append(safe)
            event["message"] = safe
        events.append(event)
    return events, usage, reported_model, forbidden, errors


def _stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, shell=False,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        os.killpg(proc.pid, signal.SIGKILL)


class CodexBackend:
    name = "codex"

    def __init__(self, *, model: str, reasoning_effort: str | None, timeout: float = 600, executable: str | None = None):
        self.info = inspect_cli(executable)
        self.model, self.reasoning_effort, self.timeout = model, reasoning_effort, timeout

    def run(self, stage: str, prompt: str, schema: dict, stage_dir: Path, context: dict) -> tuple[dict, dict]:
        schema_path = stage_dir / "schema.json"
        response_path = stage_dir / "provider_response.txt"
        command = [self.info["executable"], "--no-daemon", "--ask-for-approval", "never", "exec", "--ignore-user-config",
                   "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only", "--color", "never", "--json",
                   "--model", self.model, "--config", 'web_search="disabled"', "--config", "project_doc_max_bytes=0",
                   "--config", "skills.include_instructions=false", "--config", "skills.bundled.enabled=false"]
        for feature in self.info["disabled_features"]:
            command.extend(["--disable", feature])
        if self.info["skip_host_skill_discovery_available"]:
            command.extend(["--enable", "skip_host_skill_discovery"])
        if self.reasoning_effort:
            command.extend(["--config", f'model_reasoning_effort="{self.reasoning_effort}"'])
        command.extend(["--output-schema", str(schema_path.resolve()), "--output-last-message", str(response_path.resolve()), "-"])
        text_write(stage_dir / "command.json", json.dumps(command, ensure_ascii=False, indent=2) + "\n")
        started, start = utc_now(), time.monotonic()
        raw_stdout, stderr, timed_out, cancelled = "", "", False, False
        returncode = None
        # An empty working directory prevents previous runs and repository files
        # from becoming incidental stage context. All task data is sent on stdin.
        with tempfile.TemporaryDirectory(prefix="workspace-", dir=stage_dir) as isolated:
            kwargs: dict[str, Any] = {"cwd": isolated, "stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                                      "stderr": subprocess.PIPE, "text": True, "encoding": "utf-8", "errors": "replace", "shell": False}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            try:
                proc = subprocess.Popen(command, **kwargs)
                try:
                    raw_stdout, stderr = proc.communicate(prompt, timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _stop_process(proc)
                    raw_stdout, stderr = proc.communicate()
                except KeyboardInterrupt:
                    cancelled = True
                    _stop_process(proc)
                    raw_stdout, stderr = proc.communicate()
                except BaseException:
                    _stop_process(proc)
                    proc.communicate()
                    raise
                returncode = proc.returncode
            except OSError as exc:
                raise BackendError(f"Cannot start Codex: {exc}") from exc
        events, usage, reported_model, forbidden, errors = parse_event_stream(raw_stdout)
        text_write(stage_dir / "events.jsonl", "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events))
        text_write(stage_dir / "stderr.txt", redact(stderr))
        metadata = {"backend": self.name, "started_utc": started, "finished_utc": utc_now(), "elapsed_seconds": round(time.monotonic() - start, 4),
                    "returncode": returncode, "timed_out": timed_out, "cancelled": cancelled, "model_requested": self.model, "model_reported": reported_model,
                    "reasoning_effort": self.reasoning_effort, "usage": usage, "tool_item_types": sorted(set(forbidden)),
                    "fresh_session": True, "isolated_working_directory": True}
        json_write(stage_dir / "transport.json", metadata)
        response_text = response_path.read_text(encoding="utf-8-sig") if response_path.is_file() else None
        response_redacted = response_text is not None and redact(response_text) != response_text
        if response_redacted:
            text_write(response_path, redact(response_text))
        if cancelled:
            raise BackendError("Run cancelled; child process tree stopped", metadata=metadata)
        if timed_out:
            raise BackendError("Codex stage timed out; no retry was issued", metadata=metadata)
        intermediate = [(i, e) for i, e in enumerate(events) if e["type"] in {"error", "turn.failed"}]
        completed = [i for i, e in enumerate(events) if e["type"] == "turn.completed"]
        resolved_transient = bool(errors) and bool(completed) and not any(e["type"] == "turn.failed" for e in events) and all(
            e["type"] == "error" and TRANSIENT.fullmatch(e["message"]) for _, e in intermediate) and max(completed) > max(i for i, _ in intermediate)
        if returncode != 0 or (errors and not resolved_transient):
            raise BackendError("; ".join(errors) or f"Codex exited with code {returncode}; see redacted stderr.txt", metadata=metadata)
        if forbidden:
            raise BackendError("Unexpected tool use; stage rejected: " + ", ".join(sorted(set(forbidden))), metadata=metadata)
        if not any(e["type"] == "turn.completed" for e in events):
            raise BackendError("Codex emitted no completed turn", metadata=metadata)
        if not response_path.is_file():
            raise BackendError("Codex did not save a final response", metadata=metadata)
        text = response_text
        if response_redacted:
            raise BackendError("Potential credential in provider output; saved response was redacted and stage rejected", metadata=metadata)
        if reported_model is not None and reported_model != self.model:
            raise BackendError("Reported model differs from requested model; stage rejected", metadata=metadata)
        if resolved_transient:
            metadata["technical_resolution"] = {"rule": "intermediate_429_or_request_timeout_final_completed_v3", "new_model_invocations": 0,
                "intermediate_errors": [{"index": i, "message": e["message"]} for i, e in intermediate],
                "final_completed_index": max(completed), "provider_response_unchanged": True,
                "acceptance_requires_stage_validation": True}
            json_write(stage_dir / "transport.json", metadata)
            json_write(stage_dir / "transport_resolution.json", metadata["technical_resolution"])
        try:
            result = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BackendError("Codex final response is not valid JSON; no retry was issued", metadata=metadata) from exc
        return result, metadata
