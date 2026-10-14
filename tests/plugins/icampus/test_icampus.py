import json
import fcntl
import subprocess
import sys
import time
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from plugins.icampus.icampus import Client, CollectionError, TZ, clean, compare, current_courses, module_records, write

class ICampusTests(unittest.TestCase):
    def test_current_regular_only_and_future_excluded(self):
        courses=[{'id':n,'term':{'name':name,'start_at':start}} for n,name,start in [
            (1,'2026년 1학기','2026-02-01T00:00:00Z'),(2,'2026년 2학기','2026-08-01T00:00:00Z'),
            (3,'2026 비정규',None),(4,'2027년 1학기','2027-02-01T00:00:00Z')]]
        self.assertEqual([c['id'] for c in current_courses(courses,datetime(2026,9,15,tzinfo=TZ))],[2])
    def test_pagination_follows_all_pages(self):
        session=Mock()
        def response(data,next_url=None):
            r=Mock(status_code=200,text='while(1);'+json.dumps(data),url='https://canvas.skku.edu/api/v1/courses')
            r.links={'next':{'url':next_url}} if next_url else {};return r
        session.get.side_effect=[response([{'id':1}],'https://canvas.skku.edu/api/v1/courses?page=2'),response([{'id':2}])]
        self.assertEqual(Client(session,float('inf')).all('/api/v1/courses'),[{'id':1},{'id':2}])
        self.assertEqual(session.get.call_count,2)
    def test_cross_host_pagination_is_rejected(self):
        with self.assertRaises(CollectionError):Client(Mock(),float('inf')).get('https://evil.example/api')
    def test_disappearance_not_completion_and_deadline_change(self):
        changes=compare([{'id':'a','due_at':'old'},{'id':'b','site_status':'incomplete'}],[{'id':'a','due_at':'new'}])
        self.assertEqual(changes[0]['old_due_at'],'old');self.assertEqual(changes[1]['type'],'not_seen')
        self.assertNotIn('completed',str(changes))
    def test_auth_material_removed_recursively(self):
        result=clean({'secure_params':'secret','nested':{'token':'secret','url':'https://canvas.skku.edu/files/1?verifier=secret&download=1'}})
        self.assertNotIn('secret',str(result));self.assertIn('download=1',str(result))
    def test_unknown_completion_not_assumed(self):
        x=module_records(1,[{'module_items':[{'module_item_id':2,'content_data':{'opened':False}}]}])[0]
        self.assertEqual(x['site_status'],'unknown');self.assertTrue(x['locked'])
    def test_atomic_private_write(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'report.json';write(p,{'old':1});write(p,{'new':2})
            self.assertEqual(json.loads(p.read_text()),{'new':2});self.assertEqual(p.stat().st_mode&0o777,0o600)

    def test_metadata_classification_keeps_all_types(self):
        items=[]
        for n,typ in enumerate(('pdf','everlec','zoom','file','screenlecture')):
            items.append({'module_item_id':n,'content_data':{'item_content_data':{'content_type':typ,'content':'not needed'}}})
        rows=module_records(1,[{'module_items':items}])
        self.assertEqual([r['category'] for r in rows],['자료','강의','실시간 강의','자료','강의'])
        self.assertTrue(all('content' not in r['content'] and not r['body'] for r in rows))
    def test_missing_url_is_string_and_lesson_fields_preserved(self):
        from plugins.icampus.icampus import item_record
        self.assertIsInstance(item_record(1,'quiz',{'id':1,'url':None})['url'],str)
        self.assertEqual(clean({'lessons':[{'lesson_position':1}]}),{'lessons':[{'lesson_position':1}]})
    def test_retries_stop_after_three_server_errors(self):
        session=Mock();session.get.return_value=Mock(status_code=503)
        with patch('plugins.icampus.icampus.time.sleep'),self.assertRaises(CollectionError):
            Client(session,float('inf')).get('/api/v1/courses')
        self.assertEqual(session.get.call_count,3)
    def test_live_process_lock_blocks_second_run(self):
        with tempfile.TemporaryDirectory() as d:
            with open(Path(d)/'run.lock','w') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                r=subprocess.run([sys.executable,'-m','plugins.icampus.icampus','--root',d],capture_output=True,text=True,timeout=10)
                self.assertEqual(r.returncode,0);self.assertIn('already running',r.stdout)
                self.assertFalse((Path(d)/'session.json').exists())
    def test_failed_run_preserves_last_good_files(self):
        from plugins.icampus.icampus import main
        with tempfile.TemporaryDirectory() as d:
            paths=[Path(d)/n for n in ('latest.md','tasks.json','last-collected.json')]
            for p in paths:p.write_text('previous good result')
            with patch.object(sys,'argv',['icampus','--root',d]),patch('plugins.icampus.icampus.collect',side_effect=CollectionError('network failure')):
                self.assertEqual(main(),1)
            self.assertTrue(all(p.read_text()=='previous good result' for p in paths))
            self.assertEqual(len(list((Path(d)/'runs').glob('failure-*.json'))),1)

if __name__=='__main__':unittest.main()
