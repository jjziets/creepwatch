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


class HiddenSwordCommandTest(unittest.TestCase):
    """/sword (alias /sw) gives a target player a fully-kitted diamond
    sword. Admin-only via the dispatcher gate, deliberately omitted
    from /help.

    Strict-checks the encoded enchantment component against the exact
    fragments we want, so a future rename (e.g. sweeping_edge flipping
    back, or a Sharpness cap change) raises a red flag in CI instead of
    failing silently in-game with `Unknown enchantment`.
    """

    def test_help_text_does_not_mention_sword_command(self):
        body = mc_guard.HELP_TEXT.lower()
        self.assertNotIn("/sword", body)
        self.assertNotIn(" /sw ", body)
        self.assertFalse(body.rstrip().endswith("/sw"), "trailing '/sw' would reveal the alias")

    def test_dispatcher_invokes_cmd_sword(self):
        with patch.object(mc_guard, "cmd_sword") as spy:
            mc_guard.handle_command(1, "/sword Steve", "Op")
            spy.assert_called_once_with(1, "Steve", "Op")

    def test_dispatcher_invokes_cmd_sword_via_alias(self):
        with patch.object(mc_guard, "cmd_sword") as spy:
            mc_guard.handle_command(1, "/sw Alex", "Op")
            spy.assert_called_once_with(1, "Alex", "Op")

    def test_base_item_is_netherite_sword(self):
        # Netherite is the top melee tier — chosen to make this hidden
        # admin "god roll" command feel actually god-like in play.
        self.assertEqual("netherite_sword", mc_guard.SWORD_BASE_ITEM)

    def test_enchantment_component_contains_expected_set(self):
        comp = mc_guard.SWORD_ENCHANT_COMPONENT
        self.assertIn('"minecraft:sharpness":5', comp)
        self.assertIn('"minecraft:mending":1', comp)
        self.assertIn('"minecraft:unbreaking":3', comp)
        self.assertIn('"minecraft:looting":3', comp)
        self.assertIn('"minecraft:sweeping_edge":3', comp)
        self.assertTrue(comp.startswith("[minecraft:enchantments={"))
        self.assertTrue(comp.endswith("}]"))


class HiddenMansionCommandTest(unittest.TestCase):
    """/mansion spawns a woodland mansion 50 blocks NE of the target player.
    Admin-only, hidden from /help — same regression guards as /ship."""

    def test_help_text_does_not_mention_mansion_command(self):
        self.assertNotIn("/mansion", mc_guard.HELP_TEXT.lower())

    def test_dispatcher_invokes_cmd_mansion(self):
        with patch.object(mc_guard, "cmd_mansion") as spy:
            mc_guard.handle_command(1, "/mansion Steve", "Op")
            spy.assert_called_once_with(1, "Steve", "Op")

    def test_structure_id_is_mansion(self):
        # `minecraft:mansion` is the modern (1.18+) registry id. Older
        # versions used `woodland_mansion`; pin to the current literal.
        self.assertEqual("minecraft:mansion", mc_guard.MANSION_STRUCTURE)


class HiddenHelpMenuTest(unittest.TestCase):
    """/h_h (alias /hh) sends HIDDEN_HELP_TEXT — a cheat-sheet of every
    admin-only hidden command. The menu itself must NOT appear in /help
    (so non-admins don't discover the existence of hidden tools), and
    the body must be Markdown-balanced just like HELP_TEXT — Telegram
    rejects the whole message with HTTP 400 on a single unbalanced
    entity, and that exact failure mode bit us in PR #18."""

    def test_h_h_not_in_help_text(self):
        body = mc_guard.HELP_TEXT.lower()
        self.assertNotIn("/h_h", body)
        self.assertNotIn("/h\\_h", body)
        self.assertNotIn("/hh", body)

    def test_dispatcher_routes_h_h(self):
        with patch.object(mc_guard, "cmd_hidden_help") as spy:
            mc_guard.handle_command(1, "/h_h", "Op")
            spy.assert_called_once_with(1)

    def test_dispatcher_does_NOT_route_hh(self):
        # `/hh` is too easy to type by accident; the underscore on /h_h
        # is the discoverability barrier. Pin this so a future "add a
        # short alias for convenience" PR doesn't quietly re-add it.
        with patch.object(mc_guard, "cmd_hidden_help") as spy, \
             patch.object(mc_guard, "send"):
            mc_guard.handle_command(1, "/hh", "Op")
            spy.assert_not_called()

    def test_hidden_help_text_does_not_advertise_hh(self):
        self.assertNotIn("/hh", mc_guard.HIDDEN_HELP_TEXT)

    def test_hidden_help_text_has_balanced_code_spans(self):
        ticks = mc_guard.HIDDEN_HELP_TEXT.count("`")
        self.assertEqual(
            ticks % 2, 0,
            f"HIDDEN_HELP_TEXT has {ticks} backticks (odd parity) — "
            "Telegram will reject /h_h with HTTP 400.",
        )

    def test_hidden_help_text_has_balanced_bold(self):
        stars = mc_guard.HIDDEN_HELP_TEXT.count("*")
        self.assertEqual(
            stars % 2, 0,
            f"HIDDEN_HELP_TEXT has {stars} asterisks (odd parity) — "
            "bold spans unbalanced.",
        )

    def test_hidden_help_text_has_balanced_brackets(self):
        self.assertEqual(
            mc_guard.HIDDEN_HELP_TEXT.count("["),
            mc_guard.HIDDEN_HELP_TEXT.count("]"),
        )

    def test_hidden_help_text_lists_every_hidden_command(self):
        """If we add a new hidden command but forget to advertise it in
        the cheat-sheet, that's a real regression — admins won't know
        the new command exists. Pin the list."""
        expected = (
            "/sword", "/pickaxe", "/trident", "/bow", "/elytra",
            "/chestplate", "/leggings", "/boots", "/ts", "/totem",
            "/give", "/items",
            "/ship", "/mansion", "/buried", "/ruin", "/monument",
            "/igloo", "/portal", "/villager",
            "/spawn", "/warden", "/mobs",
        )
        for cmd in expected:
            self.assertIn(cmd, mc_guard.HIDDEN_HELP_TEXT,
                          f"{cmd} missing from HIDDEN_HELP_TEXT")


