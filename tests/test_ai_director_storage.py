"""Only explicit adoption appends durable themes, without replacing prior data."""
from copy import deepcopy
from datetime import datetime
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pandas as pd

import ai_director_storage as storage
from llm_theme_generator import HISTORY_COLUMNS, load_recommendation_runs
from theme_content_manager import CONTENT_COLUMNS, load_content_frame, content_map_for_scope

NOW = datetime(2026, 10, 22, 21, 0, tzinfo=ZoneInfo('Asia/Seoul'))


def csvbytes(frame):
    return frame.to_csv(sep='|', index=False).encode('utf-8-sig')


class DirectorStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = {'themes': self.root/'theme_pool.csv', 'history': self.root/'theme_recommendation_history.csv',
                      'contents': self.root/'theme_content_candidates.csv'}
        self.themes = pd.DataFrame([{'theme_id':'T050', 'theme_name':'골목에서 마주친 낯선 우연',
            'trigger_keywords':'골목,우연', 'genre':'로맨스', 'mood':'설렘', 'copy':'기존 카피', 'operator_note':'보존'}])
        self.history = pd.DataFrame([{**dict.fromkeys(HISTORY_COLUMNS, ''), 'run_id':'old-run','theme_id':'T050',
            'theme_name':'골목에서 마주친 낯선 우연','rank':'1','recommended_date':'2026-10-20','recommended_at':'2026-10-20T09:00:00+09:00','custom_history':'이력 보존'}])
        self.candidates = pd.DataFrame([{**dict.fromkeys(CONTENT_COLUMNS, ''), 'run_id':'old-run',
            'theme_key':'T050','theme_id':'T050','rank':'1','title':'예전 작품','custom_content':'추천 보존'}])
        self.catalog = pd.DataFrame([{'content_id':f'C{i:03}', 'title':f'검증된 복수극 {i}', 'type':'드라마', 'year':'2024'} for i in range(1,6)])
        self.catalog.to_csv(self.root/'content_db.csv', sep='|', index=False)
        self.draft = {'draft_id':'session-one:draft-two','theme_name':'빼앗긴 오늘을 되찾는 다섯 번의 복수',
            'copy':'기다림이 칼날이 되는 순간', 'rationale':'부당한 권력에 맞서는 인물의 선택으로 묶었습니다.',
            'genre':'스릴러/드라마','mood':'강렬함', 'keywords':['복수','권력'], 'content_search_terms':['복수극'],
            'source_issue':'사용자 직접 기획','creation_angle':'인물형','contents':[
               {'content_id':r['content_id'],'title':r['title'],'content_type':r['type'],'year':r['year'],
                'reason':'빼앗긴 일상을 되찾는 선택을 담았다.', 'catalog_status':'confirmed_local',
                'availability_status':'not_checked','poster_url':f'https://image.example.org/{r["content_id"]}.jpg',
                'kinolights_status':'catalog_confirmed'} for r in self.catalog.to_dict('records')]}
        for name, frame in [('themes',self.themes),('history',self.history),('contents',self.candidates)]:
            self.paths[name].write_bytes(csvbytes(frame))
        self.config = {'token':'do-not-log-token','repo':'owner/project','theme_path':'theme_pool.csv',
                       'history_path':self.paths['history'].name,'content_path':self.paths['contents'].name,'branch':'main'}

    def before(self):
        return {k:p.read_bytes() for k,p in self.paths.items()}

    def remote(self):
        return {'head':'a'*40,'tree':'b'*40,'files':self.before()}

    def adopt(self, **kwargs):
        return storage.adopt_director_draft(self.draft, themes=self.themes, theme_path=self.paths['themes'],
            history_path=self.paths['history'],content_path=self.paths['contents'],github_config={},
            now=NOW, model='test-model',allow_local=True,**kwargs)

    def test_adoption_appends_history_and_library_contents_and_reloads(self):
        self.draft['contents'][0]['availability_status']='available'
        result = self.adopt()
        self.assertEqual(result['status'],'local_only'); self.assertFalse(result['durable'])
        self.assertEqual(result['theme_id'],'T051')
        themes=pd.read_csv(self.paths['themes'],sep='|',dtype=str).fillna('')
        self.assertEqual(len(themes),2)
        self.assertEqual(themes.iloc[0]['operator_note'],'보존')
        self.assertEqual(themes.iloc[-1]['source_status'],'AI_DIRECTOR')
        self.assertEqual(themes.iloc[-1]['approved_status'],'ADOPTED')
        self.assertEqual(themes.iloc[-1]['director_draft_id'],self.draft['draft_id'])
        self.assertEqual(themes.iloc[-1]['director_adoption_key'],storage.director_adoption_key(self.draft))
        history=pd.read_csv(self.paths['history'],sep='|',dtype=str).fillna('')
        self.assertEqual(history.iloc[0]['run_id'],'old-run')
        self.assertEqual(history.iloc[0]['custom_history'],'이력 보존')
        self.assertEqual(history.iloc[-1]['recommended_at'],NOW.isoformat())
        self.assertEqual(history.iloc[-1]['recommendation_source'],'AI_DIRECTOR')
        self.assertTrue(history.iloc[-1]['run_id'].startswith('AI-DIRECTOR-'))
        self.assertEqual(result['recommendations'][0]['recommendation_source'],'AI_DIRECTOR')
        self.assertEqual(result['recommendations'][0]['source_label'],'AI 디렉터 채택')
        self.assertEqual(result['recommendations'][0]['theme']['source_status'],'AI_DIRECTOR')
        runs=load_recommendation_runs(self.paths['history'],themes)
        self.assertEqual(runs[0]['run_id'],result['run_id'])
        self.assertEqual(runs[0]['recommendations'][0]['theme']['theme_name'],self.draft['theme_name'])
        self.assertEqual(runs[0]['recommendations'][0]['recommendation_source'],'AI_DIRECTOR')
        frame=load_content_frame(self.paths['contents'])
        self.assertEqual(len(frame),11)
        self.assertEqual(len(content_map_for_scope(frame,'THEME_DB')['T051']),5)
        self.assertEqual(len(content_map_for_scope(frame,result['run_id'])['T051']),5)
        saved=pd.read_csv(self.paths['contents'],sep='|',dtype=str).fillna('')
        self.assertEqual(saved.iloc[0]['custom_content'],'추천 보존')
        self.assertEqual(set(saved.iloc[1:]['availability_status']),{'not_checked'})

    def test_identical_retry_is_idempotent_but_new_named_revision_can_be_adopted(self):
        first=self.adopt(); before=self.before()
        again=self.adopt()
        self.assertEqual(again['status'],'already_adopted')
        self.assertEqual(first['theme_id'],again['theme_id'])
        self.assertEqual(self.before(),before)
        key=storage.director_adoption_key(self.draft)
        self.draft['contents'][0]['fetched_at']='later'
        self.assertEqual(storage.director_adoption_key(self.draft),key)
        self.draft['theme_name']='되돌아온 심판자들의 잔혹한 재회'
        self.assertNotEqual(storage.director_adoption_key(self.draft),key)
        self.assertNotEqual(self.adopt()['theme_id'],first['theme_id'])

    def test_legacy_director_retry_preserves_old_source_labels_without_rewriting(self):
        first=self.adopt()
        for name, column in [('themes','source_status'),('history','recommendation_source')]:
            frame=pd.read_csv(self.paths[name],sep='|',dtype=str).fillna('')
            frame.loc[frame['theme_id'].eq(first['theme_id']),column]='AI_GENERATED'
            self.paths[name].write_bytes(csvbytes(frame))
        before=self.before()
        with patch('ai_director_storage._write_local') as write:
            retry=self.adopt()
        write.assert_not_called()
        self.assertEqual(retry['status'],'already_adopted')
        self.assertEqual(retry['theme']['source_status'],'AI_GENERATED')
        self.assertEqual(retry['recommendations'][0]['recommendation_source'],'AI_DIRECTOR')
        self.assertEqual(self.before(),before)

    def test_same_accepted_name_does_not_overwrite_previous_version(self):
        self.adopt(); before=self.before()
        self.draft['copy']='제목은 같지만 고친 카피'
        with self.assertRaises(storage.DirectorDuplicateError) as error:
            self.adopt()
        self.assertEqual(error.exception.theme_id,'T051')
        self.assertEqual(self.before(),before)

    def test_fewer_than_five_unknown_or_forged_candidates_never_mutate_csv(self):
        original=deepcopy(self.draft)
        for change in ('too_few','unknown','forged_id','forged_type','forged_external_source'):
            with self.subTest(change=change):
                self.draft=deepcopy(original)
                if change=='too_few': self.draft['contents']=self.draft['contents'][:4]
                elif change=='unknown': self.draft['contents'][0]['catalog_status']='unverified'
                elif change=='forged_id': self.draft['contents'][0]['content_id']='C999'
                elif change=='forged_type': self.draft['contents'][0]['content_type']='영화'
                else:
                    self.draft['contents'][0].update(catalog_status='verified_external',kinolights_status='verified',source_url='https://untrusted.example.org/title/pretend')
                before=self.before()
                with self.assertRaises(storage.DirectorStorageError): self.adopt()
                self.assertEqual(self.before(),before)

    def test_git_storage_is_required_unless_local_mode_is_explicit(self):
        before=self.before()
        with self.assertRaisesRegex(storage.DirectorStorageError,'GitHub'):
            storage.adopt_director_draft(self.draft,themes=self.themes, theme_path=self.paths['themes'],
                history_path=self.paths['history'],content_path=self.paths['contents'],github_config={})
        self.assertEqual(self.before(),before)

    def test_remote_commit_failure_preserves_all_local_files(self):
        before=self.before()
        writer=Mock(side_effect=storage.DirectorStorageError('저장 충돌'))
        with self.assertRaises(storage.DirectorStorageError):
            storage.adopt_director_draft(self.draft,themes=self.themes,theme_path=self.paths['themes'],
                history_path=self.paths['history'],content_path=self.paths['contents'],github_config=self.config,
                remote_reader=lambda *_:self.remote(),remote_writer=writer,now=NOW)
        self.assertEqual(self.before(),before)
        self.assertEqual(writer.call_count,1)

    def test_remote_new_rows_are_preserved_and_one_atomic_payload_is_committed(self):
        snapshot=self.remote()
        remote_themes=pd.concat([self.themes,pd.DataFrame([{'theme_id':'T099','theme_name':'먼 미래의 로봇들'}])],ignore_index=True).fillna('')
        snapshot['files']['themes']=csvbytes(remote_themes)
        captured={}
        def write(payloads,**kwargs):
            captured.update(payloads)
            self.assertEqual(self.before(),before)
            self.assertEqual(kwargs['head'],'a'*40)
            return {'status':'success','commit_sha':'c'*40}
        before=self.before()
        result=storage.adopt_director_draft(self.draft,themes=self.themes,theme_path=self.paths['themes'],
            history_path=self.paths['history'],content_path=self.paths['contents'],github_config=self.config,
            remote_reader=lambda *_:snapshot,remote_writer=write,now=NOW)
        self.assertEqual(result['status'],'success');self.assertTrue(result['durable'])
        self.assertEqual(result['theme_id'],'T100')
        self.assertEqual(set(captured),{'themes','history','contents'})
        themes=pd.read_csv(io.BytesIO(captured['themes']),sep='|').fillna('')
        self.assertEqual(set(themes.theme_id),{'T050','T099','T100'})
        self.assertEqual(themes.loc[themes.theme_id.eq('T100'),'source_status'].iloc[0],'AI_DIRECTOR')
        original=themes.loc[themes.theme_id.eq('T050')].iloc[0]
        self.assertEqual({key:original[key] for key in self.themes.columns},self.themes.iloc[0].to_dict())
        history=pd.read_csv(io.BytesIO(captured['history']),sep='|',dtype=str).fillna('')
        self.assertEqual(history.iloc[-1]['recommendation_source'],'AI_DIRECTOR')
        self.assertEqual({key:history.iloc[0][key] for key in self.history.columns},self.history.iloc[0].to_dict())
        candidates=pd.read_csv(io.BytesIO(captured['contents']),sep='|',dtype=str).fillna('')
        self.assertEqual({key:candidates.iloc[0][key] for key in self.candidates.columns},self.candidates.iloc[0].to_dict())

    def test_concurrent_remote_edit_is_rejected_before_write(self):
        snapshot=self.remote()
        changed=self.themes.copy();changed.loc[0,'copy']='다른 사람이 고친 카피'
        snapshot['files']['themes']=csvbytes(changed)
        writer=Mock();before=self.before()
        with self.assertRaises(storage.DirectorStorageError):
            storage.adopt_director_draft(self.draft,themes=self.themes,theme_path=self.paths['themes'],
                history_path=self.paths['history'],content_path=self.paths['contents'],github_config=self.config,
                remote_reader=lambda *_:snapshot,remote_writer=writer,now=NOW)
        writer.assert_not_called();self.assertEqual(self.before(),before)

    def test_remote_retry_finds_the_saved_revision_without_another_commit(self):
        snapshot=self.remote()
        writes=[]
        def write(payloads,**kwargs):
            writes.append(kwargs['run_id'])
            snapshot['files']=dict(payloads)
            snapshot['head']='c'*40
            return {'status':'success','commit_sha':'c'*40}
        kwargs=dict(themes=self.themes,theme_path=self.paths['themes'],history_path=self.paths['history'],
                    content_path=self.paths['contents'],github_config=self.config,
                    remote_reader=lambda *_:snapshot,remote_writer=write,now=NOW)
        first=storage.adopt_director_draft(self.draft,**kwargs)
        before=self.before()
        retry=storage.adopt_director_draft(self.draft,**kwargs)
        self.assertEqual(retry['status'],'already_adopted')
        self.assertEqual(retry['theme_id'],first['theme_id'])
        self.assertEqual(len(writes),1)
        self.assertEqual(self.before(),before)

    def test_local_change_during_remote_save_is_never_overwritten(self):
        snapshot=self.remote()
        changed=csvbytes(pd.concat([self.themes,pd.DataFrame([{'theme_id':'T999','theme_name':'동시에 작성한 로컬 테마'}])],ignore_index=True).fillna(''))
        def write(*args,**kwargs):
            self.paths['themes'].write_bytes(changed)
            return {'status':'success','commit_sha':'c'*40}
        result=storage.adopt_director_draft(self.draft,themes=self.themes,theme_path=self.paths['themes'],
            history_path=self.paths['history'],content_path=self.paths['contents'],github_config=self.config,
            remote_reader=lambda *_:snapshot,remote_writer=write,now=NOW)
        self.assertEqual(result['status'],'success')
        self.assertEqual(result['local_status'],'refresh_required')
        self.assertEqual(self.paths['themes'].read_bytes(),changed)

    def test_file_changed_during_staging_is_preserved_before_any_replace(self):
        before=self.before()
        changed=before['themes']+b'concurrent edit\n'
        payloads={name:b'new adoption snapshot\n' for name in self.paths}
        original_write=Path.write_bytes
        injected=[]
        def interleaved_write(path, data):
            result=original_write(path,data)
            if '.director-' in path.name and not injected:
                injected.append(True)
                original_write(self.paths['themes'],changed)
            return result
        with patch.object(Path,'write_bytes',interleaved_write), patch('ai_director_storage.os.replace') as replace:
            status=storage._write_local(self.paths,payloads,before)
        self.assertEqual(status,'refresh_required')
        replace.assert_not_called()
        self.assertEqual(self.paths['themes'].read_bytes(),changed)
        self.assertEqual(self.paths['history'].read_bytes(),before['history'])
        self.assertEqual(self.paths['contents'].read_bytes(),before['contents'])
        self.assertEqual(list(self.root.glob('*.director-*.tmp')),[])

    def test_real_git_protocol_uses_one_tree_and_commit_and_non_force_ref(self):
        replies=[]
        for sha in ('d'*40,'e'*40,'f'*40,'1'*40,'2'*40):
            response=Mock(status_code=201);response.json.return_value={'sha':sha};replies.append(response)
        response=Mock(status_code=200);response.json.return_value={'object':{'sha':'2'*40}};replies.append(response)
        with patch('ai_director_storage.requests.request',side_effect=replies) as request:
            storage._commit_remote_snapshot({'themes':b'a','history':b'b','contents':b'c'},config=self.config,
                remote_paths={'themes':'themes.csv','history':'history.csv','contents':'contents.csv'},head='a'*40,tree='b'*40,run_id='one')
        methods=[call.args[0] for call in request.call_args_list]
        self.assertEqual(methods,['POST','POST','POST','POST','POST','PATCH'])
        self.assertEqual(request.call_args_list[4].kwargs['json']['parents'],['a'*40])
        self.assertFalse(request.call_args_list[-1].kwargs['json']['force'])
        self.assertEqual(len(request.call_args_list[3].kwargs['json']['tree']),3)


if __name__=='__main__':unittest.main()
