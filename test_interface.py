"""Interface call-path tests with a minimal fake Streamlit (NOT a browser test)."""
import copy
import io
import json
import sys
import unittest
from unittest.mock import patch

import peds_clerkship_tracker as a
import test_core as c


class File:
    def __init__(self, name, data): self.name,self.data=name,data
    def getvalue(self): return self.data


class Context:
    def __enter__(self):return self
    def __exit__(self,*args):return False


class FakeStreamlit:
    def __init__(self, files=None, create=False, state=None, secrets=None):
        self.session_state={} if state is None else state
        self.secrets={'TRUSTED_HOST_AUTH':'true'} if secrets is None else secrets
        self.files=files or []
        self.create=create
        self.calls=[]
        self.downloads=[]
        self.errors=[]
        self.upload_labels=[]
    def set_page_config(self,**kw):self.calls.append(('config',kw))
    def title(self,s):self.calls.append(('title',s))
    def caption(self,s):self.calls.append(('caption',s))
    def subheader(self,s):self.calls.append(('subheader',s))
    def write(self,s):self.calls.append(('write',s))
    def info(self,s):self.calls.append(('info',s))
    def warning(self,s):self.calls.append(('warning',s))
    def error(self,s):self.errors.append(s)
    def dataframe(self,*args,**kw):pass
    def divider(self):pass
    def expander(self,*args,**kw):return Context()
    def spinner(self,*args,**kw):return Context()
    def form(self,*args,**kw):return Context()
    def form_submit_button(self,*args,**kw):return False
    def text_input(self,*args,**kw):return kw.get('value','')
    def button(self,label,**kw):
        self.calls.append(('button',label))
        return self.create and kw.get('key')=='create_files' and not kw.get('disabled',False)
    def checkbox(self,label,**kw):return self.session_state.get(kw.get('key'),kw.get('value',False))
    def file_uploader(self,label,**kw):
        self.upload_labels.append(label)
        key=kw.get('key','')
        if key.startswith('source_'):
            i=int(key.split('_')[1]);return self.files[i] if i<len(self.files) else None
        return None
    def download_button(self,label,data,file_name=None,mime=None,**kw):
        self.downloads.append({'label':label,'data':data,'filename':file_name,'disabled':kw.get('disabled',False)})
        return False
    def stop(self):raise StopIteration
    def rerun(self):raise StopIteration


def uploads():
    return [File(name,a.csv_bytes(rows)) for name,rows in (
        ('schedule.csv',c.SCHEDULE),('checklist.csv',[c.check()]),('matches.csv',[c.match()]),('oasis_me_20260919.csv',c.assessment()))]


def execute(fake):
    with patch.dict(sys.modules,{'streamlit':fake}):
        a.main()


