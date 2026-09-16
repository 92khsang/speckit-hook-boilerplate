"""End-to-end coverage: the real shim, as a subprocess, against real fixtures."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import support
from support import (BODY_HOSTILE, manifest, make_repo, payload, registry, run_hook,
                     skill_file)

MANDATORY = {"extension": "probe", "command": "speckit.probe.check", "optional": False}
OPTIONAL = {"extension": "probe", "command": "speckit.probe.offer", "optional": True}
PROBE_MANIFEST = {"probe": manifest("probe", ["speckit.probe.check",
                                              "speckit.probe.offer"])}


def check_skill(body="Run the preflight check, then report PASS."):
    return skill_file("speckit-probe-check", body, source="probe:commands/check.md")


class SilentPass(unittest.TestCase):
    """Cases that must produce no output and no interference."""

    def assert_silent(self, root, agent="claude", **kwargs):
        code, out, err = run_hook(self, root, agent, payload(agent, root), **kwargs)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")
        return err

    def test_no_registry_file(self):
        self.assert_silent(make_repo(self))

    def test_registry_variants_without_registrations(self):
        for label, text in {
                "zero bytes": "",
                "installed only": "installed: []\n",
                "empty hooks": "hooks: {}\n",
                "null hooks": "hooks:\n",
                "empty event list": "hooks:\n  before_plan: []\n",
                "null event": "hooks:\n  before_plan:\n",
                "only an after hook": registry([MANDATORY], event="after_plan"),
                "only another stage": registry([MANDATORY], event="before_tasks"),
        }.items():
            with self.subTest(label):
                self.assert_silent(make_repo(self, extensions_yml=text))

    def test_stage_not_matched(self):
        root = make_repo(self, extensions_yml=registry([MANDATORY]))
        data = payload("claude", root)
        data["command_name"] = "speckit-compound-check"
        code, out, _ = run_hook(self, root, "claude", data)
        self.assertEqual((code, out), (0, ""))

    def test_outside_any_spec_kit_project(self):
        root = make_repo(self)
        code, out, _ = run_hook(self, root, "claude", payload("claude", root))
        self.assertEqual((code, out), (0, ""))

    def test_upward_search_stops_at_the_repository_boundary(self):
        # A `.specify` above the repository must never be adopted; the walk stops at
        # `.git` so an unrelated parent directory cannot supply hooks.
        base = tempfile.mkdtemp(prefix="pre hook decoy ")
        self.addCleanup(shutil.rmtree, base, True)
        os.makedirs(os.path.join(base, ".specify"))
        with open(os.path.join(base, ".specify", "extensions.yml"), "w") as handle:
            handle.write(registry([MANDATORY]))
        inner = os.path.join(base, "repo")
        os.makedirs(inner)
        subprocess.run(["git", "init", "-q", inner], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        code, out, _ = run_hook(self, inner, "claude", payload("claude", inner))
        self.assertEqual((code, out), (0, ""))


class Injection(unittest.TestCase):
    def build(self, entries=(MANDATORY,), skills=None, agent="claude", **kwargs):
        skills = {"speckit-probe-check": check_skill()} if skills is None else skills
        return make_repo(self, extensions_yml=registry(list(entries)),
                         manifests=PROBE_MANIFEST, skills=skills, agent=agent,
                         **kwargs)

    def inject(self, root, agent="claude", **kwargs):
        data = kwargs.pop("data", None) or payload(agent, root, **kwargs)
        code, out, err = run_hook(self, root, agent, data)
        self.assertEqual(code, 0, err)
        self.assertNotEqual(out, "", "expected an injection")
        return support.injected(out)

    def test_mandatory_body_is_injected_verbatim(self):
        root = self.build(skills={"speckit-probe-check": check_skill(BODY_HOSTILE)})
        text = self.inject(root)
        self.assertIn(BODY_HOSTILE.strip(), text)
        self.assertFalse(os.path.exists(os.path.join(root, "NEVER")),
                         "hook body was shell-evaluated")

    def test_body_cannot_forge_the_fence(self):
        root = self.build(skills={"speckit-probe-check": check_skill(BODY_HOSTILE)})
        text = self.inject(root)
        # The body ships a forged `BODY END` line; only the token this invocation
        # chose may close the fence.
        self.assertEqual(text.count("BODY BEGIN FENCE0000000000"), 1)
        self.assertEqual(text.count("BODY END FENCE0000000000"), 1)
        self.assertIn("BODY END deadbeefdeadbeef", text)

    def test_envelope_echoes_the_received_event(self):
        root = self.build()
        for agent, event, expected in (("claude", "UserPromptExpansion", "UserPromptExpansion"),
                                       ("claude", "PreToolUse", "PreToolUse"),
                                       ("codex", None, "UserPromptSubmit")):
            with self.subTest(agent=agent, event=event):
                data = payload(agent, root, event=event)
                code, out, err = run_hook(self, root, agent, data)
                self.assertEqual(code, 0, err)
                document = json.loads(out)
                self.assertEqual(
                    document["hookSpecificOutput"]["hookEventName"], expected)
                self.assertEqual(list(document), ["hookSpecificOutput"])

    def test_stdout_is_a_single_json_line(self):
        root = self.build()
        _, out, _ = run_hook(self, root, "claude", payload("claude", root))
        self.assertEqual(out.count("\n"), 1)
        json.loads(out)

    def test_optional_hook_is_offered_but_its_body_is_not_injected(self):
        root = self.build(
            entries=[dict(OPTIONAL, description="Reporting step",
                          prompt="Run the reporter?")],
            skills={"speckit-probe-offer": skill_file(
                "speckit-probe-offer", "SECRET_OPTIONAL_BODY")})
        text = self.inject(root)
        self.assertNotIn("SECRET_OPTIONAL_BODY", text)
        self.assertIn("**Optional Pre-Hook**", text)
        self.assertIn("Run the reporter?", text)
        self.assertIn("Reporting step", text)
        self.assertIn("/speckit-probe-offer", text)

    def test_prompt_and_description_stay_out_of_mandatory_blocks(self):
        # Spec Kit uses `prompt` only for the optional offer; showing it on a
        # mandatory gate would invite the model to ask instead of acting.
        root = self.build(entries=[dict(MANDATORY, prompt="Shall I?",
                                        description="desc")])
        text = self.inject(root)
        self.assertIn("**Automatic Pre-Hook**", text)
        self.assertNotIn("Shall I?", text)

    def test_declaration_order_is_preserved_across_the_whole_pipeline(self):
        entries = [
            {"extension": "probe", "command": "speckit.probe.one",
             "optional": False, "priority": 30},
            {"extension": "probe", "command": "speckit.probe.two",
             "optional": False, "priority": 10},
            {"extension": "probe", "command": "speckit.probe.three",
             "optional": False, "priority": 20},
        ]
        skills = {"speckit-probe-%s" % name: skill_file(
            "speckit-probe-%s" % name, "MARKER_%s" % name.upper())
            for name in ("one", "two", "three")}
        root = make_repo(self, extensions_yml=registry(entries),
                         manifests={"probe": manifest(
                             "probe", ["speckit.probe.%s" % n
                                       for n in ("one", "two", "three")])},
                         skills=skills)
        text = self.inject(root)
        self.assertLess(text.index("MARKER_ONE"), text.index("MARKER_TWO"))
        self.assertLess(text.index("MARKER_TWO"), text.index("MARKER_THREE"))
        self.assertIn("priority", text)

    def test_condition_is_reported_as_skipped_without_blocking(self):
        root = self.build(entries=[
            MANDATORY,
            {"extension": "probe", "command": "speckit.probe.gated",
             "optional": False, "condition": "env.SKC_GATE is set"}])
        text = self.inject(root)
        self.assertIn("SKIPPED", text)
        self.assertIn("env.SKC_GATE is set", text)
        self.assertIn("never evaluate conditions", text)

    def test_disabled_registrations_are_counted_only(self):
        root = self.build(entries=[
            MANDATORY,
            {"extension": "probe", "command": "speckit.probe.off",
             "optional": False, "enabled": False}])
        text = self.inject(root)
        self.assertIn("1 registration(s) are disabled", text)
        self.assertNotIn("speckit.probe.off", text)

    def test_provenance_is_present(self):
        root = self.build()
        text = self.inject(root)
        for expected in ("Registry event: before_plan", "Native hook event:",
                         "Project root:", "sha256:", "Instruction source:",
                         "Base directory for relative references:"):
            self.assertIn(expected, text)

    def test_runs_from_a_nested_directory(self):
        root = self.build(nested=os.path.join("src", "deep", "nested"))
        nested = os.path.join(root, "src", "deep", "nested")
        data = payload("claude", root, cwd=nested)
        code, out, err = run_hook(self, root, "claude", data, cwd=nested)
        self.assertEqual(code, 0, err)
        self.assertIn("speckit.probe.check", support.injected(out))

    def test_payload_cwd_wins_over_claude_project_dir(self):
        # `CLAUDE_PROJECT_DIR` is pinned to where the session started and does not
        # follow a worktree, so the payload's `cwd` has to take precedence.
        primary = self.build(skills={"speckit-probe-check": check_skill("MARKER_PRIMARY")})
        other = self.build(skills={"speckit-probe-check": check_skill("MARKER_OTHER")})
        data = payload("claude", other)
        code, out, err = run_hook(self, other, "claude", data, cwd=other,
                                  env={"CLAUDE_PROJECT_DIR": primary})
        self.assertEqual(code, 0, err)
        self.assertIn("MARKER_OTHER", support.injected(out))

    def test_git_worktree_uses_its_own_registry(self):
        root = self.build(skills={"speckit-probe-check": check_skill("MARKER_MAIN")})
        subprocess.run(["git", "-C", root, "add", "-A"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "-C", root, "-c", "user.email=t@example.com",
                        "-c", "user.name=t", "commit", "-qm", "fixture"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tree = os.path.join(os.path.dirname(root), "worktree 한글")
        subprocess.run(["git", "-C", root, "worktree", "add", "-q", tree, "-b", "wt"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(os.path.join(tree, ".claude", "skills", "speckit-probe-check",
                               "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write(check_skill("MARKER_WORKTREE"))
        data = payload("claude", tree)
        code, out, err = run_hook(self, tree, "claude", data, cwd=tree,
                                  env={"CLAUDE_PROJECT_DIR": root})
        self.assertEqual(code, 0, err)
        self.assertIn("MARKER_WORKTREE", support.injected(out))

    def test_cross_agent_copy_is_used_with_a_note(self):
        root = make_repo(self, extensions_yml=registry([MANDATORY]),
                         manifests=PROBE_MANIFEST,
                         skills={"speckit-probe-check": check_skill("MARKER_CODEX")},
                         agent="codex")
        text = self.inject(root, agent="claude")
        self.assertIn("MARKER_CODEX", text)
        self.assertIn("resolved from the codex layout", text)

    def test_undeclared_command_resolves_with_a_warning(self):
        # Spec Kit itself never checks a hook's command against `provides.commands`,
        # and the installed file is what an agent would really run.
        root = make_repo(self, extensions_yml=registry([MANDATORY]),
                         manifests={"probe": manifest("probe", ["speckit.probe.other"])},
                         skills={"speckit-probe-check": check_skill()})
        text = self.inject(root)
        self.assertIn("not declared in `provides.commands`", text)

    def test_malformed_manifest_warns_but_does_not_block(self):
        root = make_repo(self, extensions_yml=registry([MANDATORY]),
                         manifests={"probe": "provides: [oops\n"},
                         skills={"speckit-probe-check": check_skill()})
        text = self.inject(root)
        self.assertIn("could not be parsed", text)

    def test_skill_without_frontmatter_still_injects(self):
        root = self.build(skills={"speckit-probe-check": skill_file(
            "x", "MARKER_NO_FRONTMATTER", frontmatter=False)})
        self.assertIn("MARKER_NO_FRONTMATTER", self.inject(root))

    def test_crlf_and_bom_are_normalized(self):
        raw = "﻿---\r\nname: speckit-probe-check\r\n---\r\n\r\nMARKER_CRLF\r\n"
        root = self.build(skills={"speckit-probe-check": raw})
        text = self.inject(root)
        self.assertIn("MARKER_CRLF", text)
        self.assertNotIn("\r", text)
        self.assertNotIn("﻿", text)

    def test_heading_only_body_injects_with_a_warning(self):
        # A heading is still an instruction; dropping it would silently lose a gate.
        root = self.build(skills={"speckit-probe-check": check_skill(
            "# Run the linter before planning")})
        text = self.inject(root)
        self.assertIn("# Run the linter before planning", text)
        self.assertIn("only headings or comments", text)


class Blocking(unittest.TestCase):
    """The failure policy, including the very different refusal mechanisms."""

    def assert_blocked(self, root, agent, expect, data=None):
        data = data or payload(agent, root)
        code, out, err = run_hook(self, root, agent, data)
        if agent == "claude":
            self.assertEqual(code, 2)
            self.assertEqual(out, "", "a block must not also inject context")
            message = err
        else:
            # Codex has no exit-2 refusal on UserPromptSubmit; the decision travels
            # in the document and the process must still exit 0.
            self.assertEqual(code, 0)
            document = json.loads(out)
            self.assertEqual(document["decision"], "block")
            self.assertTrue(document["reason"])
            self.assertEqual(sorted(document), ["decision", "reason"])
            message = document["reason"]
        self.assertIn(expect, message)
        return message

    def both_agents(self, build, expect):
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                self.assert_blocked(build(agent), agent, expect)

    def test_malformed_registry_blocks(self):
        self.both_agents(
            lambda agent: make_repo(self, extensions_yml="hooks:\n\tbefore_plan: []\n"),
            "could not be parsed")

    def test_duplicate_keys_block(self):
        root = make_repo(self, extensions_yml=(
            "hooks:\n  before_plan:\n  - command: speckit.a.b\n"
            "    optional: false\n    optional: true\n"))
        message = self.assert_blocked(root, "claude", "duplicate key")
        self.assertIn("No hooks were checked", message)

    def test_schema_violations_block(self):
        cases = {
            "non-list event": "hooks:\n  before_plan: speckit.a.b\n",
            "non-mapping entry": "hooks:\n  before_plan:\n  - speckit.a.b\n",
            "missing command": "hooks:\n  before_plan:\n  - extension: probe\n",
        }
        for label, text in cases.items():
            with self.subTest(label):
                root = make_repo(self, extensions_yml=text)
                self.assert_blocked(root, "claude", "hooks.before_plan")

    def test_missing_mandatory_skill_blocks(self):
        def build(agent):
            return make_repo(self, extensions_yml=registry([MANDATORY]),
                             manifests=PROBE_MANIFEST, skills={}, agent=agent)
        self.both_agents(build, "no SKILL.md at")

    def test_invalid_mandatory_command_id_blocks(self):
        root = make_repo(self, extensions_yml=registry(
            [{"extension": "probe", "command": "speckit.plan", "optional": False}]))
        self.assert_blocked(root, "claude", "not a valid Spec Kit command id")

    def test_empty_mandatory_body_blocks(self):
        for label, text in {"frontmatter only": "---\nname: speckit-probe-check\n---\n",
                            "zero bytes": "",
                            "whitespace only": "---\nname: x\n---\n\n   \n\n"}.items():
            with self.subTest(label):
                root = make_repo(self, extensions_yml=registry([MANDATORY]),
                                 manifests=PROBE_MANIFEST,
                                 skills={"speckit-probe-check": text})
                self.assert_blocked(root, "claude", "no instruction body")

    def test_unreadable_mandatory_skill_blocks(self):
        if os.geteuid() == 0:
            self.skipTest("running as root; permission bits are not enforced")
        root = make_repo(self, extensions_yml=registry([MANDATORY]),
                         manifests=PROBE_MANIFEST,
                         skills={"speckit-probe-check": check_skill()})
        target = os.path.join(root, ".claude", "skills", "speckit-probe-check",
                              "SKILL.md")
        os.chmod(target, 0)
        self.addCleanup(os.chmod, target, 0o644)
        self.assert_blocked(root, "claude", "cannot open")

    def test_symlink_escaping_the_project_blocks(self):
        outside = tempfile.mkdtemp(prefix="pre hook outside ")
        self.addCleanup(shutil.rmtree, outside, True)
        secret = os.path.join(outside, "secret.md")
        with open(secret, "w", encoding="utf-8") as handle:
            handle.write("TOP_SECRET_MATERIAL")
        root = make_repo(self, extensions_yml=registry([MANDATORY]),
                         manifests=PROBE_MANIFEST,
                         skills={"speckit-probe-check": None})
        os.symlink(secret, os.path.join(root, ".claude", "skills",
                                        "speckit-probe-check", "SKILL.md"))
        message = self.assert_blocked(root, "claude", "outside the project root")
        self.assertNotIn("TOP_SECRET_MATERIAL", message)

    def test_symlink_inside_the_project_resolves(self):
        root = make_repo(self, extensions_yml=registry([MANDATORY]),
                         manifests=PROBE_MANIFEST,
                         skills={"speckit-probe-check": None})
        real = os.path.join(root, "shared-check.md")
        with open(real, "w", encoding="utf-8") as handle:
            handle.write(check_skill("MARKER_LINKED"))
        os.symlink(real, os.path.join(root, ".claude", "skills",
                                      "speckit-probe-check", "SKILL.md"))
        code, out, err = run_hook(self, root, "claude", payload("claude", root))
        self.assertEqual(code, 0, err)
        self.assertIn("MARKER_LINKED", support.injected(out))

    def test_registry_directory_blocks(self):
        root = make_repo(self)
        os.makedirs(os.path.join(root, ".specify", "extensions.yml"))
        self.assert_blocked(root, "claude", "directory")

    def test_oversized_mandatory_body_blocks_rather_than_truncating(self):
        root = make_repo(self, extensions_yml=registry([MANDATORY]),
                         manifests=PROBE_MANIFEST,
                         skills={"speckit-probe-check": check_skill("x" * 5000)})
        code, out, err = run_hook(self, root, "claude", payload("claude", root),
                                  env={"SPECKIT_PREHOOK_MAX_BYTES": "2000"})
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("does not fit", err)

    def test_optional_failures_warn_instead_of_blocking(self):
        root = make_repo(self, extensions_yml=registry([MANDATORY, OPTIONAL]),
                         manifests=PROBE_MANIFEST,
                         skills={"speckit-probe-check": check_skill()})
        code, out, err = run_hook(self, root, "claude", payload("claude", root))
        self.assertEqual(code, 0, err)
        text = support.injected(out)
        self.assertIn("OPTIONAL AND UNRESOLVED", text)

    def test_claude_block_message_tells_the_model_not_to_proceed(self):
        # On `PreToolUse` this text reaches the model rather than the user, and a
        # model that only sees an error may route around the gate.
        root = make_repo(self, extensions_yml=registry([MANDATORY]),
                         manifests=PROBE_MANIFEST, skills={})
        _, _, err = run_hook(self, root, "claude",
                             payload("claude", root, event="PreToolUse"))
        self.assertIn("Do not proceed", err)
        self.assertIn("surface this to the user", err)

    def test_runner_faults_do_not_block_the_user(self):
        root = make_repo(self, extensions_yml=registry([MANDATORY]))
        for label, argv, raw in (("bad json", None, "not json"),
                                 ("unknown agent", ["--agent=bogus"], "{}")):
            with self.subTest(label):
                result = subprocess.run(
                    [support.HOOK_SCRIPT] + (argv or ["--agent=claude"]),
                    input=raw, text=True, capture_output=True, cwd=root)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")


class RuntimeAbsence(unittest.TestCase):
    """The one policy that cannot be implemented in Python."""

    def sandbox_path(self):
        base = tempfile.mkdtemp(prefix="pre hook nopy ")
        self.addCleanup(shutil.rmtree, base, True)
        binary = os.path.join(base, "bin")
        os.makedirs(binary)
        for tool in ("sh", "dirname", "pwd", "env", "git"):
            source = shutil.which(tool)
            if source:
                os.symlink(source, os.path.join(binary, tool))
        return binary

    def run_without_python(self, agent):
        environment = {"PATH": self.sandbox_path(), "SPECKIT_PREHOOK_PYTHON": ""}
        return subprocess.run(["/bin/sh", support.HOOK_SCRIPT, "--agent=%s" % agent],
                              input="{}", text=True, capture_output=True,
                              env=environment)

    def test_claude_blocks_with_status_two(self):
        result = self.run_without_python("claude")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Python 3.9 or newer is required", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_codex_blocks_with_a_json_document_and_status_zero(self):
        result = self.run_without_python("codex")
        self.assertEqual(result.returncode, 0)
        document = json.loads(result.stdout)
        self.assertEqual(document["decision"], "block")
        self.assertIn("Python 3.9 or newer is required", document["reason"])

    def test_never_exits_with_command_not_found(self):
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                self.assertNotEqual(self.run_without_python(agent).returncode, 127)


class Deduplication(unittest.TestCase):
    def build(self, entries):
        skills = {"speckit-probe-offer": skill_file("speckit-probe-offer", "OFFER"),
                  "speckit-probe-check": check_skill()}
        return make_repo(self, extensions_yml=registry(entries),
                         manifests=PROBE_MANIFEST, skills=skills)

    def emitted(self, root, **kwargs):
        _, out, err = run_hook(self, root, "claude", payload("claude", root, **kwargs))
        return bool(out.strip())

    def test_repeat_within_one_turn_is_suppressed(self):
        root = self.build([OPTIONAL])
        self.assertTrue(self.emitted(root))
        self.assertFalse(self.emitted(root))

    def test_both_claude_routes_share_one_turn_key(self):
        root = self.build([OPTIONAL])
        self.assertTrue(self.emitted(root, event="UserPromptExpansion"))
        self.assertFalse(self.emitted(root, event="PreToolUse"))

    def test_a_later_turn_injects_again(self):
        root = self.build([OPTIONAL])
        self.assertTrue(self.emitted(root, turn_key="turn-1"))
        self.assertTrue(self.emitted(root, turn_key="turn-2"))

    def test_different_stages_in_one_turn_both_inject(self):
        root = make_repo(
            self,
            extensions_yml=registry([OPTIONAL], event="before_plan",
                                    extra_events={"before_tasks": [OPTIONAL]}),
            manifests=PROBE_MANIFEST,
            skills={"speckit-probe-offer": skill_file("speckit-probe-offer", "OFFER")})
        self.assertTrue(self.emitted(root, stage="plan"))
        self.assertTrue(self.emitted(root, stage="tasks"))

    def test_mandatory_hooks_bypass_dedup(self):
        # Context compaction can evict an earlier injection, and a missing mandatory
        # gate costs more than a repeated one.
        root = self.build([MANDATORY])
        self.assertTrue(self.emitted(root))
        self.assertTrue(self.emitted(root))

    def test_missing_correlation_ids_disable_dedup(self):
        root = self.build([OPTIONAL])
        data = payload("claude", root)
        del data["prompt_id"]
        for _ in range(2):
            _, out, _ = run_hook(self, root, "claude", data)
            self.assertTrue(out.strip())

    def test_unwritable_state_directory_fails_open(self):
        root = self.build([OPTIONAL])
        blocked = tempfile.mkdtemp(prefix="pre hook ro ")
        self.addCleanup(shutil.rmtree, blocked, True)
        target = os.path.join(blocked, "denied")
        with open(target, "w"):
            pass
        for _ in range(2):
            _, out, err = run_hook(self, root, "claude", payload("claude", root),
                                   env={"SPECKIT_PREHOOK_STATE_DIR": target})
            self.assertTrue(out.strip(), err)

    def test_concurrent_invocations_emit_exactly_once(self):
        root = self.build([OPTIONAL])
        data = json.dumps(payload("claude", root))
        environment = dict(os.environ)
        environment["SPECKIT_PREHOOK_STATE_DIR"] = support._state_dir(self)
        environment["SPECKIT_PREHOOK_FENCE_SALT"] = "FENCE0000000000"
        processes = [subprocess.Popen(
            [support.HOOK_SCRIPT, "--agent=claude"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=root,
            env=environment) for _ in range(8)]
        outputs = [process.communicate(data)[0] for process in processes]
        self.assertEqual(sum(1 for item in outputs if item.strip()), 1)


class ConfigurationDrift(unittest.TestCase):
    """The shipped hook configurations must actually invoke the shipped script."""

    def commands(self, path):
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        found = []
        for event, groups in document["hooks"].items():
            for group in groups:
                for hook in group["hooks"]:
                    found.append((event, group.get("matcher"), hook))
        return found

    def test_claude_matchers_land_on_the_intended_side_of_the_matching_rule(self):
        # A matcher of only [A-Za-z0-9_- ,|] is an exact match; anything else is an
        # unanchored regex. "speckit-" would therefore match nothing at all.
        exact = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                    "0123456789_- ,|")
        expectations = {"UserPromptExpansion": "regex", "PreToolUse": "exact"}
        for event, matcher, _ in self.commands(
                os.path.join(support.TEMPLATE_DIR, ".claude", "settings.json")):
            with self.subTest(event=event):
                kind = "exact" if set(matcher) <= exact else "regex"
                self.assertEqual(kind, expectations[event], matcher)

    def test_configured_commands_run_the_shipped_script(self):
        root = make_repo(self, extensions_yml=registry([MANDATORY]),
                         manifests=PROBE_MANIFEST,
                         skills={"speckit-probe-check": check_skill("MARKER_VIA_CONFIG")})
        shutil.copytree(support.HOOK_DIR, os.path.join(root, ".speckit-hooks"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        sources = {
            "claude": os.path.join(support.TEMPLATE_DIR, ".claude", "settings.json"),
            "codex": os.path.join(support.TEMPLATE_DIR, ".codex", "hooks.json"),
        }
        for agent, path in sources.items():
            for event, _, hook in self.commands(path):
                with self.subTest(agent=agent, event=event):
                    environment = dict(os.environ)
                    environment["CLAUDE_PROJECT_DIR"] = root
                    environment["SPECKIT_PREHOOK_STATE_DIR"] = support._state_dir(self)
                    result = subprocess.run(
                        ["sh", "-c", hook["command"]],
                        input=json.dumps(payload(
                            agent, root,
                            event=event if event == "PreToolUse" else None)),
                        text=True, capture_output=True, cwd=root, env=environment)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("MARKER_VIA_CONFIG",
                                  support.injected(result.stdout))

    def test_timeouts_stay_within_the_host_caps(self):
        caps = {"UserPromptExpansion": 30, "PreToolUse": 15, "UserPromptSubmit": 600}
        for path in (os.path.join(support.TEMPLATE_DIR, ".claude", "settings.json"),
                     os.path.join(support.TEMPLATE_DIR, ".codex", "hooks.json")):
            for event, _, hook in self.commands(path):
                with self.subTest(event=event):
                    self.assertLessEqual(hook["timeout"], caps[event])


class Hygiene(unittest.TestCase):
    def test_no_network_access(self):
        root = make_repo(self, extensions_yml=registry([MANDATORY]),
                         manifests=PROBE_MANIFEST,
                         skills={"speckit-probe-check": check_skill()})
        guard = (
            "import socket, sys\n"
            "def deny(*a, **k):\n"
            "    raise AssertionError('the hook opened a socket')\n"
            "socket.socket = deny\n"
            "socket.create_connection = deny\n"
            "sys.argv = ['speckit-hook', '--agent=claude']\n"
            "sys.path.insert(0, %r)\n"
            "from speckit_prehook.__main__ import main\n"
            "sys.exit(main())\n" % (support.HOOK_DIR,))
        result = subprocess.run([sys.executable, "-c", guard],
                                input=json.dumps(payload("claude", root)),
                                text=True, capture_output=True, cwd=root)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_output_is_stable_across_hash_seeds(self):
        root = make_repo(self, extensions_yml=registry([MANDATORY, OPTIONAL]),
                         manifests=PROBE_MANIFEST,
                         skills={"speckit-probe-check": check_skill(),
                                 "speckit-probe-offer": skill_file(
                                     "speckit-probe-offer", "OFFER")})
        seen = set()
        for seed in ("0", "1", "12345"):
            _, out, err = run_hook(self, root, "claude", payload("claude", root),
                                   env={"PYTHONHASHSEED": seed})
            self.assertNotEqual(out, "", err)
            seen.add(support.injected(out))
        self.assertEqual(len(seen), 1)

    def test_many_hooks_stay_fast(self):
        count = 20
        entries = [{"extension": "probe", "command": "speckit.probe.h%d" % index,
                    "optional": False} for index in range(count)]
        skills = {"speckit-probe-h%d" % index: skill_file(
            "speckit-probe-h%d" % index, "body %d" % index) for index in range(count)}
        root = make_repo(self, extensions_yml=registry(entries),
                         manifests={"probe": manifest(
                             "probe", ["speckit.probe.h%d" % i for i in range(count)])},
                         skills=skills)
        import time
        started = time.time()
        code, out, err = run_hook(self, root, "claude", payload("claude", root))
        elapsed = time.time() - started
        self.assertEqual(code, 0, err)
        self.assertLess(elapsed, 2.0, "hook took %.2fs for %d hooks" % (elapsed, count))
        self.assertEqual(support.injected(out).count("### Mandatory pre-hook"), count)


if __name__ == "__main__":
    unittest.main()


class CodexSchemaConformance(unittest.TestCase):
    """Validate against the schemas the Codex binary actually enforces.

    `additionalProperties: false` at the top level means an extra key is not merely
    ignored — Codex rejects the whole document. These fixtures were extracted
    verbatim from the installed Codex CLI, so they are the contract, not a guess.
    """

    def test_extracted_schemas_describe_the_expected_contract(self):
        schema = support.load_schema("user-prompt-submit.command.output")
        self.assertIs(schema["additionalProperties"], False)
        wire = schema["definitions"]["UserPromptSubmitHookSpecificOutputWire"]
        self.assertEqual(wire["properties"]["hookEventName"]["const"],
                         "UserPromptSubmit")

    def test_injection_document_conforms(self):
        root = make_repo(self, extensions_yml=registry([MANDATORY]),
                         manifests=PROBE_MANIFEST,
                         skills={"speckit-probe-check": check_skill(BODY_HOSTILE)},
                         agent="codex")
        code, out, err = run_hook(self, root, "codex", payload("codex", root))
        self.assertEqual(code, 0, err)
        schema = support.load_schema("user-prompt-submit.command.output")
        self.assertEqual(support.validate(json.loads(out), schema), [])

    def test_block_document_conforms(self):
        root = make_repo(self, extensions_yml=registry([MANDATORY]),
                         manifests=PROBE_MANIFEST, skills={}, agent="codex")
        code, out, _ = run_hook(self, root, "codex", payload("codex", root))
        self.assertEqual(code, 0)
        schema = support.load_schema("user-prompt-submit.command.output")
        self.assertEqual(support.validate(json.loads(out), schema), [])

    def test_fixture_payloads_conform_to_the_input_schema(self):
        # If the fixtures drift from the real input shape, every other Codex test
        # would be exercising a payload Codex would never send.
        schema = support.load_schema("user-prompt-submit.command.input")
        data = payload("codex", "/tmp")
        data["transcript_path"] = "/tmp/transcript.jsonl"
        self.assertEqual(support.validate(data, schema), [])

    def test_a_stray_top_level_key_would_be_detected(self):
        schema = support.load_schema("user-prompt-submit.command.output")
        bad = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit"},
               "additionalContext": "misplaced"}
        self.assertTrue(support.validate(bad, schema))
