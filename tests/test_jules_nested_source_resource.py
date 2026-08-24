from __future__ import annotations

import json
import unittest

from ues.providers.base import HttpResponse
from ues.providers.jules import JulesClient, _resource_name


class RecordingTransport:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []

    def request(self, method, url, *, headers, body, timeout):
        self.requests.append((method, url))
        if url.endswith('/v1alpha/sources/github/hamad933/Cybersecurity-Education-Platform'):
            payload = {
                'name': 'sources/github/hamad933/Cybersecurity-Education-Platform',
                'githubRepo': {
                    'owner': 'hamad933',
                    'repo': 'Cybersecurity-Education-Platform',
                },
            }
            return HttpResponse(200, {}, json.dumps(payload).encode('utf-8'))
        return HttpResponse(404, {}, b'{}')


class JulesNestedSourceResourceTests(unittest.TestCase):
    def test_nested_source_resource_is_preserved_segment_by_segment(self) -> None:
        self.assertEqual(
            _resource_name(
                'sources/github/hamad933/Cybersecurity-Education-Platform',
                'sources',
                allow_nested=True,
            ),
            'sources/github/hamad933/Cybersecurity-Education-Platform',
        )
        self.assertEqual(
            _resource_name(
                'github/hamad933/Cybersecurity-Education-Platform',
                'sources',
                allow_nested=True,
            ),
            'sources/github/hamad933/Cybersecurity-Education-Platform',
        )

    def test_nested_session_resource_remains_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _resource_name('sessions/abc/activities/def', 'sessions')

    def test_nested_source_rejects_empty_or_dot_segments(self) -> None:
        for value in ('sources/github//repo', 'sources/github/../repo', 'sources/github/./repo'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _resource_name(value, 'sources', allow_nested=True)

    def test_get_source_uses_documented_nested_resource_path(self) -> None:
        transport = RecordingTransport()
        client = JulesClient('test-key', transport=transport, endpoint='https://jules.example')
        source = client.get_source('sources/github/hamad933/Cybersecurity-Education-Platform')

        self.assertEqual(source['repository'], 'hamad933/Cybersecurity-Education-Platform')
        self.assertTrue(source['explicitRepositoryIdentity'])
        self.assertEqual(
            transport.requests,
            [('GET', 'https://jules.example/v1alpha/sources/github/hamad933/Cybersecurity-Education-Platform')],
        )


if __name__ == '__main__':
    unittest.main()
