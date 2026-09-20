"""Routine-workflow tests with synthetic records, fake transport, and a UI double.
The UI double checks application control flow; it is not a Streamlit/browser test.
"""
import copy
import csv
import io
import json
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import peds_clerkship_tracker as a
import test_tracker as core


def rows(data):
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


def sources():
    return [a.csv_bytes(core.SCHEDULE), a.csv_bytes([core.check()]),
            a.csv_bytes([core.match()]), a.csv_bytes(core.assessment())]


class ReadClient:
    def __init__(self, rows=None, metadata=None, fail=False):
        self.rows = copy.deepcopy(rows if rows is not None else [core.parent()])
        self.meta = metadata or core.meta_for(a.import_candidates(core.run(), False))
        self.reads = 0
        self.calls = []
        self.fail = fail
        self.repeats = []

    def read(self):
        self.reads += 1
        if self.fail:
            raise RuntimeError("Simulated connection failure")
        return copy.deepcopy(self.rows), self.meta

    def call(self, content, **kw):
        self.calls.append((content, kw))
        if content == "repeatingFormsEvents":
            return self.repeats
        raise AssertionError("The automatic reference loader must not import or write records")


class RoutineTests(unittest.TestCase):
    def test_four_files_work_without_reference(self):
        r = a.prepare_routine_run(sources(), [], core.run().settings)
        self.assertFalse(r.messages.blocked)
        self.assertEqual(set(a.reminder_only_files(r)), set(a.REMINDER_NAMES))
        self.assertTrue(all(a.daily_readiness(r).values()))

    def test_name_only_schedule_resolves_from_four_source_files(self):
        source = sources()
        source[0] = a.csv_bytes([{"legal_name":"Example, Alex (MD)", "start_date":core.START}])
        r = a.prepare_routine_run(source, [], core.run().settings)
        self.assertFalse(r.messages.blocked)
        self.assertEqual(r.roster[0]["record_id"], "s001")

    def test_unmatched_student_not_silently_dropped_or_created(self):
        source = sources()
        source[0] = a.csv_bytes([{"legal_name":"Unknown, Student (MD)", "start_date":core.START}])
        r = a.prepare_routine_run(source, [], core.run().settings)
        self.assertTrue(r.messages.blocked)
        self.assertFalse(any(a.daily_readiness(r).values()))

    def test_reference_resolves_zero_activity_student(self):
        parent = {**core.parent(), "record_id":"s002", "legal_name":"Missing, Morgan (MD)", "email":"morgan@example.invalid"}
        source = sources()
        source[0] = a.csv_bytes([{"legal_name":"Missing, Morgan (MD)","start_date":core.START}])
        r = a.prepare_routine_run(source, [parent], core.run().settings)
        self.assertFalse(r.messages.blocked)
        self.assertEqual(r.reports["checklist_review"][0]["missing_count"],10)
        self.assertEqual(len(rows(a.reminder_only_files(r)[a.REMINDER_NAMES[0]])),1)

    def test_wrong_cohort_withholds_affected_download(self):
        opts = a.Settings(as_of="2026-09-19")
        r = core.run(checklist=[core.check(start="2026-07-06")], settings=opts)
        ready = a.daily_readiness(r)
        self.assertFalse(ready[a.REMINDER_NAMES[0]])
        self.assertTrue(ready[a.REMINDER_NAMES[1]])
        self.assertTrue(ready[a.REMINDER_NAMES[2]])

    def test_missing_oasis_coverage_not_treated_as_no_submissions(self):
        r = core.run(evaluations=core.assessment(start="2026-07-06"), settings=a.Settings(as_of="2026-09-19"))
        self.assertFalse(a.daily_readiness(r)[a.REMINDER_NAMES[2]])
        self.assertEqual(rows(a.reminder_only_files(r)[a.REMINDER_NAMES[2]]),[])

    def test_reminder_zip_does_not_include_grades_or_raw_narratives(self):
        r = core.run()
        f = a.reminder_only_files(r)
        self.assertEqual(len(f),3)
        self.assertFalse(any("Strong participation" in x.decode("utf-8-sig") for x in f.values()))
        self.assertNotIn("clinical_scores.csv",f)

    def test_repeat_download_does_not_change_results(self):
        r = core.run()
        self.assertEqual(a.reminder_only_files(r),a.reminder_only_files(r))

    def test_combined_preceptor_is_still_one_row_per_pair(self):
        r = core.run(matches=[core.match(),core.match(),core.match(kind="hp")],evaluations=[])
        mailed = rows(a.reminder_only_files(r)[a.REMINDER_NAMES[2]])
        self.assertEqual(len(mailed),1)
        self.assertIn(a.LABELS["hp"],mailed[0]["evaluation_type"])
        self.assertTrue(mailed[0]["cas_link"])
        self.assertTrue(mailed[0]["hp_link"])

    def test_submitted_cas_suppresses_only_cas_part(self):
        r = core.run(matches=[core.match(),core.match(),core.match(kind="hp")])
        mailed = rows(a.reminder_only_files(r)[a.REMINDER_NAMES[2]])
        self.assertEqual(len(mailed),1)
        self.assertEqual(mailed[0]["evaluation_type"],a.LABELS["hp"])
        self.assertEqual(mailed[0]["cas_link"],"")

    def test_legacy_layout_has_exact_original_headers(self):
        r = core.run(matches=[core.match(),core.match(kind="hp")],evaluations=[])
        f = a.legacy_power_automate_files(r)
        expected = [a.CHECKLIST_COLUMNS[:10],a.STUDENT_COLUMNS[:8],a.LEGACY_CAS_COLUMNS,a.LEGACY_HP_COLUMNS]
        for name, header in zip([*a.REMINDER_NAMES,"observed_hp_reminders.csv"],expected):
            actual = next(csv.reader(io.StringIO(f[name].decode("utf-8-sig"))))
            self.assertEqual(actual,header)
        self.assertEqual(len(rows(f[a.REMINDER_NAMES[2]])),1)
        self.assertEqual(len(rows(f["observed_hp_reminders.csv"])),1)

    def test_legacy_layout_does_not_invent_handoff_url(self):
        r = core.run(matches=[core.match(kind="handoff")],evaluations=[])
        f = a.legacy_power_automate_files(r)
        self.assertFalse(rows(f[a.REMINDER_NAMES[2]]))
        self.assertFalse(rows(f["observed_hp_reminders.csv"]))
        self.assertEqual(len(r.reports["preceptor_reminders"]),1)

    def test_legacy_incomplete_item_in_existing_message(self):
        r = core.run(checklist=[core.check(status="Incomplete")])
        f = a.legacy_power_automate_files(r)
        self.assertIn("not marked complete", rows(f[a.REMINDER_NAMES[0]])[0]["missing_items"])

    def test_partial_checklist_does_not_mark_all_encounters_submitted(self):
        self.assertFalse(any("submitted_ce" in r for r in a.import_candidates(core.run(),False)))

    def test_complete_checklist_preserves_submission_date_behavior(self):
        r = core.run(checklist=[core.check(item=x) for x in a.REQUIRED_ITEMS])
        candidates = a.import_candidates(r,False)
        self.assertEqual(next(x["submitted_ce"] for x in candidates if "submitted_ce" in x),"2026-09-05")

    def test_filename_date_is_inferred_not_latest_submission(self):
        self.assertEqual(a.inferred_export_date("oasis_clerk_me_20260910_134737.csv","2026-09-19"),"2026-09-10")
        self.assertEqual(a.inferred_export_date("oasis.csv","2026-09-19"),"2026-09-19")
        self.assertEqual(a.inferred_export_date("oasis_20269999.csv","2026-09-19"),"2026-09-19")

    def test_changed_files_settings_rules_invalidate_fingerprint(self):
        settings = core.run().settings
        raw = sources()
        first = a.routine_fingerprint(raw,["a","b","c","d"],settings,b"","")
        self.assertNotEqual(first,a.routine_fingerprint(raw,["a","b","c","e"],settings,b"",""))
        self.assertNotEqual(first,a.routine_fingerprint(raw,["a","b","c","d"],settings,b"reference",""))
        self.assertNotEqual(first,a.routine_fingerprint(raw,["a","b","c","d"],settings,b"","another_connection"))

    def test_input_count_validated(self):
        with self.assertRaises(ValueError):
            a.prepare_routine_run([b"a,b\n"],[],a.Settings())


