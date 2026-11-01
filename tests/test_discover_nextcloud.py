import importlib.util
from http.client import IncompleteRead
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

spec = importlib.util.spec_from_file_location(
    "discovery", Path(__file__).parents[1] / "scripts/discover-nextcloud.py"
)
discovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discovery)

DIGEST = "sha256:" + "a" * 64
NEW_DIGEST = "sha256:" + "b" * 64
RECIPE = "recipe"
IMAGE = "ghcr.io/codefallacy/nextcloud"


def manifest(digest=DIGEST, recipe=RECIPE):
    return {
        "annotations": {discovery.BASE_KEY: digest, discovery.RECIPE_KEY: recipe},
        "manifests": [
            {"platform": {"os": "linux", "architecture": architecture}}
            for architecture in ("amd64", "arm64", "unknown")
        ],
    }


def descriptor(digest=DIGEST):
    return dict(manifest(), digest=digest)


class DiscoveryTests(unittest.TestCase):
    def test_truncated_json_and_incomplete_reads_are_retried(self):
        for error in (json.JSONDecodeError("truncated", "{", 1), IncompleteRead(b"{")):
            with self.subTest(error=type(error).__name__), patch.object(
                discovery, "urlopen", side_effect=[error, io.BytesIO(b'{"ok": true}')]
            ) as request, patch.object(discovery.time, "sleep"):
                self.assertEqual(discovery.fetch_page("https://hub.docker.com/test"), {"ok": True})
                self.assertEqual(request.call_count, 2)

    def test_persistently_truncated_json_exhausts_bounded_retries(self):
        with patch.object(discovery, "urlopen", side_effect=lambda *a, **kw: io.BytesIO(b'{')) as request, patch.object(
            discovery.time, "sleep"
        ), self.assertRaisesRegex(RuntimeError, "after 4 attempts"):
            discovery.fetch_page("https://hub.docker.com/test")
        self.assertEqual(request.call_count, 4)

    def test_historical_lookup_failure_preserves_newest_build(self):
        for failing_lookup in ("upstream", "destination"):
            calls = [descriptor(NEW_DIGEST), None]
            if failing_lookup == "destination":
                calls.append(descriptor())
            calls.append(RuntimeError("registry unavailable after retries"))
            with self.subTest(lookup=failing_lookup), patch.object(discovery, "inspect", side_effect=calls):
                pending, rows, latest, digest = discovery.discover(["35.0.0", "34.0.10"], IMAGE, RECIPE)
            self.assertEqual(pending, [{"version": "35.0.0", "digest": NEW_DIGEST}])
            self.assertEqual((latest, digest), ("35.0.0", NEW_DIGEST))
            self.assertTrue(rows[-1][1].startswith("ERROR:"))

    def test_newest_lookup_failure_never_promotes_older_build(self):
        with patch.object(discovery, "inspect", side_effect=[
            RuntimeError("newest registry lookup failed"), descriptor(), None,
        ]):
            pending, rows, latest, digest = discovery.discover(["35.0.0", "34.0.10"], IMAGE, RECIPE)
        self.assertEqual([item["version"] for item in pending], ["34.0.10"])
        self.assertEqual((latest, digest), ("", ""))

    def test_global_registry_auth_failure_still_aborts_discovery(self):
        with patch.object(discovery, "inspect", side_effect=[
            descriptor(), None, discovery.RegistryAccessError("401 Unauthorized"),
        ]), self.assertRaises(discovery.RegistryAccessError):
            discovery.discover(["35.0.0", "34.0.10"], IMAGE, RECIPE)

    def test_dockerhub_connection_failure_recovers(self):
        with patch.object(discovery, "urlopen", side_effect=[
            URLError("Temporary failure in name resolution"),
            TimeoutError("connection timed out"),
            io.BytesIO(b'{"results": [], "next": null}'),
        ]) as request, patch.object(discovery.time, "sleep") as sleep:
            self.assertEqual(discovery.fetch_page("https://hub.docker.com/test"),
                             {"results": [], "next": None})
            self.assertEqual(request.call_count, 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])

    def test_dockerhub_rate_limit_respects_retry_after(self):
        error = HTTPError("https://hub.docker.com/test", 429, "rate limited",
                          {"Retry-After": "17"}, None)
        with patch.object(discovery, "urlopen", side_effect=[
            error, io.BytesIO(b'{"ok": true}'),
        ]), patch.object(discovery.time, "sleep") as sleep:
            self.assertEqual(discovery.fetch_page("https://hub.docker.com/test"), {"ok": True})
            sleep.assert_called_once_with(17)

    def test_dockerhub_outage_fails_after_four_attempts(self):
        error = HTTPError("https://hub.docker.com/test", 503, "unavailable", {}, None)
        with patch.object(discovery, "urlopen", side_effect=error) as request, patch.object(
            discovery.time, "sleep"
        ), self.assertRaisesRegex(RuntimeError, "after 4 attempts"):
            discovery.fetch_page("https://hub.docker.com/test")
        self.assertEqual(request.call_count, 4)

    def test_dockerhub_permanent_error_does_not_retry(self):
        error = HTTPError("https://hub.docker.com/test", 403, "forbidden", {}, None)
        with patch.object(discovery, "urlopen", side_effect=error) as request, patch.object(
            discovery.time, "sleep"
        ) as sleep, self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            discovery.fetch_page("https://hub.docker.com/test")
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_registry_timeout_retries_and_fails_clearly(self):
        with patch.object(discovery.subprocess, "run", side_effect=subprocess.TimeoutExpired(
            "docker", 90
        )) as run, patch.object(discovery.time, "sleep"), self.assertRaisesRegex(
            RuntimeError, "timed out after 4 attempts"
        ):
            discovery.inspect(IMAGE, missing_ok=True)
        self.assertEqual(run.call_count, 4)

    def test_pagination_selects_only_version_matching_official_latest(self):
        pages = [
            {"results": [{"name": name} for name in (
                "34.0.9-apache", "latest", "35.0.0-rc1-apache", "34.0.10-apache",
                "32.0.14-apache", "35-apache", "34.0.10-fpm-alpine",
            )], "next": "https://hub.docker.com/v2/repositories/library/nextcloud/tags?page=2"},
            {"results": [{"name": "35.0.0-apache"}, {"name": "34.0.10-apache"},
                         {"name": "32.0.15-apache"}], "next": None},
        ]
        for page in pages:
            for tag in page['results']:
                tag['digest'] = DIGEST if tag['name'] == '34.0.10-apache' else NEW_DIGEST
        with patch.object(discovery, "inspect", return_value=descriptor()), patch.object(discovery, "urlopen", side_effect=[
            io.BytesIO(json.dumps(page).encode()) for page in pages
        ]) as fetch:
            self.assertEqual(discovery.published_versions(), ['34.0.10'])
            self.assertEqual(fetch.call_count, 2)

    def test_empty_upstream_list_is_an_error(self):
        with patch.object(discovery, "urlopen", return_value=io.BytesIO(
            b'{"results": [], "next": null}'
        )), patch.object(discovery, "inspect", return_value=descriptor()), self.assertRaises(RuntimeError):
            discovery.published_versions()

    def test_rebuild_reasons(self):
        self.assertFalse(discovery.needs_build(manifest(), DIGEST, RECIPE))
        self.assertTrue(discovery.needs_build(None, DIGEST, RECIPE))
        self.assertTrue(discovery.needs_build(manifest(), NEW_DIGEST, RECIPE))
        self.assertTrue(discovery.needs_build(manifest(), DIGEST, "changed recipe"))
        self.assertTrue(discovery.needs_build(manifest(), DIGEST, RECIPE, force=True))
        self.assertTrue(discovery.needs_build({"manifests": []}, DIGEST, RECIPE))
        legacy = manifest()
        legacy.pop("annotations")
        self.assertTrue(discovery.needs_build(legacy, DIGEST, RECIPE))

    def test_missing_platform_rebuilds_even_with_matching_metadata(self):
        existing = manifest()
        existing["manifests"] = existing["manifests"][:1]
        self.assertTrue(discovery.needs_build(existing, DIGEST, RECIPE))

    def test_auth_rate_limit_and_network_errors_are_not_missing_images(self):
        for error in ("401 Unauthorized", "403 Forbidden", "429 Too Many Requests",
                      "failed to fetch anonymous token: 403 Forbidden", "network timeout",
                      "exec: docker-credential-secretservice: executable file not found"):
            with self.subTest(error=error), patch.object(
                discovery.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 1, "", error)
            ), patch.object(discovery.time, "sleep"), self.assertRaises(RuntimeError):
                discovery.inspect(IMAGE + ":35.0.0", missing_ok=True)

    def test_only_explicit_missing_manifest_is_treated_as_missing(self):
        for error in ("ERROR: manifest unknown", f"ERROR: {IMAGE}:35.0.0: not found"):
            with self.subTest(error=error), patch.object(
                discovery.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 1, "", error)
            ):
                self.assertIsNone(discovery.inspect(IMAGE + ":35.0.0", missing_ok=True))

    def test_transient_registry_error_is_retried(self):
        with patch.object(discovery.subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 1, "", "500 Internal Server Error"),
            subprocess.CompletedProcess([], 0, json.dumps(manifest()), ""),
        ]) as run, patch.object(discovery.time, "sleep"):
            self.assertEqual(discovery.inspect(IMAGE), manifest())
            self.assertEqual(run.call_count, 2)

    def test_build_plan_skips_unchanged_and_rebuilds_changed_digest(self):
        def registry(reference, **kwargs):
            if kwargs.get("raw") is False:
                return descriptor(NEW_DIGEST if "35.0.0" in reference else DIGEST)
            if reference.startswith("docker.io/"):
                return manifest()
            return manifest()
        with patch.object(discovery, "inspect", side_effect=registry):
            pending, rows, latest, latest_digest = discovery.discover(["35.0.0", "34.0.10"], IMAGE, RECIPE)
        self.assertEqual(pending, [{"version": "35.0.0", "digest": NEW_DIGEST}])
        self.assertEqual(rows[-1], ("34.0.10", "Unchanged"))
        self.assertEqual(latest, "35.0.0")

    def test_partial_newest_release_does_not_downgrade_latest(self):
        partial = manifest()
        partial["manifests"] = partial["manifests"][:1]
        with patch.object(discovery, "inspect", side_effect=[
            dict(partial, digest=NEW_DIGEST), descriptor(), None,
        ]):
            pending, rows, latest, latest_digest = discovery.discover(["35.0.0", "34.0.10"], IMAGE, RECIPE)
        self.assertEqual(latest, "")
        self.assertEqual([item["version"] for item in pending], ["34.0.10"])
        self.assertIn("Waiting", rows[0][1])

    def test_missing_upstream_tag_is_deferred(self):
        with patch.object(discovery, "inspect", return_value=None):
            pending, rows, latest, latest_digest = discovery.discover(["35.0.0"], IMAGE, RECIPE)
        self.assertEqual(pending, [])
        self.assertEqual(latest, "")
        self.assertIn("retry", rows[0][1])

    def test_batch_limit_and_failed_image_retry(self):
        def registry(reference, **kwargs):
            if kwargs.get("raw") is False:
                return descriptor()
            return manifest() if reference.startswith("docker.io/") else None
        with patch.object(discovery, "inspect", side_effect=registry):
            first, rows, _, _ = discovery.discover(["35.0.0", "34.0.10"], IMAGE, RECIPE, limit=1)
            retry, _, _, _ = discovery.discover(["35.0.0", "34.0.10"], IMAGE, RECIPE, limit=1)
        self.assertEqual(first, retry)
        self.assertEqual(len(first), 1)
        self.assertIn("Deferred", rows[-1][1])

    def test_outputs_and_summary_for_manual_force_run(self):
        with tempfile.TemporaryDirectory() as folder:
            output, summary = Path(folder) / "output", Path(folder) / "summary"
            env = {"GITHUB_REPOSITORY": "CodeFallacy/nextcloud", "FORCE_REBUILD": "true",
                   "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary),
                   "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}
            with patch.dict(os.environ, env), patch.object(
                discovery, "published_versions", return_value=["35.0.0"]
            ), patch.object(discovery, "recipe_hash", return_value=RECIPE), patch.object(
                discovery, "inspect", side_effect=[descriptor()]
            ):
                discovery.main()
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(values["image"], IMAGE)
            self.assertEqual(values["has_changes"], "true")
            self.assertEqual(values["latest_digest"], DIGEST)
            self.assertEqual(json.loads(values["matrix"])["include"][0]["version"], "35.0.0")
            self.assertIn("Build queued", summary.read_text())

    def test_unchanged_run_emits_empty_matrix_and_no_build_flag(self):
        with tempfile.TemporaryDirectory() as folder:
            output, summary = Path(folder) / "output", Path(folder) / "summary"
            env = {"GITHUB_REPOSITORY": "CodeFallacy/nextcloud", "FORCE_REBUILD": "false",
                   "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary),
                   "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}
            with patch.dict(os.environ, env), patch.object(
                discovery, "published_versions", return_value=["35.0.0"]
            ), patch.object(discovery, "recipe_hash", return_value=RECIPE), patch.object(
                discovery, "inspect", side_effect=[descriptor(), manifest()]
            ):
                discovery.main()
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(values["has_changes"], "false")
            self.assertEqual(json.loads(values["matrix"]), {"include": []})
            self.assertIn("Unchanged", summary.read_text())


if __name__ == "__main__":
    unittest.main()
