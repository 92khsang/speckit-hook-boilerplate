"""Unit coverage for the pure layers: YAML subset, stage detection, classification."""

import unittest

import support  # noqa: F401  (puts the hook package on sys.path)

from speckit_prehook import config, miniyaml, route, skilldoc


class MiniYAMLAccepts(unittest.TestCase):
    def test_block_mapping_and_sequence_at_key_column(self):
        # PyYAML puts a sequence at the same column as its key; hand-written files
        # usually indent it. Both are valid and both occur in the wild.
        flush = "hooks:\n  before_plan:\n  - command: a\n    optional: false\n"
        indented = "hooks:\n  before_plan:\n    - command: a\n      optional: false\n"
        expected = {"hooks": {"before_plan": [{"command": "a", "optional": False}]}}
        self.assertEqual(miniyaml.parse(flush), expected)
        self.assertEqual(miniyaml.parse(indented), expected)

    def test_scalar_resolution_matches_pyyaml_semantics(self):
        parsed = miniyaml.parse(
            "a: null\nb: ~\nc:\nd: true\ne: False\nf: yes\ng: 10\nh: -3\n"
            "i: 0x10\nj: '10'\nk: \"quoted\"\nl: plain text\n")
        self.assertEqual(parsed, {
            "a": None, "b": None, "c": None, "d": True, "e": False, "f": True,
            "g": 10, "h": -3, "i": "0x10", "j": "10", "k": "quoted",
            "l": "plain text"})

    def test_empty_collections(self):
        self.assertEqual(miniyaml.parse("hooks: {}\n"), {"hooks": {}})
        self.assertEqual(miniyaml.parse("hooks:\n  before_plan: []\n"),
                         {"hooks": {"before_plan": []}})

    def test_empty_document(self):
        self.assertIsNone(miniyaml.parse(""))
        self.assertIsNone(miniyaml.parse("# only a comment\n"))

    def test_wrapped_scalars(self):
        # Both forms PyYAML's 80-column emitter produces for long or multi-line text.
        self.assertEqual(
            miniyaml.parse("d: 'multi\n\n  line\n\n  description'\n"),
            {"d": "multi\nline\ndescription"})
        self.assertEqual(
            miniyaml.parse("d: a long plain description that the emitter\n"
                           "  wrapped across two lines\n"),
            {"d": "a long plain description that the emitter wrapped across two lines"})

    def test_comment_handling(self):
        self.assertEqual(miniyaml.parse("a: value # trailing\nb: has#hash\n"),
                         {"a": "value", "b": "has#hash"})

    def test_block_scalars(self):
        self.assertEqual(miniyaml.parse("a: |\n  one\n  two\n"), {"a": "one\ntwo\n"})
        self.assertEqual(miniyaml.parse("a: |-\n  one\n  two\n"), {"a": "one\ntwo"})


class MiniYAMLRefuses(unittest.TestCase):
    CASES = {
        "non-empty flow sequence": "hooks: [a, b]\n",
        "non-empty flow mapping": "hooks: {a: 1}\n",
        "anchor and alias": "a: &x 1\nb: *x\n",
        "explicit tag": "a: !!python/object x\n",
        "second document": "a: 1\n---\nb: 2\n",
        "merge key": "a: 1\n<<: *base\n",
        "tab indentation": "a:\n\tb: 1\n",
        "duplicate top-level key": "hooks:\n  x: 1\nhooks:\n  y: 2\n",
        "duplicate nested key": "a:\n  command: x\n  command: y\n",
        "unterminated quote": 'a: "abc\n',
        "inconsistent indentation": "a: 1\n   b: 2\n",
        "complex key": "? [a, b]\n: 1\n",
    }

    def test_every_unsupported_construct_raises(self):
        for label, text in self.CASES.items():
            with self.subTest(label):
                with self.assertRaises(miniyaml.YAMLError):
                    miniyaml.parse(text)

    def test_duplicate_key_error_names_the_key_and_position(self):
        with self.assertRaises(miniyaml.YAMLError) as caught:
            miniyaml.parse("a:\n  command: x\n  command: y\n")
        self.assertIn("command", str(caught.exception))
        self.assertIn("line 3", str(caught.exception))