class AutomaticReferenceTests(unittest.TestCase):
    def test_no_token_no_network(self):
        client = ReadClient()
        ref = a.automatic_reference({},a.API_URL,"",client=client)
        self.assertFalse(ref["connected"])
        self.assertEqual(client.reads,0)

    def test_reference_auto_reads_and_never_writes(self):
        client = ReadClient()
        state = {}
        ref = a.automatic_reference(state,a.API_URL,"fake",client=client)
        self.assertTrue(ref["connected"])
        self.assertFalse(ref["error"])
        self.assertEqual(client.reads,1)
        self.assertFalse(client.calls)
        self.assertEqual(len(a.ensure_exclusion_state(state,a.API_URL,"fake")["rules"]),3)

    def test_download_reruns_can_reuse_reference_cache(self):
        client = ReadClient(); state={}
        now=datetime(2026,9,19,10)
        a.automatic_reference(state,a.API_URL,"fake",client=client,now=now)
        a.automatic_reference(state,a.API_URL,"fake",client=client,now=now+timedelta(seconds=20))
        self.assertEqual(client.reads,1)
        a.automatic_reference(state,a.API_URL,"fake",client=client,now=now+timedelta(seconds=601))
        self.assertEqual(client.reads,2)

    def test_refresh_retries_failed_read(self):
        client=ReadClient(fail=True); state={}
        self.assertFalse(a.automatic_reference(state,a.API_URL,"fake",client=client)["connected"])
        client.fail=False
        self.assertTrue(a.automatic_reference(state,a.API_URL,"fake",client=client,refresh=True)["connected"])

    def test_bad_configuration_is_soft_reference_error(self):
        ref = a.automatic_reference({},"http://bad.invalid/api/","fake")
        self.assertFalse(ref["connected"])
        self.assertTrue(ref["error"])

    def test_failed_api_does_not_prevent_four_file_reminders(self):
        ref = a.automatic_reference({},a.API_URL,"fake",client=ReadClient(fail=True))
        r=a.prepare_routine_run(sources(),ref["rows"],core.run().settings)
        self.assertFalse(r.messages.blocked)
        self.assertTrue(all(a.daily_readiness(r).values()))
        self.assertTrue(ref["rules_error"])

    def saved_client(self, value):
        meta=core.meta_for(a.import_candidates(core.run(),False),[{"field_name":a.EXCLUSION_FIELD,"form_name":a.EXCLUSION_FORM,"field_type":"notes"}])
        return ReadClient(rows=[{**core.parent(),a.EXCLUSION_FIELD:value}],metadata=meta)

    def test_saved_rules_auto_load(self):
        rule={"record_id":"s001","evaluator":"Able, Jamie","active":True,"reason":"Reviewed correction"}
        client=self.saved_client(json.dumps({"version":1,"rules":[rule]})); state={}
        ref=a.automatic_reference(state,a.API_URL,"fake",client=client)
        self.assertFalse(ref["rules_error"])
        rules=a.ensure_exclusion_state(state,a.API_URL,"fake")["rules"]
        self.assertTrue(any(r["record_id"]=="s001" for r in rules))
        self.assertEqual(client.calls[0][0],"repeatingFormsEvents")

    def test_broken_saved_rules_disable_verified_scoring_not_identity_reference(self):
        ref=a.automatic_reference({},a.API_URL,"fake",client=self.saved_client("not JSON"))
        self.assertTrue(ref["connected"])
        self.assertTrue(ref["rules_error"])
        self.assertTrue(ref["rows"])

    def test_repeating_saved_rules_rejected(self):
        client=self.saved_client(""); client.repeats=[{"form_name":a.EXCLUSION_FORM}]
        ref=a.automatic_reference({},a.API_URL,"fake",client=client)
        self.assertTrue(ref["rules_error"])

    def test_unsaved_edits_not_overwritten_by_reference_refresh(self):
        state={}; client=ReadClient()
        a.automatic_reference(state,a.API_URL,"fake",client=client)
        s=a.ensure_exclusion_state(state,a.API_URL,"fake")
        s["rules"]=a.merge_rule_updates(s["rules"],[{"record_id":"s001","evaluator":"Able, Jamie","reason":"Session edit"}])
        a.automatic_reference(state,a.API_URL,"fake",client=client,refresh=True)
        self.assertTrue(any(r["record_id"]=="s001" for r in s["rules"]))

    def test_connection_change_does_not_reuse_other_project_reference(self):
        state={}; client=ReadClient()
        a.automatic_reference(state,a.API_URL,"fake",client=client)
        a.automatic_reference(state,a.API_URL,"different",client=client)
        self.assertEqual(client.reads,2)