class InterfaceTests(unittest.TestCase):
    def test_home_has_exactly_four_uploads(self):
        st=FakeStreamlit();execute(st)
        self.assertEqual(len(st.upload_labels),4)
        self.assertFalse(st.errors)

    def test_home_has_no_api_or_export_configuration_inputs(self):
        st=FakeStreamlit();execute(st)
        self.assertEqual(st.upload_labels,['Rotation schedule','Updated checklist','Preceptor match file','OASIS ME evaluation export'])

    def test_data_dictionary_available_before_processing(self):
        st=FakeStreamlit();execute(st)
        self.assertIn('redcap_data_dictionary.csv',[d['filename'] for d in st.downloads])

    def test_one_button_generates_four_downloads(self):
        st=FakeStreamlit(uploads(),True,{'options':{'as_of':'2026-09-19'}});execute(st)
        names={d['filename'] for d in st.downloads if not d['disabled']}
        self.assertTrue(set(a.REMINDER_NAMES)|{'redcap_import.csv'} <= names)
        self.assertFalse(st.errors)

    def test_no_new_api_token_required_to_process(self):
        st=FakeStreamlit(uploads(),True,{'options':{'as_of':'2026-09-19'}},secrets={'TRUSTED_HOST_AUTH':'true'});execute(st)
        self.assertTrue(st.session_state['result']['redcap'])

    def test_source_change_hides_stale_downloads(self):
        st=FakeStreamlit(uploads(),True,{'options':{'as_of':'2026-09-19'}});execute(st)
        later=uploads();later[1]=File('changed.csv',a.csv_bytes([c.check(status='Incomplete')]))
        again=FakeStreamlit(later,False,st.session_state);execute(again)
        self.assertNotIn('redcap_import.csv',[d['filename'] for d in again.downloads])
        self.assertTrue(any(kind=='info' and 'changed' in content for kind,content in again.calls))

    def test_exclusion_change_hides_stale_downloads(self):
        st=FakeStreamlit(uploads(),True,{'options':{'as_of':'2026-09-19'}});execute(st)
        st.session_state['rules']=a.normalize_rules([{'record_id':'s001','evaluator':'Able, Jamie'}])
        again=FakeStreamlit(uploads(),False,st.session_state);execute(again)
        self.assertNotIn('redcap_import.csv',[d['filename'] for d in again.downloads])

    def test_default_exclusions_in_session(self):
        st=FakeStreamlit();execute(st)
        self.assertEqual(st.session_state['rules'],a.normalize_rules(a.LEGACY_EXCLUSIONS))

    def test_secrets_rules_override_defaults_including_disabled(self):
        rules=a.normalize_rules(a.LEGACY_EXCLUSIONS);rules[0]['active']=False
        st=FakeStreamlit(secrets={'TRUSTED_HOST_AUTH':'true','EXCLUSIONS_JSON':json.dumps(rules)});execute(st)
        self.assertFalse(st.session_state['rules'][0]['active'])

    def test_explicit_empty_secrets_rules_disable_all(self):
        st=FakeStreamlit(secrets={'TRUSTED_HOST_AUTH':'true','EXCLUSIONS_JSON':'[]'});execute(st)
        self.assertEqual(st.session_state['rules'],[])

    def test_invalid_secrets_rule_blocks_instead_of_silent_reset(self):
        st=FakeStreamlit(secrets={'TRUSTED_HOST_AUTH':'true','EXCLUSIONS_JSON':'not json'})
        with self.assertRaises(StopIteration):execute(st)
        self.assertTrue(st.errors)
        self.assertEqual(st.upload_labels,[])

    def test_missing_auth_blocks_uploaders(self):
        st=FakeStreamlit(secrets={})
        with self.assertRaises(StopIteration):execute(st)
        self.assertEqual(st.upload_labels,[])

    def test_malformed_source_has_no_redcap_download(self):
        files=uploads();files[1]=File('bad.csv',b'a,b\n1,2,3\n')
        st=FakeStreamlit(files,True,{'options':{'as_of':'2026-09-19'}});execute(st)
        self.assertTrue(st.errors)
        self.assertNotIn('redcap_import.csv',[d['filename'] for d in st.downloads])

    def test_missing_timestamp_does_not_remove_ready_reminders(self):
        raw=c.check();raw['Time entered']='';files=uploads();files[1]=File('checklist.csv',a.csv_bytes([raw]))
        st=FakeStreamlit(files,True,{'options':{'as_of':'2026-09-19'}});execute(st)
        self.assertTrue(set(a.REMINDER_NAMES) <= {d['filename'] for d in st.downloads if not d['disabled']})
        self.assertTrue(next(d['disabled'] for d in st.downloads if d['filename']=='redcap_import.csv'))

    def test_mismatched_cohort_withholds_affected_downloads(self):
        files=uploads();files[1]=File('checklist.csv',a.csv_bytes([c.check(start='2026-07-06')]))
        st=FakeStreamlit(files,True,{'options':{'as_of':'2026-09-19'}});execute(st)
        disabled={d['filename']:d['disabled'] for d in st.downloads}
        self.assertTrue(disabled['redcap_import.csv'])
        self.assertTrue(disabled[a.REMINDER_NAMES[0]])


if __name__=='__main__':unittest.main()
