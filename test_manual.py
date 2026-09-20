"""Synthetic tests of offline REDCap CSV preparation. No external service is used."""
import copy
import csv
import io
import json
import sys
import unittest
from unittest.mock import patch

import peds_clerkship_tracker as a
import test_tracker as core
import test_simple as ui


def full_reference(run=None, rows=None):
    run = run or core.run()
    data = copy.deepcopy(rows if rows is not None else [core.parent()])
    names = set(a.MANUAL_REFERENCE_COLUMNS)
    names.update(k for r in a.import_candidates(run, False) for k in r)
    for r in data:
        for name in names:
            r.setdefault(name, '')
    return data


def source_for(run=None):
    return ui.sources()


def prepare(reference=None, settings=None, metadata=None, sources=None, baseline=None):
    return a.prepare_manual_import(sources or ui.sources(), reference if reference is not None else full_reference(),
                                   settings or core.run().settings, metadata, baseline_rules=baseline)


class ManualCSVTests(unittest.TestCase):
    def test_reference_required(self):
        self.assertTrue(prepare(reference=[]).plan.errors)

    def test_partial_report_blocked(self):
        self.assertTrue(prepare(reference=[core.parent()]).plan.errors)

    def test_no_parent_records_blocked(self):
        rows=full_reference(rows=[{'record_id':'s001','redcap_repeat_instrument':'oasis_eval','redcap_repeat_instance':'1'}])
        self.assertTrue(prepare(reference=rows).plan.errors)

    def test_duplicate_parents_blocked(self):
        self.assertTrue(prepare(reference=full_reference(rows=[core.parent(),core.parent()])).plan.errors)

    def test_blank_record_id_blocked(self):
        rows=full_reference(); rows[0]['record_id']=''
        self.assertTrue(prepare(reference=rows).plan.errors)

    def test_parent_repeat_instance_blocked(self):
        rows=full_reference(); rows[0]['redcap_repeat_instance']='1'
        self.assertTrue(prepare(reference=rows).plan.errors)

    def test_longitudinal_blocked(self):
        rows=full_reference(); rows[0]['redcap_event_name']='event_1_arm_1'
        self.assertTrue(prepare(reference=rows).plan.errors)

    def test_no_network_in_manual_prepare(self):
        with patch.object(a.RedcapClient,'call',side_effect=AssertionError('no calls')), patch.object(a.RedcapClient,'read',side_effect=AssertionError('no reads')), patch.object(a.RedcapClient,'upload',side_effect=AssertionError('no writes')):
            result=prepare()
            self.assertFalse(result.plan.errors,result.plan.errors)
            self.assertTrue(a.manual_import_csv(result.plan))

    def test_header_starts_with_identifiers(self):
        result=prepare()
        reader=csv.DictReader(io.StringIO(a.manual_import_csv(result.plan).decode('utf-8-sig')))
        self.assertEqual(reader.fieldnames[:3],list(a.REPEAT))
        self.assertEqual(len(reader.fieldnames),len(set(reader.fieldnames)))
        self.assertTrue(all(r['redcap_repeat_instance'].isdigit() for r in reader if r['redcap_repeat_instrument']))

    def test_bom_and_roundtrip(self):
        result=prepare(); data=a.manual_import_csv(result.plan)
        self.assertTrue(data.startswith(b'\xef\xbb\xbf'))
        rows=ui.rows(data)
        self.assertEqual(len(rows),len(result.plan.records))
        for got,want in zip(rows,result.plan.records):
            for k,v in want.items(): self.assertEqual(got[k],a.text(v))

    def test_reimport_same_snapshot_after_simulated_import_is_noop(self):
        ref=full_reference(); first=prepare(reference=ref)
        after=full_reference(rows=core.apply_plan(ref,first.plan))
        second=prepare(reference=after)
        self.assertFalse(second.plan.errors,second.plan.errors)
        self.assertFalse(second.plan.records)
        with self.assertRaises(ValueError): a.manual_import_csv(second.plan)

    def test_old_instance_kept(self):
        ref=full_reference(); first=prepare(reference=ref)
        after=core.apply_plan(ref,first.plan)
        e=next(r for r in after if r['redcap_repeat_instrument']=='oasis_eval')
        e['redcap_repeat_instance']='17'; e['student_email']=''
        result=prepare(reference=full_reference(rows=after))
        self.assertFalse(result.plan.errors,result.plan.errors)
        target=next(r for r in result.plan.records if r['redcap_repeat_instrument']=='oasis_eval')
        self.assertEqual(target['redcap_repeat_instance'],'17')

    def test_appends_after_max_not_row_order(self):
        ref=full_reference(); first=prepare(reference=ref)
        after=core.apply_plan(ref,first.plan)
        e=next(r for r in after if r['redcap_repeat_instrument']=='oasis_eval'); e['redcap_repeat_instance']='17'
        src=ui.sources(); src[3]=a.csv_bytes(core.assessment()+core.assessment(form='f2',assessor='Baker, Robin',email='robin@example.invalid',submit='2026-09-06 11:00:00'))
        result=prepare(reference=full_reference(rows=after),sources=src)
        self.assertFalse(result.plan.errors,result.plan.errors)
        target=next(r for r in result.plan.records if r['redcap_repeat_instrument']=='oasis_eval')
        self.assertEqual(target['redcap_repeat_instance'],'18')

    def test_conflict_preserved(self):
        ref=full_reference(); after=core.apply_plan(ref,prepare(reference=ref).plan)
        e=next(r for r in after if r['redcap_repeat_instrument']=='oasis_eval'); e['cas_strengths']='Director verified wording'
        result=prepare(reference=full_reference(rows=after))
        self.assertTrue(any(c['action']=='Conflict — preserved' for c in result.plan.changes))
        self.assertFalse(any('cas_strengths' in r for r in result.plan.records))
        self.assertFalse(result.plan.clear_records)

    def test_unrelated_and_final_grade_fields_never_written(self):
        ref=full_reference();ref[0].update(final_grade='PASS',nbme='90',gb_why='Manual note',name='Alex Example')
        result=prepare(reference=ref)
        keys={k for r in result.plan.records for k in r}
        self.assertFalse({'final_grade','nbme','gb_why','name'} & keys)

    def test_calculated_fields_not_written_with_dictionary(self):
        meta=core.meta_for(a.import_candidates(core.run(),False),[{'field_name':'tot','form_name':'oasis_eval','field_type':'calc'}])
        result=prepare(metadata=meta)
        self.assertTrue(result.metadata_checked)
        self.assertFalse(result.plan.errors,result.plan.errors)
        self.assertFalse(any('tot' in r for r in result.plan.records))

    def test_dictionary_missing_reports_not_fully_validated(self):
        result=prepare()
        self.assertFalse(result.metadata_checked)
        self.assertTrue(any('not fully validated' in w for w in result.plan.warnings))

    def test_coded_assessment_without_dictionary_blocked(self):
        ref=full_reference(); after=core.apply_plan(ref,prepare(reference=ref).plan)
        e=next(r for r in after if r['redcap_repeat_instrument']=='oasis_eval');e['evaluation']='1'
        result=prepare(reference=full_reference(rows=after))
        self.assertTrue(result.plan.errors)
        with self.assertRaises(ValueError): a.manual_import_csv(result.plan)

    def test_coded_assessment_with_dictionary_maps_same_instance(self):
        ref=full_reference();after=core.apply_plan(ref,prepare(reference=ref).plan)
        e=next(r for r in after if r['redcap_repeat_instrument']=='oasis_eval');e['evaluation']='1';e['student_email']=''
        meta=core.meta_for(a.import_candidates(core.run(),False),[{'field_name':'evaluation','form_name':'oasis_eval','field_type':'dropdown','select_choices_or_calculations':'1, Clinical Assessment of Student'}])
        result=prepare(reference=full_reference(rows=after),metadata=meta)
        self.assertFalse(result.plan.errors,result.plan.errors)
        self.assertEqual(next(r['redcap_repeat_instance'] for r in result.plan.records if r['redcap_repeat_instrument']=='oasis_eval'),'1')

    def test_handoff_and_hp_keep_separate_repeat_instances_in_one_csv(self):
        src=ui.sources()
        src[3]=a.csv_bytes(core.assessment()+core.assessment(form='h1',kind='hp')+core.assessment(form='ho1',kind='handoff'))
        src[2]=a.csv_bytes([core.match(),core.match(kind='hp'),core.match(kind='handoff')])
        r=a.prepare_routine_run(src,[],core.run().settings)
        reference=full_reference(run=r)
        result=prepare(reference=reference,sources=src)
        self.assertFalse(result.plan.errors,result.plan.errors)
        rows=ui.rows(a.manual_import_csv(result.plan))
        epas=[row for row in rows if row['redcap_repeat_instrument']=='epa']
        self.assertEqual(len(epas),2)
        self.assertEqual(len({row['redcap_repeat_instance'] for row in epas}),2)
        self.assertEqual({a.kind_of(row['epa_evaluation']) for row in epas},{'hp','handoff'})
        self.assertTrue(any(row.get('epa_obho_score')=='3.0' or row.get('epa_obho_score')=='3' for row in epas))

    def test_duplicate_matches_do_not_add_duplicate_target_instances(self):
        src=ui.sources();src[2]=a.csv_bytes([core.match(),core.match(),core.match()])
        result=prepare(sources=src)
        self.assertFalse(result.plan.errors,result.plan.errors)
        self.assertEqual(len([row for row in result.plan.records if row['redcap_repeat_instrument']=='preceptor_matching']),1)

    def test_coded_checklist_without_dictionary_blocked(self):
        ref=full_reference();after=core.apply_plan(ref,prepare(reference=ref).plan)
        e=next(r for r in after if r['redcap_repeat_instrument']=='checklist_entry');e['item']='1'
        self.assertTrue(prepare(reference=full_reference(rows=after)).plan.errors)

    def test_coded_checklist_with_dictionary_maps_same_instance(self):
        ref=full_reference();after=core.apply_plan(ref,prepare(reference=ref).plan)
        e=next(r for r in after if r['redcap_repeat_instrument']=='checklist_entry');e['item']='1';e['comments']=''
        meta=core.meta_for(a.import_candidates(core.run(),False),[{'field_name':'item','form_name':'checklist_entry','field_type':'dropdown','select_choices_or_calculations':'1, '+a.REQUIRED_ITEMS[0]}])
        result=prepare(reference=full_reference(rows=after),metadata=meta)
        self.assertFalse(result.plan.errors,result.plan.errors)
        self.assertEqual(next(r['redcap_repeat_instance'] for r in result.plan.records if r['redcap_repeat_instrument']=='checklist_entry'),'1')

    def test_wrong_cohort_no_csv(self):
        src=ui.sources();src[1]=a.csv_bytes([core.check(start='2026-07-06')])
        settings=a.Settings(as_of='2026-09-19',data_through='2026-09-19')
        self.assertTrue(prepare(sources=src,settings=settings).plan.errors)

    def test_old_rotation_not_new_record(self):
        ref=full_reference();ref[0]['start_date']='2026-07-06'
        self.assertTrue(prepare(reference=ref).plan.errors)

    def test_ambiguous_date_only_matching_blocks_entire_download(self):
        ref=full_reference();after=core.apply_plan(ref,prepare(reference=ref).plan)
        e=next(r for r in after if r['redcap_repeat_instrument']=='oasis_eval');e['oasis_form_record']='';e['submit_date']='2026-09-05 23:59'
        src=ui.sources();src[3]=a.csv_bytes(core.assessment()+core.assessment(form='f2',submit='2026-09-05 11:00:00'))
        result=prepare(reference=full_reference(rows=after),sources=src)
        self.assertTrue(result.plan.errors)
        with self.assertRaises(ValueError):a.manual_import_csv(result.plan)

    def test_clinical_free_text_not_powerautomate_sanitized(self):
        src=ui.sources();raw=core.assessment();value='Café, "excellent".\nKeep reading.'
        raw[-1]['Answer text']=value;src[3]=a.csv_bytes(raw)
        result=prepare(sources=src)
        got=next(r for r in ui.rows(a.manual_import_csv(result.plan)) if r['redcap_repeat_instrument']=='oasis_eval')
        self.assertEqual(got['cas_strengths'],value)

    def test_explicit_blank_clear_rejected(self):
        plan=a.SyncPlan(records=[{'record_id':'s001','redcap_repeat_instrument':'','redcap_repeat_instance':'','exclude':''}])
        with self.assertRaises(ValueError):a.manual_import_csv(plan)

    def test_clear_plan_rejected(self):
        plan=prepare().plan;plan.clear_records=[{'record_id':'s001','exclude':''}]
        with self.assertRaises(ValueError):a.manual_import_csv(plan)

    def test_duplicate_target_rejected(self):
        plan=prepare().plan;plan.records.append(copy.deepcopy(plan.records[0]))
        with self.assertRaises(ValueError):a.manual_import_csv(plan)

    def test_new_keyword_not_allowed(self):
        plan=prepare().plan;plan.records[0]['redcap_repeat_instance']='NEW'
        with self.assertRaises(ValueError):a.manual_import_csv(plan)

    def test_decimal_instance_not_allowed(self):
        plan=prepare().plan;plan.records[0]['redcap_repeat_instance']='1.0'
        with self.assertRaises(ValueError):a.manual_import_csv(plan)

    def test_instruction_settings_and_no_receipt_claim(self):
        self.assertIn('Overwrite data with blank values: NO',a.MANUAL_IMPORT_HELP)
        self.assertIn('Data Import Tool',a.MANUAL_IMPORT_HELP)
        self.assertIn('not a Data Dictionary',a.MANUAL_IMPORT_HELP)
        self.assertIn('not imported',a.MANUAL_IMPORT_HELP)


