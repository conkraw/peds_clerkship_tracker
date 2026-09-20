"""Synthetic regression tests. No user student data is embedded in this file."""
import copy
import csv
import io
import json
import unittest
from unittest.mock import patch

import peds_clerkship_tracker as a

START = "2026-08-31"
END = "2026-09-25"
SCHEDULE = [{"record_id": "s001", "legal_name": "Example, Alex (MD)", "start_date": START, "end_date": END, "email": "alex@example.invalid"}]
QUESTIONS = ["KP2.1 Knowledge", "PC1.2/1.3/1.11 Reasoning", "PC1.5/ICS4.3/PC1.6/ICS4.4 Documentation", "ICS4.1 Communication", "ICS4.2/SBP6.1 Teamwork"]


def assessment(form="f1", assessor="Able, Jamie", scores=(4, 4, 4, 4, 4), *, email="jamie@example.invalid", kind="cas", start=START, submit="2026-09-05 10:00:00", prof=5):
    base = {"Student": "Example, Alex; MD2028", "Student External ID": "s001", "Student Email": "alex@example.invalid", "Evaluator": assessor, "Evaluator Email": email, "Evaluation": "*" + a.FORM_NAMES[kind], "Form Record": form, "Start Date": start, "End Date": END, "Submit Date": submit}
    if kind == "cas":
        rows = [{**base, "Question": q, "Multiple Choice Value": "" if n is None else str(n)} for q, n in zip(QUESTIONS, scores)]
        rows.append({**base, "Question": "Prof5.1/HH7.2 Professional Behavior", "Multiple Choice Value": str(prof)})
        rows.append({**base, "Question": "Describe at least one instance", "Multiple Choice Value": "", "Answer text": "Strong participation."})
        return rows
    if kind == "hp":
        return [{**base, "Question": "Confidence in performing history", "Multiple Choice Value": "4"}, {**base, "Question": "Confidence in performing physical exam", "Multiple Choice Value": "3"}]
    return [{**base, "Question": "Confidence in oral presentation", "Multiple Choice Value": "3"}]


def match(assessor="Able, Jamie", email="jamie@example.invalid", kind="cas", start=START, begin="2026-09-01", end="2026-09-04"):
    return {"Student Name": "Example, Alex; MD2028", "Student External ID": "s001", "Student Email": "alex@example.invalid", "Faculty Name": assessor, "Faculty Email": email, "Start Date": start, "End Date": END, "Evaluation Period Start Date": begin, "Evaluation Period End Date": end, "Manual Evaluations": "*" + a.FORM_NAMES[kind], "Type of Association": "evaluator_evaluates"}


def check(item=None, activity="Performing with direct preceptor supervision", status="Complete", start=START):
    return {"Student name": "Example, Alex; MD2028", "External ID": "s001", "Email": "alex@example.invalid", "Start Date": start, "Checklist": "Pediatrics Case Logs", "Checklist status": "Incomplete", "Item": item or a.REQUIRED_ITEMS[0], "Item status": status, "Time entered": "09/05/2026 03:30:00 PM", "Date": "09/05/2026", "*Assisted or Above": activity, "Comments": "A routine encounter."}


def run(*, evaluations=None, matches=None, checklist=None, settings=None, schedule=None, snapshot=None):
    return a.process(SCHEDULE if schedule is None else schedule, [check()] if checklist is None else checklist, [match()] if matches is None else matches, assessment() if evaluations is None else evaluations, snapshot or [], settings=settings or a.Settings(as_of="2026-09-19", data_through="2026-09-19", confirm_coverage=True))


def meta_for(candidates, extra=None):
    names = {}
    for r in candidates:
        for k in r:
            if k not in a.REPEAT:
                names[k] = r.get("redcap_repeat_instrument") or ("clerkship_tracking" if k.startswith("cst_") else "pediatric_clerkship_achievement_portfolio")
    rows = [{"field_name": name, "form_name": form, "field_type": "text", "text_validation_type_or_show_slider_number": "datetime_seconds_ymd" if name in {"submit_date", "epa_submit_date", "time_entered"} else ""} for name, form in names.items()]
    for change in extra or []:
        rows = [r for r in rows if r["field_name"] != change["field_name"]] + [change]
    return a.Metadata(rows)


