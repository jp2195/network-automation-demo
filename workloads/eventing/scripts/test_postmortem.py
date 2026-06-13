"""Unit tests for postmortem.

Run: cd workloads/eventing/scripts && \
     uv run --quiet --with fakeredis --with valkey python3 -m unittest test_postmortem -v
"""

import json
import os
import unittest
from unittest import mock

import fakeredis

from constants import AI_ANALYSIS_MARKER, POSTMORTEM_KEY_PREFIX, POSTMORTEM_TTL_SECONDS
import postmortem
from postmortem import (
    build_markdown,
    extract_ai_analysis,
    store,
)

# Shapes mirror the live pipeline (smoke-verified during features 1+2):
# enrich.py output, analyze_impact.py output, notify.py resolved stdout.
ENRICHMENT = {
    "alert": {
        "name": "SRLInterfaceOperDown",
        "severity": "critical",
        "corridor": "i-285",
        "link_id": "hubi20e-fci20e",
        "started": "2026-06-11T17:01:02Z",
        "ended": "2026-06-11T17:03:15Z",
        "status": "resolved",
        "fingerprint": "abc123def456",
    },
    "device": {"name": "hub-i20e", "role": "corridor-hub", "site": "Eastfield",
               "site_slug": "eastfield"},
    "interface": {"name": "ethernet-1/4", "type": "10gbase-x-sfpp",
                  "description": "to fc-i20e"},
    "cable": {
        "id": 7, "label": "FOC-HUBI20E-FCI20E", "status": "connected",
        "custom_fields": {"restoration_sla_hours": 4},
        "owner": {"name": "Apex Fiber Networks"},
        "site_group": {"slug": "i-285"},
    },
    "degraded": [],
}

IMPACT = {
    "affected_device": "hub-i20e",
    "site_slug": "eastfield",
    "downstream_devices": [
        {"device": "fc-i20e", "interface": "eth1", "cable_label": "FOC-HUBI20E-FCI20E"},
        {"device": "hub-e", "interface": "ethernet-1/2", "cable_label": "FOC-RING-EI20E"},
    ],
    "affected_agencies": ["adot-region-7", "eastfield-county-dot"],
    "severity_class": "high",
}

NOTIFY = {
    "posted": False,
    "status": "resolved",
    "downtime_seconds": 133,
    "first_seen": "2026-06-11T17:01:02Z",
    "fingerprint": "abc123def456",
}

SERIES = [{
    "metric": {"node": "hub-i20e", "interface": "ethernet-1/4",
               "link_id": "hubi20e-fci20e"},
    "values": [[1781456460, "1"], [1781456490, "2"], [1781456580, "2"],
               [1781456610, "1"]],
}]

ANALYSIS = {
    "fingerprint": "abc123def456",
    "summary": "Fiber cut isolated fc-i20e until restoration.",
    "probable_root_cause": "physical layer fault on FOC-HUBI20E-FCI20E",
    "evidence": [{"source": "prometheus",
                  "query": "link:oper_state_with_meta",
                  "observation": "link down 17:01-17:03"}],
    "recommendation": "inspect splice case at I-20E mile 12",
    "confidence": 0.8,
}


def md(**overrides):
    kwargs = dict(enrichment=ENRICHMENT, impact=IMPACT, notify_result=NOTIFY,
                  series=(), log_lines=(), analysis=None,
                  generated_at="2026-06-11T17:03:20+00:00")
    kwargs.update(overrides)
    return build_markdown(**kwargs)


