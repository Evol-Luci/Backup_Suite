import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import unittest
import test_catalog
from solace_core.errors import CoreError


class OperationTests(unittest.TestCase):
    setUp = test_catalog.CatalogTests.setUp
    create = test_catalog.CatalogTests.create
    def request(self, **changes):
        req = dict(version=1, id=str(uuid.uuid4()), method='checkpoint.create',
                   params={'project':'p', 'message':'private prompt-like message'}, actor='test')
        req.update(changes)
        return req

    def test_acceptance_retries_and_argument_conflicts(self):
        catalog = self.create()
        request = self.request()
        accepted = catalog.accept_operation(request)
        self.assertEqual(catalog.accept_operation(request), accepted)
        changed = dict(request, actor='different')
        with self.assertRaises(CoreError) as caught:
            catalog.accept_operation(changed)
        self.assertEqual(caught.exception.code, 'REQUEST_ID_REUSED')
        self.assertEqual(catalog.get_operation(accepted['operation_id'])['state'], 'queued')

    def test_checkpoint_hook_atomic_and_messages_absent_from_events(self):
        catalog = self.create()
        repo = catalog.open_existing()
        operation = catalog.accept_operation(self.request())['operation_id']
        catalog.transition(operation, 'running')
        checkpoint = repo.create_checkpoint('private prompt-like message',
                     publication_hook=lambda db, cp: catalog.complete_checkpoint(db, operation, cp))
        outcome = catalog.get_operation(operation)
        self.assertEqual(outcome['result']['checkpoint_id'], checkpoint.id)
        self.assertEqual(outcome['state'], 'succeeded')
        events = catalog.events_after(0)
        self.assertEqual([e['seq'] for e in events], [1,2,3])
        self.assertNotIn('private prompt-like message', str(events))
        self.assertEqual(events[-1]['kind'], 'checkpoint.created')
        self.assertEqual(catalog.events_after(2), [events[-1]])
        for cursor in [-1, True, 4]:
            with self.assertRaises(CoreError): catalog.events_after(cursor)

    def test_hook_failure_rolls_back_checkpoint_outcome_and_event(self):
        catalog = self.create()
        repo = catalog.open_existing()
        operation = catalog.accept_operation(self.request())['operation_id']
        catalog.transition(operation, 'running')
        def fail(db, cp):
            catalog.complete_checkpoint(db, operation, cp)
            raise RuntimeError('crash before commit')
        with self.assertRaises(RuntimeError):
            repo.create_checkpoint('failed', publication_hook=fail)
        self.assertEqual(repo.list_checkpoints(), [])
        self.assertEqual(catalog.get_operation(operation)['state'], 'running')
        self.assertEqual(len(catalog.events_after(0)), 2)
        recovered = catalog.recover_interrupted()
        self.assertEqual(recovered, [operation])
        self.assertEqual(catalog.get_operation(operation)['state'], 'interrupted')
        self.assertEqual(catalog.recover_interrupted(), [])
        self.assertEqual(len(catalog.events_after(0)), 3)

    def test_concurrent_events_order_and_illegal_transitions(self):
        catalog = self.create()
        def accept(_): return catalog.accept_operation(self.request())
        with ThreadPoolExecutor(max_workers=4) as pool:
            accepted = list(pool.map(accept, range(12)))
        events = catalog.events_after(0)
        self.assertEqual([e['seq'] for e in events], list(range(1,13)))
        with self.assertRaises(CoreError):
            catalog.transition(accepted[0]['operation_id'], 'succeeded', result={})
        self.assertEqual(len(catalog.events_after(0)), 12)