def parent():
    return {"record_id": "s001", "redcap_repeat_instrument": "", "redcap_repeat_instance": "", "legal_name": "Example, Alex (MD)", "start_date": START, "end_date": END, "email": "alex@example.invalid"}


def apply_plan(snapshot, plan):
    result = copy.deepcopy(snapshot)
    for incoming in plan.records + plan.clear_records:
        found = next((r for r in result if tuple(r.get(k, "") for k in a.REPEAT) == tuple(incoming.get(k, "") for k in a.REPEAT)), None)
        if found is None:
            result.append(copy.deepcopy(incoming))
        else:
            found.update(incoming)
    return result


class InputTests(unittest.TestCase):
    def test_date_formats(self):
        for x in ("2026-08-31", "08-31-2026", "08/31/2026", "08/31/2026 03:30:00 PM"):
            self.assertEqual(a.day(x), START)
        self.assertIsNone(a.dt("entire_course"))
        self.assertIsNone(a.dt("impossible"))

    def test_unicode_names_and_credentials(self):
        self.assertEqual(a.name_key("Example, Alex; MD2028"), a.name_key("Example, Alex (MD)"))
        self.assertEqual(a.name_key("Example, Alex"), a.name_key("Alex Example"))
        self.assertEqual(a.name_key("García, María"), "maria garcia")

    def test_short_rows_preserved(self):
        log = a.Messages()
        rows = a.read_csv_bytes(b"a,b,c\n1,2\n", "test", log)
        self.assertEqual(rows, [{"a": "1", "b": "2", "c": ""}])
        self.assertEqual(len(log.rows), 1)

    def test_extra_fields_not_guessed(self):
        with self.assertRaises(ValueError):
            a.read_csv_bytes(b"a,b\n1,2,3\n", "test", a.Messages())

    def test_duplicate_activity_headers_retained(self):
        raw = a.read_csv_bytes(b'Item,*Assisted or Above,*Assisted or Above\nAcute,,Performing\n', "test", a.Messages())[0]
        self.assertIn("*Assisted or Above__dup2", raw)
        self.assertEqual(a.activity_of(raw), "Performing")

    def test_utf16(self):
        self.assertEqual(a.read_csv_bytes("a,b\n1,2\n".encode("utf-16"), "test", a.Messages())[0]["a"], "1")

    def test_schedule_only_names_resolve(self):
        schedule = [{"legal_name": "Example, Alex (MD)", "start_date": "08-31-2026"}]
        result = run(schedule=schedule)
        self.assertFalse(result.messages.blocked)
        self.assertEqual(result.roster[0]["record_id"], "s001")

    def test_ambiguous_student_name_blocks(self):
        extra = assessment()
        for r in extra:
            r["Student External ID"] = "s002"
        result = run(schedule=[{"legal_name": "Example, Alex (MD)", "start_date": START}], evaluations=assessment() + extra)
        self.assertTrue(result.messages.blocked)

    def test_no_activity_student_retained(self):
        result = run(evaluations=[], matches=[], checklist=[])
        self.assertEqual(len(result.roster), 1)
        self.assertEqual(result.reports["checklist_review"][0]["missing_count"], 10)
        self.assertEqual(result.reports["student_review"][0]["cas_credit"], 0)

    def test_wrong_cohort_is_not_missing(self):
        result = run(checklist=[check(start="2026-07-06")], matches=[match(start="2026-07-06")], settings=a.Settings(as_of="2026-09-19"))
        self.assertEqual(result.reports["checklist_review"][0]["status"], "Coverage not confirmed")
        self.assertEqual(result.reports["student_review"][0]["reminder_needed"], "Coverage not confirmed")
        self.assertFalse(result.reports["preceptor_reminders"])

    def test_flow_compatibility(self):
        b = a.csv_bytes([{"a": 'one,two\n"quoted"', "b": "=bad"}], ["a", "b"], flow=True)
        result = list(csv.DictReader(io.StringIO(b.decode("utf-8-sig"))))[0]
        self.assertNotIn(",", result["a"])
        self.assertTrue(result["b"].startswith("'="))