class HiddenGearBatchTest(unittest.TestCase):
    """The gear batch (bow, elytra, chestplate, leggings, boots, totem)
    rides on top of _give_enchanted_item and follows the same regression
    contract as /sword and /pickaxe: each command is dispatcher-routed
    on its short and (optionally) alias forms, absent from /help, and
    the base item literal is pinned so a future Minecraft rename doesn't
    silently turn it into a bare item. Component contents are spot-checked
    only where the enchant choice matters (Infinity vs Mending on bow,
    Swift Sneak presence on leggings, totem has empty component)."""

    CASES = (
        # (full_cmd, alias_or_None, cmd_fn_attr, base_item_attr, expected_base)
        ("/bow",        "/bw", "cmd_bow",        "BOW_BASE_ITEM",        "bow"),
        ("/elytra",     "/el", "cmd_elytra",     "ELYTRA_BASE_ITEM",     "elytra"),
        ("/chestplate", "/cp", "cmd_chestplate", "CHESTPLATE_BASE_ITEM", "netherite_chestplate"),
        ("/leggings",   None,  "cmd_leggings",   "LEGGINGS_BASE_ITEM",   "netherite_leggings"),
        ("/boots",      "/bt", "cmd_boots",      "BOOTS_BASE_ITEM",      "netherite_boots"),
        ("/totem",      None,  "cmd_totem",      "TOTEM_BASE_ITEM",      "totem_of_undying"),
    )

    def test_no_gear_command_leaks_into_help(self):
        body = mc_guard.HELP_TEXT.lower()
        for full_cmd, alias, _fn, _const, _base in self.CASES:
            self.assertNotIn(full_cmd, body, f"{full_cmd} must not appear in HELP_TEXT")
            if alias is not None:
                self.assertNotIn(f" {alias} ", body)
                self.assertFalse(body.rstrip().endswith(alias), f"trailing {alias} would reveal the alias")

    def test_dispatcher_routes_each_full_command(self):
        for full_cmd, _alias, fn_attr, _const, _base in self.CASES:
            with self.subTest(cmd=full_cmd):
                with patch.object(mc_guard, fn_attr) as spy:
                    mc_guard.handle_command(1, f"{full_cmd} Steve", "Op")
                    spy.assert_called_once_with(1, "Steve", "Op")

    def test_dispatcher_routes_each_alias(self):
        for full_cmd, alias, fn_attr, _const, _base in self.CASES:
            if alias is None:
                continue
            with self.subTest(cmd=alias):
                with patch.object(mc_guard, fn_attr) as spy:
                    mc_guard.handle_command(1, f"{alias} Steve", "Op")
                    spy.assert_called_once_with(1, "Steve", "Op")

    def test_base_items_are_pinned(self):
        for full_cmd, _alias, _fn, const_attr, expected in self.CASES:
            with self.subTest(cmd=full_cmd):
                self.assertEqual(
                    expected,
                    getattr(mc_guard, const_attr),
                    f"{const_attr} drifted from {expected!r}",
                )

    def test_bow_uses_infinity_not_mending(self):
        # Mending is mutex with Infinity. The product call was Infinity.
        # If a future edit drops Infinity (or adds Mending), this fails.
        comp = mc_guard.BOW_ENCHANT_COMPONENT
        self.assertIn('"minecraft:infinity":1', comp)
        self.assertNotIn("mending", comp)

    def test_leggings_carry_swift_sneak(self):
        # Swift Sneak is treasure-only; without it the leggings are
        # just a tier-IV protection set. Worth pinning.
        self.assertIn('"minecraft:swift_sneak":3', mc_guard.LEGGINGS_ENCHANT_COMPONENT)

    def test_boots_carry_full_movement_set(self):
        comp = mc_guard.BOOTS_ENCHANT_COMPONENT
        for enchant in (
            "feather_falling", "depth_strider", "soul_speed",
            "protection", "unbreaking", "mending",
        ):
            self.assertIn(f"minecraft:{enchant}", comp)

    def test_totem_component_is_empty(self):
        # Totems take no enchants. An empty component means the give
        # command is `give <player> totem_of_undying 1` — exactly what
        # we want.
        self.assertEqual("", mc_guard.TOTEM_ENCHANT_COMPONENT)


class HiddenStructureBatchTest(unittest.TestCase):
    """The five additional /place-structure commands (/buried /ruin
    /monument /igloo /portal) plus the refactored /ship and /mansion
    all share _place_structure_near_player. Each gets the same triad
    of regression guards: dispatcher routes, token absent from /help,
    and the structure registry id is pinned to its expected literal.
    A future Mojang rename would silently turn the command into
    "Could not place" at runtime; these tests catch it in CI instead.
    """

    CASES = (
        # (cmd_text, cmd_fn_attr, structure_const_attr, expected_id)
        ("/buried",   "cmd_buried",   "BURIED_TREASURE_STRUCTURE", "minecraft:buried_treasure"),
        ("/ruin",     "cmd_ruin",     "OCEAN_RUIN_STRUCTURE",      "minecraft:ocean_ruin_warm"),
        ("/monument", "cmd_monument", "MONUMENT_STRUCTURE",        "minecraft:monument"),
        ("/igloo",    "cmd_igloo",    "IGLOO_STRUCTURE",           "minecraft:igloo"),
        ("/portal",   "cmd_portal",   "RUINED_PORTAL_STRUCTURE",   "minecraft:ruined_portal"),
    )

    def test_no_new_structure_command_leaks_into_help(self):
        body = mc_guard.HELP_TEXT.lower()
        for cmd_text, _fn, _const, _id in self.CASES:
            self.assertNotIn(cmd_text, body, f"{cmd_text} must not appear in HELP_TEXT")

    def test_dispatcher_routes_each_command(self):
        for cmd_text, fn_attr, _const, _id in self.CASES:
            with self.subTest(cmd=cmd_text):
                with patch.object(mc_guard, fn_attr) as spy:
                    mc_guard.handle_command(1, f"{cmd_text} Steve", "Op")
                    spy.assert_called_once_with(1, "Steve", "Op")

    def test_structure_registry_ids_are_pinned(self):
        for cmd_text, _fn, const_attr, expected_id in self.CASES:
            with self.subTest(cmd=cmd_text):
                self.assertEqual(
                    expected_id,
                    getattr(mc_guard, const_attr),
                    f"{const_attr} drifted from {expected_id!r}",
                )

    def test_place_command_projects_onto_world_surface(self):
        """Without `positioned over world_surface`, a flying player gets a
        floating-in-the-sky structure at their current altitude. This pins
        the projection so a future refactor can't silently drop it."""
        # Mock both rcon (we don't actually want to send to a Minecraft server)
        # and send (to avoid Telegram round-trips). Then assert that the rcon
        # invocation carries the projection clause exactly once.
        with patch.object(mc_guard, "rcon", return_value="ok") as rcon_spy, \
             patch.object(mc_guard, "send"):
            mc_guard.cmd_ship(1, "Steve", "Op")
        rcon_spy.assert_called_once()
        cmd_sent = rcon_spy.call_args.args[0]
        self.assertIn("positioned over world_surface", cmd_sent)
        # And the structure id + offset are still part of the command.
        self.assertIn("place structure minecraft:shipwreck_beached", cmd_sent)
        self.assertIn("~3 ~ ~3", cmd_sent)


