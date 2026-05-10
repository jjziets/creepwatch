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


class BackupArchiveNameTest(unittest.TestCase):
    def test_valid_backup_filename(self):
        self.assertTrue(
            mc_guard.BACKUP_ARCHIVE_RE.fullmatch("minecraft-20260510T120000Z.tar.gz")
        )

    def test_rejects_wrong_timestamp_or_suffix(self):
        self.assertIsNone(mc_guard.BACKUP_ARCHIVE_RE.fullmatch("minecraft-20260510120000Z.tar.gz"))
        self.assertIsNone(mc_guard.BACKUP_ARCHIVE_RE.fullmatch("minecraft-20260510T120000Z.zip"))
        self.assertIsNone(mc_guard.BACKUP_ARCHIVE_RE.fullmatch("other-20260510T120000Z.tar.gz"))


class RconCommandGateTest(unittest.TestCase):
    def test_mc_profile_name_accepts_vanilla_style(self):
        self.assertTrue(mc_guard.MC_PROFILE_NAME.match("Steve"))
        self.assertTrue(mc_guard.MC_PROFILE_NAME.match("x1"))

    def test_mc_profile_name_rejects_injection_chars(self):
        self.assertIsNone(mc_guard.MC_PROFILE_NAME.match("a;b"))
        self.assertIsNone(mc_guard.MC_PROFILE_NAME.match(""))

    def test_banip_target_allows_ipv4(self):
        self.assertTrue(mc_guard.BANIP_TARGET.match("192.168.0.1"))

    def test_gamerule_name(self):
        self.assertTrue(mc_guard.GAMERULE_NAME.match("doMobSpawning"))
        self.assertIsNone(mc_guard.GAMERULE_NAME.match("9bad"))


class TelegramAdminLockdownTest(unittest.TestCase):
    def test_private_listed_admin_accepted(self):
        self.assertTrue(
            mc_guard.telegram_allows_admin_interaction(
                chat_type="private", chat_id=12345, from_id=12345
            )
        )

    def test_private_non_listed_user_rejected(self):
        self.assertFalse(
            mc_guard.telegram_allows_admin_interaction(
                chat_type="private", chat_id=99999, from_id=99999
            )
        )

    def test_supergroup_rejected_even_if_user_id_matches(self):
        self.assertFalse(
            mc_guard.telegram_allows_admin_interaction(
                chat_type="supergroup", chat_id=-100111, from_id=12345
            )
        )

    def test_private_chat_id_mismatch_rejected(self):
        self.assertFalse(
            mc_guard.telegram_allows_admin_interaction(
                chat_type="private", chat_id=11111, from_id=12345
            )
        )

    def test_missing_from_rejected(self):
        self.assertFalse(
            mc_guard.telegram_allows_admin_interaction(
                chat_type="private", chat_id=12345, from_id=None
            )
        )


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


class SlotPickerTest(unittest.TestCase):
    """The 24h-gap slot picker is what stops same-day repeat /backup runs
    from evicting day-1 / day-2 anchors. Both backup retention (when R2
    isn't configured) and /restore <N> resolution call into it, so its
    contract has to be precise enough to test directly."""

    @staticmethod
    def _name(ts: str) -> str:
        # Helper: ts like "20260510T204650" → "minecraft-20260510T204650Z.tar.gz".
        return f"minecraft-{ts}Z.tar.gz"

    def test_picks_only_newest_when_all_same_day(self):
        names = [
            self._name("20260510T230000"),
            self._name("20260510T204650"),
            self._name("20260510T204113"),
            self._name("20260510T020314"),  # same UTC day, ~21h before slot 1
        ]
        slots = mc_guard.pick_slot_archives(names)
        self.assertEqual(slots, [self._name("20260510T230000")])

    def test_three_consecutive_days_each_fill_a_slot(self):
        names = [
            self._name("20260510T230000"),
            self._name("20260509T230000"),
            self._name("20260508T230000"),
            self._name("20260507T230000"),
        ]
        slots = mc_guard.pick_slot_archives(names)
        self.assertEqual(slots, names[:3])

    def test_skips_archives_inside_24h_window(self):
        # Slot 1 = May 10 23:00. Anything within the next 24h backwards is skipped
        # for slot 2; slot 2 anchors at the first archive ≥24h older.
        names = [
            self._name("20260510T230000"),  # slot 1
            self._name("20260510T120000"),  # 11h older — skipped
            self._name("20260509T220000"),  # 25h older — slot 2
            self._name("20260509T100000"),  # 13h older than slot 2 — skipped
            self._name("20260508T200000"),  # 26h older than slot 2 — slot 3
        ]
        slots = mc_guard.pick_slot_archives(names)
        self.assertEqual(
            slots,
            [
                self._name("20260510T230000"),
                self._name("20260509T220000"),
                self._name("20260508T200000"),
            ],
        )

    def test_unparseable_basenames_skipped(self):
        names = [
            "minecraft-INVALID.tar.gz",
            self._name("20260510T230000"),
            "garbage",
            self._name("20260509T100000"),  # ~37h older
        ]
        slots = mc_guard.pick_slot_archives(names)
        self.assertEqual(
            slots,
            [self._name("20260510T230000"), self._name("20260509T100000")],
        )

    def test_returns_empty_for_empty_input(self):
        self.assertEqual([], mc_guard.pick_slot_archives([]))

    def test_resolve_restore_slot_passes_through_filenames(self):
        bn = self._name("20260510T230000")
        # mc_guard.resolve_restore_slot does an is_file() check via
        # sorted_backup_basenames; a verbatim filename is returned without
        # a disk lookup as long as it matches BACKUP_ARCHIVE_RE.
        self.assertEqual(bn, mc_guard.resolve_restore_slot(bn))

    def test_resolve_restore_slot_returns_none_for_garbage(self):
        self.assertIsNone(mc_guard.resolve_restore_slot("not-a-spec"))


