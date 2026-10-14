import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from solace_core.errors import CoreError
from solace_core.paths import CorePaths
from solace_core.protocol import decode_frame, encode_frame, validate_request
from solace_core.storage import StoragePolicy


class FoundationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.base.chmod(0o700)

    def request(self, **changes):
        value = dict(version=1, id=str(uuid.uuid4()), method='project.list', params={}, actor='test')
        value.update(changes)
        return value

    def assert_code(self, code, fn, *args, **kwargs):
        with self.assertRaises(CoreError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_frames_roundtrip_and_reject_invalid_inputs(self):
        req = self.request()
        self.assertEqual(decode_frame(encode_frame(req)), req)
        for data in [b'[]\n', b'{"x":NaN}\n', b'{"x":1,"x":2}\n', b'\xff\n', b'{}', b'x' * (1048576 + 1)]:
            self.assert_code('INVALID_REQUEST', decode_frame, data)
        self.assert_code('INVALID_REQUEST', encode_frame, {'x': float('inf')})

    def test_envelope_version_fields_actor_and_cursor(self):
        validate_request(self.request())
        for changes in [dict(version=True), dict(params=[]), dict(id='bad'), dict(extra=1), dict(actor='\n')]:
            self.assert_code('INVALID_REQUEST', validate_request, self.request(**changes))
        self.assert_code('PROTOCOL_VERSION_UNSUPPORTED', validate_request, self.request(version=2))
        for after in [True, -1, '0']:
            self.assert_code('INVALID_REQUEST', validate_request,
                             self.request(method='event.subscribe', params={'project': 'p', 'after': after}))

    def test_runtime_and_xdg_validation(self):
        env = {'HOME': str(self.base), 'XDG_RUNTIME_DIR': str(self.base)}
        paths = CorePaths.from_environment(env, os.getuid())
        self.assertEqual(paths.socket, self.base / 'solace/core.sock')
        self.assertEqual(paths.registry, self.base / '.local/share/solace/registry.sqlite3')
        self.assert_code('CORE_UNAVAILABLE', CorePaths.from_environment, {'HOME': str(self.base)}, os.getuid())
        self.assert_code('INVALID_REQUEST', CorePaths.from_environment,
                         dict(env, XDG_DATA_HOME='relative'), os.getuid())
        self.base.chmod(0o755)
        self.assert_code('PERMISSION_DENIED', CorePaths.from_environment, env, os.getuid())

    def test_mount_longest_prefix_escapes_and_rejections(self):
        spaced = self.base / 'some space'
        spaced.mkdir()
        mountinfo = ('1 0 0:1 / / rw - ext4 /dev/a rw\n'
                     f'2 1 0:2 / {str(spaced).replace(" ", chr(92) + "040")} rw - nfs server rw\n')
        policy = StoragePolicy(mountinfo=mountinfo, sync_roots=[])
        self.assertEqual(policy.check(self.base / 'new', creating=True).filesystem, 'ext4')
        self.assert_code('UNSUPPORTED_STORAGE', policy.check, spaced / 'new', creating=True)
        self.assertEqual(policy.check(self.base / 'some space-two', creating=True).mount_id, 1)
        for fs in ['cifs', 'fuse.sshfs', 'fuse', 'overlay', 'tmpfs']:
            self.assert_code('UNSUPPORTED_STORAGE', StoragePolicy(mountinfo=f'1 0 0:1 / / rw - {fs} x rw\n').check, self.base)
        self.assert_code('UNSUPPORTED_STORAGE', StoragePolicy(mountinfo='1 0 0:1 / / ro - ext4 x ro\n').check, self.base)
        self.assert_code('UNSUPPORTED_STORAGE', StoragePolicy(mountinfo=mountinfo, sync_roots=[self.base]).check, self.base)
        self.assert_code('STORE_UNAVAILABLE', policy.check, self.base / 'absent')