class DailySyncTests(unittest.TestCase):
    def test_no_metadata_gives_optional_connection_message(self):
        p=a.safe_daily_plan(core.run(),[],None)
        self.assertTrue(p.errors)
        self.assertNotIn("full-project",p.errors[0])

    def test_existing_project_no_new_tracking_fields_needed(self):
        r=core.run(); meta=core.meta_for(a.import_candidates(r,False))
        p=a.safe_daily_plan(r,[core.parent()],meta)
        self.assertFalse(p.errors,p.errors)
        self.assertFalse(any(k.startswith("cst_") for row in p.records for k in row))

    def test_safe_daily_plan_preserves_manually_entered_and_calculated_values(self):
        r=core.run(); meta=core.meta_for(a.import_candidates(r,False),[{"field_name":"tot","field_type":"calc","form_name":"oasis_eval"}])
        parent={**core.parent(),"final_grade":"MANUAL","nbme":"88"}
        p=a.safe_daily_plan(r,[parent],meta)
        self.assertFalse(any(k in {"final_grade","nbme","tot"} for row in p.records for k in row))

    def test_wrong_cohort_blocks_sync_but_not_other_downloads(self):
        r=core.run(checklist=[core.check(start="2026-07-06")],settings=a.Settings(as_of="2026-09-19"))
        meta=core.meta_for(a.import_candidates(r,False))
        self.assertTrue(a.safe_daily_plan(r,[core.parent()],meta).errors)
        self.assertTrue(a.daily_readiness(r)[a.REMINDER_NAMES[1]])

    def test_review_pdf_fields_not_treated_as_import_authority(self):
        meta=a.Metadata([{"field_name":"strengths","field_type":"text","form_name":"portfolio","field_annotation":"@CALCTEXT"}])
        review=a.portfolio_field_review(meta)
        row=next(r for r in review if r["field"]=="strengths")
        self.assertTrue(row["calculated"])
        self.assertIn("unchanged",row["handling"])