class HiddenShipCommandTest(unittest.TestCase):
    """/ship spawns a beached shipwreck structure near a target player.
    Admin-only via the dispatcher, omitted from /help. Same regression
    guards as the other hidden commands."""

    def test_help_text_does_not_mention_ship_command(self):
        self.assertNotIn("/ship", mc_guard.HELP_TEXT.lower())

    def test_dispatcher_invokes_cmd_ship(self):
        with patch.object(mc_guard, "cmd_ship") as spy:
            mc_guard.handle_command(1, "/ship Steve", "Op")
            spy.assert_called_once_with(1, "Steve", "Op")

    def test_structure_id_is_beached_shipwreck(self):
        # If Mojang ever renames the registry id, vanilla `place structure`
        # silently returns "Could not place"; pin the expected literal.
        self.assertEqual("minecraft:shipwreck_beached", mc_guard.SHIPWRECK_STRUCTURE)


class HiddenTurtleShellCommandTest(unittest.TestCase):
    """/ts gives a target player a fully-kitted turtle-shell helmet
    (Respiration III · Aqua Affinity · Protection IV · Unbreaking III ·
    Mending). Admin-only via the existing dispatcher gate, omitted
    from /help. Same regression guards as /sword and /pickaxe."""

    def test_help_text_does_not_mention_ts_command(self):
        body = mc_guard.HELP_TEXT.lower()
        self.assertNotIn("/ts", body)

    def test_dispatcher_invokes_cmd_turtle_shell(self):
        with patch.object(mc_guard, "cmd_turtle_shell") as spy:
            mc_guard.handle_command(1, "/ts Steve", "Op")
            spy.assert_called_once_with(1, "Steve", "Op")

    def test_base_item_is_turtle_helmet(self):
        # Turtle shell *helmet* is the vanilla item id, not "turtle_shell".
        self.assertEqual("turtle_helmet", mc_guard.TURTLE_SHELL_BASE_ITEM)

    def test_component_contains_expected_enchantment_set(self):
        comp = mc_guard.TURTLE_SHELL_COMPONENT
        self.assertIn('"minecraft:respiration":3', comp)
        self.assertIn('"minecraft:aqua_affinity":1', comp)
        self.assertIn('"minecraft:protection":4', comp)
        self.assertIn('"minecraft:unbreaking":3', comp)
        self.assertIn('"minecraft:mending":1', comp)

    def test_component_carries_armor_attribute_modifier(self):
        # The helmet has to deliver the ~50% damage-reduction promise.
        # If a future Minecraft version renames the attribute or changes
        # the operation enum, the give command will silently fall back to
        # a vanilla turtle helmet — this assertion makes that loud.
        comp = mc_guard.TURTLE_SHELL_COMPONENT
        self.assertIn("minecraft:attribute_modifiers", comp)
        self.assertIn('type:"minecraft:armor"', comp)
        self.assertIn("amount:10", comp)
        self.assertIn('operation:"add_value"', comp)
        self.assertIn('slot:"head"', comp)
        # Resource-location id keeps this modifier identifiable and
        # prevents collisions with any future modifier we add.
        self.assertIn('id:"creepwatch:ts_armor"', comp)

    def test_component_uses_bare_list_attribute_modifier_form(self):
        # Minecraft 1.21.2+ requires a bare list directly after `=`,
        # NOT `={modifiers:[...]}`. Production hit this with the wrong
        # form on the live server (DataVersion 4790, April 2026 build)
        # and got:
        #   Malformed 'minecraft:attribute_modifiers' component:
        #   'Not a list: {modifiers:[...]}'
        # Pin the new form so a copy-paste from older docs cannot
        # silently regress.
        comp = mc_guard.TURTLE_SHELL_COMPONENT
        self.assertIn("minecraft:attribute_modifiers=[", comp)
        self.assertNotIn("attribute_modifiers={modifiers:", comp)

    def test_component_is_well_formed_component_list(self):
        comp = mc_guard.TURTLE_SHELL_COMPONENT
        self.assertTrue(comp.startswith("["))
        self.assertTrue(comp.endswith("]"))
        # Two components inside the brackets: enchantments closes with
        # `}` then a comma then attribute_modifiers begins with `=[`.
        # Plain string check — not a full SNBT parse, but catches the
        # obvious "forgot the comma" shape error.
        inside = comp[1:-1]
        self.assertIn("},minecraft:attribute_modifiers=[", inside)


class GiveFailureSignalsTest(unittest.TestCase):
    """Defensive: the helper has to recognise "Malformed" as a failure.
    Without it, a data-component validation rejection (e.g. wrong shape
    after a Minecraft version bump) reaches the user as a fake "✅
    Gave a turtle helmet" reply while the inventory stays empty.
    Caught us once on the live server with /ts; pin it."""

    def test_malformed_is_a_give_failure_signal(self):
        self.assertIn("Malformed", mc_guard.GIVE_FAILURE_SIGNALS)