class ManualExclusionTests(unittest.TestCase):
    def rule(self):
        return a.normalize_rules([{'record_id':'s001','evaluator':'Able, Jamie','reason':'Director reviewed'}])[0]

    def test_manual_exclusion_applied_before_csv(self):
        settings=core.run().settings;settings.exclusions=[self.rule()]
        result=prepare(settings=settings)
        self.assertFalse(result.plan.errors,result.plan.errors)
        self.assertFalse(any(r['redcap_repeat_instrument']=='oasis_eval' for r in result.plan.records))
        self.assertFalse(result.run.reports['preceptor_reminders'])

    def test_saved_rules_loaded_offline(self):
        ref=full_reference();ref[0][a.EXCLUSION_FIELD]=json.dumps([self.rule()])
        result=prepare(reference=ref)
        self.assertFalse(result.plan.errors,result.plan.errors)
        self.assertEqual(result.run.reports['scores'][0]['manual_exclusions'],1)

    def test_invalid_saved_rules_block(self):
        ref=full_reference();ref[0][a.EXCLUSION_FIELD]='not json'
        self.assertTrue(prepare(reference=ref).plan.errors)

    def test_unsaved_nonconflicting_edit_retained(self):
        ref=full_reference();ref[0][a.EXCLUSION_FIELD]=''
        settings=core.run().settings;settings.exclusions=[self.rule()]
        result=prepare(reference=ref,settings=settings,baseline=[])
        self.assertFalse(result.plan.errors,result.plan.errors)
        self.assertEqual(result.run.reports['scores'][0]['manual_exclusions'],1)

    def test_saved_unsaved_conflict_not_silently_resolved(self):
        r=self.rule();other={**r,'active':False}
        ref=full_reference();ref[0][a.EXCLUSION_FIELD]=json.dumps([other])
        settings=core.run().settings;settings.exclusions=[r]
        self.assertTrue(prepare(reference=ref,settings=settings,baseline=[]).plan.errors)

    def test_no_mutation_or_rule_configuration_import(self):
        ref=full_reference();ref[0][a.EXCLUSION_FIELD]=json.dumps([self.rule()]); before=copy.deepcopy(ref)
        settings=core.run().settings;old=copy.deepcopy(settings)
        result=prepare(reference=ref,settings=settings)
        self.assertEqual(ref,before);self.assertEqual(settings,old)
        self.assertFalse(any(a.EXCLUSION_FIELD in r for r in result.plan.records))


