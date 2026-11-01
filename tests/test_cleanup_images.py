import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
spec = importlib.util.spec_from_file_location("cleanup", Path(__file__).parents[1] / "scripts/cleanup-images.py")
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


def digest(number):
    return "sha256:" + f"{number:064x}"


def version(number, tags):
    return {"id": number, "name": digest(number), "created_at": f"2026-01-{min(number, 28):02d}T00:00:00Z",
            "metadata": {"container": {"tags": tags}}}


def index(*members):
    return {"schemaVersion": 2, "manifests": [{"digest": digest(member)} for member in members]}


class RetentionTests(unittest.TestCase):
    def test_twenty_snapshots_can_include_rebuilds_of_one_release(self):
        versions = [version(n, [f'build-35.0.2-run-{n}']) for n in range(1, 26)]
        versions[-1]['metadata']['container']['tags'] += ['35.0.2', 'latest']
        retained, _, deleted = cleanup.cleanup_plan(versions, {v['name']: index() for v in versions})
        self.assertEqual(retained, {digest(n) for n in range(6, 26)})
        self.assertEqual({v['id'] for v in deleted}, set(range(1, 6)))

    def test_upload_time_not_upstream_version_determines_oldest(self):
        versions = [version(1, ['99.0.0']), version(2, ['35.0.2']), version(3, ['35.0.2-new', 'latest'])]
        retained, _, deleted = cleanup.cleanup_plan(versions, {v['name']: index() for v in versions}, keep=2)
        self.assertEqual(retained, {digest(2), digest(3)})
        self.assertEqual([v['id'] for v in deleted], [1])

    def test_keep_twenty_release_roots_and_all_architectures(self):
        versions, manifests = [], {}
        for number in range(1, 26):
            versions.append(version(number, [f"35.0.{number}"] + (["latest"] if number == 25 else [])))
            manifests[digest(number)] = index(number + 100, number + 200, number + 300)
            for child in (number + 100, number + 200, number + 300):
                versions.append(version(child, []))
                manifests[digest(child)] = {"schemaVersion": 2}
        retained, protected, deleted = cleanup.cleanup_plan(versions, manifests)
        self.assertEqual(len(retained), 20)
        self.assertEqual(len(protected), 80)
        self.assertEqual({v['id'] for v in deleted}, {n + offset for n in range(1, 6) for offset in (0, 100, 200, 300)})
        self.assertIn(digest(25), retained)
        self.assertTrue(all(digest(n) in retained for n in range(6, 26)))
        self.assertTrue(all(v['id'] <= 5 for v in deleted[:5]))

    def test_latest_is_protected_even_if_outside_release_window(self):
        versions = [version(n, [f"35.0.{n}"] + (["latest"] if n == 1 else [])) for n in range(1, 26)]
        retained, _, _ = cleanup.cleanup_plan(versions, {v['name']: index() for v in versions})
        self.assertEqual(len(retained), 20)
        self.assertIn(digest(1), retained)
        self.assertIn(digest(25), retained)
        self.assertNotIn(digest(6), retained)

    def test_excess_snapshots_and_orphans_are_deleted(self):
        versions = [version(1, ['35.0.2', 'latest']), version(2, ['build-35.0.2-old']),
                    version(3, []), version(4, ['sha256-old'])]
        retained, protected, deleted = cleanup.cleanup_plan(versions, {v['name']: index() for v in versions}, keep=1)
        self.assertEqual(retained, {digest(1)})
        self.assertEqual({v['id'] for v in deleted}, {2, 3, 4})

    def test_nested_indexes_attestations_and_shared_children_are_protected(self):
        versions = [version(1, ['35.0.2', 'latest']), version(2, []), version(3, []), version(4, [])]
        manifests = {digest(1): index(2, 3), digest(2): index(4),
                     digest(3): {'schemaVersion': 2, 'subject': {'digest': digest(4)}},
                     digest(4): {'schemaVersion': 2}}
        _, protected, deleted = cleanup.cleanup_plan(versions, manifests)
        self.assertEqual(len(protected), 4)
        self.assertEqual(deleted, [])

    def test_missing_latest_or_dependency_aborts_plan(self):
        for versions, manifests in (
            ([version(1, ['35.0.2'])], {digest(1): index()}),
            ([version(1, ['35.0.2', 'latest'])], {digest(1): index(2)}),
        ):
            with self.subTest(versions=versions), self.assertRaises(RuntimeError):
                cleanup.cleanup_plan(versions, manifests)

    def test_pagination_is_complete_before_cleanup(self):
        with patch.object(cleanup.registry, 'request_json', side_effect=[[{}] * 100, [{}] * 3]) as request:
            self.assertEqual(len(cleanup.list_versions('https://api.github.com/package', 'secret')), 103)
        self.assertIn('page=2', request.call_args.args[0])

    def test_deletion_never_touches_retained_manifests(self):
        image = 'ghcr.io/example/nextcloud'
        versions = [version(1, ['35.0.2', 'latest']), version(2, [])]
        deleted = []
        def api(url, token, method='GET', **kwargs):
            if method == 'DELETE':
                deleted.append(url)
                return None
            if '/versions?' in url:
                return versions
            if url.endswith('/versions/2'):
                return versions[1]
            return {'name': 'nextcloud'}
        def registry(reference, **kwargs):
            return {'digest': digest(1)} if reference.endswith(':latest') else (index() if reference.endswith('@' + digest(1)) else {'schemaVersion': 2})
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': folder + '/summary'}), patch.object(
            cleanup.registry, 'require_current_revision'
        ), patch.object(cleanup.registry, 'package_url', return_value='https://api.github.com/package'), patch.object(
            cleanup.registry, 'request_json', side_effect=api
        ), patch.object(cleanup.registry, 'inspect', side_effect=registry):
            self.assertEqual(cleanup.cleanup(image, 'nextcloud', 'secret'), 1)
        self.assertEqual(deleted, ['https://api.github.com/package/versions/2'])

    def test_manifest_snapshot_failure_causes_no_deletes(self):
        with patch.object(cleanup.registry, 'require_current_revision'), patch.object(
            cleanup.registry, 'package_url', return_value='https://api.github.com/package'
        ), patch.object(cleanup.registry, 'request_json', side_effect=[{}, [version(1, ['latest'])]]) as api, patch.object(
            cleanup.registry, 'inspect', side_effect=RuntimeError('unreachable')
        ), self.assertRaises(RuntimeError):
            cleanup.cleanup('ghcr.io/example/nextcloud', 'nextcloud', 'secret')
        self.assertFalse(any(call.kwargs.get('method') == 'DELETE' for call in api.call_args_list))

    def test_dependencies_not_listed_by_packages_api_are_resolved(self):
        image = 'ghcr.io/example/nextcloud'
        def inspect(reference, **kwargs):
            if reference.endswith(':latest'):
                return {'digest': digest(1)}
            return index(2) if reference.endswith('@' + digest(1)) else {'schemaVersion': 2}
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': folder + '/summary'}), patch.object(
            cleanup.registry, 'require_current_revision'
        ), patch.object(cleanup.registry, 'package_url', return_value='https://api.github.com/package'), patch.object(
            cleanup.registry, 'request_json', side_effect=[{}, [version(1, ['latest'])]]
        ), patch.object(cleanup.registry, 'inspect', side_effect=inspect) as registry:
            self.assertEqual(cleanup.cleanup(image, 'nextcloud', 'secret'), 0)
        registry.assert_any_call(image + '@' + digest(2))

    def test_tags_changing_after_planning_stop_deletion(self):
        versions = [version(1, ['latest']), version(2, [])]
        def inspect(reference, **kwargs):
            if reference.endswith(':latest'):
                return {'digest': digest(1)}
            return index() if reference.endswith('@' + digest(1)) else {'schemaVersion': 2}
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': folder + '/summary'}), patch.object(
            cleanup.registry, 'require_current_revision'
        ), patch.object(cleanup.registry, 'package_url', return_value='https://api.github.com/package'), patch.object(
            cleanup.registry, 'request_json', side_effect=[{}, versions, version(2, ['newly-published'])]
        ) as api, patch.object(cleanup.registry, 'inspect', side_effect=inspect), self.assertRaisesRegex(RuntimeError, 'tags changed'):
            cleanup.cleanup('ghcr.io/example/nextcloud', 'nextcloud', 'secret')
        self.assertFalse(any(call.kwargs.get('method') == 'DELETE' for call in api.call_args_list))


if __name__ == '__main__':
    unittest.main()