class HiddenTridentCommandTest(unittest.TestCase):
    """/trident (alias /td) gives the thrown-build trident: Loyalty III ·
    Impaling V · Channeling · Mending · Unbreaking III. Loyalty +
    Channeling is mutex with Riptide — keeping that flavour separate
    would be a follow-up command. Admin-only, hidden from /help."""

    def test_help_text_does_not_mention_trident_command(self):
        body = mc_guard.HELP_TEXT.lower()
        self.assertNotIn("/trident", body)
        self.assertNotIn(" /td ", body)
        self.assertFalse(body.rstrip().endswith("/td"), "trailing '/td' would reveal the alias")

    def test_dispatcher_invokes_cmd_trident(self):
        with patch.object(mc_guard, "cmd_trident") as spy:
            mc_guard.handle_command(1, "/trident Steve", "Op")
            spy.assert_called_once_with(1, "Steve", "Op")

    def test_dispatcher_invokes_cmd_trident_via_alias(self):
        with patch.object(mc_guard, "cmd_trident") as spy:
            mc_guard.handle_command(1, "/td Alex", "Op")
            spy.assert_called_once_with(1, "Alex", "Op")

    def test_base_item_is_trident(self):
        # Tridents have no tier variants — just "minecraft:trident".
        self.assertEqual("trident", mc_guard.TRIDENT_BASE_ITEM)

    def test_enchantment_component_contains_expected_set(self):
        comp = mc_guard.TRIDENT_ENCHANT_COMPONENT
        self.assertIn('"minecraft:loyalty":3', comp)
        self.assertIn('"minecraft:impaling":5', comp)
        self.assertIn('"minecraft:channeling":1', comp)
        self.assertIn('"minecraft:mending":1', comp)
        self.assertIn('"minecraft:unbreaking":3', comp)
        # Riptide is mutex with Loyalty/Channeling — its absence is part
        # of the contract for this command.
        self.assertNotIn("riptide", comp)
        self.assertTrue(comp.startswith("[minecraft:enchantments={"))
        self.assertTrue(comp.endswith("}]"))


class HiddenPickaxeCommandTest(unittest.TestCase):
    """/pickaxe (alias /pk) gives a target player a fully-kitted diamond
    pickaxe (Efficiency V · Unbreaking III · Mending · Fortune III).
    Same hidden-from-/help contract as /sword and /villager."""

    def test_help_text_does_not_mention_pickaxe_command(self):
        body = mc_guard.HELP_TEXT.lower()
        self.assertNotIn("/pickaxe", body)
        self.assertNotIn(" /pk ", body)
        self.assertFalse(body.rstrip().endswith("/pk"), "trailing '/pk' would reveal the alias")

    def test_dispatcher_invokes_cmd_pickaxe(self):
        with patch.object(mc_guard, "cmd_pickaxe") as spy:
            mc_guard.handle_command(1, "/pickaxe Steve", "Op")
            spy.assert_called_once_with(1, "Steve", "Op")

    def test_dispatcher_invokes_cmd_pickaxe_via_alias(self):
        with patch.object(mc_guard, "cmd_pickaxe") as spy:
            mc_guard.handle_command(1, "/pk Alex", "Op")
            spy.assert_called_once_with(1, "Alex", "Op")

    def test_base_item_is_netherite_pickaxe(self):
        self.assertEqual("netherite_pickaxe", mc_guard.PICKAXE_BASE_ITEM)

    def test_enchantment_component_contains_expected_set(self):
        comp = mc_guard.PICKAXE_ENCHANT_COMPONENT
        self.assertIn('"minecraft:efficiency":5', comp)
        self.assertIn('"minecraft:unbreaking":3', comp)
        self.assertIn('"minecraft:mending":1', comp)
        self.assertIn('"minecraft:fortune":3', comp)
        self.assertTrue(comp.startswith("[minecraft:enchantments={"))
        self.assertTrue(comp.endswith("}]"))


class HiddenVillagerCommandTest(unittest.TestCase):
    """/villager is admin-only and not advertised in /help. Two contracts:

    1. The dispatcher routes /villager and /vil to cmd_villager — i.e.
       the command actually works for admins who know the name.
    2. The string "/villager" and "/vil" do not appear in HELP_TEXT —
       i.e. it stays hidden even if someone later adds an unrelated
       help line that happens to mention villagers.
    """

    def test_help_text_does_not_mention_villager_command(self):
        body = mc_guard.HELP_TEXT.lower()
        # The /command form is the only thing that matters — narrative
        # references to "villager" elsewhere would be fine. There aren't
        # any today.
        self.assertNotIn("/villager", body)
        self.assertNotIn("/vil ", body)
        self.assertFalse(
            body.rstrip().endswith("/vil"),
            "trailing '/vil' would reveal the hidden alias",
        )

    def test_dispatcher_invokes_cmd_villager(self):
        with patch.object(mc_guard, "cmd_villager") as spy:
            mc_guard.handle_command(1, "/villager Steve 3", "Op")
            spy.assert_called_once_with(1, "Steve 3", "Op")

    def test_dispatcher_invokes_cmd_villager_via_alias(self):
        with patch.object(mc_guard, "cmd_villager") as spy:
            mc_guard.handle_command(1, "/vil Alex", "Op")
            spy.assert_called_once_with(1, "Alex", "Op")