class ManualUI(ui.FakeStreamlit):
    def __init__(self,*args,checks=None,**kwargs):
        super().__init__(*args,**kwargs);self.checks=checks or {}
    def checkbox(self,label,value=False,**kw):
        return self.checks.get(kw.get('key'),value)


class ManualInterfaceTests(unittest.TestCase):
    def run_ui(self,st):
        with patch.dict(sys.modules,{'streamlit':st}):a.main()

    def st_with_ref(self,token=''):
        files=ui.uploads_for_ui();ref=io.BytesIO(a.csv_bytes(full_reference()));ref.name='current_redcap.csv'
        files['manual_reference_upload']=ref
        return ManualUI(files,click='simple_create',token=token,checks={'manual_reference_confirmed':True})

    def test_no_api_prepare_download(self):
        st=self.st_with_ref();self.run_ui(st);st.click='manual_prepare'
        with patch.object(a.RedcapClient,'read',side_effect=AssertionError('no network')),patch.object(a.RedcapClient,'upload',side_effect=AssertionError('no write')):
            self.run_ui(st)
        file=next(f for f in st.downloads if f['name']=='redcap_import.csv')
        self.assertFalse(file['disabled']);self.assertTrue(ui.rows(file['data']))
        self.assertFalse(st.session_state.get('upload_receipts'))

    def test_manual_prepare_after_failed_api_connection(self):
        st=self.st_with_ref(token='fake-token')
        with patch.object(a.RedcapClient,'read',side_effect=RuntimeError('offline')):self.run_ui(st)
        self.assertFalse(st.session_state.result['rules_verified'])
        st.click='manual_prepare'
        with patch.object(a.RedcapClient,'read',side_effect=AssertionError('no additional network')):self.run_ui(st)
        self.assertTrue(any(f['name']=='redcap_import.csv' and not f['disabled'] for f in st.downloads))

    def test_creation_does_not_prepare_import(self):
        st=self.st_with_ref()
        with patch.object(a,'prepare_manual_import',side_effect=AssertionError('separate click required')):self.run_ui(st)
        self.assertFalse(any(f['name']=='redcap_import.csv' for f in st.downloads))

    def test_requires_reference_confirmation(self):
        st=self.st_with_ref();st.checks={};self.run_ui(st);st.click='manual_prepare';self.run_ui(st)
        self.assertFalse(st.session_state.get('manual_import'))

    def test_download_no_read_and_no_writes(self):
        st=self.st_with_ref();self.run_ui(st);st.click='manual_prepare';self.run_ui(st);st.click=''
        with patch.object(a.RedcapClient,'read',side_effect=AssertionError('no read')),patch.object(a.RedcapClient,'upload',side_effect=AssertionError('no write')):self.run_ui(st)
        self.assertFalse(st.session_state.get('upload_receipts'))

    def test_reference_change_hides_old_import_file(self):
        st=self.st_with_ref();self.run_ui(st);st.click='manual_prepare';self.run_ui(st)
        ref=full_reference();ref[0]['end_date']='2026-09-26'
        st.uploads['manual_reference_upload']=io.BytesIO(a.csv_bytes(ref));st.click='';st.downloads=[];self.run_ui(st)
        self.assertFalse(any(f['name']=='redcap_import.csv' for f in st.downloads))

    def test_corrupt_reference_hides_old_file(self):
        st=self.st_with_ref();self.run_ui(st);st.click='manual_prepare';self.run_ui(st)
        st.uploads['manual_reference_upload']=io.BytesIO(b'a,b\n1,2,3\n');st.click='';st.downloads=[];self.run_ui(st)
        self.assertFalse(any(f['name']=='redcap_import.csv' for f in st.downloads))

    def test_can_use_already_read_reference_without_api_upload(self):
        st=ManualUI(ui.uploads_for_ui(),click='simple_create',token='fake',checks={'manual_reference_confirmed':True})
        meta=core.meta_for(a.import_candidates(core.run(),False))
        with patch.object(a.RedcapClient,'read',return_value=(full_reference(),meta)):self.run_ui(st)
        st.click='manual_prepare'
        with patch.object(a.RedcapClient,'read',side_effect=AssertionError('no more reads')),patch.object(a.RedcapClient,'upload',side_effect=AssertionError('no imports')):self.run_ui(st)
        self.assertTrue(any(f['name']=='redcap_import.csv' for f in st.downloads))

    def test_saved_exclusion_failure_does_not_affect_reminder_downloads(self):
        st=self.st_with_ref();ref=full_reference();ref[0][a.EXCLUSION_FIELD]='bad'
        st.uploads['manual_reference_upload']=io.BytesIO(a.csv_bytes(ref))
        self.run_ui(st);st.click='manual_prepare';self.run_ui(st)
        self.assertTrue(st.session_state.manual_import['value'].plan.errors)
        self.assertFalse(any(f['name']=='redcap_import.csv' for f in st.downloads))
        self.assertEqual(len([f for f in st.downloads if f['name'] in a.REMINDER_NAMES and not f['disabled']]),6)


if __name__=='__main__': unittest.main()
