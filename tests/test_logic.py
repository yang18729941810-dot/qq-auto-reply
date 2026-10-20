# -*- coding: utf-8 -*-
"""离线单元测试：只测纯逻辑，不碰 QQ、不碰真实配置。

    python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import auto_reply as ar          # noqa: E402
import qq_friends as friends     # noqa: E402


class NameMatchTest(unittest.TestCase):
    def test_exact(self):
        self.assertTrue(ar.name_matches("小明", "小明"))

    def test_spaces_and_zero_width(self):
        self.assertTrue(ar.name_matches("小 明", "小明"))
        self.assertTrue(ar.name_matches("小明\u200b", "小明"))
        self.assertTrue(ar.name_matches("小　明", "小明"))      # 全角空格

    def test_no_substring_match(self):
        self.assertFalse(ar.name_matches("牛马一家亲", "牛"))
        self.assertFalse(ar.name_matches("小明同学", "小明"))

    def test_empty(self):
        self.assertFalse(ar.name_matches("", "小明"))
        self.assertFalse(ar.name_matches("小明", ""))


class AccountNameTest(unittest.TestCase):
    def test_loose_match(self):
        self.assertTrue(friends.name_matches_loose("小 明", "小明"))
        self.assertTrue(friends.name_matches_loose("小明\u200b", "小明"))

    def test_no_substring(self):
        self.assertFalse(friends.name_matches_loose("牛马一家亲", "牛"))

    def test_empty(self):
        self.assertFalse(friends.name_matches_loose("", "小明"))
        self.assertFalse(friends.name_matches_loose("小明", ""))


class SelectionTest(unittest.TestCase):
    ROWS = [{"name": n} for n in ("小明", "小红", "小刚", "某群")]

    def sel(self, raw):
        return friends._parse_selection(raw, self.ROWS)

    def test_numbers(self):
        self.assertEqual(self.sel("1 3"), (["小明", "小刚"], []))

    def test_ranges(self):
        self.assertEqual(self.sel("1-3"), (["小明", "小红", "小刚"], []))

    def test_all_and_none(self):
        self.assertEqual(self.sel("all"), (["小明", "小红", "小刚", "某群"], []))
        self.assertEqual(self.sel("none"), ([], []))

    def test_empty_keeps(self):
        self.assertEqual(self.sel("   "), ([], []))

    def test_out_of_range_ignored(self):
        self.assertEqual(self.sel("9"), ([], []))

    def test_names(self):
        self.assertEqual(self.sel("小明 某群"), (["小明", "某群"], []))

    def test_unknown_name_reported(self):
        self.assertEqual(self.sel("查无此人"), ([], ["查无此人"]))


SAMPLE_CONFIG = """{
  "_说明_targets": "注释键，改名单时不能被吃掉",
  "target": "小明",
  "targets": [ "小明" ],
  "listen_all": false,
  "api_key": "sk-test",
  "system_prompt_lines": [ "第一行", "第二行" ]
}
"""


class ConfigWriteTest(unittest.TestCase):
    """set_config_list 只替换目标数组，其它字段和注释原样保留。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qqar-test-")
        self.old = (friends.CONFIG_PATH, friends.EXAMPLE_PATH)
        friends.CONFIG_PATH = os.path.join(self.tmp, "auto_reply_config.json")
        friends.EXAMPLE_PATH = os.path.join(self.tmp, "auto_reply_config.example.json")
        with open(friends.CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(SAMPLE_CONFIG)

    def tearDown(self):
        friends.CONFIG_PATH, friends.EXAMPLE_PATH = self.old

    def test_replaces_only_targets(self):
        friends.set_config_list("targets", ["小明", "小红"])
        with open(friends.CONFIG_PATH, encoding="utf-8") as f:
            text = f.read()
        cfg = json.loads(text)
        self.assertEqual(cfg["targets"], ["小明", "小红"])
        self.assertEqual(cfg["api_key"], "sk-test")
        self.assertEqual(cfg["system_prompt_lines"], ["第一行", "第二行"])
        self.assertFalse(cfg["listen_all"])
        self.assertIn("_说明_targets", cfg)
        self.assertIn("注释键，改名单时不能被吃掉", text)

    def test_bootstrap_from_example(self):
        os.remove(friends.CONFIG_PATH)
        with open(friends.EXAMPLE_PATH, "w", encoding="utf-8") as f:
            f.write(SAMPLE_CONFIG)
        friends.ensure_config()
        self.assertTrue(os.path.exists(friends.CONFIG_PATH))
        self.assertEqual(friends.config_list("targets"), ["小明"])

    def test_dedupe(self):
        friends.set_config_list("targets", ["小明", "小明", " 小红 ", ""])
        self.assertEqual(friends.config_list("targets"), ["小明", "小红"])


if __name__ == "__main__":
    unittest.main()
