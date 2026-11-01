from datetime import datetime
import importlib.util
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "publishing", Path(__file__).parents[1] / "scripts/publish-nextcloud.py"
)
publishing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publishing)

IMAGE = "ghcr.io/codefallacy/nextcloud"
DIGEST = "sha256:" + "a" * 64
BASE = "sha256:" + "b" * 64
RECIPE = "recipe"
VERSION = "35.0.2"
CANDIDATE = "build-35.0.2-123-1"
CREATED = "2026-11-01T01:00:00Z"


def manifest(version=VERSION, recipe=RECIPE, base=BASE, created=CREATED):
    return {
        "annotations": {
            publishing.VERSION_KEY: version,
            publishing.CREATED_KEY: created,
            publishing.discovery.BASE_KEY: base,
            publishing.discovery.RECIPE_KEY: recipe,
        },
        "manifests": [
            {"platform": {"os": "linux", "architecture": arch}}
            for arch in ("amd64", "arm64")
        ],
    }


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.upstream = f"docker.io/library/nextcloud:{VERSION}-apache"
        self.images = {
            self.upstream: dict(manifest(), digest=BASE),
            f"{IMAGE}:{CANDIDATE}": {"digest": DIGEST},
            f"{IMAGE}@{DIGEST}": manifest(),
            f"{IMAGE}:{VERSION}": None,
            f"{IMAGE}:latest": None,
        }
        self.inspect = self.start_patch(publishing.discovery, "inspect", side_effect=self.registry)
        self.guard = self.start_patch(publishing, "require_current_revision")
        self.write = self.start_patch(publishing.subprocess, "run", side_effect=self.publish)
        self.sleep = self.start_patch(publishing.time, "sleep")

    def start_patch(self, target, name, **kwargs):
        patcher = patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def registry(self, reference, **kwargs):
        result = self.images[reference]
        if isinstance(result, Exception):
            raise result
        return result

    def publish(self, command, **kwargs):
        target = command[command.index("--tag") + 1]
        self.images[target] = dict(self.images[f"{IMAGE}@{DIGEST}"], digest=DIGEST)
        return subprocess.CompletedProcess(command, 0)

    def promote(self, candidate=CANDIDATE, tag="latest"):
        return publishing.promote(IMAGE, candidate, tag, VERSION, BASE, RECIPE)

    def test_only_verified_digest_gets_version_then_latest(self):
        self.assertEqual(self.promote(), DIGEST)
        commands = [call.args[0] for call in self.write.call_args_list]
        self.assertIn(f"{IMAGE}:{VERSION}", commands[0])
        self.assertIn(f"{IMAGE}:latest", commands[1])
        self.assertEqual(commands[0][-1], f"{IMAGE}@{DIGEST}")
        self.assertEqual(commands[0][-1], commands[1][-1])
        self.assertEqual(self.guard.call_count, 2)

    def test_invalid_candidates_never_change_shared_tags(self):
        for invalid in (manifest("34.0.5"), manifest(recipe="old recipe"),
                        {**manifest(), "manifests": []}):
            self.images[f"{IMAGE}@{DIGEST}"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                self.promote()
        self.write.assert_not_called()

    def test_failed_newest_version_cannot_fall_back_to_older_tag(self):
        self.images[f"{IMAGE}:{VERSION}"] = RuntimeError("published version not found")
        with self.assertRaises(RuntimeError):
            self.promote(candidate=VERSION)
        self.inspect.assert_called_once_with(f"{IMAGE}:{VERSION}", raw=False)
        self.write.assert_not_called()

    def test_latest_cannot_be_downgraded(self):
        self.images[f"{IMAGE}:latest"] = manifest("36.0.0")
        with self.assertRaisesRegex(RuntimeError, "Refusing to downgrade"):
            self.promote()
        self.write.assert_not_called()

    def test_stale_revision_cannot_write_verified_candidate(self):
        self.guard.side_effect = RuntimeError("stale")
        with self.assertRaisesRegex(RuntimeError, "stale"):
            self.promote()
        self.write.assert_not_called()

    def test_older_build_cannot_replace_any_shared_tag(self):
        for tag in (VERSION, "latest"):
            self.images[f"{IMAGE}:{tag}"] = manifest(created="2026-11-01T02:00:00Z")
            with self.subTest(tag=tag), self.assertRaisesRegex(RuntimeError, "older build"):
                self.promote(tag=tag)
            self.images[f"{IMAGE}:{tag}"] = None
        self.write.assert_not_called()

    def test_old_upstream_rejected_even_when_rebuilt_with_new_timestamp(self):
        self.images[self.upstream] = dict(manifest(), digest="sha256:" + "c" * 64)
        self.images[f"{IMAGE}@{DIGEST}"] = manifest(created="2026-11-02T01:00:00Z")
        for tag in (VERSION, "latest"):
            with self.subTest(tag=tag), self.assertRaisesRegex(RuntimeError, "superseded"):
                self.promote(tag=tag)
        self.write.assert_not_called()

    def test_partial_or_unreachable_upstream_prevents_promotion(self):
        for upstream in ({"digest": BASE, "manifests": []}, RuntimeError("Docker Hub unavailable")):
            self.images[self.upstream] = upstream
            with self.subTest(upstream=upstream), self.assertRaises(RuntimeError):
                self.promote()
        self.write.assert_not_called()

    def test_correct_upstream_can_replace_newer_timestamp_on_superseded_base(self):
        self.images[f"{IMAGE}:{VERSION}"] = manifest(
            base="sha256:" + "c" * 64, created="2026-11-02T01:00:00Z"
        )
        self.promote(tag=VERSION)
        self.assertEqual(self.write.call_count, 1)

    def test_upstream_change_between_validation_and_write_is_rejected(self):
        def advance_upstream():
            self.images[self.upstream] = dict(manifest(), digest="sha256:" + "c" * 64)
        self.guard.side_effect = advance_upstream
        with self.assertRaisesRegex(RuntimeError, "superseded"):
            self.promote()
        self.write.assert_not_called()

    def test_retries_recheck_upstream_and_revision(self):
        self.write.side_effect = [
            subprocess.CompletedProcess([], 1), subprocess.TimeoutExpired("docker", 120),
            subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0),
        ]
        self.promote()
        self.assertEqual(self.write.call_count, 4)
        self.assertEqual(self.guard.call_count, 4)
        upstream_reads = [call for call in self.inspect.call_args_list if call.args == (self.upstream,)]
        self.assertEqual(len(upstream_reads), 5)  # Latest preflight plus four writes.
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [15, 30])

    def test_exact_version_promotion_does_not_write_latest(self):
        self.promote(tag=VERSION)
        self.assertIn(f"{IMAGE}:{VERSION}", self.write.call_args.args[0])
        self.assertNotIn(f"{IMAGE}:latest", self.write.call_args.args[0])

    def test_exhausted_version_retries_never_write_latest(self):
        self.write.side_effect = None
        self.write.return_value = subprocess.CompletedProcess([], 1)
        with self.assertRaisesRegex(RuntimeError, "after 3 attempts"):
            self.promote()
        self.assertEqual(self.write.call_count, 3)
        for call in self.write.call_args_list:
            self.assertIn(f"{IMAGE}:{VERSION}", call.args[0])
            self.assertNotIn(f"{IMAGE}:latest", call.args[0])

    def test_failed_job_rerun_promotes_attempt_two_via_exact_version(self):
        attempt_two = "build-35.0.2-123-2"
        self.images[f"{IMAGE}:{attempt_two}"] = {"digest": DIGEST}
        self.images[f"{IMAGE}:{CANDIDATE}"] = RuntimeError("attempt one never uploaded")
        self.promote(candidate=attempt_two, tag=VERSION)
        self.write.reset_mock()
        self.inspect.reset_mock()
        self.promote(candidate=VERSION)
        self.assertEqual(self.write.call_count, 2)
        self.inspect.assert_any_call(f"{IMAGE}:{VERSION}", raw=False)
        self.assertFalse(any(call.args == (f"{IMAGE}:{CANDIDATE}",) for call in self.inspect.call_args_list))
        for call in self.write.call_args_list:
            self.assertEqual(call.args[0][-1], f"{IMAGE}@{DIGEST}")


class RevisionTests(unittest.TestCase):
    def test_branch_and_revision_guards(self):
        env = {"DEFAULT_BRANCH": "main", "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "new-sha"}
        with patch.dict(os.environ, env), patch.object(publishing.subprocess, "run", return_value=
            subprocess.CompletedProcess([], 0, "new-sha\trefs/heads/main\n", "")
        ):
            publishing.require_current_revision()
        with patch.dict(os.environ, dict(env, GITHUB_REF="refs/heads/production")), patch.object(
            publishing.subprocess, "run"
        ) as remote, self.assertRaisesRegex(RuntimeError, "default branch"):
            publishing.require_current_revision()
        remote.assert_not_called()
        with patch.dict(os.environ, env), patch.object(publishing.subprocess, "run", return_value=
            subprocess.CompletedProcess([], 0, "newer-sha\trefs/heads/main\n", "")
        ), self.assertRaisesRegex(RuntimeError, "stale"):
            publishing.require_current_revision()


if __name__ == "__main__":
    unittest.main()