class ScoreTests(unittest.TestCase):
    def test_full_score(self):
        result = run(evaluations=assessment(scores=(5, 5, 5, 5, 5)))
        self.assertEqual(result.evaluations[0]["tot"], 375.0)
        self.assertEqual(result.reports["scores"][0]["clinical_score_375"], 375.0)

    def test_imputation_preserves_raw_na(self):
        result = run(evaluations=assessment(scores=(4, 5, None, 3, None)))
        e = result.evaluations[0]
        self.assertEqual(e["iv"], 4.0)
        self.assertEqual(e["tot"], 300.0)
        self.assertFalse(a.text(e.get("do")))
        self.assertEqual(e["effective_do"], 4.0)

    def test_all_na_is_received_not_scored(self):
        result = run(evaluations=assessment(scores=(None,) * 5))
        self.assertEqual(len(result.evaluations), 1)
        self.assertEqual(result.evaluations[0]["tot"], "")
        self.assertEqual(result.reports["scores"][0]["unscorable_submissions"], 1)
        self.assertFalse(result.reports["preceptor_reminders"])

    def test_question_rows_count_once(self):
        result = run(evaluations=assessment() + assessment())
        self.assertEqual(len(result.evaluations), 1)

    def test_distinct_same_day_forms_retained(self):
        result = run(evaluations=assessment("f1") + assessment("f2"))
        self.assertEqual(len(result.evaluations), 2)
        self.assertEqual(result.reports["student_review"][0]["cas_credit"], 1)

    def test_drop_only_at_four(self):
        for count in (1, 2, 3, 4, 5):
            ev = sum((assessment(f"f{i}", scores=(i + 1,) * 5) for i in range(count)), [])
            result = run(evaluations=ev)
            self.assertEqual(sum(e["drop_lowest"] for e in result.evaluations), int(count >= 4))

    def test_drop_lowest_keeps_received(self):
        ev = sum((assessment(f"f{i}", scores=(v,) * 5) for i, v in enumerate((2, 3, 4, 5))), [])
        result = run(evaluations=ev)
        self.assertEqual(result.reports["scores"][0]["clinical_score_375"], 300.0)
        self.assertFalse(result.reports["preceptor_reminders"])

    def test_manual_exclusion_before_drop_and_import(self):
        ev = sum((assessment(f"f{i}", assessor=f"Assessor, Person{i}", email=f"person{i}@example.invalid", scores=(v,) * 5) for i, v in enumerate((1, 2, 3, 4))), [])
        settings = a.Settings(as_of="2026-09-19", confirm_coverage=True, exclusions=[{"record_id": "s001", "evaluator": "Assessor, Person0"}])
        result = run(evaluations=ev, matches=[match("Assessor, Person0", "person0@example.invalid")], settings=settings)
        self.assertEqual(result.reports["scores"][0]["manual_exclusions"], 1)
        self.assertEqual(result.reports["scores"][0]["scorable_evaluations"], 3)
        self.assertFalse(any(e["drop_lowest"] for e in result.evaluations))
        self.assertFalse(result.reports["preceptor_reminders"])
        self.assertFalse(any(r.get("evaluator") == "Assessor, Person0" for r in a.import_candidates(result)))

    def test_tied_drop_earliest_submission(self):
        ev = []
        for i in (4, 3, 2, 1):
            ev += assessment(f"f{i}", submit=f"2026-09-0{i + 1} 10:00:00")
        result = run(evaluations=ev)
        self.assertEqual(next(e["form_record"] for e in result.evaluations if e["drop_lowest"]), "f1")

    def test_future_submissions_not_counted(self):
        result = run(evaluations=assessment(submit="2026-09-20 10:00:00"))
        self.assertFalse(result.evaluations)
        self.assertEqual(len(result.reports["preceptor_reminders"]), 1)

    def test_wrong_rotation_submission_does_not_satisfy(self):
        result = run(evaluations=assessment(start="2026-07-06", submit="2026-07-20 10:00:00"))
        self.assertFalse(result.evaluations)
        self.assertEqual(len(result.reports["preceptor_reminders"]), 1)

    def test_conflicting_answers_block(self):
        ev = assessment()
        ev.append({**ev[0], "Multiple Choice Value": "2"})
        self.assertTrue(run(evaluations=ev).messages.blocked)

    def test_rating_six_not_treated_as_score(self):
        self.assertTrue(run(evaluations=assessment(scores=(6, 4, 4, 4, 4))).messages.blocked)

    def test_professionalism_survives_drop(self):
        ev = assessment("f0", scores=(1,) * 5, prof=1)
        for i in range(1, 4):
            ev += assessment(f"f{i}", scores=(5,) * 5)
        result = run(evaluations=ev)
        self.assertTrue(result.reports["scores"][0]["professionalism_review"])
        self.assertTrue(result.reports["scores"][0]["individual_domain_below_3"])