class HiddenGiveCommandTest(unittest.TestCase):
    """`/give` is a generic admin-only item-giver — admins hand any item id
    to a target player without needing a dedicated /<thing> command per
    item. Hidden from /help; advertised in /h_h. The character whitelist
    on the item id is the load-bearing security check — without it, a
    mistyped item id with a space could smuggle a second RCON command
    onto the `give` line."""

    def test_help_text_does_not_mention_give_command(self):
        body = mc_guard.HELP_TEXT.lower()
        self.assertNotIn("/give", body)
        self.assertNotIn(" /gv ", body)
        self.assertFalse(
            body.rstrip().endswith("/gv"),
            "trailing '/gv' would reveal the hidden alias",
        )

    def test_dispatcher_invokes_cmd_give(self):
        with patch.object(mc_guard, "cmd_give") as spy:
            mc_guard.handle_command(1, "/give Elite_Eb diamond 32", "Op")
            spy.assert_called_once_with(1, "Elite_Eb diamond 32", "Op")

    def test_dispatcher_invokes_cmd_give_via_alias(self):
        with patch.object(mc_guard, "cmd_give") as spy:
            mc_guard.handle_command(1, "/gv Steve cake", "Op")
            spy.assert_called_once_with(1, "Steve cake", "Op")

    def test_cmd_give_rejects_missing_item(self):
        with patch.object(mc_guard, "send") as send_spy, \
             patch.object(mc_guard, "rcon") as rcon_spy:
            mc_guard.cmd_give(1, "Elite_Eb", "Op")
            rcon_spy.assert_not_called()
            self.assertIn("Usage", send_spy.call_args.args[1])

    def test_cmd_give_rejects_invalid_player_name(self):
        with patch.object(mc_guard, "send") as send_spy, \
             patch.object(mc_guard, "rcon") as rcon_spy:
            mc_guard.cmd_give(1, "Bad@Name diamond", "Op")
            rcon_spy.assert_not_called()
            self.assertIn("Invalid player name", send_spy.call_args.args[1])

    def test_cmd_give_rejects_invalid_item_id(self):
        # A space in the item id is the smuggling vector — `diamond; op @s`
        # tokenizes into player=Steve, item=diamond;, count=op which then
        # passes the item-id regex *if it allowed semicolons*. Pin the
        # regex against this exact shape.
        with patch.object(mc_guard, "send") as send_spy, \
             patch.object(mc_guard, "rcon") as rcon_spy:
            mc_guard.cmd_give(1, "Steve diamond;extra 1", "Op")
            rcon_spy.assert_not_called()
            self.assertIn("Invalid item id", send_spy.call_args.args[1])

    def test_cmd_give_rejects_zero_count(self):
        with patch.object(mc_guard, "send") as send_spy, \
             patch.object(mc_guard, "rcon") as rcon_spy:
            mc_guard.cmd_give(1, "Steve diamond 0", "Op")
            rcon_spy.assert_not_called()
            self.assertIn("Invalid count", send_spy.call_args.args[1])

    def test_cmd_give_rejects_non_integer_count(self):
        with patch.object(mc_guard, "send") as send_spy, \
             patch.object(mc_guard, "rcon") as rcon_spy:
            mc_guard.cmd_give(1, "Steve diamond seventeen", "Op")
            rcon_spy.assert_not_called()
            self.assertIn("Invalid count", send_spy.call_args.args[1])

    def test_cmd_give_default_count_is_one(self):
        with patch.object(mc_guard, "send"), \
             patch.object(mc_guard, "rcon", return_value="Gave 1 [Diamond] to Steve") as rcon_spy:
            mc_guard.cmd_give(1, "Steve diamond", "Op")
            rcon_spy.assert_called_once_with("give Steve diamond 1")

    def test_cmd_give_passes_qualified_item_id(self):
        # The dotted/colon-namespaced form (`minecraft:diamond_sword`) is
        # the canonical id; the shorthand (`diamond`) is the convenience
        # form. Both must pass the regex.
        with patch.object(mc_guard, "send"), \
             patch.object(mc_guard, "rcon", return_value="ok") as rcon_spy:
            mc_guard.cmd_give(1, "Steve minecraft:diamond_sword 1", "Op")
            rcon_spy.assert_called_once_with("give Steve minecraft:diamond_sword 1")

    def test_cmd_give_underscored_player_name_works(self):
        # Issue from production: a player named `Elite_Eb` could not be
        # given items because there was no /give command at all. This test
        # pins the underscore-friendly happy path so a future regression
        # to MC_PROFILE_NAME (e.g. dropping `_`) would fail loudly here
        # before reaching live admin chat.
        with patch.object(mc_guard, "send"), \
             patch.object(mc_guard, "rcon", return_value="Gave 1 [Cake] to Elite_Eb") as rcon_spy:
            mc_guard.cmd_give(1, "Elite_Eb cake 1", "Op")
            rcon_spy.assert_called_once_with("give Elite_Eb cake 1")

    def test_cmd_give_surfaces_rcon_failure(self):
        with patch.object(mc_guard, "send") as send_spy, \
             patch.object(mc_guard, "rcon", return_value="No entity was found"):
            mc_guard.cmd_give(1, "Ghost diamond 1", "Op")
            self.assertIn("failed", send_spy.call_args.args[1])

    def test_give_failure_signals_catch_unknown_item(self):
        # Caught in production: /give Elite_Eb iron 32 returned "Unknown
        # item 'minecraft:iron'" from RCON, but the bot reported it as
        # success ("🎁 Gave 32× iron"). The Unknown-item phrasing has to
        # be a recognized failure signal so the bot tells the truth.
        self.assertIn("Unknown item", mc_guard.GIVE_FAILURE_SIGNALS)
        self.assertIn("Can't find element", mc_guard.GIVE_FAILURE_SIGNALS)

    def test_cmd_give_unknown_item_suggests_items_command(self):
        rcon_out = "Unknown item 'minecraft:iron' at position 12: ...iron 32<--[HERE]"
        with patch.object(mc_guard, "send") as send_spy, \
             patch.object(mc_guard, "rcon", return_value=rcon_out):
            mc_guard.cmd_give(1, "Elite_Eb iron 32", "Op")
            body = send_spy.call_args.args[1]
            # The "failed" line surfaces the raw RCON message, and the
            # follow-up sentence points the operator at /items so they
            # don't have to ask a second time how to spell `iron_ingot`.
            self.assertIn("failed", body)
            self.assertIn("/items", body)