class Session(dict):
    def __getattr__(self,key):
        try: return self[key]
        except KeyError as e: raise AttributeError(key) from e
    def __setattr__(self,key,value): self[key]=value


class StopUI(BaseException): pass
class RerunUI(BaseException): pass


class Context:
    def __init__(self,st,expander=False): self.st,self.expander=st,expander
    def __enter__(self):
        if self.expander:
            if self.st.expander_depth:
                raise AssertionError("Nested expanders are not allowed in this UI test")
            self.st.expander_depth += 1
        return self.st
    def __exit__(self,*exc):
        if self.expander: self.st.expander_depth -= 1
    def __getattr__(self,name): return getattr(self.st,name)


class FakeStreamlit:
    """Small API double for the routine screen. Not a renderer or real browser."""
    def __init__(self, uploads=None, click="", token=""):
        self.password="not-a-real-password-for-tests"
        self.secrets={"APP_PASSWORD":self.password,"REDCAP_API_TOKEN":token}
        self.session_state=Session(authenticated=a.digest(self.password))
        self.uploads=uploads or {}
        self.click=click
        self.events=[]; self.downloads=[]; self.expander_depth=0
        self.sidebar=Context(self)
    def set_page_config(self,**kw): self.events.append(("page",kw))
    def file_uploader(self,label,**kw):
        self.events.append(("uploader",label))
        return self.uploads.get(kw.get("key"))
    def button(self,label,**kw):
        self.events.append(("button",label))
        return kw.get("key",label)==self.click and not kw.get("disabled",False)
    def checkbox(self,label,value=False,**kw): return value
    def columns(self,n): return [Context(self) for _ in range(n)]
    def expander(self,*args,**kw): return Context(self,expander=True)
    def spinner(self,*args,**kw): return Context(self)
    def container(self,*args,**kw): return Context(self)
    def download_button(self,label,data,file_name,mime=None,**kw):
        self.downloads.append({"label":label,"name":file_name,"data":data,"disabled":kw.get("disabled",False)})
        return False
    def stop(self): raise StopUI()
    def rerun(self): raise RerunUI()
    def __getattr__(self,name):
        if name in {"title","caption","write","subheader","markdown","info","success","warning","error","dataframe","divider"}:
            return lambda *args,**kw: self.events.append((name,args[0] if args else ""))
        raise AttributeError(name)