class ReminderTests(unittest.TestCase):
    def test_duplicate_matches_one_obligation(self):
        result = run(evaluations=[], matches=[match()] * 3 + [match(begin="2026-09-06", end="2026-09-09")])
        self.assertEqual(len(result.reports["preceptor_reminders"]), 1)
        self.assertEqual(result.reports["preceptor_reminders"][0]["expected_eval_count"], 1)

    def test_one_submission_clears_all_duplicate_matches(self):
        result = run(matches=[match(), match(begin="2026-09-06", end="2026-09-09")])
        self.assertFalse(result.reports["preceptor_reminders"])

    def test_three_types_combined_one_row(self):
        result = run(evaluations=[], matches=[match(kind=k) for k in a.KINDS])
        self.assertEqual(len(result.reports["preceptor_reminders"]), 1)
        self.assertEqual(result.reports["preceptor_reminders"][0]["pending_eval_count"], 3)
        self.assertTrue(result.reports["preceptor_reminders"][0]["cas_link"])
        self.assertTrue(result.reports["preceptor_reminders"][0]["hp_link"])
        self.assertEqual(result.reports["preceptor_reminders"][0]["handoff_link"], "")

    def test_assessment_type_matters(self):
        result = run(matches=[match(kind=k) for k in a.KINDS])
        row = result.reports["preceptor_reminders"][0]
        self.assertEqual(row["pending_eval_count"], 2)
        self.assertEqual(row["cas_link"], "")

    def test_pipe_split_ignores_teaching_eval(self):
        m = match()
        m["Manual Evaluations"] = "*Clinical Teaching Eval|*Clinical Assessment of Student"
        result = run(evaluations=[], matches=[m])
        self.assertEqual(len(result.matches), 1)

    def test_not_due_with_grace(self):
        result = run(evaluations=[], matches=[match(end="2026-09-19")], settings=a.Settings(as_of="2026-09-19", confirm_coverage=True, grace_days=1))
        self.assertFalse(result.reports["preceptor_reminders"])

    def test_due_on_end_date(self):
        result = run(evaluations=[], matches=[match(end="2026-09-19")])
        self.assertEqual(len(result.reports["preceptor_reminders"]), 1)

    def test_request_plus_submission_not_double_counted(self):
        result = run()
        self.assertEqual(result.reports["student_review"][0]["cas_credit"], 1)
        self.assertEqual(result.reports["student_review"][0]["cas_matched"], 1)
        self.assertEqual(result.reports["student_review"][0]["cas_submitted"], 1)

    def test_name_fallback_when_email_absent(self):
        result = run(evaluations=assessment(email=""))
        self.assertFalse(result.reports["preceptor_reminders"])

    def test_missing_preceptor_email_not_director_substitution(self):
        result = run(evaluations=[], matches=[match(email="")])
        self.assertFalse(result.reports["preceptor_reminders"])
        self.assertEqual(result.reports["match_audit"][0]["status"], "Pending")

    def test_no_default_prefilled_grades(self):
        result = run(evaluations=[])
        self.assertEqual(result.reports["preceptor_reminders"][0]["partial_form_link"], "")

    def test_observation_allowed_only_two_categories(self):
        checks = [check(item=x, activity="Observing") for x in a.REQUIRED_ITEMS]
        result = run(checklist=checks)
        row = result.reports["checklist_review"][0]
        self.assertEqual(row["missing_count"], 0)
        self.assertEqual(row["observing_only_count"], 8)

    def test_unknown_participation_not_complete(self):
        result = run(checklist=[check(item=x, activity="") for x in a.REQUIRED_ITEMS])
        row = result.reports["checklist_review"][0]
        self.assertEqual(row["status"], "Needs review")
        self.assertTrue(row["participation_review_items"])

    def test_incomplete_item_not_satisfied(self):
        result = run(checklist=[check(status="Incomplete")])
        self.assertIn(a.REQUIRED_ITEMS[0], result.reports["checklist_review"][0]["incomplete_items"])

    def test_performed_entry_satisfies_observation_only_item(self):
        result = run(checklist=[check(activity="Observing"), check(activity="Performing")])
        self.assertEqual(result.reports["checklist_review"][0]["observing_only_count"], 0)

    def test_all_complete_student_no_reminder(self):
        ev, matches = [], []
        for kind, n in (("cas", 8), ("hp", 2), ("handoff", 1)):
            for i in range(n):
                ev += assessment(f"{kind}{i}", assessor=f"Assessor, Person{i}", email=f"p{i}@example.invalid", kind=kind)
        result = run(evaluations=ev, matches=[], checklist=[check(item=x) for x in a.REQUIRED_ITEMS])
        self.assertEqual(result.reports["student_review"][0]["reminder_needed"], "No")
        self.assertEqual(result.reports["checklist_review"][0]["status"], "Complete")
        files = a.output_files(result)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(files["student_checklist_review.csv"].decode("utf-8-sig"))))), 0)


