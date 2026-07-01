"""Unit tests for the interactive chat agent HTTP server (console chat).

Run: cd workloads/eventing/scripts && \
     uv run --quiet --with "pydantic-ai-slim[openai]" --with fastapi --with httpx \
       python3 -m unittest test_chat_server -v

No model API key, no network: endpoint tests inject a FunctionModel;
everything else exercises pure helpers.
"""

import json
import os
import unittest

from fastapi.testclient import TestClient
from pydantic_ai.messages import (FunctionToolCallEvent, ModelResponse,
                                  TextPart, ToolCallPart)
from pydantic_ai.models.function import FunctionModel

import chat_server


def _sse_events(body):
    """Parse an SSE body into [(event, data_dict), ...]."""
    out = []
    for block in body.strip().split("\n\n"):
        ev, data = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                ev = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        if ev:
            out.append((ev, data))
    return out


class HistorySplitTest(unittest.TestCase):
    def test_last_user_message_becomes_prompt(self):
        prompt, history = chat_server.split_history([
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "second"},
        ])
        self.assertEqual(prompt, "second")
        self.assertEqual(len(history), 2)  # one request + one response

    def test_single_message_has_empty_history(self):
        prompt, history = chat_server.split_history(
            [{"role": "user", "content": "hi"}])
        self.assertEqual(prompt, "hi")
        self.assertEqual(history, [])

    def test_history_is_capped(self):
        msgs = []
        for i in range(60):
            msgs.append({"role": "user", "content": f"q{i}"})
            msgs.append({"role": "assistant", "content": f"a{i}"})
        msgs.append({"role": "user", "content": "now"})
        _, history = chat_server.split_history(msgs)
        self.assertLessEqual(len(history), chat_server.MAX_HISTORY_MESSAGES)

    def test_rejects_when_last_message_not_user(self):
        with self.assertRaises(ValueError):
            chat_server.split_history(
                [{"role": "assistant", "content": "hello"}])

    def test_rejects_oversized_message(self):
        with self.assertRaises(ValueError):
            chat_server.split_history(
                [{"role": "user",
                  "content": "x" * (chat_server.MAX_MESSAGE_CHARS + 1)}])


class SSEFramingTest(unittest.TestCase):
    def test_sse_line_shape(self):
        self.assertEqual(chat_server.sse("token", {"text": "hi"}),
                         'event: token\ndata: {"text": "hi"}\n\n')

    def test_tool_call_event_maps_to_tool_sse(self):
        ev = FunctionToolCallEvent(part=ToolCallPart(
            tool_name="corridor_impact", args={"corridor": "I285"}))
        name, payload = chat_server.event_to_sse(ev)
        self.assertEqual(name, "tool")
        self.assertEqual(payload["name"], "corridor_impact")
        self.assertIn("I285", payload["summary"])


class ToolRosterTest(unittest.TestCase):
    def test_chat_tools_are_the_read_only_qa_set(self):
        # No gNMI/SNMP device pokes in chat v1, and the deterministic
        # corridor walk must be present.
        self.assertEqual(
            [t.__name__ for t in chat_server.CHAT_TOOLS],
            ["query_prometheus", "query_prometheus_range", "query_loki",
             "query_netbox", "corridor_impact"])


class EndpointTest(unittest.TestCase):
    def _app(self, reply="The ring is intact.", lifetime=100):
        async def stream(messages, info):
            for word in reply.split(" "):
                yield word + " "
        return chat_server.create_app(model=FunctionModel(stream_function=stream),
                                      lifetime_requests=lifetime)

    def test_status_enabled_with_model(self):
        c = TestClient(self._app())
        self.assertTrue(c.get("/api/chat/status").json()["enabled"])

    def test_status_disabled_without_model_or_secret(self):
        os.environ.pop("AI_BASE_URL", None)
        os.environ.pop("AI_MODEL", None)
        c = TestClient(chat_server.create_app())
        self.assertFalse(c.get("/api/chat/status").json()["enabled"])

    def test_chat_disabled_without_model_returns_503(self):
        os.environ.pop("AI_BASE_URL", None)
        os.environ.pop("AI_MODEL", None)
        c = TestClient(chat_server.create_app())
        r = c.post("/api/chat",
                   json={"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 503)

    def test_chat_streams_tokens_then_done(self):
        c = TestClient(self._app())
        r = c.post("/api/chat",
                   json={"messages": [{"role": "user", "content": "status?"}]})
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/event-stream", r.headers["content-type"])
        events = _sse_events(r.text)
        kinds = [e for e, _ in events]
        self.assertIn("token", kinds)
        self.assertEqual(kinds[-1], "done")
        done = events[-1][1]
        self.assertEqual(done["text"].strip(), "The ring is intact.")

    def test_bad_body_is_400(self):
        c = TestClient(self._app())
        r = c.post("/api/chat", json={"messages": []})
        self.assertEqual(r.status_code, 400)

    def test_lifetime_request_cap_returns_429(self):
        c = TestClient(self._app(lifetime=1))
        body = {"messages": [{"role": "user", "content": "hi"}]}
        self.assertEqual(c.post("/api/chat", json=body).status_code, 200)
        self.assertEqual(c.post("/api/chat", json=body).status_code, 429)

    def test_repeat_guard_reset_between_questions(self):
        # analyst_tools._seen_calls is process-global; a long-lived chat
        # server must clear it per question or question N+1 inherits
        # question N's repeat counts.
        import analyst_tools
        analyst_tools._seen_calls[("query_prometheus", "up")] = 99
        c = TestClient(self._app())
        c.post("/api/chat",
               json={"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(analyst_tools._seen_calls, {})


if __name__ == "__main__":
    unittest.main()
