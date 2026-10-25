"""Approved command profiles preserve exact, bounded revision history."""
import copy
import json
import unittest
import uuid
from datetime import timedelta
import test_catalog
from solace_core.errors import CoreError
from solace_core.check_profiles import CheckProfiles, validate_profile, ENVIRONMENT_TEMPLATE


def definition(name='tests'):
    return dict(version=1, name=name, steps=[dict(name='unit', argv=['python', '-m', 'unittest'], timeout_seconds=30, output_limit_bytes=4096)], environment={})


class CheckProfileTests(unittest.TestCase):
    def setUp(self):
        test_catalog.CatalogTests.setUp(self)
        self.catalog = test_catalog.CatalogTests.create(self)
        self.profiles = CheckProfiles(self.catalog)

    def request(self, method='preview', **params):
        return dict(id=uuid.uuid4().hex, method='project.check-profile.' + method, actor='owner', params=dict(project='p', **params))

    def create_profile(self, name='tests'):
        p = dict(action='create', definition=definition(name))
        preview = self.profiles.preview(self.request(**p))
        return self.profiles.apply(self.request('apply', **p, plan_token=preview['plan_token'], confirmed=True))

    def assert_code(self, code, call):
        with self.assertRaises(CoreError) as caught: call()
        self.assertEqual(caught.exception.code, code)

    def test_preview_apply_requires_exact_fresh_confirmation(self):
        p = dict(action='create', definition=definition())
        request = self.request(**p)
        preview = self.profiles.preview(request)
        self.assertEqual(self.profiles.preview(request), preview)
        self.assertIn('authority', preview['authority_warning'])
        self.assertEqual(preview['commands'][0]['argv'], definition()['steps'][0]['argv'])
        self.assert_code('CHECK_PROFILE_CONFIRMATION_REQUIRED', lambda: self.profiles.apply(self.request('apply', **p, plan_token=preview['plan_token'])))
        changed = copy.deepcopy(p); changed['definition']['steps'][0]['argv'] = ['false']
        self.assert_code('CHECK_PROFILE_PREVIEW_STALE', lambda: self.profiles.apply(self.request('apply', **changed, plan_token=preview['plan_token'], confirmed=True)))
        apply = self.request('apply', **p, plan_token=preview['plan_token'], confirmed=True)
        result = self.profiles.apply(apply)
        self.assertEqual(self.profiles.apply(apply), result)
        self.assertEqual(result['revision'], 1)
        update = dict(action='update', profile_id=result['profile_id'], definition=definition('changed'))
        stale = self.profiles.preview(self.request(**update))
        other = copy.deepcopy(update); other['definition']['name'] = 'other'
        next_preview = self.profiles.preview(self.request(**other))
        second = self.profiles.apply(self.request('apply', **other, confirmed=True, plan_token=next_preview['plan_token']))
        self.assertEqual(second['revision'], 2)
        self.assert_code('CHECK_PROFILE_PREVIEW_STALE', lambda: self.profiles.apply(self.request('apply', **update, confirmed=True, plan_token=stale['plan_token'])))
        self.assertEqual(self.profiles.show(result['profile_id'], revision=1)['definition']['name'], 'tests')
        self.assertEqual(self.profiles.apply(apply), result)
        reused = copy.deepcopy(apply); reused['actor'] = 'task'
        self.assert_code('REQUEST_ID_REUSED', lambda: self.profiles.apply(reused))

    def test_used_create_preview_cannot_create_again_after_rename(self):
        params = dict(action='create', definition=definition())
        preview = self.profiles.preview(self.request(**params))
        first = self.profiles.apply(self.request('apply', **params, confirmed=True, plan_token=preview['plan_token']))
        update = dict(action='update', profile_id=first['profile_id'], definition=definition('renamed'))
        approved = self.profiles.preview(self.request(**update))
        self.profiles.apply(self.request('apply', **update, confirmed=True, plan_token=approved['plan_token']))
        self.assert_code('CHECK_PROFILE_PREVIEW_STALE', lambda: self.profiles.apply(self.request('apply', **params, confirmed=True, plan_token=preview['plan_token'])))
        self.assertEqual(len(self.profiles.list()['profiles']), 1)

    def test_profile_rejects_shell_string_nul_duplicate_name_and_bad_limits(self):
        mutations = [lambda p: p['steps'][0].update(argv='echo yes'), lambda p: p['steps'][0].update(argv=[]), lambda p: p['steps'][0].update(argv=['bad\0']), lambda p: p['steps'].append(copy.deepcopy(p['steps'][0])), lambda p: p['steps'][0].update(timeout_seconds=True), lambda p: p['steps'][0].update(timeout_seconds=float('nan')), lambda p: p['steps'][0].update(timeout_seconds=3601), lambda p: p['steps'][0].update(timeout_seconds=10**400), lambda p: p['steps'][0].update(output_limit_bytes=1), lambda p: p['steps'][0].update(cwd='../escape'), lambda p: p.update(environment={'HOME':'/tmp'}), lambda p: p.update(environment={'A=B':'bad'}), lambda p: p.update(environment={'OK':'bad\0'}), lambda p: p.update(name='bad\nname'), lambda p: p['steps'][0].update(argv=['x' * 70000])]
        for mutate in mutations:
            value = definition(); mutate(value)
            with self.subTest(value=str(value)[:100]), self.assertRaises(CoreError): validate_profile(value)
        normalized = validate_profile(definition())
        self.assertEqual(normalized['environment'], ENVIRONMENT_TEMPLATE)
        self.assertEqual(validate_profile(normalized), normalized)
        self.assertEqual(validate_profile(dict(definition(), environment={'PATH':'/bin', 'FOO':'value'}))['environment']['FOO'], 'value')
        self.create_profile()
        self.assert_code('CHECK_PROFILE_NAME_EXISTS', lambda: self.profiles.preview(self.request(action='create', definition=definition())))

    def test_task_actor_string_is_not_authority(self):
        for actor in ('task', 'owner', 'root'):
            request = self.request('apply', action='create', definition=definition(), confirmed=True, plan_token='invented')
            request['actor'] = actor
            self.assert_code('CHECK_PROFILE_PREVIEW_STALE', lambda: self.profiles.apply(request))
        preview = self.profiles.preview(self.request(action='create', definition=definition()))
        request = self.request('apply', action='create', definition=definition(), confirmed=1, plan_token=preview['plan_token'])
        self.assert_code('CHECK_PROFILE_CONFIRMATION_REQUIRED', lambda: self.profiles.apply(request))

    def test_profile_output_is_bounded_and_retired_revision_readable(self):
        first = self.create_profile('one'); self.create_profile('two'); self.create_profile('three')
        page = self.profiles.list(limit=2)
        self.assertEqual(len(page['profiles']), 2)
        self.assertNotIn('definition', page['profiles'][0])
        rest = self.profiles.list(after=page['next_after'], limit=2)
        self.assertEqual(len(rest['profiles']), 1)
        p = dict(action='retire', profile_id=first['profile_id'])
        preview = self.profiles.preview(self.request(**p))
        retire_request = self.request('apply', **p, plan_token=preview['plan_token'], confirmed=True)
        retired = self.profiles.apply(retire_request)
        self.assertEqual(self.profiles.apply(retire_request), retired)
        self.assertTrue(retired['retired'])
        self.assertEqual(retired['revision'], 2)
        self.assertEqual(self.profiles.show(first['profile_id'], revision=1)['definition']['name'], 'one')
        self.assertEqual(len(self.profiles.list()['profiles']), 2)
        with self.catalog.connect() as db:
            self.assert_code('CHECK_PROFILE_RETIRED', lambda: self.profiles.active(db, first['profile_id']))
        for after, limit in ((-1, 1), (0, 257), (True, 1), (0, 0), (2**64, 1)):
            self.assert_code('INVALID_REQUEST', lambda: self.profiles.list(after, limit))
        self.assertLess(len(json.dumps(self.profiles.show(first['profile_id']))), 100000)

    def test_preview_expiry_store_binding_and_revision_immutability(self):
        p = dict(action='create', definition=definition())
        preview = self.profiles.preview(self.request(**p))
        from unittest.mock import patch
        with patch.object(self.profiles, 'now', return_value=self.profiles.now() + timedelta(minutes=16)):
            self.assert_code('CHECK_PROFILE_PREVIEW_STALE', lambda: self.profiles.apply(self.request('apply', **p, confirmed=True, plan_token=preview['plan_token'])))
        with patch.object(self.profiles, '_identity', return_value={'changed': True}):
            self.assert_code('CHECK_PROFILE_PREVIEW_STALE', lambda: self.profiles.apply(self.request('apply', **p, confirmed=True, plan_token=preview['plan_token'])))
        result = self.profiles.apply(self.request('apply', **p, confirmed=True, plan_token=preview['plan_token']))
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            with self.catalog.connect(write=True) as db:
                db.execute("UPDATE check_profile_revisions SET definition_json='{}'")
        import hashlib
        from solace_core.integration_snapshots import canonical
        self.assertEqual(result['digest'], hashlib.sha256(canonical(result['definition']).encode()).hexdigest())

    def test_active_checked_apply_blocks_profile_mutation(self):
        # A journal's checked approval protects the profile until receipt commit or recovery.
        from test_check_schema import CheckSchemaTests
        source, task, operation = CheckSchemaTests.seed_plan(self)
        result = self.create_profile()
        profile_id = result['profile_id']
        with self.catalog.connect(write=True) as db:
            db.execute("INSERT INTO check_runs VALUES ('run','plan',1,?,?,?,'{}','{}',?,1,?,'passed','/private/check',NULL,'pending','{}','now','now')", (task, source, 'a'*64, profile_id, result['digest']))
            db.execute("INSERT INTO integration_checked_approvals VALUES ('approval','plan',1,'run',?,'{}','owner','now')", ('b'*64,))
            db.execute("INSERT INTO integration_journals VALUES (?, 'plan',1,'/journal',NULL,'{}','{}','files_ready')", (operation,))
        for action in ('update', 'retire'):
            params = dict(action=action, profile_id=profile_id)
            if action == 'update': params['definition'] = definition('updated')
            self.assert_code('CHECK_PROFILE_BUSY', lambda: self.profiles.preview(self.request(**params)))
        with self.catalog.connect(write=True) as db:
            db.execute("UPDATE integration_journals SET phase='committed'")
        self.profiles.preview(self.request(action='retire', profile_id=profile_id))