class StageDetection(unittest.TestCase):
    STAGES = ("constitution", "specify", "clarify", "plan", "tasks", "analyze",
              "implement", "checklist", "converge", "taskstoissues")

    def test_every_stage_on_both_claude_routes(self):
        for stage in self.STAGES:
            for event in ("UserPromptExpansion", "PreToolUse"):
                with self.subTest(stage=stage, event=event):
                    data = support.payload("claude", "/tmp", stage=stage, event=event)
                    self.assertEqual(route.extract_stage("claude", data), stage)

    def test_taskstoissues_is_not_read_as_tasks(self):
        data = support.payload("claude", "/tmp", stage="taskstoissues")
        self.assertEqual(route.build_route("claude", data).registry_event,
                         "before_taskstoissues")

    def test_claude_near_misses_do_not_match(self):
        for name in ("speckit-implementation", "speckit-implement-extra",
                     "speckit-compound-check", "speckit-git-commit", "Speckit-Plan",
                     "speckit-", "speckit.compound.planverify", "plan"):
            for event, field in (("UserPromptExpansion", "command_name"),
                                 ("PreToolUse", "tool_input")):
                with self.subTest(name=name, event=event):
                    data = support.payload("claude", "/tmp", event=event)
                    if field == "command_name":
                        data["command_name"] = name
                    else:
                        data["tool_input"] = {"skill": name}
                    self.assertIsNone(route.extract_stage("claude", data))

    def test_claude_ignores_non_slash_expansions_and_other_tools(self):
        mcp = support.payload("claude", "/tmp")
        mcp["expansion_type"] = "mcp_prompt"
        self.assertIsNone(route.extract_stage("claude", mcp))
        other = support.payload("claude", "/tmp", event="PreToolUse")
        other["tool_name"] = "Bash"
        self.assertIsNone(route.extract_stage("claude", other))

    def test_pretooluse_without_args_key(self):
        data = support.payload("claude", "/tmp", event="PreToolUse")
        self.assertNotIn("args", data["tool_input"])
        self.assertEqual(route.extract_stage("claude", data), "plan")

    def test_codex_matching_mirrors_codex_expansion(self):
        # Measured on codex-cli 0.154.0: Codex expands a `$skill` token wherever it
        # appears, mid-sentence and inside backticks alike. Matching only at line
        # start would let a real invocation run with its mandatory gate skipped.
        accepted = ["$speckit-plan", "  $speckit-plan", "$speckit-plan the auth work",
                    "first line\n$speckit-implement", "$speckit.plan",
                    "Explain what $speckit-implement does", "`$speckit-implement`",
                    "```\n$speckit-implement\n```", "see $speckit-plan above",
                    "(run $speckit-plan)"]
        rejected = ["$speckit-implementation", "$speckit-implement-extra",
                    "speckit-implement", "$SPECKIT-PLAN", "", "x$speckit-plan",
                    "$speckit-compound-check", "$speckit-", "plan"]
        for prompt in accepted:
            with self.subTest(accept=prompt):
                data = support.payload("codex", "/tmp", prompt=prompt)
                self.assertIsNotNone(route.extract_stage("codex", data))
        for prompt in rejected:
            with self.subTest(reject=prompt):
                data = support.payload("codex", "/tmp", prompt=prompt)
                self.assertIsNone(route.extract_stage("codex", data))

    def test_several_stages_in_one_prompt_are_all_reported(self):
        data = support.payload("codex", "/tmp",
                               prompt="run $speckit-plan then $speckit-implement")
        self.assertEqual(route.extract_stages("codex", data), ["plan", "implement"])
        current = route.build_route("codex", data)
        self.assertEqual(current.stage, "plan")
        self.assertEqual(current.other_stages, ("implement",))

    def test_a_repeated_stage_is_listed_once(self):
        data = support.payload("codex", "/tmp",
                               prompt="$speckit-plan and again $speckit-plan")
        self.assertEqual(route.extract_stages("codex", data), ["plan"])

    def test_agent_is_taken_from_the_flag_not_the_payload(self):
        # `UserPromptSubmit` exists in both CLIs, so sniffing would be ambiguous.
        codex_payload = support.payload("codex", "/tmp")
        self.assertIsNone(route.extract_stage("claude", codex_payload))


