"""Existing content refreshes preserve adopted provenance in other scopes."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import pandas as pd
from theme_content_manager import ensure_content_schema,upsert_content_frame,load_content_frame,content_map_for_scope


class DirectorProvenanceTests(unittest.TestCase):
    def test_unrelated_legacy_refresh_keeps_verified_identity_and_unknown_availability(self):
        old=ensure_content_schema(pd.DataFrame([{'run_id':'THEME_DB','theme_key':'ADOPTED-1','title':'검증 작품','rank':'1',
             'catalog_status':'confirmed_local','content_id':'LOCAL-1','availability_status':'not_checked','custom_column':'preserve'}]))
        incoming=ensure_content_schema(pd.DataFrame([{'run_id':'THEME_DB','theme_key':'OLD-2','title':'기존 작품','rank':'1'}]))
        with TemporaryDirectory() as folder:
            path=Path(folder)/'candidates.csv'; old.to_csv(path,sep='|',index=False)
            upsert_content_frame(path,incoming)
            saved=load_content_frame(path)
            actual=saved[saved.theme_key.eq('ADOPTED-1')].iloc[0]
            self.assertEqual(actual['catalog_status'],'confirmed_local')
            self.assertEqual(actual['content_id'],'LOCAL-1')
            self.assertEqual(actual['availability_status'],'not_checked')
            self.assertEqual(actual['custom_column'],'preserve')
            rendered=content_map_for_scope(saved,'THEME_DB')['ADOPTED-1'][0]
            self.assertEqual(rendered['catalog_status'],'confirmed_local')
            self.assertEqual(rendered['availability_status'],'not_checked')
