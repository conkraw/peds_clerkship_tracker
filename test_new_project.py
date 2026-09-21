"""New-project CSV tests. All fixtures are synthetic; no REDCap connection."""
import copy
import csv
import io
import json
import re
import unittest
import zipfile
from unittest.mock import patch

import peds_clerkship_tracker as a
import test_core as c


def typed(rows, kind):
    return [r for r in rows if r['record_type'] == str(kind)]


def rows_for(**kw):
    return a.redcap_rows(c.run(**kw))


def import_memory(db, csv_data):
    """Simulate explicit record-ID matching and blank overwrites, not a REDCap server."""
    for row in csv.DictReader(io.StringIO(csv_data.decode('utf-8-sig'))):
        db.setdefault(row['record_id'], {}).update(row)
    return db


class NewProjectTests(unittest.TestCase):
    def test_no_api_export_or_previous_database_is_needed(self):
        rows = rows_for()
        self.assertTrue(rows)
        self.assertEqual({r['record_type'] for r in rows}, {'1','2','5','6'})

    def test_all_six_record_types(self):
        rows = rows_for(evaluations=c.assessment()+c.assessment('hp',kind='hp')+c.assessment('ho',kind='handoff'))
        self.assertEqual({r['record_type'] for r in rows}, set(a.RECORD_TYPES))

    def test_summary_and_details_share_one_student_key(self):
        rows=rows_for()
        summary=typed(rows,1)[0]
        self.assertTrue(all(r['student_key']==summary['record_id'] for r in rows))
        self.assertTrue(all(r['student_external_id']=='s001' for r in rows))

    def test_new_project_has_no_repeat_numbering(self):
        self.assertTrue(all(not any(k.startswith('redcap_repeat') for k in r) for r in rows_for()))
        self.assertNotIn('redcap_repeat_instance', [s['name'] for s in a.field_specs()])

    def test_record_ids_unique_and_short(self):
        rows=rows_for()
        self.assertEqual(len(rows),len({r['record_id'] for r in rows}))
        self.assertTrue(all(len(r['record_id'])==34 for r in rows))

    def test_all_csv_columns_have_dictionary_fields(self):
        rows=rows_for()
        names={s['name'] for s in a.field_specs()}
        self.assertEqual(names, {r['Variable / Field Name'] for r in a.redcap_dictionary()})
        self.assertTrue(all(set(r)<=names for r in rows))

    def test_dictionary_has_one_form_unique_fields_and_first_id(self):
        dd=a.redcap_dictionary()
        self.assertEqual(dd[0]['Variable / Field Name'],'record_id')
        self.assertEqual({r['Form Name'] for r in dd}, {a.NEW_FORM})
        self.assertEqual(len(dd),len({r['Variable / Field Name'] for r in dd}))
        self.assertTrue(all(re.fullmatch('[a-z][a-z0-9_]*',r['Variable / Field Name']) for r in dd))

    def test_no_invented_final_grade_or_nbme(self):
        columns={s['name'] for s in a.field_specs()}
        self.assertFalse({'final_grade','final_nbme_score','nbme'} & columns)
        self.assertIn('clinical_score_375',columns)

    def test_manual_excluded_evaluation_retained_flagged(self):
        s=a.Settings(as_of='2026-09-19',confirm_coverage=True,exclusions=[{'record_id':'s001','evaluator':'Able, Jamie','reason':'Approved correction'}])
        row=typed(rows_for(settings=s),2)[0]
        self.assertEqual((row['manual_excluded'],row['included_in_score']),('1','0'))
        self.assertEqual(row['manual_exclusion_reason'],'Approved correction')
        self.assertEqual(row['tot'],'300.0')

    def test_auto_drop_retained_flagged(self):
        ev=sum((c.assessment('f'+str(i),scores=(i+2,)*5) for i in range(4)),[])
        rows=rows_for(evaluations=ev)
        self.assertEqual(len(typed(rows,2)),4)
        self.assertEqual(sum(r['drop_lowest']=='1' for r in typed(rows,2)),1)
        self.assertEqual(typed(rows,1)[0]['clinical_score_375'],'300.0')

    def test_mean_imputed_and_original_values_both_export(self):
        row=typed(rows_for(evaluations=c.assessment(scores=(5,3,None,4,None))),2)[0]
        self.assertEqual(float(row['effective_do']),4.0)
        self.assertFalse(row.get('do',''))
        self.assertEqual(row['tot'],'300.0')

    def test_unscorable_no_zero_grade(self):
        rows=rows_for(evaluations=c.assessment(scores=(None,)*5))
        e=typed(rows,2)[0]
        self.assertEqual((e['tot'],e['scorable'],e['included_in_score']),('','0','0'))
        self.assertEqual(typed(rows,1)[0]['clinical_score_375'],'')

    def test_same_source_reimport_updates_not_duplicate(self):
        rows=rows_for()
        data=a.csv_bytes(rows,[s['name'] for s in a.field_specs()])
        db={};import_memory(db,data);import_memory(db,data)
        self.assertEqual(len(db),len(rows))

    def test_new_forms_do_not_renumber_existing(self):
        before=rows_for(evaluations=c.assessment('f2'))
        after=rows_for(evaluations=c.assessment('f1',submit='2026-09-04 10:00:00')+c.assessment('f2'))
        self.assertEqual(typed(before,2)[0]['record_id'],next(r['record_id'] for r in typed(after,2) if r['form_record']=='f2'))

    def test_input_row_order_does_not_change_ids(self):
        ev=c.assessment('f2')+c.assessment('f1')
        before=rows_for(evaluations=ev)
        after=rows_for(evaluations=list(reversed(ev)))
        self.assertEqual({r['record_id'] for r in before},{r['record_id'] for r in after})

    def test_corrected_score_and_comment_keep_evaluation_id(self):
        before=typed(rows_for(evaluations=c.assessment('f1',scores=(3,)*5)),2)[0]
        after=typed(rows_for(evaluations=c.assessment('f1',scores=(5,)*5)),2)[0]
        self.assertEqual(before['record_id'],after['record_id'])
        self.assertNotEqual(before['tot'],after['tot'])

    def test_added_email_does_not_change_form_record_identity(self):
        before=typed(rows_for(evaluations=c.assessment(email='')),2)[0]
        after=typed(rows_for(),2)[0]
        self.assertEqual(before['record_id'],after['record_id'])

    def test_exclusion_toggle_keeps_same_id(self):
        s=a.Settings(as_of='2026-09-19',confirm_coverage=True,exclusions=[{'record_id':'s001','evaluator':'Able, Jamie','active':True}])
        before=typed(rows_for(settings=s),2)[0]
        s.exclusions[0]['active']=False
        after=typed(rows_for(settings=s),2)[0]
        self.assertEqual(before['record_id'],after['record_id'])
        self.assertEqual((before['included_in_score'],after['included_in_score']),('0','1'))

    def test_blank_overwrite_clears_previous_drop(self):
        ev=sum((c.assessment('f'+str(i),scores=(v,)*5) for i,v in enumerate([2,3,4,5])),[])
        r1=rows_for(evaluations=ev)
        r2=rows_for(evaluations=c.assessment('f0'))
        columns=[s['name'] for s in a.field_specs()]
        db={};import_memory(db,a.csv_bytes(r1,columns));import_memory(db,a.csv_bytes(r2,columns))
        summary=typed(r2,1)[0]['record_id']
        self.assertEqual(db[summary]['dropped_form_record'],'')
        self.assertEqual(db[summary]['exclude'],'')

    def test_absent_rows_not_silently_claimed_deleted(self):
        before=rows_for(evaluations=c.assessment('f1')+c.assessment('f2'))
        after=rows_for(evaluations=c.assessment('f1'))
        db={}; cols=[s['name'] for s in a.field_specs()]
        import_memory(db,a.csv_bytes(before,cols));import_memory(db,a.csv_bytes(after,cols))
        old_id=next(r['record_id'] for r in before if r.get('form_record')=='f2')
        self.assertIn(old_id,db)
        self.assertNotEqual(db[old_id]['batch_id'],after[0]['batch_id'])

    def test_rows_in_same_batch_have_same_id(self):
        rows=rows_for()
        self.assertEqual(len({r['batch_id'] for r in rows}),1)

    def test_batch_id_deterministic_for_identical_inputs(self):
        self.assertEqual(rows_for(),rows_for())

    def test_checklist_correction_keeps_identity(self):
        before=typed(rows_for(checklist=[c.check(status='Incomplete')]),5)[0]
        after=typed(rows_for(checklist=[c.check(status='Complete')]),5)[0]
        self.assertEqual(before['record_id'],after['record_id'])
        self.assertNotEqual(before['item_status'],after['item_status'])

    def test_missing_checklist_timestamp_blocks_only_import(self):
        x=c.check();x['Time entered']=''
        result=c.run(checklist=[x])
        self.assertFalse(result.messages.blocked)
        self.assertTrue(a.output_files(result)[a.REMINDER_NAMES[0]])
        with self.assertRaisesRegex(ValueError,'Time entered'):
            a.redcap_rows(result)

    def test_conflicting_checklist_identity_not_silently_reassigned(self):
        x=c.check(); y={**x,'Comments':'Different entry with identical source key'}
        with self.assertRaisesRegex(ValueError,'same source identity'):
            rows_for(checklist=[x,y])

    def test_raw_unknown_oasis_answers_retained(self):
        raw=c.assessment()
        raw += [{**raw[0],'Question':'Additional unscored observation','Multiple Choice Value':'','Answer text':'Good work, including "new" detail.\nSecond line.'}]
        result=c.run(evaluations=raw)
        row=typed(a.redcap_rows(result,raw_oasis=raw),2)[0]
        answers=json.loads(row['source_answers_json'])
        self.assertTrue(any(r['Question']=='Additional unscored observation' and '\nSecond line.' in r['Answer text'] for r in answers))

    def test_raw_extra_checklist_field_retained(self):
        raw=[{**c.check(),'*Assisted or Above comments':'Additional feedback, unchanged'}]
        result=c.run(checklist=raw)
        entry=typed(a.redcap_rows(result,raw_checklist=raw),5)[0]
        self.assertEqual(json.loads(entry['source_entry_json'])[0]['*Assisted or Above comments'],'Additional feedback, unchanged')

    def test_redcap_csv_preserves_quotes_commas_and_lines(self):
        raw=[{**c.check(),'Comments':'A, B "C"\nSecond line'}]
        rows=a.redcap_rows(c.run(checklist=raw))
        decoded=list(csv.DictReader(io.StringIO(a.csv_bytes(rows,[s['name'] for s in a.field_specs()]).decode('utf-8-sig'))))
        self.assertEqual(typed(decoded,5)[0]['comments'],raw[0]['Comments'])

    def test_partial_checklist_has_no_all_completed_date(self):
        summary=typed(rows_for(),1)[0]
        self.assertEqual(summary['submitted_ce'],'')
        self.assertEqual(summary['completed_categories'],'1')

    def test_complete_checklist_has_completion_timestamp(self):
        summary=typed(rows_for(checklist=[c.check(item=i) for i in a.REQUIRED_ITEMS]),1)[0]
        self.assertEqual(summary['completed_categories'],'10')
        self.assertEqual(summary['submitted_ce'],'2026-09-05 15:30:00')

    def test_different_rotation_different_student_record(self):
        self.assertNotEqual(a.record_key('s',['s001','2026-08-31']),a.record_key('s',['s001','2026-07-06']))

    def test_future_or_previous_rotation_not_mixed(self):
        rows=rows_for(evaluations=c.assessment('old',start='2026-07-06',submit='2026-07-20 10:00:00')+c.assessment('now'))
        self.assertEqual([r['form_record'] for r in typed(rows,2)],['now'])

    def test_zero_activity_student_can_export_when_coverage_confirmed(self):
        rows=rows_for(evaluations=[],matches=[],checklist=[])
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['missing_count'],'10')
        self.assertEqual(rows[0]['clinical_score_375'],'')

    def test_unknown_source_coverage_blocks_import_not_assumed_zero(self):
        result=c.run(checklist=[],settings=a.Settings(as_of='2026-09-19'))
        with self.assertRaisesRegex(ValueError,'cover'):
            a.redcap_rows(result)

    def test_no_final_grade_automatically_failed(self):
        result=c.run(evaluations=[])
        rows=a.redcap_rows(result)
        self.assertNotIn('FAIL',typed(rows,1)[0]['summary_review'])

    def test_raw_errors_block_import(self):
        result=c.run(evaluations=c.assessment(scores=(8,4,4,4,4)))
        with self.assertRaises(ValueError):
            a.redcap_rows(result)

    def test_invalid_email_block(self):
        schedule=[{**c.SCHEDULE[0],'email':'notanemail'}]
        with self.assertRaisesRegex(ValueError,'email'):
            rows_for(schedule=schedule)

    def test_missing_student_id_not_guessed(self):
        result=c.run(schedule=[{'legal_name':'No, Source','start_date':c.START}],evaluations=[],matches=[],checklist=[])
        self.assertTrue(result.messages.blocked)

    def test_simulated_hash_collision_blocks(self):
        with patch.object(a,'record_key',return_value='s_'+'a'*32):
            with self.assertRaisesRegex(ValueError,'same source identity'):
                rows_for()

    def test_dictionary_csv_order_and_header(self):
        rows=list(csv.reader(io.StringIO(a.csv_bytes(a.redcap_dictionary(),a.DD_COLUMNS).decode('utf-8-sig'))))
        self.assertEqual(rows[0],a.DD_COLUMNS)
        self.assertTrue(all(len(r)==len(a.DD_COLUMNS) for r in rows))

    def test_reminder_zip_has_only_three_mailing_files(self):
        reports=a.output_files(c.run())
        with zipfile.ZipFile(io.BytesIO(a.zipped({k:reports[k] for k in a.REMINDER_NAMES}))) as z:
            self.assertEqual(set(z.namelist()),set(a.REMINDER_NAMES))

    def test_original_three_exclusions_default_automatic(self):
        self.assertEqual(a.Settings().exclusions,a.normalize_rules(a.LEGACY_EXCLUSIONS))
        self.assertEqual(len(a.Settings().exclusions),3)

    def test_backup_disabled_rule_stays_disabled(self):
        rules=a.normalize_rules(a.LEGACY_EXCLUSIONS)
        rules[0]['active']=False
        restored=a.normalize_rules(json.loads(a.exclusion_backup(rules)))
        self.assertFalse(restored[0]['active'])

    def test_explicit_empty_exclusions_remain_empty(self):
        self.assertEqual(a.Settings(exclusions=[]).exclusions,[])
        self.assertEqual(a.normalize_rules(json.loads(a.exclusion_backup([]))),[])

    def test_no_network_client_or_old_sync_planner_in_app(self):
        self.assertFalse(hasattr(a,'RedcapClient'))
        self.assertFalse(hasattr(a,'plan_sync'))
        self.assertFalse(hasattr(a,'Metadata'))

    def test_original_separate_flow_option_still_present(self):
        output=a.legacy_power_automate_files(c.run())
        self.assertIn('observed_hp_reminders.csv',output)
        header=next(csv.reader(io.StringIO(output['observed_hp_reminders.csv'].decode('utf-8-sig'))))
        self.assertEqual(header,a.LEGACY_HP_COLUMNS)


if __name__=='__main__':
    unittest.main()