class OwnerCheatGateTest(unittest.TestCase):
    """OWNER_CHAT_IDS narrows the cheat-tier commands (`/give`, `/h_h`,
    `/sword`, `/mansion`, `/villager`, …) to a strict subset of admins.
    Non-owner admins still get the routine commands. The fallback when
    OWNER_CHAT_IDS is empty is 'every admin gets the cheats', which
    preserves the existing behavior for deployments that haven't
    migrated."""

    def test_is_cheat_owner_no_op_when_unset(self):
        # When OWNER_CHAT_IDS is empty, the cheat gate is a no-op —
        # every caller passes. The outer admin gate in poll_callbacks
        # has already filtered non-admins before handle_command runs,
        # so duplicating the admin check here would only break legit
        # admin access. The point of the gate is to *narrow* the
        # admin set when an owner subset is explicitly declared.
        with patch.object(mc_guard, "OWNER_CHAT_IDS", frozenset()):
            self.assertTrue(mc_guard.is_cheat_owner(111))
            self.assertTrue(mc_guard.is_cheat_owner(222))
            self.assertTrue(mc_guard.is_cheat_owner(333))

    def test_is_cheat_owner_strict_when_set(self):
        # OWNER_CHAT_IDS narrows to the listed ids only; other admins
        # are rejected from the cheat tier even though they remain
        # full admins for the routine commands.
        with patch.object(mc_guard, "OWNER_CHAT_IDS", frozenset({111})), \
             patch.object(mc_guard, "ADMIN_IDS", frozenset({111, 222})):
            self.assertTrue(mc_guard.is_cheat_owner(111))
            self.assertFalse(mc_guard.is_cheat_owner(222),
                             "222 is an admin but not an owner — must be denied")
            self.assertFalse(mc_guard.is_cheat_owner(333))

    def test_cheat_commands_covers_every_hidden_help_entry(self):
        # The HIDDEN_HELP_TEXT cheat-sheet is the user-visible list of
        # cheat commands; if a command is advertised there but not
        # gated, a non-owner admin could discover it from /h_h and use
        # it. Pin the truth-table so a future addition to the cheat-
        # sheet automatically forces the gate to grow too.
        for cmd in (
            "/give", "/items", "/h_h",
            "/sword", "/pickaxe", "/trident", "/bow", "/elytra",
            "/chestplate", "/leggings", "/boots", "/ts", "/totem",
            "/ship", "/mansion", "/buried", "/ruin", "/monument",
            "/igloo", "/portal", "/villager",
            "/spawn", "/warden", "/mobs",
        ):
            self.assertIn(cmd, mc_guard.CHEAT_COMMANDS,
                          f"{cmd} appears in HIDDEN_HELP_TEXT but is not in CHEAT_COMMANDS — non-owner admins could use it")

    def test_cheat_commands_includes_short_aliases(self):
        # Aliases share the gate as the long form. If only /sword is
        # gated and /sw is not, an attacker only needs to learn the
        # short form to bypass.
        for short in ("/gv", "/sw", "/pk", "/td", "/bw", "/el", "/cp",
                      "/bt", "/vil", "/wd"):
            self.assertIn(short, mc_guard.CHEAT_COMMANDS,
                          f"{short} (short alias) must be gated alongside its long form")

    def test_handle_command_refuses_cheat_from_non_owner(self):
        # 222 is an admin (so they reach handle_command at all) but
        # not an owner — /give should be refused with the explicit
        # owner-only message and rcon must never be called.
        with patch.object(mc_guard, "OWNER_CHAT_IDS", frozenset({111})), \
             patch.object(mc_guard, "ADMIN_IDS", frozenset({111, 222})), \
             patch.object(mc_guard, "send") as send_spy, \
             patch.object(mc_guard, "cmd_give") as give_spy:
            mc_guard.handle_command(222, "/give Steve diamond 1", "Lourens")
            give_spy.assert_not_called()
            send_spy.assert_called_once()
            self.assertIn("owner", send_spy.call_args.args[1].lower())

    def test_handle_command_allows_cheat_from_owner(self):
        with patch.object(mc_guard, "OWNER_CHAT_IDS", frozenset({111})), \
             patch.object(mc_guard, "ADMIN_IDS", frozenset({111, 222})), \
             patch.object(mc_guard, "cmd_give") as give_spy:
            mc_guard.handle_command(111, "/give Steve diamond 1", "Hannes")
            give_spy.assert_called_once_with(111, "Steve diamond 1", "Hannes")

    def test_handle_command_allows_routine_from_non_owner(self):
        # Routine commands (/online, /whitelist, /kick, …) must still
        # be reachable by every admin, owner or not — only the cheat
        # tier is narrowed.
        with patch.object(mc_guard, "OWNER_CHAT_IDS", frozenset({111})), \
             patch.object(mc_guard, "ADMIN_IDS", frozenset({111, 222})), \
             patch.object(mc_guard, "cmd_online") as online_spy:
            mc_guard.handle_command(222, "/online", "Lourens")
            online_spy.assert_called_once_with(222)

    def test_owner_chat_ids_parser_rejects_non_positive_ids(self):
        # Mirror the same defensive check ADMIN_CHAT_IDS gets — a
        # group/channel id is negative and must never be allowed to
        # widen the owner gate.
        with self.assertRaises(SystemExit):
            mc_guard._parse_optional_owner_ids("-100123")
        with self.assertRaises(SystemExit):
            mc_guard._parse_optional_owner_ids("0")

    def test_owner_chat_ids_parser_empty_returns_empty_frozenset(self):
        self.assertEqual(mc_guard._parse_optional_owner_ids(""), frozenset())
        self.assertEqual(mc_guard._parse_optional_owner_ids("   "), frozenset())

    def test_owner_chat_ids_parser_accepts_positive_ids(self):
        self.assertEqual(
            mc_guard._parse_optional_owner_ids("111,222"),
            frozenset({111, 222}),
        )


class ItemsHelpCommandTest(unittest.TestCase):
    """/items sends the curated common-item-id cheat-sheet. Hidden from
    /help, listed in /h_h. Same markdown discipline as HIDDEN_HELP_TEXT —
    balanced backticks, asterisks, and brackets — because Telegram
    rejects unbalanced messages with HTTP 400 and the failure mode is
    silent (the user sees nothing back). ItemsHelp's cheat-sheet is
    static; a Minecraft version bump that drops an item id requires a
    manual edit, but the wiki link covers everything we don't enumerate."""

    def test_items_help_text_has_balanced_code_spans(self):
        ticks = mc_guard.ITEMS_HELP_TEXT.count("`")
        self.assertEqual(
            ticks % 2, 0,
            f"ITEMS_HELP_TEXT has {ticks} backticks (odd parity) — "
            "Telegram will reject /items with HTTP 400.",
        )

    def test_items_help_text_has_balanced_bold(self):
        stars = mc_guard.ITEMS_HELP_TEXT.count("*")
        self.assertEqual(
            stars % 2, 0,
            f"ITEMS_HELP_TEXT has {stars} asterisks (odd parity) — "
            "bold spans unbalanced.",
        )

    def test_items_help_text_has_balanced_brackets(self):
        self.assertEqual(
            mc_guard.ITEMS_HELP_TEXT.count("["),
            mc_guard.ITEMS_HELP_TEXT.count("]"),
        )

    def test_items_help_text_includes_common_items(self):
        # Pin a handful of items the operator is most likely to ask
        # for. If these disappear from the cheat-sheet during a
        # version-bump edit, the test fails before the cheat-sheet
        # ships missing the basics.
        for item in (
            "iron_ingot", "gold_ingot", "diamond", "netherite_ingot",
            "totem_of_undying", "elytra", "enchanted_book",
            "water_bucket", "cake",
        ):
            self.assertIn(item, mc_guard.ITEMS_HELP_TEXT,
                          f"{item} missing from ITEMS_HELP_TEXT")

    def test_items_help_text_includes_wiki_link(self):
        # The long tail (~1500 vanilla item ids) lives in the wiki;
        # cheat-sheet curates only ~60. The wiki URL is the safety
        # net for everything we don't enumerate.
        self.assertIn("minecraft.wiki", mc_guard.ITEMS_HELP_TEXT)

    def test_dispatcher_invokes_cmd_items(self):
        with patch.object(mc_guard, "cmd_items") as spy:
            mc_guard.handle_command(1, "/items", "Op")
            spy.assert_called_once_with(1)

    def test_help_text_does_not_mention_items_command(self):
        self.assertNotIn("/items", mc_guard.HELP_TEXT)


