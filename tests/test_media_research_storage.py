"""Encrypted project isolation, durable recovery and conflict-safe event merging."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import base64
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import uuid

import media_research_storage as store


CONFIG = {"repo": "owner/public-app", "token": "test-secret", "branch": "main"}


class Remote:
    """A Contents API fake that stores only opaque encrypted envelopes."""
    def __init__(self):
        self.files = {}
        self.calls = []
        self.conflict = None

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, deepcopy(kwargs)))
        path = url.partition('/contents/')[2]
        if not path:
            raise AssertionError('Repository metadata is unnecessary for encrypted data')
        if method == 'GET':
            if path not in self.files:
                return Mock(status_code=404)
            payload, version = self.files[path]
            if kwargs['headers']['Accept'] == 'application/vnd.github.raw+json':
                return Mock(status_code=200, headers={}, iter_content=Mock(return_value=iter([payload])))
            return Mock(status_code=200, json=Mock(return_value={
                'sha': str(version), 'encoding': 'base64', 'content': base64.b64encode(payload).decode()}))
        if method == 'PUT':
            body = kwargs['json']
            if self.conflict:
                callback, self.conflict = self.conflict, None
                callback(path)
                return Mock(status_code=409)
            prior = self.files.get(path)
            if prior and body.get('sha') != str(prior[1]):
                return Mock(status_code=409)
            if prior is None and body.get('sha'):
                return Mock(status_code=409)
            payload = base64.b64decode(body['content'])
            self.files[path] = (payload, prior[1] + 1 if prior else 1)
            return Mock(status_code=200 if prior else 201)
        raise AssertionError(method)


class ResearchStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'private'

    def create(self, **kwargs):
        return store.create_project('가격 검토', '원가는 비공개 사업 조건', storage_dir=self.directory, **kwargs)

    def test_local_roundtrip_is_encrypted_and_unlisted_capability_scoped(self):
        with patch.object(store.requests, 'request') as request:
            first, second = self.create(), self.create()
            code = first['recovery_code']
            result = store.append_turn(code, '단품 가격은?', {'summary': '조건을 먼저 확인합니다.'}, storage_dir=self.directory)
            reopened = store.open_project(code, storage_dir=self.directory)
            request.assert_not_called()
        self.assertEqual(result['status'], 'local_only')
        self.assertTrue(result['encrypted'])
        self.assertEqual(reopened['project']['turns'][0]['question'], '단품 가격은?')
        self.assertNotIn('recovery_code', reopened)
        self.assertEqual(store.open_project(second['recovery_code'], storage_dir=self.directory)['project']['turns'], [])
        payload = (self.directory / (first['project']['id'] + '.json')).read_bytes()
        self.assertNotIn(code.encode(), payload)
        self.assertNotIn('단품 가격'.encode(), payload)
        self.assertNotIn('원가는 비공개'.encode(), payload)
        self.assertEqual(set(json.loads(payload)), {'schema_version','encryption','salt','nonce','ciphertext'})
        self.assertEqual((self.directory / (first['project']['id'] + '.json')).stat().st_mode & 0o777, 0o600)

    def test_invalid_and_unknown_codes_never_open_another_project(self):
        first = self.create()
        for value in ('../project', first['project']['id'], 'rp_' + 'a' * 43):
            with self.subTest(code=value), self.assertRaises(store.ResearchStorageError):
                store.open_project(value, storage_dir=self.directory)
        self.assertEqual(store.open_project(first['recovery_code'], storage_dir=self.directory)['project']['title'], '가격 검토')

    def test_full_history_and_explicit_decisions_remain_distinct_and_editable(self):
        first = self.create(); code = first['recovery_code']
        ids = []
        for index in range(12):
            result = store.append_turn(code, f'질문 {index}', {'summary': f'제안 {index}'}, storage_dir=self.directory)
            ids.append(result['turn_id'])
        self.assertEqual(len(result['project']['turns']), 12)
        self.assertEqual(result['project']['decisions'], [])
        result = store.approve_decision(code, ids[1], '후보 가격은 내부 검토를 거친다', storage_dir=self.directory)
        decision = result['project']['decisions'][0]
        result = store.update_decision(code, decision['id'], '먼저 고객 조사를 한다', storage_dir=self.directory)
        self.assertEqual(result['project']['decisions'][0]['text'], '먼저 고객 조사를 한다')
        result = store.remove_decision(code, decision['id'], storage_dir=self.directory)
        self.assertEqual(result['project']['decisions'], [])
        identifier = first['project']['id']
        document = store._decode((self.directory / (identifier + '.json')).read_bytes(), identifier, code)
        self.assertEqual([row['kind'] for row in document['events']][-3:], ['decision','decision_update','decision_remove'])
        with self.assertRaises(store.ResearchStorageError):
            store.approve_decision(code, uuid.uuid4().hex, '없는 답변', storage_dir=self.directory)

    def test_business_brief_and_title_updates_preserve_prior_turns(self):
        first = self.create(); code = first['recovery_code']
        store.append_turn(code, '브랜드 질문', {'summary': '브랜드 제안'}, storage_dir=self.directory)
        store.update_brief(code, '목표 고객은 성인 가구', storage_dir=self.directory)
        result = store.rename_project(code, '브랜드 방향', storage_dir=self.directory)
        self.assertEqual(result['project']['title'], '브랜드 방향')
        self.assertEqual(result['project']['brief'], '목표 고객은 성인 가구')
        self.assertEqual(len(result['project']['turns']), 1)

    def test_parallel_appends_preserve_all_independent_turns(self):
        first = self.create(); code = first['recovery_code']
        def append(index):
            return store.append_turn(code, f'질문 {index}', {'summary': str(index)}, storage_dir=self.directory)
        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(append, range(20)))
        reopened = store.open_project(code, storage_dir=self.directory)
        self.assertEqual({row['question'] for row in reopened['project']['turns']}, {f'질문 {i}' for i in range(20)})

    def test_slow_research_sync_does_not_hold_theme_lock_or_block_other_projects(self):
        from data_storage_lock import DATA_WRITE_LOCK
        first, second = self.create(), self.create()
        started, release = threading.Event(), threading.Event()
        def stalled(*args, **kwargs):
            started.set()
            release.wait(3)
            return Mock(status_code=503)
        with patch.object(store.requests, 'request', side_effect=stalled), ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(store.update_brief, first['recovery_code'], '새 조건',
                                      storage_dir=self.directory, github_config=CONFIG)
            self.assertTrue(started.wait(1))
            try:
                acquired = DATA_WRITE_LOCK.acquire(blocking=False)
                self.assertTrue(acquired)
                if acquired:
                    DATA_WRITE_LOCK.release()
                other = store.update_brief(second['recovery_code'], '다른 프로젝트 조건', storage_dir=self.directory)
                self.assertEqual(other['project']['brief'], '다른 프로젝트 조건')
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=2)['status'], 'local_only')

    def test_project_lock_is_reentrant_without_a_second_flock(self):
        first = self.create()
        identifier = first['project']['id']
        with store._locked(self.directory, identifier) as outer:
            with store._locked(self.directory, identifier) as inner:
                self.assertEqual(outer, inner)

    def test_retry_of_same_turn_id_is_idempotent_but_changed_payload_is_refused(self):
        first = self.create(); code = first['recovery_code']; turn_id = uuid.uuid4().hex
        for _ in range(2):
            result = store.append_turn(code, '질문', {'summary': '답'}, turn_id=turn_id, storage_dir=self.directory)
        self.assertEqual(len(result['project']['turns']), 1)
        with self.assertRaises(store.ResearchStorageError):
            store.append_turn(code, '다른 질문', {'summary': '답'}, turn_id=turn_id, storage_dir=self.directory)

    def test_random_salts_and_nonces_make_identical_documents_different(self):
        first = self.create(); code = first['recovery_code']; identifier = first['project']['id']
        document = store._decode((self.directory / (identifier + '.json')).read_bytes(), identifier, code)
        a, b = store._encode(document, code), store._encode(document, code)
        self.assertNotEqual(a, b)
        self.assertNotEqual(json.loads(a)['salt'], json.loads(b)['salt'])
        self.assertNotEqual(json.loads(a)['nonce'], json.loads(b)['nonce'])
        self.assertEqual(store._decode(a, identifier, code), store._decode(b, identifier, code))

    def test_wrong_key_ciphertext_tampering_and_file_swaps_fail_authentication(self):
        first, second = self.create(), self.create()
        code = first['recovery_code']; identifier = first['project']['id']
        payload = (self.directory / (identifier + '.json')).read_bytes()
        with self.assertRaises(store.ResearchStorageError):
            store._decode(payload, identifier, second['recovery_code'])
        envelope = json.loads(payload)
        raw = bytearray(base64.b64decode(envelope['ciphertext'])); raw[3] ^= 1
        envelope['ciphertext'] = base64.b64encode(raw).decode()
        with self.assertRaises(store.ResearchStorageError):
            store._decode(json.dumps(envelope).encode(), identifier, code)
        with self.assertRaises(store.ResearchStorageError):
            store._decode(payload, second['project']['id'], second['recovery_code'])

    def test_plaintext_or_malformed_data_is_never_accepted_as_a_project(self):
        first = self.create(); code = first['recovery_code']; identifier = first['project']['id']
        path = self.directory / (identifier + '.json')
        for payload in (b'{}', b'not json', json.dumps(first['project']).encode()):
            path.write_bytes(payload)
            with self.assertRaises(store.ResearchStorageError):
                store.open_project(code, storage_dir=self.directory)
            self.assertEqual(path.read_bytes(), payload)

    def test_atomic_write_failure_keeps_existing_encrypted_record(self):
        first = self.create(); code = first['recovery_code']
        path = self.directory / (first['project']['id'] + '.json'); before = path.read_bytes()
        with patch.object(store.os, 'replace', side_effect=OSError('sensitive error')):
            with self.assertRaises(store.ResearchStorageError) as error:
                store.update_brief(code, '다른 조건', storage_dir=self.directory)
        self.assertNotIn('sensitive error', str(error.exception))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.directory.glob('.research-*')), [])

    def test_public_repo_receives_only_ciphertext_and_recovers_after_local_loss(self):
        remote = Remote()
        with patch.object(store.requests, 'request', side_effect=remote):
            first = self.create(github_config=CONFIG); code = first['recovery_code']
            store.append_turn(code, '내부 가격 검토 질문', {'summary':'아직 확정하지 않은 검토안'},
                              storage_dir=self.directory, github_config=CONFIG)
            other_dir = Path(self.temp.name) / 'new-instance'
            recovered = store.open_project(code, storage_dir=other_dir, github_config=CONFIG)
        self.assertEqual(first['status'], recovered['status'])
        self.assertEqual(recovered['status'], 'remote_synced')
        self.assertEqual(recovered['project']['turns'][0]['question'], '내부 가격 검토 질문')
        for method, url, kwargs in remote.calls:
            self.assertNotIn(code, url)
            if method == 'PUT':
                payload = base64.b64decode(kwargs['json']['content'])
                self.assertNotIn(code.encode(), payload)
                self.assertNotIn('내부 가격'.encode(), payload)
                self.assertNotIn('비공개 사업'.encode(), payload)
                self.assertEqual(kwargs['json']['message'], 'Update private research project')

    def test_conflict_rereads_decrypts_and_merges_a_concurrent_event(self):
        remote = Remote()
        with patch.object(store.requests, 'request', side_effect=remote):
            first = self.create(github_config=CONFIG); code = first['recovery_code']; identifier = first['project']['id']
            def concurrent(path):
                payload, version = remote.files[path]
                document = store._decode(payload, identifier, code)
                document['events'].append({'id': uuid.uuid4().hex, 'kind':'brief',
                    'created_at':store._now(), 'text':'동시에 저장한 사업 조건'})
                remote.files[path] = (store._encode(document, code), version + 1)
            remote.conflict = concurrent
            result = store.append_turn(code, '내 질문', {'summary':'새 답변'}, storage_dir=self.directory, github_config=CONFIG)
        self.assertEqual(result['status'], 'remote_synced')
        self.assertEqual(result['project']['brief'], '동시에 저장한 사업 조건')
        self.assertEqual(len(result['project']['turns']), 1)
        self.assertGreaterEqual(sum(method=='PUT' for method, _, _ in remote.calls), 3)

    def test_large_contents_encoding_none_is_recovered_from_same_authenticated_endpoint(self):
        remote = Remote()
        with patch.object(store.requests, 'request', side_effect=remote):
            first = self.create(github_config=CONFIG)
        def large_contents(method, url, **kwargs):
            response = remote(method, url, **kwargs)
            if method == 'GET' and kwargs['headers']['Accept'] == 'application/vnd.github+json' and response.status_code == 200:
                response.json.return_value = {'sha':response.json()['sha'], 'encoding':'none'}
            return response
        with patch.object(store.requests, 'request', side_effect=large_contents):
            result = store.open_project(first['recovery_code'], storage_dir=Path(self.temp.name)/'reboot', github_config=CONFIG)
        self.assertEqual(result['status'], 'remote_synced')
        self.assertEqual(result['project']['brief'], '원가는 비공개 사업 조건')
        self.assertTrue(any(kwargs['headers']['Accept']=='application/vnd.github.raw+json' for _,_,kwargs in remote.calls))

    def test_tampered_remote_is_not_overwritten_and_local_record_is_kept(self):
        remote = Remote()
        with patch.object(store.requests, 'request', side_effect=remote):
            first = self.create(github_config=CONFIG); code = first['recovery_code']
            path = next(iter(remote.files)); remote.files[path] = (b'{"unsafe":"plaintext"}', 2)
            before_puts = sum(method=='PUT' for method,_,_ in remote.calls)
            result = store.update_brief(code, '로컬에서 보존하는 조건', storage_dir=self.directory, github_config=CONFIG)
        self.assertEqual(result['status'], 'local_only')
        self.assertEqual(result['project']['brief'], '로컬에서 보존하는 조건')
        self.assertEqual(before_puts, sum(method=='PUT' for method,_,_ in remote.calls))
        self.assertEqual(remote.files[path][0], b'{"unsafe":"plaintext"}')

    def test_network_failure_keeps_local_work_and_error_text_is_sanitized(self):
        with patch.object(store.requests, 'request', side_effect=RuntimeError('test-secret request content')):
            first = self.create(github_config=CONFIG)
            result = store.append_turn(first['recovery_code'], '질문', {'summary':'답변'}, storage_dir=self.directory, github_config=CONFIG)
        self.assertEqual(result['status'], 'local_only')
        self.assertEqual(result['storage_reason'], 'remote_sync_failed')
        self.assertNotIn('test-secret', json.dumps(result))
        self.assertEqual(len(result['project']['turns']), 1)

    def test_unsafe_remote_paths_never_trigger_network(self):
        for directory in ('../theme_pool.csv', '/research', 'notes.json', 'a\\b', '.git'):
            with self.subTest(directory=directory), patch.object(store.requests,'request') as request:
                result = self.create(github_config={**CONFIG,'research_path':directory})
                self.assertEqual(result['status'], 'local_only')
                request.assert_not_called()

    def test_merge_is_union_and_rejects_same_id_with_changed_event(self):
        first = self.create(); code = first['recovery_code']; identifier = first['project']['id']
        original = store._decode((self.directory/(identifier+'.json')).read_bytes(), identifier, code)
        left, right = deepcopy(original), deepcopy(original)
        left['events'].append({'id':uuid.uuid4().hex,'kind':'brief','created_at':store._now(),'text':'왼쪽'})
        right['events'].append({'id':uuid.uuid4().hex,'kind':'title','created_at':store._now(),'text':'오른쪽'})
        merged = store.merge_project_documents(left,right)
        self.assertEqual(len(merged['events']),3)
        self.assertEqual(store.merge_project_documents(merged,merged),merged)
        changed = deepcopy(left); changed['events'][-1]['text']='충돌'
        with self.assertRaises(store.ResearchStorageError):
            store.merge_project_documents(left,changed)


if __name__ == '__main__':
    unittest.main()
