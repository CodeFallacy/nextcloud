import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
spec = importlib.util.spec_from_file_location('mirror', Path(__file__).parents[1] / 'scripts/mirror-collabora.py')
mirror = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mirror)

IMAGE = 'ghcr.io/example/collabora'
VERSION = '26.04.4.2.1'
DIGEST = 'sha256:' + 'a' * 64
OLDER_DIGEST = 'sha256:' + 'b' * 64


def descriptor(digest=DIGEST):
    return {'digest': digest, 'manifests': [
        {'platform': {'os': 'linux', 'architecture': architecture}}
        for architecture in ('amd64', 'arm64', 'ppc64le')
    ]}


class MirrorTests(unittest.TestCase):
    def test_pagination_selects_one_version_matching_official_latest(self):
        with patch.object(mirror.registry, 'request_json', side_effect=[
            {'results': [{'name': f'26.04.4.{i}.1', 'digest': OLDER_DIGEST} for i in range(1, 25)] + [
                {'name': 'latest'}, {'name': '26.04.4.1.1-rc1'}, {'name': 'sha256-abc.sig'},
            ], 'next': 'https://hub.docker.com/v2/repositories/collabora/code/tags?page=2'},
            {'results': [{'name': '26.04.5.1.1', 'digest': DIGEST}], 'next': None},
        ]) as request, patch.object(mirror.registry, 'inspect', return_value=descriptor()):
            versions = mirror.published_versions()
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0], '26.04.5.1.1')
        self.assertEqual(request.call_count, 2)

    def test_copy_preserves_digest_and_all_platforms(self):
        with patch.object(mirror.registry, 'require_current_revision'), patch.object(
            mirror.registry, 'inspect', return_value=descriptor()
        ), patch.object(mirror.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as copy:
            mirror.copy_tag(IMAGE, VERSION, DIGEST, 'sha256-' + 'a' * 64)
        command = copy.call_args.args[0]
        self.assertIn('--all', command)
        self.assertIn('--preserve-digests', command)
        self.assertEqual(command[-2], f'docker://docker.io/collabora/code@{DIGEST}')
        self.assertFalse(any('TOKEN' in item for item in command))

    def test_stale_upstream_cannot_overwrite_any_tag(self):
        with patch.object(mirror.registry, 'require_current_revision'), patch.object(
            mirror.registry, 'inspect', return_value=descriptor(OLDER_DIGEST)
        ), patch.object(mirror.subprocess, 'run') as copy, self.assertRaisesRegex(RuntimeError, 'changed upstream'):
            mirror.copy_tag(IMAGE, VERSION, DIGEST, VERSION)
        copy.assert_not_called()

    def test_official_latest_changes_block_promotion(self):
        with patch.object(mirror.registry, 'require_current_revision'), patch.object(
            mirror.registry, 'inspect', side_effect=[descriptor(), descriptor(OLDER_DIGEST)]
        ), patch.object(mirror.subprocess, 'run') as copy, self.assertRaisesRegex(RuntimeError, 'latest changed'):
            mirror.copy_tag(IMAGE, VERSION, DIGEST, 'latest')
        copy.assert_not_called()

    def test_copy_retry_rechecks_upstream_before_writing(self):
        with patch.object(mirror.registry, 'require_current_revision') as guard, patch.object(
            mirror.registry, 'inspect', side_effect=[descriptor(), descriptor(OLDER_DIGEST)]
        ), patch.object(mirror.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'skopeo')) as copy, patch.object(
            mirror.registry, 'pause'
        ), self.assertRaisesRegex(RuntimeError, 'changed upstream'):
            mirror.copy_tag(IMAGE, VERSION, DIGEST, VERSION)
        self.assertEqual(copy.call_count, 1)
        self.assertEqual(guard.call_count, 2)

    def test_copy_timeouts_exhaust_bounded_retries(self):
        with patch.object(mirror.registry, 'require_current_revision'), patch.object(
            mirror.registry, 'inspect', return_value=descriptor()
        ), patch.object(mirror.subprocess, 'run', side_effect=subprocess.TimeoutExpired('skopeo', 900)) as copy, patch.object(
            mirror.registry, 'pause'
        ), self.assertRaisesRegex(RuntimeError, 'failed after retries'):
            mirror.copy_tag(IMAGE, VERSION, DIGEST, VERSION)
        self.assertEqual(copy.call_count, 4)

    def test_bootstrap_and_only_official_latest_digest_is_promoted(self):
        old_version = '26.04.3.2.1'
        images = {f'{mirror.SOURCE}:latest': descriptor(), f'{mirror.SOURCE}:{VERSION}': descriptor(),
                  f'{mirror.SOURCE}:{old_version}': descriptor(OLDER_DIGEST)}
        writes = []
        def inspect(reference, **kwargs):
            return images.get(reference)
        def copy(image, version, digest, tag):
            writes.append((version, digest, tag))
            images[f'{image}:{tag}'] = descriptor(digest)
        with patch.object(mirror.registry, 'inspect', side_effect=inspect), patch.object(mirror, 'copy_tag', side_effect=copy):
            rows, failures = mirror.mirror(IMAGE, [VERSION, old_version], package_exists=False)
        self.assertEqual(failures, 0)
        self.assertEqual(len(rows), 2)
        self.assertEqual([item for item in writes if item[2] == 'latest'], [(VERSION, DIGEST, 'latest')])
        self.assertTrue(any(item[2] == old_version for item in writes))
        self.assertTrue(any(item[2] == VERSION for item in writes))

    def test_failed_historical_lookup_does_not_block_latest(self):
        def inspect(reference, **kwargs):
            if reference.startswith(mirror.SOURCE + ':'):
                if reference.endswith(':26.04.3.2.1'):
                    raise RuntimeError('lookup failed after retries')
                return descriptor()
            return None if reference.endswith(':latest') else descriptor()
        with patch.object(mirror.registry, 'inspect', side_effect=inspect), patch.object(mirror, 'copy_tag') as copy:
            rows, failures = mirror.mirror(IMAGE, [VERSION, '26.04.3.2.1'], True)
        self.assertEqual(failures, 1)
        copy.assert_called_once_with(IMAGE, VERSION, DIGEST, 'latest')
        self.assertEqual(len(rows), 2)

    def test_missing_arm64_is_rejected(self):
        with self.assertRaises(RuntimeError):
            mirror.validate({'digest': DIGEST, 'manifests': []})

    def test_unchanged_latest_performs_no_copy_or_backfill(self):
        with patch.object(mirror.registry, 'inspect', return_value=descriptor()), patch.object(mirror, 'copy_tag') as copy:
            rows, failures = mirror.mirror(IMAGE, [VERSION], True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(failures, 0)
        copy.assert_not_called()


if __name__ == '__main__':
    unittest.main()