class SpawnCommandTest(unittest.TestCase):
    """/spawn (generic mob summoner), /warden (lethal shortcut), and
    /mobs (cheat-sheet). All admin-only via the dispatcher gate AND
    owner-only via CHEAT_COMMANDS. The mob-id regex is the load-bearing
    security check — a space or semicolon in the mob id could otherwise
    smuggle a second RCON command onto the summon line."""

    def test_help_text_does_not_mention_spawn_warden_or_mobs(self):
        body = mc_guard.HELP_TEXT.lower()
        self.assertNotIn("/spawn", body)
        self.assertNotIn("/warden", body)
        self.assertNotIn("/wd ", body)
        self.assertNotIn("/mobs", body)

    def test_dispatcher_invokes_cmd_spawn(self):
        with patch.object(mc_guard, "cmd_spawn") as spy:
            mc_guard.handle_command(1, "/spawn Elite_Eb warden 1", "Op")
            spy.assert_called_once_with(1, "Elite_Eb warden 1", "Op")

    def test_dispatcher_invokes_cmd_warden_full_and_alias(self):
        for cmd in ("/warden Steve", "/wd Steve"):
            with patch.object(mc_guard, "cmd_warden") as spy:
                mc_guard.handle_command(1, cmd, "Op")
                spy.assert_called_once_with(1, "Steve", "Op")

    def test_dispatcher_invokes_cmd_mobs(self):
        with patch.object(mc_guard, "cmd_mobs") as spy:
            mc_guard.handle_command(1, "/mobs", "Op")
            spy.assert_called_once_with(1)

    def test_cmd_spawn_rejects_missing_mob(self):
        with patch.object(mc_guard, "send") as send_spy, \
             patch.object(mc_guard, "rcon") as rcon_spy:
            mc_guard.cmd_spawn(1, "Elite_Eb", "Op")
            rcon_spy.assert_not_called()
            self.assertIn("Usage", send_spy.call_args.args[1])

    def test_cmd_spawn_rejects_invalid_player_name(self):
        with patch.object(mc_guard, "send") as send_spy, \
             patch.object(mc_guard, "rcon") as rcon_spy:
            mc_guard.cmd_spawn(1, "Bad@Name warden", "Op")
            rcon_spy.assert_not_called()
            self.assertIn("Invalid player name", send_spy.call_args.args[1])

    def test_cmd_spawn_rejects_invalid_mob_id(self):
        # Same smuggling vector as /give: a semicolon in the mob id
        # would otherwise tack a second RCON command onto the summon
        # line. Pin the regex against this exact shape.
        with patch.object(mc_guard, "send") as send_spy, \
             patch.object(mc_guard, "rcon") as rcon_spy:
            mc_guard.cmd_spawn(1, "Steve warden;extra 1", "Op")
            rcon_spy.assert_not_called()
            self.assertIn("Invalid mob id", send_spy.call_args.args[1])

    def test_cmd_spawn_rejects_zero_count(self):
        with patch.object(mc_guard, "send") as send_spy, \
             patch.object(mc_guard, "rcon") as rcon_spy:
            mc_guard.cmd_spawn(1, "Steve warden 0", "Op")
            rcon_spy.assert_not_called()
            self.assertIn("Invalid count", send_spy.call_args.args[1])

    def test_cmd_spawn_rejects_over_cap_count(self):
        # 50 wardens at once would lag (and grief) any server. The
        # MAX_SPAWN_COUNT cap is the fat-finger guard above the
        # owner gate.
        over = mc_guard.MAX_SPAWN_COUNT + 1
        with patch.object(mc_guard, "send") as send_spy, \
             patch.object(mc_guard, "rcon") as rcon_spy:
            mc_guard.cmd_spawn(1, f"Steve warden {over}", "Op")
            rcon_spy.assert_not_called()
            self.assertIn("Count too high", send_spy.call_args.args[1])

    def test_cmd_spawn_default_count_is_one(self):
        with patch.object(mc_guard, "send"), \
             patch.object(mc_guard, "rcon", return_value="Summoned new Warden") as rcon_spy:
            mc_guard.cmd_spawn(1, "Steve warden", "Op")
            rcon_spy.assert_called_once_with("execute at Steve run summon warden ~ ~ ~")

    def test_cmd_spawn_underscored_player_name_works(self):
        # Mirrors the /give Elite_Eb regression — owner's son's name
        # contains underscore, the dispatcher must not reject it.
        with patch.object(mc_guard, "send"), \
             patch.object(mc_guard, "rcon", return_value="Summoned new Warden") as rcon_spy:
            mc_guard.cmd_spawn(1, "Elite_Eb warden 1", "Op")
            rcon_spy.assert_called_once_with("execute at Elite_Eb run summon warden ~ ~ ~")

    def test_cmd_spawn_runs_count_summons(self):
        # Each summon is a separate RCON call so the failure path can
        # bail mid-loop with an accurate progress count.
        with patch.object(mc_guard, "send"), \
             patch.object(mc_guard, "rcon", return_value="Summoned new Cow") as rcon_spy:
            mc_guard.cmd_spawn(1, "Steve cow 3", "Op")
            self.assertEqual(rcon_spy.call_count, 3)

    def test_cmd_spawn_unknown_entity_suggests_mobs_command(self):
        rcon_out = "Unknown entity type 'minecraft:wardn'"
        with patch.object(mc_guard, "send") as send_spy, \
             patch.object(mc_guard, "rcon", return_value=rcon_out):
            mc_guard.cmd_spawn(1, "Steve wardn 1", "Op")
            body = send_spy.call_args.args[1]
            self.assertIn("aborted", body)
            self.assertIn("/mobs", body)

    def test_cmd_warden_defaults_count_to_one(self):
        # /warden Steve (no count) must summon exactly one warden;
        # without the default the inner /spawn call would get
        # `Steve warden ` (trailing space → empty count token).
        with patch.object(mc_guard, "cmd_spawn") as spy:
            mc_guard.cmd_warden(1, "Steve", "Op")
            spy.assert_called_once_with(1, "Steve warden 1", "Op")

    def test_cmd_warden_forwards_count(self):
        with patch.object(mc_guard, "cmd_spawn") as spy:
            mc_guard.cmd_warden(1, "Steve 2", "Op")
            spy.assert_called_once_with(1, "Steve warden 2", "Op")

    def test_cmd_warden_rejects_missing_player(self):
        with patch.object(mc_guard, "send") as send_spy, \
             patch.object(mc_guard, "cmd_spawn") as spawn_spy:
            mc_guard.cmd_warden(1, "", "Op")
            spawn_spy.assert_not_called()
            self.assertIn("Usage", send_spy.call_args.args[1])

    def test_mobs_help_text_balanced_markdown(self):
        # Same parity checks as HIDDEN_HELP_TEXT / ITEMS_HELP_TEXT —
        # Telegram rejects unbalanced messages with HTTP 400.
        self.assertEqual(mc_guard.MOBS_HELP_TEXT.count("`") % 2, 0,
                         "MOBS_HELP_TEXT backticks unbalanced")
        self.assertEqual(mc_guard.MOBS_HELP_TEXT.count("*") % 2, 0,
                         "MOBS_HELP_TEXT asterisks unbalanced")
        self.assertEqual(mc_guard.MOBS_HELP_TEXT.count("["),
                         mc_guard.MOBS_HELP_TEXT.count("]"))

    def test_mobs_help_text_includes_warden_and_common_mobs(self):
        # The owner asked specifically for wardens; if a future edit
        # drops warden from the cheat-sheet the test fails before
        # the change ships. Plus a few staples.
        for mob in ("warden", "wither", "ender_dragon", "zombie",
                    "skeleton", "creeper", "villager", "iron_golem"):
            self.assertIn(mob, mc_guard.MOBS_HELP_TEXT,
                          f"{mob} missing from MOBS_HELP_TEXT")

    def test_mob_spawn_failure_signals_catch_unknown_entity(self):
        # Mirror of the /give "Unknown item" regression: if the bot
        # doesn't recognize "Unknown entity" as a failure, the wrong
        # mob id would be reported as a successful spawn.
        self.assertIn("Unknown entity", mc_guard.MOB_SPAWN_FAILURE_SIGNALS)
        self.assertIn("Can't find element", mc_guard.MOB_SPAWN_FAILURE_SIGNALS)


