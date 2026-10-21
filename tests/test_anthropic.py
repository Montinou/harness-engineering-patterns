"""Anthropic backend tests. No network."""

from __future__ import annotations

import json
import os
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from harness_workbench.cli import main
from harness_workbench.core.anthropic_backend import (
    IMPROVER_MODEL,
    SOLVER_MODEL,
    AnthropicClient,
    AnthropicError,
    AnthropicHarnessExecutor,
    Completion,
    parse_judge,
    parse_message,
)
from harness_workbench.core.executor import Task
from harness_workbench.core.harness import Harness, ToolSpec
from harness_workbench.core.improver import Improver
from harness_workbench.core.proposer import LLMProposer

SEED = Path(__file__).resolve().parents[1] / "examples" / "workbench" / "base_research_harness.json"
ROOT_HARNESS = Harness(
    id="research-agent",
    version="1",
    system_prompt="Answer the research question.",
    tools=[ToolSpec("calc", "Basic calculator.")],
    memory_config={"window": 2, "strategy": "recent"},
)


def _message(text: str, *, model: str, thinking: str = "") -> dict:
    content = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking})
    content.append({"type": "text", "text": text})
    return {
        "model": model,
        "stop_reason": "end_turn",
        "content": content,
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }


class ParseTests(unittest.TestCase):
    def test_text_is_kept_and_thinking_is_dropped(self) -> None:
        parsed = parse_message(_message("the answer", model=SOLVER_MODEL, thinking="hidden"))
        self.assertEqual(parsed.text, "the answer")
        self.assertEqual(parsed.tokens, 18)

    def test_empty_text_is_an_error(self) -> None:
        with self.assertRaises(AnthropicError):
            parse_message({"content": [{"type": "thinking", "thinking": "only"}], "stop_reason": "max_tokens"})

    def test_judge_json_in_a_fence(self) -> None:
        score, failures, notes = parse_judge('```json\n{"score": 1.4, "failures": ["invented_source"], "notes": "too sure"}\n```')
        self.assertEqual(score, 1.0)
        self.assertEqual(failures, ["invented_source"])
        self.assertEqual(notes, "too sure")


class ClientTests(unittest.TestCase):
    def test_request_uses_messages_api_and_keeps_the_key_out_of_the_body(self) -> None:
        seen: dict = {}

        def transport(url: str, headers: dict, body: bytes, timeout: float):
            seen["url"] = url
            seen["headers"] = headers
            seen["body"] = json.loads(body.decode("utf-8"))
            seen["timeout"] = timeout
            raw = json.dumps(_message("ok", model=SOLVER_MODEL)).encode("utf-8")
            return 200, raw

        client = AnthropicClient("test-key", transport=transport)
        result = client.complete(model=SOLVER_MODEL, system="sys", user="hello", max_tokens=128)
        self.assertEqual(result.text, "ok")
        self.assertEqual(seen["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(seen["headers"]["x-api-key"], "test-key")
        self.assertEqual(seen["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(seen["body"]["model"], SOLVER_MODEL)
        self.assertNotIn("test-key", json.dumps(seen["body"]))
        self.assertEqual(seen["body"]["messages"], [{"role": "user", "content": "hello"}])

    def test_http_error_does_not_include_the_key(self) -> None:
        def transport(url: str, headers: dict, body: bytes, timeout: float):
            return 401, b'{"error":"bad key secret-value"}'

        client = AnthropicClient("secret-value", transport=transport)
        with self.assertRaises(AnthropicError) as caught:
            client.complete(model=SOLVER_MODEL, user="hi")
        self.assertIn("401", str(caught.exception))
        self.assertNotIn("x-api-key", str(caught.exception))

    def test_missing_env_key(self) -> None:
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False):
            with self.assertRaises(AnthropicError):
                AnthropicClient.from_env()


class ScriptedClient:
    """Stands in for AnthropicClient. Solver text is fixed. Judge score follows the harness."""

    def complete(self, *, model: str, user: str, system: str = "", max_tokens: int = 4096) -> Completion:
        if model == SOLVER_MODEL:
            return Completion("A careful answer.", model, 20, 10, "end_turn")
        if system.startswith("You score"):
            score = 0.82 if "Verify claims" in user else 0.30
            payload = json.dumps({"score": score, "failures": [] if score > 0.5 else ["weak"], "notes": "scored"})
            return Completion(payload, model, 8, 6, "end_turn")
        edit = {
            "edits": [
                {
                    "id": "haiku-verify",
                    "kind": "invariant",
                    "component": "guardrails",
                    "op": "add_guardrail",
                    "payload": {"text": "Verify claims against a second source before answering."},
                    "rationale": "Haiku added verification",
                }
            ]
        }
        return Completion(json.dumps(edit), model, 9, 12, "end_turn")


class LoopTests(unittest.TestCase):
    def test_sonnet_solves_and_haiku_judge_feeds_compose(self) -> None:
        client = ScriptedClient()
        executor = AnthropicHarnessExecutor(client, solver_model=SOLVER_MODEL, judge_model=IMPROVER_MODEL)
        proposer = LLMProposer(lambda prompt: client.complete(model=IMPROVER_MODEL, user=prompt, system="edit").text)
        task = Task.from_text("Fact-check a claim", domain="research", task_id="fact")
        result = Improver(proposer, executor).improve(ROOT_HARNESS, task, k=1)
        self.assertEqual(result.adopted.version, "2")
        self.assertIn("Verify claims", result.adopted.guardrails[0])
        self.assertGreater(result.adopted_report.score, result.base_report.score)
        self.assertIn(SOLVER_MODEL, result.adopted_report.notes)
        self.assertIn(IMPROVER_MODEL, result.adopted_report.notes)
        self.assertGreater(result.adopted_report.cost_tokens, 0)


class CliProviderTests(unittest.TestCase):
    def test_anthropic_without_a_key_fails_before_any_call(self) -> None:
        env = os.environ.copy()
        env["ANTHROPIC_API_KEY"] = ""
        stderr = StringIO()
        with patch.dict(os.environ, env, clear=True):
            with redirect_stderr(stderr):
                code = main(
                    [
                        "probe",
                        "--provider",
                        "anthropic",
                        "--harness",
                        str(SEED),
                        "--task",
                        "Fact-check a claim",
                        "--json",
                    ]
                )
        self.assertEqual(code, 2)
        self.assertIn("ANTHROPIC_API_KEY", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
