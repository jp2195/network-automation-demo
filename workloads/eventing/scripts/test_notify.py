"""Unit tests for notify.py: Slack failures are non-fatal, Alertmanager
hourly repeats don't re-post, and the ledger carries link_id.

Run: cd workloads/eventing/scripts && \
     uv run --quiet --with fakeredis --with valkey python3 -m unittest test_notify -v
"""

import io
import json
import os
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import fakeredis

import notify

FP = "398c85567411b882"
STARTED = "2026-08-24T16:44:03Z"


def enrichment(status="firing", started=STARTED):
    return {
        "alert": {"name": "SRLInterfaceOperDown", "status": status,
                  "fingerprint": FP, "link_id": "ring-n-e",
                  "started": started, "ended": "2026-08-24T17:44:03Z"},
        "device": {"name": "hub-e", "site": "atl"},
        "interface": {"name": "ethernet-1/2"},
    }


IMPACT = {"severity_class": "high", "downstream_devices": [], "affected_agencies": []}


class FakeSlack:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []
        outer = self

        class WebClient:
            def __init__(self, token=None):
                pass

            def chat_postMessage(self, **kw):
                return outer._call("post", kw)

            def chat_update(self, **kw):
                return outer._call("update", kw)

        self.module = types.ModuleType("slack_sdk")
        self.module.WebClient = WebClient

    def _call(self, kind, kw):
        self.calls.append((kind, kw))
        if self.fail:
            raise RuntimeError("slack is down")
        return {"ts": f"{len(self.calls)}.000"}


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.vk = fakeredis.FakeRedis(decode_responses=True)

    def run_notify(self, enr, slack=None, token="xoxb"):
        env = {"ENRICHMENT_JSON": json.dumps(enr), "IMPACT_JSON": json.dumps(IMPACT),
               "VALKEY_URL": "valkey://stub:6379/2",
               "SLACK_BOT_TOKEN": token, "SLACK_CHANNEL_ID": "C1" if token else ""}
        slack = slack or FakeSlack()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), \
                mock.patch.dict("sys.modules", {"slack_sdk": slack.module}), \
                mock.patch("valkey.from_url", return_value=self.vk), \
                redirect_stdout(out), redirect_stderr(err):
            notify.main()
        return json.loads(out.getvalue()), slack, err.getvalue()

    def ledger(self):
        raw = self.vk.get(f"incident:{FP}")
        return json.loads(raw) if raw else None

    def test_firing_posts_and_records_link_id(self):
        out, slack, _ = self.run_notify(enrichment())
        self.assertTrue(out["posted"])
        self.assertEqual([c[0] for c in slack.calls], ["post"])
        rec = self.ledger()
        self.assertEqual(rec["link_id"], "ring-n-e")
        self.assertEqual(rec["ts"], "1.000")

    def test_hourly_repeat_does_not_repost(self):
        self.run_notify(enrichment())
        out, slack, _ = self.run_notify(enrichment())
        self.assertEqual(slack.calls, [])
        self.assertTrue(out["deduped"])
        self.assertFalse(out["posted"])
        self.assertEqual(out["ts"], "1.000")
        self.assertEqual(self.ledger()["ts"], "1.000")
        self.assertGreater(self.vk.ttl(f"incident:{FP}"), 0)

    def test_new_episode_with_stale_record_reposts(self):
        self.run_notify(enrichment())
        out, slack, _ = self.run_notify(enrichment(started="2026-08-24T20:00:00Z"))
        self.assertEqual([c[0] for c in slack.calls], ["post"])
        self.assertFalse(out["deduped"])

    def test_unconfigured_placeholder_does_not_block_real_post(self):
        self.run_notify(enrichment(), token="")
        self.assertEqual(self.ledger()["ts"], "unconfigured.000000")
        out, slack, _ = self.run_notify(enrichment())
        self.assertEqual([c[0] for c in slack.calls], ["post"])
        self.assertTrue(out["posted"])

    def test_firing_slack_failure_still_writes_ledger_and_stdout(self):
        out, _, err = self.run_notify(enrichment(), slack=FakeSlack(fail=True))
        self.assertFalse(out["posted"])
        self.assertIsNone(out["ts"])
        self.assertIn("non-fatal", err)
        rec = self.ledger()
        self.assertIsNotNone(rec)
        self.assertEqual(rec["first_seen"], STARTED)
        # the next repeat retries the post since no real ts was recorded
        out, slack, _ = self.run_notify(enrichment())
        self.assertTrue(out["posted"])

    def test_resolved_slack_failure_still_emits_json(self):
        self.run_notify(enrichment())
        out, slack, err = self.run_notify(enrichment(status="resolved"),
                                          slack=FakeSlack(fail=True))
        self.assertEqual(out["status"], "resolved")
        self.assertFalse(out["posted"])
        self.assertTrue(out["ledger_found"])
        self.assertEqual(out["downtime_seconds"], 3600)
        self.assertEqual([c[0] for c in slack.calls], ["update", "post"])
        self.assertIsNone(self.ledger())

    def test_resolved_updates_card_and_threads(self):
        self.run_notify(enrichment())
        out, slack, _ = self.run_notify(enrichment(status="resolved"))
        self.assertTrue(out["posted"])
        kinds = [(c[0], c[1].get("ts") or c[1].get("thread_ts")) for c in slack.calls]
        self.assertEqual(kinds, [("update", "1.000"), ("post", "1.000")])

    def test_resolved_with_corrupt_ledger_falls_back(self):
        self.vk.set(f"incident:{FP}", "{not json")
        out, slack, _ = self.run_notify(enrichment(status="resolved"))
        self.assertFalse(out["ledger_found"])
        self.assertEqual([c[0] for c in slack.calls], ["post"])
        self.assertNotIn("thread_ts", slack.calls[0][1])


if __name__ == "__main__":
    unittest.main()
