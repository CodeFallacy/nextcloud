import io
from http.client import IncompleteRead
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import registry_utils as registry


class Response(io.BytesIO):
    status = 200


class RegistryTests(unittest.TestCase):
    def test_http_and_truncated_response_retries(self):
        with patch.object(registry, 'urlopen', side_effect=[
            HTTPError('https://example.com', 503, 'unavailable', {}, None),
            IncompleteRead(b'{'), Response(b'{'), Response(b'{"ok": true}'),
        ]) as request, patch.object(registry.time, 'sleep') as sleep:
            self.assertEqual(registry.request_json('https://example.com'), {'ok': True})
        self.assertEqual(request.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4, 8])

    def test_http_access_denied_is_fatal(self):
        with patch.object(registry, 'urlopen', side_effect=HTTPError(
            'https://example.com', 403, 'forbidden', {}, None
        )) as request, self.assertRaises(registry.AccessError):
            registry.request_json('https://example.com', 'secret')
        self.assertEqual(request.call_count, 1)

    def test_registry_access_denied_is_not_missing(self):
        with patch.object(registry.subprocess, 'run', return_value=subprocess.CompletedProcess(
            [], 1, '', '401 Unauthorized'
        )), self.assertRaises(registry.AccessError):
            registry.inspect('ghcr.io/example/image:latest', missing_ok=True)

    def test_user_and_organization_package_routes(self):
        for owner_type, scope in [('User', 'users'), ('Organization', 'orgs')]:
            with patch.dict(os.environ, {'GITHUB_REPOSITORY_OWNER': 'example', 'GITHUB_REPOSITORY_OWNER_TYPE': owner_type}):
                self.assertEqual(registry.package_url('my/image'), f'https://api.github.com/{scope}/example/packages/container/my%2Fimage')


if __name__ == '__main__':
    unittest.main()
