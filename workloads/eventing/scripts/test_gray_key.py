import json
import unittest

import fakeredis

import gray_key


class TestGrayKey(unittest.TestCase):
    def test_start_sets_key_json_and_ttl(self):
        vk = fakeredis.FakeRedis(decode_responses=True)
        key, payload = gray_key.apply(vk, "ring-e-i20e", "start",
                                      duration=180, rx_offset=8.0,
                                      err_rate=120, now=1000)
        self.assertEqual(key, "gray:ring-e-i20e")
        stored = json.loads(vk.get(key))
        self.assertEqual(stored["start_ts"], 1000)
        self.assertEqual(stored["duration_s"], 180)
        self.assertEqual(stored["peak_rx_offset_dbm"], 8.0)
        self.assertEqual(stored["peak_errors_per_sec"], 120)
        ttl = vk.ttl(key)
        self.assertTrue(0 < ttl <= 210)  # duration + 30
        self.assertEqual(payload["start_ts"], 1000)

    def test_end_deletes_key(self):
        vk = fakeredis.FakeRedis(decode_responses=True)
        vk.set("gray:ring-e-i20e", "x")
        key, payload = gray_key.apply(vk, "ring-e-i20e", "end")
        self.assertEqual(key, "gray:ring-e-i20e")
        self.assertIsNone(payload)
        self.assertIsNone(vk.get("gray:ring-e-i20e"))

    def test_bad_link_rejected(self):
        with self.assertRaises(ValueError):
            gray_key.validate_link("ring-e-i20e; rm -rf")
        self.assertEqual(gray_key.validate_link("ring-e-i20e"), "ring-e-i20e")


if __name__ == "__main__":
    unittest.main()
