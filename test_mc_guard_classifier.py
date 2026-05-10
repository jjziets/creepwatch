import importlib
import logging
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ADMIN_CHAT_IDS", "12345")

mc_guard = importlib.import_module("mc_guard")


class MarkdownEscapeTest(unittest.TestCase):
    def test_player_name_with_markdown_metacharacters(self):
        s = mc_guard.md_escape("Steve_*Master*")
        self.assertIn("\\_", s)
        self.assertIn("\\*", s)

    def test_brackets_and_backtick_escaped(self):
        self.assertEqual("\\[x\\]", mc_guard.md_escape("[x]"))
        self.assertIn("\\`", mc_guard.md_escape("a`b"))


class SendFailureLogTest(unittest.TestCase):
    def test_send_logs_when_telegram_returns_not_ok(self):
        mock_r = MagicMock()
        mock_r.ok = True
        mock_r.json.return_value = {"ok": False, "description": "Bad Request: can't parse entities"}
        with patch.object(mc_guard.requests, "post", return_value=mock_r):
            with self.assertLogs(mc_guard.log, level=logging.WARNING) as cm:
                mc_guard.send(1, "hello")
        self.assertTrue(any("sendMessage" in x for x in cm.output))


class ChatBridgeTest(unittest.TestCase):
    def test_extract_chat_from_player_log_line(self):
        line = "[19:12:01] [Server thread/INFO]: <CraftyTm> hello admins"

        self.assertEqual(("CraftyTm", "hello admins"), mc_guard.extract_chat(line))

    def test_extract_chat_ignores_join_lines(self):
        line = "[19:12:01] [Server thread/INFO]: CraftyTm joined the game"

        self.assertIsNone(mc_guard.extract_chat(line))

    def test_player_chat_message_escapes_markdown_for_telegram(self):
        text = mc_guard.format_player_chat_for_telegram("CraftyTm", "hello *admins* `now`")

        self.assertEqual("💬 *CraftyTm*: hello \\*admins\\* \\`now\\`", text)

    def test_admin_message_builds_tellraw_command(self):
        command = mc_guard.build_admin_tellraw_command("Hannes", "hello\nplayers")

        self.assertTrue(command.startswith("tellraw @a "))
        self.assertIn("[Admin] ", command)
        self.assertIn("Hannes: ", command)
        self.assertIn("hello players", command)


class ErrorClassifierTest(unittest.TestCase):
    def test_large_dripstone_far_chunk_is_suppressed_noise(self):
        err = (
            "Detected setBlock in a far chunk [-104, 60], "
            "pos: BlockPos{x=-1649, y=56, z=969}, "
            "status: minecraft:features, currently generating: "
            "ResourceKey[minecraft:worldgen/placed_feature / minecraft:large_dripstone]"
        )

        event = mc_guard.classify_error(err)

        self.assertFalse(event.alert)
        self.assertEqual("worldgen_far_chunk", event.kind)

    def test_disconnect_packet_error_is_suppressed_noise(self):
        event = mc_guard.classify_error("Error sending packet clientbound/minecraft:disconnect")

        self.assertFalse(event.alert)
        self.assertEqual("disconnect_packet", event.kind)

    def test_positions_normalize_to_same_signature(self):
        first = mc_guard.classify_error(
            "Detected setBlock in a far chunk [-104, 60], "
            "pos: BlockPos{x=-1649, y=56, z=969}, status: minecraft:features, "
            "currently generating: ResourceKey[minecraft:worldgen/placed_feature / minecraft:large_dripstone]"
        )
        second = mc_guard.classify_error(
            "Detected setBlock in a far chunk [-104, 60], "
            "pos: BlockPos{x=-1649, y=57, z=971}, status: minecraft:features, "
            "currently generating: ResourceKey[minecraft:worldgen/placed_feature / minecraft:large_dripstone]"
        )

        self.assertEqual(first.signature, second.signature)

    def test_unrecognized_error_still_alerts(self):
        event = mc_guard.classify_error("Crash report saved to: /data/crash-reports/crash.txt")

        self.assertTrue(event.alert)
        self.assertEqual("error", event.kind)


if __name__ == "__main__":
    unittest.main()