class R2IndicatorTest(unittest.TestCase):
    """The /restore listings show whether each archive is mirrored to R2.
    The indicator function and the cache invalidator are pure, so we can
    test them without an R2 client. The boto3-backed listing is exercised
    indirectly via the cache contract."""

    def test_indicator_empty_when_remote_unknown(self):
        # None means "R2 not configured or listing failed". We do not
        # downgrade every archive to local-only just because R2 hiccupped.
        self.assertEqual("", mc_guard.r2_indicator_for("anything.tar.gz", None))

    def test_indicator_green_check_for_mirrored_archive(self):
        remote = frozenset({"minecraft-20260511T040000Z.tar.gz"})
        self.assertEqual(
            "✅",
            mc_guard.r2_indicator_for("minecraft-20260511T040000Z.tar.gz", remote),
        )

    def test_indicator_pin_for_local_only_archive(self):
        remote = frozenset({"minecraft-20260510T040000Z.tar.gz"})
        self.assertEqual(
            "📍",
            mc_guard.r2_indicator_for("minecraft-20260511T040000Z.tar.gz", remote),
        )

    def test_r2_list_returns_none_when_not_configured(self):
        # Empty R2 env vars should short-circuit before any network call.
        with patch.dict(
            os.environ,
            {
                "R2_BUCKET": "",
                "R2_ACCESS_KEY_ID": "",
                "R2_SECRET_ACCESS_KEY": "",
                "R2_S3_ENDPOINT": "",
            },
            clear=False,
        ):
            mc_guard._r2_list_cache = (0.0, None)
            self.assertIsNone(mc_guard.r2_list_basenames(force=True))


class HelpTextMarkdownTest(unittest.TestCase):
    """HELP_TEXT is sent with parse_mode=Markdown. A single unbalanced
    entity (backtick, asterisk, or square bracket) makes Telegram reject
    the whole message with HTTP 400, silently breaking every /help and /h
    reply. This bit production on 2026-05-11 — the slimmed HELP_TEXT had
    an odd backtick count on the /restore line. These parity checks would
    have failed in CI and stopped the bad deploy."""

    def test_help_text_has_balanced_code_spans(self):
        ticks = mc_guard.HELP_TEXT.count("`")
        self.assertEqual(
            ticks % 2,
            0,
            f"HELP_TEXT has {ticks} backticks (odd parity) — code spans "
            "are unbalanced and Telegram will reject /help with HTTP 400.",
        )

    def test_help_text_has_balanced_bold(self):
        stars = mc_guard.HELP_TEXT.count("*")
        self.assertEqual(
            stars % 2,
            0,
            f"HELP_TEXT has {stars} asterisks (odd parity) — bold spans "
            "are unbalanced and Telegram will reject /help with HTTP 400.",
        )

    def test_help_text_has_balanced_brackets(self):
        # Square brackets are link syntax in Markdown; mismatched counts
        # cause parser fallback weirdness even when the message is
        # otherwise valid.
        self.assertEqual(
            mc_guard.HELP_TEXT.count("["),
            mc_guard.HELP_TEXT.count("]"),
            "HELP_TEXT has unbalanced [ vs ] counts.",
        )


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
