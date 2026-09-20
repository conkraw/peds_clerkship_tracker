"""Online-exclusion regression tests using synthetic records and a fake REDCap API.
No network requests are made. These tests are not a live browser/cloud test.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import peds_clerkship_tracker as a
import test_tracker as core


def rule(**kw):
    return {"record_id":"s001", "student_name":"Alex Example", "evaluator":"Able, Jamie",
            "reason":"Approved correction for testing", "active":True, **kw}


def row(**kw):
    return {"record_id":"s001", "evaluator":"Able, Jamie", "evaluator_email":"jamie@example.invalid",
            "kind":"cas", "form_record":"f1", "rotation_start":core.START,
            "submit_date":"2026-09-05 10:00:00", **kw}


class FakeClient:
    """Minimal transport mock. Records stay in memory; only expected calls allowed."""
    def __init__(self):
        self.rows=[{"record_id":"s001", "name":"Alex Example", a.EXCLUSION_FIELD:""},
                   {"record_id":"s002", "name":"Sam Example", a.EXCLUSION_FIELD:""}]
        self.metadata=[{"field_name":"record_id","form_name":"demographics","field_type":"text"},
                       {"field_name":"name","form_name":"demographics","field_type":"text"},
                       {"field_name":a.EXCLUSION_FIELD,"form_name":a.EXCLUSION_FORM,"field_type":"notes"}]
        self.repeats=[]
        self.calls=[]
        self.reject=False
        self.drop_write=False

    def call(self, content, **kw):
        self.calls.append((content, copy.deepcopy(kw)))
        if content=="metadata":
            return copy.deepcopy(self.metadata)
        if content=="repeatingFormsEvents":
            return copy.deepcopy(self.repeats)
        if content=="record" and kw.get("action")=="import":
            if self.reject:
                raise RuntimeError("Simulated timeout; outcome unknown")
            payload=json.loads(kw["data"])
            if not self.drop_write:
                for incoming in payload:
                    found=next(r for r in self.rows if r["record_id"]==incoming["record_id"])
                    found.update(incoming)
            return {"count":len(payload)}
        if content=="record":
            return copy.deepcopy(self.rows)
        raise AssertionError("Unexpected API operation")

    @property
    def imports(self):
        return [kw for content,kw in self.calls if content=="record" and kw.get("action")=="import"]


class RuleTests(unittest.TestCase):
    def test_original_rules_load_without_file(self):
        rules=a.load_private_rules()
        self.assertEqual(len(rules),3)
        self.assertTrue(all(r["active"] for r in rules))
        self.assertEqual(len({r["record_id"] for r in rules}),2)

    def test_each_original_pair_applies_only_to_that_student_and_preceptor(self):
        for r in a.load_private_rules():
            e=row(record_id=r["record_id"],evaluator=r["evaluator"])
            self.assertTrue(a.exclusion_matches(r,e))
            self.assertFalse(a.exclusion_matches(r,{**e,"record_id":"another_student"}))
            self.assertFalse(a.exclusion_matches(r,{**e,"evaluator":"Someone, Else"}))

    def test_disabled_builtin_remains_disabled_after_overlay(self):
        defaults=a.load_private_rules()
        saved=a.normalize_rules([{**r,"active":False} for r in defaults if r["record_id"]==defaults[0]["record_id"]])
        loaded=a.overlay_saved_rules(defaults,{saved[0]["record_id"]:saved})
        self.assertTrue(all(not r["active"] for r in loaded if r["record_id"]==saved[0]["record_id"]))

    def test_normalized_name_credentials_match(self):
        self.assertTrue(a.exclusion_matches(rule(evaluator="Able, Jamie; MD"),row(evaluator="Jamie Able (MD)")))

    def test_email_mismatch_prevents_name_only_match(self):
        self.assertFalse(a.exclusion_matches(rule(evaluator_email="other@example.invalid"),row()))

    def test_email_only_rule_matches(self):
        self.assertTrue(a.exclusion_matches(rule(evaluator="",evaluator_email="JAMIE@example.invalid"),row()))
        self.assertFalse(a.exclusion_matches(rule(evaluator="",evaluator_email="jamie@example.invalid"),row(evaluator_email="")))

    def test_blank_source_email_falls_back_to_name(self):
        self.assertTrue(a.exclusion_matches(rule(evaluator_email="jamie@example.invalid"),row(evaluator_email="")))

    def test_form_scope(self):
        r=rule(form_record="f1")
        self.assertTrue(a.exclusion_matches(r,row()))
        self.assertFalse(a.exclusion_matches(r,row(form_record="f2")))
        self.assertFalse(a.exclusion_matches(r,row(form_record="")))

    def test_rotation_scope(self):
        self.assertTrue(a.exclusion_matches(rule(rotation_start=core.START),row()))
        self.assertFalse(a.exclusion_matches(rule(rotation_start="2026-07-06"),row()))

    def test_submission_scope(self):
        self.assertTrue(a.exclusion_matches(rule(submit_date="09/05/2026 10:00:00"),row()))
        self.assertFalse(a.exclusion_matches(rule(submit_date="2026-09-05 11:00:00"),row()))

    def test_no_hp_or_handoff_exclusion(self):
        for kind in ("hp","handoff"):
            self.assertFalse(a.exclusion_matches(rule(),row(kind=kind)))

    def test_inactive_does_not_match(self):
        for value in (False,"false","0","no"):
            self.assertFalse(a.exclusion_matches(rule(active=value),row()))

    def test_invalid_rules_fail_closed(self):
        for x in ({},[None],[{}],[rule(record_id="")],[rule(record_id="*")],
                  [rule(record_id="all students")],[rule(evaluator="")],
                  [rule(rotation_start="bad")],[rule(submit_date="bad")],
                  [rule(active="maybe")], {"version":2,"rules":[rule()]}):
            with self.subTest(x=x), self.assertRaises(ValueError):
                a.normalize_rules(x)

    def test_backup_round_trip(self):
        r=a.normalize_rules([rule()])
        self.assertEqual(a.normalize_rules(json.loads(json.dumps({"version":1,"rules":r}))),r)

    def test_same_name_different_emails_have_distinct_rules(self):
        rules=a.normalize_rules([rule(evaluator_email="one@example.invalid"),rule(evaluator_email="two@example.invalid")])
        self.assertEqual(len(rules),2)
        self.assertNotEqual(rules[0]["rule_id"],rules[1]["rule_id"])

    def test_same_identity_conflicting_active_in_backup_rejected(self):
        with self.assertRaises(ValueError):
            a.normalize_rules([rule(),rule(active=False)])

    def test_same_rule_deduplicated(self):
        self.assertEqual(len(a.normalize_rules([rule(),rule()])),1)

    def test_mutation_does_not_modify_defaults(self):
        r=a.load_private_rules(); r[0]["active"]=False
        self.assertTrue(a.load_private_rules()[0]["active"])

    def test_rule_update_idempotent_and_can_deactivate(self):
        r=a.normalize_rules([rule()]); updated=a.merge_rule_updates(r,[rule(active=False)])
        self.assertEqual(len(updated),1)
        self.assertFalse(updated[0]["active"])
        self.assertEqual(a.merge_rule_updates(updated,updated),updated)
        self.assertEqual(a.changed_rule_students(r,updated),{"s001"})

    def test_explicit_saved_empty_set_is_authoritative(self):
        self.assertEqual(a.overlay_saved_rules([rule()],{"s001":[]}),[])

    def test_other_students_keep_their_defaults(self):
        r=a.overlay_saved_rules([rule(),rule(record_id="s002")],{"s001":[]})
        self.assertEqual([x["record_id"] for x in r],["s002"])

    def test_legacy_sidecar_import_is_optional(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"rules.json"; p.write_text(json.dumps([rule()]))
            self.assertEqual(a.load_private_rules(p),a.normalize_rules([rule()]))
            with self.assertRaises(ValueError):
                a.load_private_rules(Path(tmp)/"missing.json")

    def test_invalidation_clears_stale_scores_and_upload_plans(self):
        st=SimpleNamespace(session_state={"result":1,"sync_plan":2,"upload_receipts":3,"other":4})
        a.invalidate_rule_results(st)
        self.assertEqual(st.session_state,{"other":4})


class ScoringIntegrationTests(unittest.TestCase):
    def settings(self,rules):
        return a.Settings(as_of="2026-09-19",data_through="2026-09-19",confirm_coverage=True,exclusions=a.normalize_rules(rules))

    def test_specific_form_excluded_not_other_submission(self):
        result=core.run(evaluations=core.assessment("f1")+core.assessment("f2"),settings=self.settings([rule(form_record="f1")]))
        flags={r["form_record"]:r["manual_excluded"] for r in result.evaluations}
        self.assertEqual(flags,{"f1":True,"f2":False})
        self.assertEqual(result.reports["scores"][0]["manual_exclusions"],1)
        self.assertFalse(result.reports["preceptor_reminders"])

    def test_inactive_pair_restores_score_and_import(self):
        result=core.run(settings=self.settings([rule(active=False)]))
        self.assertFalse(result.evaluations[0]["manual_excluded"])
        self.assertEqual(result.reports["scores"][0]["clinical_score_375"],300.0)
        self.assertTrue(any(r.get("redcap_repeat_instrument")=="oasis_eval" for r in a.import_candidates(result)))

    def test_one_manual_and_one_automatic_drop_with_five(self):
        ev=sum((core.assessment(f"f{i}",scores=(i+1,)*5) for i in range(5)),[])
        result=core.run(evaluations=ev,settings=self.settings([rule(form_record="f0")]))
        self.assertEqual(sum(r["manual_excluded"] for r in result.evaluations),1)
        self.assertEqual(sum(r["drop_lowest"] for r in result.evaluations),1)
        self.assertEqual(result.reports["scores"][0]["clinical_score_375"],300.0)
        self.assertFalse(result.reports["preceptor_reminders"])

    def test_builtin_rules_apply_to_normalizer(self):
        for r in a.load_private_rules():
            ev=core.assessment(assessor=r["evaluator"])
            for e in ev:
                e["Student External ID"]=r["record_id"]
                e["Student"]=r["student_name"]
            schedule=[{**core.SCHEDULE[0],"record_id":r["record_id"],"legal_name":r["student_name"]}]
            result=core.run(evaluations=ev,schedule=schedule,matches=[],checklist=[],settings=self.settings(a.load_private_rules()))
            self.assertEqual(len(result.evaluations),1)
            self.assertTrue(result.evaluations[0]["manual_excluded"])

    def test_picker_deduplicates_question_rows(self):
        choices=a.exclusion_choices(core.assessment()+core.assessment())
        self.assertEqual(len(choices),1)
        p=next(iter(choices["s001"]["preceptors"].values()))
        self.assertEqual(len(p["forms"]),1)

    def test_picker_retains_separate_same_day_forms(self):
        choices=a.exclusion_choices(core.assessment("f1")+core.assessment("f2"))
        p=next(iter(choices["s001"]["preceptors"].values()))
        self.assertEqual(len(p["forms"]),2)

    def test_picker_no_hp_drafts_or_unsubmitted(self):
        raw=core.assessment(kind="hp")+[dict(r,**{"Eval Status":"draft"}) for r in core.assessment()]+core.assessment(submit="")
        self.assertFalse(a.exclusion_choices(raw))

    def test_picker_missing_form_uses_timestamp(self):
        raw=core.assessment(form="",submit="2026-09-05 10:00:00")+core.assessment(form="",submit="2026-09-05 11:00:00")
        choices=a.exclusion_choices(raw);p=next(iter(choices["s001"]["preceptors"].values()))
        self.assertEqual(len(p["forms"]),2)


class PersistenceTests(unittest.TestCase):
    def test_missing_field_is_explicit_uninstalled(self):
        c=FakeClient();c.metadata=c.metadata[:2]
        self.assertFalse(a.ExclusionStore(c).read().installed)
        self.assertEqual(len(c.calls),1)

    def test_only_selected_configuration_fields_are_requested(self):
        c=FakeClient();a.ExclusionStore(c).read()
        kw=next(kw for content,kw in c.calls if content=="record")
        self.assertEqual({v for k,v in kw.items() if k.startswith("fields[")},{"record_id","name",a.EXCLUSION_FIELD})
        self.assertFalse(c.imports)

    def test_text_or_readonly_field_rejected(self):
        for change in ({"field_type":"text"},{"field_annotation":"@READONLY"}):
            c=FakeClient();c.metadata[-1].update(change)
            with self.assertRaises(ValueError):
                a.ExclusionStore(c).read()
            self.assertFalse(c.imports)

    def test_repeating_configuration_rejected(self):
        c=FakeClient();c.repeats=[{"form_name":a.EXCLUSION_FORM}]
        with self.assertRaises(ValueError):
            a.ExclusionStore(c).read()

    def test_invalid_repeat_configuration_response_rejected(self):
        c=FakeClient();c.repeats={"error":"not authorized"}
        with self.assertRaises(RuntimeError):
            a.ExclusionStore(c).read()

    def test_invalid_saved_json_blocks(self):
        for value in ("bad JSON",json.dumps([rule(record_id="wrongstudent")])):
            with self.assertRaises(ValueError):
                a.parse_exclusion_snapshot([{"record_id":"s001",a.EXCLUSION_FIELD:value}])

    def test_duplicate_parents_and_longitudinal_rejected(self):
        for rows in ([{"record_id":"s001"}]*2,[{"record_id":"s001","redcap_event_name":"event_1_arm_1"}], [{"record_id":""}]):
            with self.assertRaises(ValueError):
                a.parse_exclusion_snapshot(rows)

    def test_blank_repeat_rows_not_parent_duplicates(self):
        data=[{"record_id":"s001"},{"record_id":"s001","redcap_repeat_instrument":"oasis_eval","redcap_repeat_instance":"1"}]
        self.assertEqual(len(a.parse_exclusion_snapshot(data).parents),1)

    def test_configuration_in_repeat_row_rejected(self):
        with self.assertRaises(ValueError):
            a.parse_exclusion_snapshot([{"record_id":"s001","redcap_repeat_instrument":"other",a.EXCLUSION_FIELD:"[]"}])

    def test_save_sparse_only_changed_student_and_field(self):
        c=FakeClient();c.rows[0]["some_grade"]="do not overwrite";store=a.ExclusionStore(c);base=store.read()
        saved=store.save([rule()],base,{"s001"})
        self.assertEqual(saved.stored["s001"],a.normalize_rules([rule()]))
        self.assertEqual(len(c.imports),1)
        incoming=json.loads(c.imports[0]["data"])
        self.assertEqual(len(incoming),1)
        self.assertEqual(set(incoming[0]),{"record_id","redcap_repeat_instrument","redcap_repeat_instance",a.EXCLUSION_FIELD})
        self.assertEqual(c.rows[0]["some_grade"],"do not overwrite")
        payload=json.loads(incoming[0][a.EXCLUSION_FIELD])
        self.assertIn("updated_at",payload)
        self.assertIn("not individually authenticated",payload["updated_by"])

    def test_disabled_rules_survive_new_store_instance(self):
        c=FakeClient();store=a.ExclusionStore(c)
        store.save([rule(active=False)],store.read(),{"s001"})
        reopened=a.ExclusionStore(c).read()
        self.assertFalse(a.overlay_saved_rules([rule()],reopened.stored)[0]["active"])

    def test_empty_rules_store_authoritative_empty_list(self):
        c=FakeClient();store=a.ExclusionStore(c)
        saved=store.save([],store.read(),{"s001"})
        self.assertIn("s001",saved.stored)
        self.assertEqual(a.overlay_saved_rules([rule()],saved.stored),[])

    def test_same_student_concurrent_change_prevents_write(self):
        c=FakeClient();store=a.ExclusionStore(c);base=store.read()
        c.rows[0][a.EXCLUSION_FIELD]=json.dumps([rule(active=False)])
        with self.assertRaisesRegex(ValueError,"changed in another session"):
            store.save([rule()],base,{"s001"})
        self.assertFalse(c.imports)

    def test_other_student_concurrent_change_preserved(self):
        c=FakeClient();store=a.ExclusionStore(c);base=store.read()
        other=json.dumps([rule(record_id="s002",active=False)])
        c.rows[1][a.EXCLUSION_FIELD]=other
        saved=store.save([rule()],base,{"s001"})
        self.assertEqual(c.rows[1][a.EXCLUSION_FIELD],other)
        self.assertFalse(saved.stored["s002"][0]["active"])

    def test_no_new_students_created(self):
        c=FakeClient();store=a.ExclusionStore(c)
        with self.assertRaisesRegex(ValueError,"not an existing REDCap record"):
            store.save([rule(record_id="new")],store.read(),{"new"})
        self.assertFalse(c.imports)

    def test_missing_field_save_rejected(self):
        c=FakeClient();store=a.ExclusionStore(c)
        with self.assertRaises(ValueError):
            store.save([rule()],a.ExclusionSnapshot(),{"s001"})
        self.assertFalse(c.imports)

    def test_no_changes_no_write(self):
        c=FakeClient();store=a.ExclusionStore(c);base=store.read();calls=len(c.calls)
        self.assertIs(store.save([rule()],base,set()),base)
        self.assertEqual(len(c.calls),calls)

    def test_field_removed_before_save_prevents_write(self):
        c=FakeClient();store=a.ExclusionStore(c);base=store.read();c.metadata=c.metadata[:2]
        with self.assertRaisesRegex(ValueError,"configuration changed"):
            store.save([rule()],base,{"s001"})
        self.assertFalse(c.imports)

    def test_failed_verification_no_automatic_retry(self):
        c=FakeClient();store=a.ExclusionStore(c);base=store.read();c.drop_write=True
        with self.assertRaisesRegex(RuntimeError,"verification failed"):
            store.save([rule()],base,{"s001"})
        self.assertEqual(len(c.imports),1)

    def test_import_timeout_no_automatic_retry(self):
        c=FakeClient();store=a.ExclusionStore(c);base=store.read();c.reject=True
        with self.assertRaisesRegex(RuntimeError,"timeout"):
            store.save([rule()],base,{"s001"})
        self.assertEqual(len(c.imports),1)

    def test_field_fragment_targets_only_new_internal_form(self):
        rows=a.exclusion_dictionary()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["Variable / Field Name"],a.EXCLUSION_FIELD)
        self.assertEqual(rows[0]["Form Name"],a.EXCLUSION_FORM)
        self.assertEqual(rows[0]["Field Type"],"notes")


if __name__=="__main__":
    unittest.main()
