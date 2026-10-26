"""Offline transport checks. No subprocess or real model is started by these tests."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from readeraware import codex


class NativeTransportTests(unittest.TestCase):
    def invoke(self, directory: Path, events: list[dict], *, response: dict | None = None,
               stderr: str = "") -> tuple[dict, dict]:
        self.commands = []
        self.prompts = []
        info = {
            "executable": "C:/offline-test/codex.exe",
            "disabled_features": list(codex.DISABLE_FEATURES),
            "skip_host_skill_discovery_available": True,
        }
        test = self

        class Process:
            returncode = 0

            def __init__(self, command, **kwargs):
                test.commands.append((command, kwargs))
                test.assertIs(kwargs["shell"], False)
                isolated = Path(kwargs["cwd"])
                test.assertTrue(isolated.is_dir())
                test.assertEqual(list(isolated.iterdir()), [])
                test.assertEqual(isolated.parent, directory)
                Path(command[command.index("--output-last-message") + 1]).write_text(
                    json.dumps(response if response is not None else {"document": "Offline body."}),
                    encoding="utf-8",
                )

            def communicate(self, prompt=None, timeout=None):
                test.prompts.append(prompt)
                return "\n".join(json.dumps(e) for e in events), stderr

        with patch.object(codex, "inspect_cli", return_value=info), patch.object(codex.subprocess, "Popen", Process):
            backend = codex.CodexBackend(model="offline-test-model", reasoning_effort="high")
            return backend.run("body", "PROMPT_ON_STDIN_ONLY", {}, directory, {})

    def test_native_invocation_is_fresh_readonly_and_tools_disabled(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            stage = Path(temporary)
            response, metadata = self.invoke(stage, [{"type": "turn.completed", "usage": {"input_tokens": 3}}])
            command, kwargs = self.commands[0]
            self.assertEqual(len(self.commands), 1)
            self.assertEqual(command[0], "C:/offline-test/codex.exe")
            self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
            self.assertEqual(command[command.index("--ask-for-approval") + 1], "never")
            for flag in ("--no-daemon", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "--json"):
                self.assertIn(flag, command)
            disabled = [command[index + 1] for index, value in enumerate(command) if value == "--disable"]
            self.assertEqual(set(disabled), set(codex.DISABLE_FEATURES))
            self.assertIn("skip_host_skill_discovery", command)
            self.assertIn('web_search="disabled"', command)
            self.assertIn("project_doc_max_bytes=0", command)
            self.assertIn("skills.include_instructions=false", command)
            self.assertIn("skills.bundled.enabled=false", command)
            self.assertEqual(command[-1], "-")
            self.assertNotIn("PROMPT_ON_STDIN_ONLY", command)
            self.assertEqual(self.prompts, ["PROMPT_ON_STDIN_ONLY"])
            self.assertFalse(Path(kwargs["cwd"]).exists())
            self.assertTrue(metadata["fresh_session"])
            self.assertEqual(response, {"document": "Offline body."})

    def test_tool_event_aborts_without_second_subprocess(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            with self.assertRaisesRegex(codex.BackendError, "tool use"):
                self.invoke(Path(temporary), [
                    {"type": "item.completed", "item": {"id": "call-1", "type": "web_search", "text": "must not be logged"}},
                    {"type": "turn.completed"},
                ])
            self.assertEqual(len(self.commands), 1)

    def test_reasoning_and_credentials_are_not_saved_in_transport_logs(self):
        secret = "sk-" + "x" * 32
        hidden_reasoning = "PRIVATE_REASONING_SENTINEL_1249"
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            stage = Path(temporary)
            self.invoke(stage, [
                {"type": "item.completed", "item": {"id": "r1", "type": "reasoning", "text": hidden_reasoning}},
                {"type": "item.completed", "item": {"id": "d1", "type": "error", "message": "Authorization: Bearer " + secret}},
                {"type": "turn.completed"},
            ], stderr="x-api-key: " + secret)
            saved = "\n".join(p.read_text(encoding="utf-8") for p in stage.iterdir() if p.is_file())
            self.assertNotIn(hidden_reasoning, saved)
            self.assertNotIn(secret, saved)
            self.assertIn("REDACTED", saved)

    def test_response_credential_is_redacted_and_rejected(self):
        secret = "ark-" + "q" * 32
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            stage = Path(temporary)
            with self.assertRaisesRegex(codex.BackendError, "credential"):
                self.invoke(stage, [{"type": "turn.completed"}], response={"document": secret})
            self.assertNotIn(secret, (stage / "provider_response.txt").read_text(encoding="utf-8"))
            self.assertEqual(len(self.commands), 1)


if __name__ == "__main__":
    unittest.main()