class ProgressBoardTest(unittest.TestCase):
    """The Telegram progress board has to be parser-tight and idempotent.

    A malformed PROGRESS line from the script must not crash mc-guard or
    move the board into a state the operator can't recognise; partial reads
    are routine because the script can be mid-write when we poll.
    """

    def setUp(self):
        import tempfile

        from progress_board import ProgressBoard, ProgressFileTail

        self.ProgressBoard = ProgressBoard
        self.ProgressFileTail = ProgressFileTail
        self._tmp = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        self.addCleanup(lambda: os.unlink(self._tmp.name))
        self._tmp.close()

    def _write_events(self, *lines: str) -> None:
        with open(self._tmp.name, "a", encoding="utf-8") as fh:
            for ln in lines:
                fh.write(ln)
                if not ln.endswith("\n"):
                    fh.write("\n")

    def test_initial_render_has_all_steps_pending(self):
        board = self.ProgressBoard("backup")
        text = board.render()
        self.assertIn("step 1 / 8", text)
        # All 8 backup steps default to ⏳ (pending) before any update.
        self.assertEqual(text.count("⏳"), 8)
        self.assertIn("`▱▱▱▱▱▱▱▱▱▱`", text)

    def test_tail_applies_step_updates_idempotently(self):
        board = self.ProgressBoard("backup")
        tail = self.ProgressFileTail(__import__("pathlib").Path(self._tmp.name), board)
        self._write_events(
            "PROGRESS\t1\trunning\tPreflight\t",
            "PROGRESS\t1\tok\tPreflight\t",
            "PROGRESS\t2\trunning\tSave-all flush\t",
        )
        self.assertTrue(tail.poll())
        # Re-poll without new content must be a no-op (returns False, state unchanged).
        self.assertFalse(tail.poll())
        text = board.render()
        self.assertIn("✅ Preflight", text)
        self.assertIn("🔄 Save-all flush", text)

    def test_tail_ignores_malformed_lines_and_partial_writes(self):
        board = self.ProgressBoard("backup")
        tail = self.ProgressFileTail(__import__("pathlib").Path(self._tmp.name), board)
        # Junk lines (e.g., a stray log line) and a partial event without
        # newline must not corrupt board state.
        self._write_events(
            "this is not a progress line",
            "PROGRESS\tnotanint\trunning\tx\ty",
        )
        with open(self._tmp.name, "a", encoding="utf-8") as fh:
            fh.write("PROGRESS\t1\trunn")  # partial, no newline
        tail.poll()
        # Step 1 must still be pending — the partial line cannot be applied.
        self.assertIn("⏳ Preflight", board.render())
        # Completing the partial line must apply on the next poll.
        with open(self._tmp.name, "a", encoding="utf-8") as fh:
            fh.write("ing\tPreflight\t\n")
        self.assertTrue(tail.poll())
        self.assertIn("🔄 Preflight", board.render())

    def test_detail_renders_in_backticks(self):
        board = self.ProgressBoard("backup")
        board.update(4, "running", "Compress world", "minecraft-20260510T204650Z.tar.gz")
        text = board.render()
        self.assertIn("`minecraft-20260510T204650Z.tar.gz`", text)
        self.assertIn("🔄 Compress world", text)

    def test_mark_done_reflects_failure_step(self):
        board = self.ProgressBoard("restore")
        board.update(1, "ok", "Safety snapshot of current world")
        board.update(2, "fail", "Stop Minecraft", "compose stop failed")
        board.mark_done(False)
        text = board.render()
        self.assertIn("❌ failed at step 2", text)
        self.assertIn("`compose stop failed`", text)


if __name__ == "__main__":
    unittest.main()