def uploads_for_ui():
    data=sources()
    out={}
    for key,b,name in zip(("schedule_upload","checklist_upload","match_upload","oasis_upload"),data,("schedule.csv","checklist.csv","match.csv","oasis_clerk_me_20260919_090000.csv")):
        obj=io.BytesIO(b); obj.name=name; out[key]=obj
    return out


class InterfaceControlFlowTests(unittest.TestCase):
    def exercise(self,st):
        with patch.dict(sys.modules,{"streamlit":st}):
            a.main()

    def test_initial_screen_has_four_main_uploads_no_api_token_input(self):
        st=FakeStreamlit(token="fake")
        with patch.object(a.RedcapClient,"read",side_effect=AssertionError("No read before Create")):
            self.exercise(st)
        self.assertEqual(len([x for x in st.events if x[0]=="uploader"]),4)
        self.assertFalse(any(x[0]=="error" for x in st.events))
        self.assertIn(("button","Create reminder files"),st.events)

    def test_create_without_redcap_releases_three_downloads(self):
        st=FakeStreamlit(uploads_for_ui(),click="simple_create")
        self.exercise(st)
        files=[f for f in st.downloads if f["name"] in a.REMINDER_NAMES]
        self.assertEqual(len(files),3)
        self.assertFalse(any(f["disabled"] for f in files))
        self.assertFalse(any("full-project" in str(e) for e in st.events))

    def test_create_auto_reads_reference_but_does_not_prepare_or_import_sync(self):
        st=FakeStreamlit(uploads_for_ui(),click="simple_create",token="fake")
        meta=core.meta_for(a.import_candidates(core.run(),False))
        with patch.object(a.RedcapClient,"read",return_value=([core.parent()],meta)) as read, patch.object(a.RedcapClient,"upload",side_effect=AssertionError("No import")), patch.object(a,"safe_daily_plan",side_effect=AssertionError("Do not prepare sync on Create")):
            self.exercise(st)
        self.assertEqual(read.call_count,1)
        self.assertTrue(st.session_state.result["rules_verified"])
        self.assertFalse(any(f["disabled"] for f in st.downloads if f["name"] in a.REMINDER_NAMES))

    def test_api_failure_does_not_disable_valid_reminder_downloads(self):
        st=FakeStreamlit(uploads_for_ui(),click="simple_create",token="fake")
        with patch.object(a.RedcapClient,"read",side_effect=RuntimeError("Unavailable")):
            self.exercise(st)
        self.assertFalse(st.session_state.result["rules_verified"])
        self.assertFalse(any(f["disabled"] for f in st.downloads if f["name"] in a.REMINDER_NAMES))

    def test_rerun_after_download_does_not_read_again(self):
        st=FakeStreamlit(uploads_for_ui(),click="simple_create",token="fake")
        meta=core.meta_for(a.import_candidates(core.run(),False))
        with patch.object(a.RedcapClient,"read",return_value=([core.parent()],meta)) as read:
            self.exercise(st)
            st.click=""
            self.exercise(st)
        self.assertEqual(read.call_count,1)

    def test_file_change_hides_outdated_downloads(self):
        st=FakeStreamlit(uploads_for_ui(),click="simple_create")
        self.exercise(st)
        st.click=""; st.downloads=[]
        new=io.BytesIO(a.csv_bytes([core.check(item=a.REQUIRED_ITEMS[1])]))
        new.name="changed.csv"; st.uploads["checklist_upload"]=new
        self.exercise(st)
        self.assertFalse(any(f["name"] in a.REMINDER_NAMES for f in st.downloads))

    def test_redcap_review_requires_separate_button_and_does_not_upload(self):
        st=FakeStreamlit(uploads_for_ui(),click="simple_create",token="fake")
        meta=core.meta_for(a.import_candidates(core.run(),False))
        with patch.object(a.RedcapClient,"read",return_value=([core.parent()],meta)), patch.object(a.RedcapClient,"upload",side_effect=AssertionError("No import")):
            self.exercise(st)
            st.click="simple_prepare_redcap"
            self.exercise(st)
        self.assertIn("sync_plan",st.session_state)
        self.assertFalse(st.session_state.sync_plan["plan"].errors)
        self.assertFalse(st.session_state.get("upload_receipts"))


if __name__ == "__main__":
    unittest.main()