class TestBuildMarkdown(unittest.TestCase):
    def test_core_sections_present(self):
        out = md()
        self.assertIn("# Postmortem — SRLInterfaceOperDown on hub-i20e", out)
        self.assertIn("`abc123def456`", out)
        self.assertIn("## Timeline", out)
        self.assertIn("2026-06-11T17:01:02Z", out)
        self.assertIn("2026-06-11T17:03:15Z", out)
        self.assertIn("**Duration:** 2m 13s", out)
        self.assertIn("## Alert", out)
        self.assertIn("## Impact", out)
        self.assertIn("| fc-i20e | eth1 | FOC-HUBI20E-FCI20E |", out)
        self.assertIn("adot-region-7", out)
        self.assertIn("## SLA", out)

    def test_deterministic(self):
        self.assertEqual(md(), md())

    def test_sla_within(self):
        out = md()
        self.assertIn("**Restoration SLA:** 4h", out)
        self.assertIn("✅ within SLA", out)

    def test_sla_breach(self):
        notify = dict(NOTIFY, downtime_seconds=5 * 3600)
        out = md(notify_result=notify)
        self.assertIn("❌ SLA BREACH", out)
        self.assertIn("exceeded by 1h 0m", out)

    def test_sla_absent_omits_section(self):
        enrichment = json.loads(json.dumps(ENRICHMENT))
        del enrichment["cable"]["custom_fields"]["restoration_sla_hours"]
        out = md(enrichment=enrichment)
        self.assertNotIn("## SLA", out)

    def test_no_downstream(self):
        impact = dict(IMPACT, downstream_devices=[], affected_agencies=[])
        out = md(impact=impact)
        self.assertIn("ring redundancy held", out)
        self.assertIn("**Agencies affected:** none", out)

    def test_telemetry_transitions(self):
        out = md(series=SERIES)
        self.assertIn("## Link-state telemetry", out)
        self.assertIn("`hub-i20e:ethernet-1/4`", out)
        # 4 samples but only 3 state changes: 1 → 2 → 1
        self.assertIn("UP → ", out)
        self.assertIn("DOWN → ", out)

    def test_telemetry_omitted_without_series(self):
        self.assertNotIn("## Link-state telemetry", md())

    def test_logs_section(self):
        lines = [(1781456500_000000000, "hub-i20e: admin-state disable committed")]
        out = md(log_lines=lines)
        self.assertIn("## Device log excerpts", out)
        self.assertIn("admin-state disable", out)

    def test_ai_section_present(self):
        out = md(analysis=ANALYSIS)
        self.assertIn("## Analyst narrative (AI)", out)
        self.assertIn("Fiber cut isolated fc-i20e", out)
        self.assertIn("**Probable root cause:**", out)
        self.assertIn("| prometheus |", out)

    def test_ai_section_omitted_when_absent(self):
        self.assertNotIn("## Analyst narrative", md(analysis=None))


class TestExtractAIAnalysis(unittest.TestCase):
    def test_picks_latest_valid(self):
        old = (1, f"x {AI_ANALYSIS_MARKER} " + json.dumps(dict(ANALYSIS, summary="old")))
        new = (2, f"x {AI_ANALYSIS_MARKER} " + json.dumps(ANALYSIS))
        got = extract_ai_analysis([old, new])
        self.assertEqual(got["summary"], ANALYSIS["summary"])

    def test_tolerates_garbage(self):
        lines = [(1, "no marker here"),
                 (2, f"{AI_ANALYSIS_MARKER} not-json{{"),
                 (3, "")]
        self.assertIsNone(extract_ai_analysis(lines))

    def test_empty(self):
        self.assertIsNone(extract_ai_analysis([]))


class TestStore(unittest.TestCase):
    def test_sets_key_with_ttl(self):
        client = fakeredis.FakeRedis(decode_responses=True)
        store(client, "abc123def456", "# Postmortem\n")
        key = POSTMORTEM_KEY_PREFIX + "abc123def456"
        self.assertEqual(client.get(key), "# Postmortem\n")
        ttl = client.ttl(key)
        self.assertTrue(0 < ttl <= POSTMORTEM_TTL_SECONDS)

    def test_degraded_never_clobbers_existing(self):
        client = fakeredis.FakeRedis(decode_responses=True)
        self.assertTrue(store(client, "fp1", "# good\n"))
        self.assertFalse(store(client, "fp1", "# degraded\n", degraded=True))
        self.assertEqual(client.get(POSTMORTEM_KEY_PREFIX + "fp1"), "# good\n")
        # degraded still stores when nothing exists yet
        self.assertTrue(store(client, "fp2", "# degraded\n", degraded=True))
        self.assertEqual(client.get(POSTMORTEM_KEY_PREFIX + "fp2"),
                         "# degraded\n")
        # a non-degraded report may overwrite (fresh real incident)
        self.assertTrue(store(client, "fp1", "# newer good\n"))
        self.assertEqual(client.get(POSTMORTEM_KEY_PREFIX + "fp1"),
                         "# newer good\n")


class TestMainGate(unittest.TestCase):
    def test_firing_is_noop(self):
        # No VALKEY_URL/PROM_URL/LOKI_URL set: reaching any of them would
        # raise, so returning cleanly proves the status gate short-circuits.
        env = {
            "ENRICHMENT_JSON": json.dumps(ENRICHMENT),
            "IMPACT_JSON": json.dumps(IMPACT),
            "NOTIFY_JSON": json.dumps(dict(NOTIFY, status="firing")),
        }
        with mock.patch.dict(os.environ, env, clear=False):
            postmortem.main()  # must not raise


if __name__ == "__main__":
    unittest.main()