class Classification(unittest.TestCase):
    def test_defaults_match_spec_kit(self):
        parsed = miniyaml.parse(support.registry([{"command": "speckit.a.b"}]))
        collected = config.collect(parsed, "before_plan")
        entry = collected.optional[0]
        self.assertTrue(entry.enabled)
        self.assertTrue(entry.optional)
        self.assertEqual(entry.priority, 10)
        self.assertEqual(entry.prompt, "Execute speckit.a.b?")
        self.assertEqual(entry.description, "")
        self.assertIsNone(entry.condition)

    def test_string_false_stays_enabled_like_spec_kit(self):
        parsed = miniyaml.parse(support.registry(
            [{"command": "speckit.a.b", "enabled": "'false'", "optional": False}]))
        collected = config.collect(parsed, "before_plan")
        self.assertEqual(len(collected.mandatory), 1)
        self.assertTrue(any("truthiness" in note
                            for note in collected.mandatory[0].notes))

    def test_disabled_entries_are_counted_not_listed(self):
        parsed = miniyaml.parse(support.registry(
            [{"command": "speckit.a.b", "enabled": False, "optional": False}]))
        collected = config.collect(parsed, "before_plan")
        self.assertEqual(collected.disabled_count, 1)
        self.assertEqual(collected.mandatory, [])
        self.assertTrue(collected.is_empty)

    def test_condition_skips_even_when_mandatory(self):
        parsed = miniyaml.parse(support.registry(
            [{"command": "speckit.a.b", "optional": False,
              "condition": "env.FOO is set"}]))
        collected = config.collect(parsed, "before_plan")
        self.assertEqual(collected.mandatory, [])
        self.assertEqual(len(collected.skipped), 1)

    def test_declaration_order_is_preserved_and_priority_mismatch_noted(self):
        parsed = miniyaml.parse(support.registry([
            {"command": "speckit.a.one", "priority": 30},
            {"command": "speckit.a.two", "priority": 10},
            {"command": "speckit.a.three", "priority": 20}]))
        collected = config.collect(parsed, "before_plan")
        self.assertEqual([entry.command for entry in collected.optional],
                         ["speckit.a.one", "speckit.a.two", "speckit.a.three"])
        self.assertTrue(any("priority" in note for note in collected.notes))

    def test_duplicate_command_keeps_the_last_registration(self):
        parsed = miniyaml.parse(support.registry([
            {"command": "speckit.a.b", "priority": 1},
            {"command": "speckit.a.b", "priority": 2}]))
        collected = config.collect(parsed, "before_plan")
        self.assertEqual(len(collected.optional), 1)
        self.assertEqual(collected.optional[0].priority, 2)
        self.assertTrue(any("more than once" in note for note in collected.notes))

    def test_other_events_are_not_read(self):
        parsed = miniyaml.parse(support.registry(
            [{"command": "speckit.a.b"}], event="after_plan"))
        self.assertTrue(config.collect(parsed, "before_plan").is_empty)

    def test_unknown_fields_are_ignored(self):
        parsed = miniyaml.parse(support.registry(
            [{"command": "speckit.a.b", "id": "legacy", "timeout": 30}]))
        self.assertEqual(len(config.collect(parsed, "before_plan").optional), 1)

    def test_schema_violations_raise(self):
        cases = {
            "hooks is a list": "hooks:\n- a\n",
            "event value is a scalar": "hooks:\n  before_plan: speckit.a.b\n",
            "event value is a mapping": "hooks:\n  before_plan:\n    command: x\n",
            "entry is a scalar": "hooks:\n  before_plan:\n  - speckit.a.b\n",
            "entry is null": "hooks:\n  before_plan:\n  - null\n",
            "command missing": "hooks:\n  before_plan:\n  - extension: a\n",
            "command blank": "hooks:\n  before_plan:\n  - command: '  '\n",
            "command not a string": "hooks:\n  before_plan:\n  - command: 123\n",
        }
        for label, text in cases.items():
            with self.subTest(label):
                with self.assertRaises(config.RegistryError):
                    config.collect(miniyaml.parse(text), "before_plan")

    def test_absent_and_empty_registries_are_silent(self):
        for text in ("", "installed: []\n", "hooks: {}\n",
                     "hooks:\n  before_plan: []\n", "hooks:\n"):
            with self.subTest(text=text):
                parsed = miniyaml.parse(text) or {}
                self.assertTrue(config.collect(parsed, "before_plan").is_empty)


class FrontmatterSplitting(unittest.TestCase):
    def split(self, text):
        return skilldoc.split_frontmatter(text)

    def test_normal_document(self):
        front, body, warnings = self.split("---\nname: x\n---\n\nBody line\n")
        self.assertEqual(front, "name: x")
        self.assertEqual(body, "Body line")
        self.assertEqual(warnings, [])

    def test_missing_frontmatter_keeps_whole_file(self):
        front, body, _ = self.split("# Title\n\ntext\n")
        self.assertIsNone(front)
        self.assertEqual(body, "# Title\n\ntext")

    def test_frontmatter_only_yields_empty_body(self):
        front, body, _ = self.split("---\nname: x\n---\n")
        self.assertEqual(front, "name: x")
        self.assertEqual(body, "")

    def test_unterminated_frontmatter_keeps_the_body(self):
        # Treating the whole file as metadata would silently delete a mandatory
        # instruction, which is the worst available outcome.
        front, body, warnings = self.split("---\nname: x\nstill going\n")
        self.assertIsNone(front)
        self.assertIn("name: x", body)
        self.assertTrue(warnings)

    def test_indented_opening_fence_is_not_frontmatter(self):
        front, body, _ = self.split("  ---\nname: x\n  ---\nBody\n")
        self.assertIsNone(front)
        self.assertIn("name: x", body)

    def test_separator_inside_body_stays_in_body(self):
        front, body, _ = self.split("---\nname: x\n---\nabove\n---\nbelow\n")
        self.assertEqual(front, "name: x")
        self.assertEqual(body, "above\n---\nbelow")


if __name__ == "__main__":
    unittest.main()
