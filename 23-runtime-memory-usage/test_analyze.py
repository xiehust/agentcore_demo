"""Metering tests based on the observed AWS log structure (sanitized)."""
import json
import unittest

import analyze


def event(elapsed=1.0, memory=0.0003):
    return {"timestamp": 1789011626195, "ingestionTime": 1789012685809,
            "message": json.dumps({"event_timestamp": 1789011626195,
                "attributes": {"session.id": "test", "time_elapsed_seconds": elapsed},
                "metrics": {"agent.runtime.memory.gb_hours.used": memory}})}


class AnalysisTests(unittest.TestCase):
    def test_interval_conversion_and_deduplication(self):
        records, errors = analyze.parse_events([event(), event()])
        self.assertEqual(errors, [])
        self.assertEqual(len(records), 1)
        self.assertAlmostEqual(records[0]["memory_gb_equivalent"], 1.08)
        self.assertGreater(records[0]["delivery_delay_seconds"], 1000)

    def test_rounded_zero_interval_keeps_usage_without_gauge(self):
        records, errors = analyze.parse_events([event(elapsed=0, memory=1.184372724e-6)])
        self.assertEqual(errors, [])
        self.assertIsNone(records[0]["memory_gb_equivalent"])
        self.assertGreater(records[0]["memory_gb_hours"], 0)
        self.assertIsNone(analyze.stats([None]))

    def test_schema_drift_is_reported(self):
        records, errors = analyze.parse_events([{"message": "{}", "eventId": "bad"}])
        self.assertEqual(records, [])
        self.assertEqual(len(errors), 1)

    def test_timestamp_disagreement_is_not_silently_aligned(self):
        record = event()
        record["timestamp"] += 5000
        records, errors = analyze.parse_events([record])
        self.assertEqual(records, [])
        self.assertEqual(len(errors), 1)


if __name__ == "__main__":
    unittest.main()