class SyncTests(unittest.TestCase):
    def test_new_repeat_append_and_reimport_idempotent(self):
        result = run()
        candidates = a.import_candidates(result, include_tracking=False)
        metadata = meta_for(candidates)
        snapshot = [parent()]
        plan = a.plan_sync(result, snapshot, metadata, include_tracking=False)
        self.assertFalse(plan.errors, plan.errors)
        self.assertEqual({r['redcap_repeat_instance'] for r in plan.records if r['redcap_repeat_instrument']}, {"1"})
        snapshot = apply_plan(snapshot, plan)
        second = a.plan_sync(result, snapshot, metadata, include_tracking=False)
        self.assertFalse(second.errors, second.errors)
        self.assertFalse(second.records)

    def test_existing_instances_not_renumbered(self):
        result = run()
        candidates = a.import_candidates(result, False)
        old = next(r for r in candidates if r.get("redcap_repeat_instrument") == "oasis_eval")
        snapshot = [parent(), {**old, "redcap_repeat_instance": "7"}]
        metadata = meta_for(candidates)
        plan = a.plan_sync(result, snapshot, metadata, include_tracking=False)
        self.assertFalse(plan.errors, plan.errors)
        self.assertFalse(any(r['redcap_repeat_instrument'] == 'oasis_eval' for r in plan.records))

    def test_partial_grading_source_withholds_exclusion(self):
        result = run()
        extra = {"record_id": "s001", "redcap_repeat_instrument": "oasis_eval", "redcap_repeat_instance": "1", "evaluator": "Other, Person", "evaluator_email": "other@example.invalid", "evaluation": a.FORM_NAMES["cas"], "submit_date": "2026-09-01 23:59:00"}
        plan = a.plan_sync(result, [parent(), extra], meta_for(a.import_candidates(result)), include_tracking=True)
        self.assertFalse(any("exclude" in r for r in plan.records))
        self.assertTrue(any("absent from" in m for m in plan.warnings))

    def test_legacy_date_only_singleton_reused(self):
        result = run()
        candidates = a.import_candidates(result, False)
        old = next(r for r in candidates if r.get("redcap_repeat_instrument") == "oasis_eval")
        old = {**old, "redcap_repeat_instance": "7", "submit_date": "09-05-2026 23:59"}
        old.pop("oasis_form_record", None)
        metadata = meta_for(candidates)
        plan = a.plan_sync(result, [parent(), old], metadata, include_tracking=False)
        self.assertFalse(plan.errors, plan.errors)
        updated = next(r for r in plan.records if r['redcap_repeat_instrument'] == 'oasis_eval')
        self.assertEqual(updated['redcap_repeat_instance'], '7')
        self.assertNotIn('submit_date', updated)
        self.assertEqual(updated['oasis_form_record'], 'f1')

    def test_ambiguous_same_day_mapping_blocks(self):
        result = run(evaluations=assessment('f1') + assessment('f2'))
        candidates = a.import_candidates(result, False)
        old = next(r for r in candidates if r.get("redcap_repeat_instrument") == "oasis_eval")
        old = {**old, "redcap_repeat_instance": "1", "submit_date": "09-05-2026 23:59"}
        old.pop("oasis_form_record", None)
        plan = a.plan_sync(result, [parent(), old], meta_for(candidates), include_tracking=False)
        self.assertTrue(any("same-day" in e for e in plan.errors))

    def test_conflicting_existing_values_preserved(self):
        result = run()
        candidates = a.import_candidates(result, False)
        old = next(r for r in candidates if r.get("redcap_repeat_instrument") == "oasis_eval")
        old = {**old, 'redcap_repeat_instance': '1', 'cas_strengths': 'Manually corrected narrative'}
        plan = a.plan_sync(result, [parent(), old], meta_for(candidates), include_tracking=False)
        self.assertTrue(any(c['action'] == 'Conflict — preserved' and c['field'] == 'cas_strengths' for c in plan.changes))
        self.assertFalse(any('cas_strengths' in r for r in plan.records))

    def test_calculated_and_unrelated_fields_not_written(self):
        result = run()
        candidates = a.import_candidates(result, False)
        metadata = meta_for(candidates, [{'field_name': 'tot', 'form_name': 'oasis_eval', 'field_type': 'calc'}])
        existing = {**parent(), 'final_grade': 'MANUAL', 'nbme': '88'}
        plan = a.plan_sync(result, [existing], metadata, include_tracking=False)
        self.assertFalse(plan.errors, plan.errors)
        self.assertFalse(any('tot' in r or 'final_grade' in r or 'nbme' in r for r in plan.records))

    def test_readonly_annotated_field_not_written(self):
        result = run()
        candidates = a.import_candidates(result, False)
        metadata = meta_for(candidates, [{'field_name': 'cas_strengths', 'form_name': 'oasis_eval', 'field_type': 'notes', 'field_annotation': '@READONLY'}])
        plan = a.plan_sync(result, [parent()], metadata, include_tracking=False)
        self.assertFalse(plan.errors, plan.errors)
        self.assertFalse(any('cas_strengths' in r for r in plan.records))

    def test_wrong_rotation_parent_blocks(self):
        plan = a.plan_sync(run(), [{**parent(), 'start_date': '2026-07-06'}])
        self.assertTrue(plan.errors)

    def test_no_snapshot_does_not_create_students(self):
        self.assertTrue(a.plan_sync(run(), []).errors)

    def test_choice_encoding_and_dates(self):
        metadata = a.Metadata([{'field_name':'evaluation','form_name':'oasis_eval','field_type':'dropdown','select_choices_or_calculations':'1, Clinical Assessment of Student | 2, Other'}, {'field_name':'date','field_type':'text','text_validation_type_or_show_slider_number':'date_mdy'}])
        self.assertEqual(metadata.encode('evaluation', 'Clinical Assessment of Student'), '1')
        self.assertEqual(metadata.encode('date', '09-19-2026'), '2026-09-19')
        with self.assertRaises(ValueError):
            metadata.encode('evaluation', 'Not a choice')

    def test_exclusion_clears_only_with_explicit_opt_in(self):
        result = run()
        candidates = a.import_candidates(result, False)
        old = {**parent(), 'exclude': 'Exclude lowest of 4: Old'}
        normal = a.plan_sync(result, [old], meta_for(candidates), include_tracking=False)
        self.assertFalse(normal.clear_records)
        clear = a.plan_sync(result, [old], meta_for(candidates), include_tracking=False, allow_clears=True)
        self.assertEqual(clear.clear_records, [{'record_id':'s001','redcap_repeat_instrument':'','redcap_repeat_instance':'','exclude':''}])

    def test_snapshot_change_prevents_any_import(self):
        result = run()
        candidates = a.import_candidates(result, False)
        metadata = meta_for(candidates)
        plan = a.plan_sync(result, [parent()], metadata, include_tracking=False)
        client = a.RedcapClient(a.API_URL, 'TEST_TOKEN_NOT_VALID')
        with patch.object(client, 'read', return_value=([{**parent(), 'name':'Changed'}], metadata)), patch.object(client, 'call') as call:
            with self.assertRaises(RuntimeError):
                client.upload(plan, result)
            call.assert_not_called()

    def test_metadata_change_prevents_any_import(self):
        result = run()
        candidates = a.import_candidates(result, False)
        metadata = meta_for(candidates)
        plan = a.plan_sync(result, [parent()], metadata, include_tracking=False)
        changed = meta_for(candidates, [{'field_name': 'tot', 'form_name': 'oasis_eval', 'field_type': 'calc'}])
        client = a.RedcapClient(a.API_URL, 'TEST_TOKEN_NOT_VALID')
        with patch.object(client, 'read', return_value=([parent()], changed)), patch.object(client, 'call') as call:
            with self.assertRaisesRegex(RuntimeError, 'field definitions changed'):
                client.upload(plan, result)
            call.assert_not_called()

    def test_mocked_upload_and_readback_success(self):
        result = run()
        candidates = a.import_candidates(result, False)
        metadata = meta_for(candidates)
        snapshot = [parent()]
        plan = a.plan_sync(result, snapshot, metadata, include_tracking=False)
        client = a.RedcapClient(a.API_URL, 'TEST_TOKEN_NOT_VALID')
        final = apply_plan(snapshot, plan)
        with patch.object(client, 'read', side_effect=[(snapshot, metadata), (final, metadata)]), patch.object(client, 'call', return_value={'count': len(plan.records)}) as call:
            receipts = client.upload(plan, result)
            self.assertEqual(receipts[-1]['server_response'], 'All submitted fields verified')
            self.assertTrue(call.called)
            self.assertTrue(all(c.kwargs.get('action') == 'import' for c in call.call_args_list))

    def test_mocked_readback_difference_reported(self):
        result = run()
        candidates = a.import_candidates(result, False)
        metadata = meta_for(candidates)
        snapshot = [parent()]
        plan = a.plan_sync(result, snapshot, metadata, include_tracking=False)
        client = a.RedcapClient(a.API_URL, 'TEST_TOKEN_NOT_VALID')
        with patch.object(client, 'read', side_effect=[(snapshot, metadata), (snapshot, metadata)]), patch.object(client, 'call', return_value={'count': len(plan.records)}):
            with self.assertRaisesRegex(RuntimeError, 'read-back verification found differences'):
                client.upload(plan, result)

    def test_api_requires_https(self):
        with self.assertRaises(ValueError):
            a.RedcapClient('http://example.invalid/api/', 'not_real')
        with self.assertRaises(ValueError):
            a.RedcapClient(a.API_URL, '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
