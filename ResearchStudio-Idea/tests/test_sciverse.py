"""Offline Sciverse regression tests. Run with unittest discovery from repo root."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
IDEA = ROOT/'skills/idea_spark'
SEARCH = ROOT/'skills/paper_search/scripts'
sys.path.insert(0, str(IDEA))
sys.path.insert(0, str(SEARCH))
from scripts import sciverse_client as sc
from scripts import fetch_sections
from postprocess import dedup


def hit(**kw):
    d = dict(title='Widget Gating', publication_published_year=2025.0,
             publication_published_date='2025-06-01', unique_id='paper:10.1/widget',
             doc_id='doc-1', doi='10.1/widget', author=[{'name':'Alice'}, 'Alice'],
             publication_venue_type='journal', citation_count=5)
    d.update(kw)
    return d


class SciverseTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'SCIVERSE_API_TOKEN':'test-secret',
                              'SCIVERSE_BASE_URL':'https://api.sciverse.space',
                              'SCIVERSE_DISABLED':'0','SCIVERSE_SEMANTIC':'1'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_standalone_clients_identical(self):
        self.assertEqual((IDEA/'scripts/sciverse_client.py').read_bytes(),
                         (SEARCH/'sciverse_client.py').read_bytes())

    def test_host_and_redirect_guard(self):
        for url in ['http://api.sciverse.space','https://sciverse.space.evil.test',
                    'https://user:pass@api.sciverse.space','https://api.sciverse.space:444']:
            with patch.dict(os.environ, {'SCIVERSE_BASE_URL':url}):
                with self.assertRaises(sc.SciverseError): sc.Client()
        with self.assertRaises(sc.SciverseError):
            sc._NoRedirect().redirect_request(None,None,302,'',{},'https://example.com')

    def test_missing_token_and_opt_out(self):
        with patch.dict(os.environ, {'SCIVERSE_API_TOKEN':''}):
            self.assertFalse(sc.available())
            with self.assertRaises(sc.SciverseError): sc.Client()
        with patch.dict(os.environ, {'SCIVERSE_DISABLED':'1'}):self.assertFalse(sc.available())

    def test_dates_authors_ids_and_snippets(self):
        p=sc.normalize(hit(publication_published_date='2024-06-01',chunk='evidence',offset=16))
        self.assertIsNone(p['publication_date'])
        self.assertEqual(p['year'],2025)
        self.assertEqual(p['authors'],['Alice'])
        self.assertEqual(p['sciverse_records'][0]['unique_id'],'paper:10.1/widget')
        self.assertEqual(p['sciverse_records'][0]['doc_id'],'doc-1')
        self.assertEqual(p['sciverse_records'][0]['snippets'][0]['offset'],16)
        self.assertEqual(p['abstract'],'')  # snippet must never masquerade as abstract/full text

    def test_duplicate_chunks_collapse_and_filters_are_sent(self):
        client=sc.Client()
        with patch.object(client,'request',side_effect=[{'results':[hit()], 'total_pages':1},
                   {'hits':[hit(chunk='one',offset=10),hit(chunk='two',offset=20),
                            hit(title='Future',publication_published_year=2030)]}]) as req:
            rows=client.search('widgets',2024,2026,5)
        self.assertEqual(len(rows),1)
        self.assertEqual(len(rows[0]['sciverse_records'][0]['snippets']),2)
        body=req.call_args_list[0].args[1]
        self.assertIn('filters',body);self.assertIn('doc_id',body['fields'])
        self.assertNotIn('year_from',body)

    def test_semantic_failure_preserves_metadata(self):
        client=sc.Client()
        with patch.object(client,'request',side_effect=[{'results':[hit()], 'total_pages':1},
                                                       sc.SciverseError('HTTP 503')]):
            self.assertEqual(len(client.search('widgets',2024,2026,5)),1)

    def test_cross_source_dedup_preserves_sciverse_evidence(self):
        source=sc.normalize(hit(chunk='a',offset=1))
        preferred=dict(source,source='open_alex',sciverse_records=[])
        rows=dedup({'open_alex':[preferred], 'sciverse':[source]})
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['source'],'open_alex')
        self.assertEqual(rows[0]['sciverse_records'][0]['doc_id'],'doc-1')
        self.assertIn('sciverse',rows[0]['found_in'])

    def test_ideaspark_merge_preserves_pointers(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths=[Path(tmp)/n for n in ['oa.json','sc.json','out.json']]
            first=dict(title='Widget Gating',title_norm='widget gating',year=2025,
                       source='openalex',source_id='W1')
            paths[0].write_text(json.dumps([first]));paths[1].write_text(json.dumps([sc.normalize(hit())]))
            subprocess.run([sys.executable,'-m','scripts.dedup_merge','--inputs',str(paths[0]),
                            str(paths[1]),'--out',str(paths[2])],cwd=IDEA,check=True,capture_output=True)
            row=json.loads(paths[2].read_text())[0]
            self.assertEqual(row['paper_id'],'openalex:W1')
            self.assertEqual(row['sciverse_records'][0]['doc_id'],'doc-1')

    def test_content_cursor_and_partial_read(self):
        client=sc.Client()
        pages=[{'text':'介绍','more':True,'next_offset':120,'bytes_returned':6},
               {'text':'method','more':False,'next_offset':126,'bytes_returned':6}]
        with patch.object(client,'request',side_effect=pages) as req:
            doc=client.read_document('doc-1')
            self.assertEqual(req.call_args_list[1].kwargs['params']['offset'],120)
        self.assertTrue(doc['complete']);self.assertEqual(len(doc['ranges']),2)
        with patch.object(client,'request',return_value=pages[0]):
            self.assertFalse(client.read_document('doc-1',max_pages=1)['complete'])
        with patch.object(client,'request',return_value={'text':'x','more':True,'next_offset':0}):
            with self.assertRaises(sc.SciverseError):client.read_document('doc-1')

    def test_exact_identity_resolution(self):
        client=sc.Client()
        with patch.object(client,'request',return_value={'results':[hit(title='Other',doi='10.2/wrong')]}):
            self.assertEqual(client.locate({'doi':'10.1/widget','title':'Widget Gating'}),[])
            self.assertEqual(client.locate({'title':'Widget Gate'}),[])

    def test_http_error_is_redacted_and_retry_bounded(self):
        client=sc.Client()
        error=urllib.error.HTTPError(client.base+'/meta-search',401,'test-secret',{},io.BytesIO(b'test-secret'))
        with patch.object(client.opener,'open',side_effect=error):
            with self.assertRaises(sc.SciverseError) as caught:client.request('/meta-search',{})
        self.assertNotIn('test-secret',str(caught.exception))
        error=urllib.error.HTTPError(client.base,429,'',{'Retry-After':'120'},io.BytesIO())
        with patch.object(client.opener,'open',side_effect=error) as call:
            with self.assertRaises(sc.SciverseError):client.request('/meta-search',{})
            self.assertEqual(call.call_count,1)

    def test_section_parsing_and_provenance_survive_cache(self):
        document={'text':'# 1 Introduction\nContext.\n# 2 Method\nA real method.\n# 3 Experiments\nResults.',
                  'complete':False,'ranges':[{'offset':0,'next_offset':100}], 'next_offset':100}
        with patch.object(sc.Client,'locate',return_value=[{'doc_id':'doc-1','unique_id':'paper:1'}]), \
             patch.object(sc.Client,'read_document',return_value=document):
            sections=fetch_sections._fetch_sciverse_sections({},time.monotonic()+10)
        self.assertEqual(sections['method'],'A real method.')
        self.assertFalse(sections['sciverse_content_complete']);self.assertTrue(sections['warning'])
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'IDEASPARK_FETCH_CACHE':tmp}):
            fetch_sections._fetch_cache_put({'paper_id':'sciverse:1'},sections)
            self.assertEqual(fetch_sections._fetch_cache_get({'paper_id':'sciverse:1'}),sections)

    def test_paper_search_skips_optional_source_without_token(self):
        import search_papers
        with patch.dict(os.environ, {'SCIVERSE_DISABLED':'1'}), \
             patch.object(search_papers,'_run_source_workers',return_value={}) as worker:
            search_papers.search_papers('widgets',2024,2026)
        self.assertNotIn('sciverse',worker.call_args.args[0])

    def test_multi_query_merge_keeps_all_chunk_offsets(self):
        from scripts._merge import interleave_by_query
        a=sc.normalize(hit(chunk='one',offset=1))
        b=sc.normalize(hit(chunk='two',offset=2))
        rows=interleave_by_query([[a],[b]],lambda r:r['title_norm'],queries=['q1','q2'])
        self.assertEqual(rows[0]['from_query'],['q1','q2'])
        self.assertEqual(len(rows[0]['sciverse_records'][0]['snippets']),2)

    def test_cli_exports_json_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'papers.json'
            env=dict(os.environ,SCIVERSE_DISABLED='1')
            subprocess.run([sys.executable,str(SEARCH/'search_papers.py'),'--query','widget',
                            '--start-year','2024','--end-year','2026','--sources','sciverse',
                            '--json',str(path)],env=env,check=True,capture_output=True)
            self.assertEqual(json.loads(path.read_text()),{'sciverse':[]})

    def test_ideaspark_wiring_is_optional(self):
        code=("from scripts.run import CONNECTORS,PHASE0_RETRIEVAL_JOBS; "
              "import json; print(json.dumps({'connectors':[c[0] for c in CONNECTORS],"
              "'jobs':[j['job'] for j in PHASE0_RETRIEVAL_JOBS]}))")
        for disabled in ['0','1']:
            env=dict(os.environ,SCIVERSE_DISABLED=disabled)
            p=subprocess.run([sys.executable,'-c',code],cwd=IDEA,env=env,
                             check=True,capture_output=True,text=True)
            data=json.loads(p.stdout)
            self.assertEqual('sciverse' in data['connectors'],disabled=='0')
            self.assertEqual('sciverse_recent' in data['jobs'],disabled=='0')

    def test_failed_fulltext_falls_through(self):
        with patch.object(sc.Client,'locate',side_effect=sc.SciverseError('unavailable')):
            self.assertIsNone(fetch_sections._fetch_sciverse_sections({},time.monotonic()+10))


if __name__=='__main__': unittest.main()
