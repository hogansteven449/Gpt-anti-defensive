"""Bounded synthetic plumbing checks; these make no writing-efficacy claims."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from readeraware import pipeline
from readeraware.fixture import FixtureBackend
from readeraware.util import ValidationError, read_json, validate_schema

EXPECTED_STAGES = ["review", "probabilities", "selection", "body", "future"]


def data():
    sources = [
        {"source_id": "S001", "name": "first.md", "text": "Alpha finding. Literal {{USER_PROMPT}} stays literal."},
        {"source_id": "S002", "name": "second.md", "text": "Beta analysis. Gamma method. Delta result."},
    ]
    refs = [
        [{"source_id": "S001", "quote": "Alpha finding."}],
        [{"source_id": "S002", "quote": "Beta analysis."}],
        [{"source_id": "S002", "quote": "Gamma method."}],
        [{"source_id": "S002", "quote": "Delta result."}],
    ]
    review = {"points": [{"id": f"R{i + 1}", "question": f"RAW_QUESTION_SENTINEL_{i + 1}?",
               "source_basis": refs[i], "answer_basis": "partly_supplied",
               "relevance": f"PRIVATE_RELEVANCE_SENTINEL_{i + 1}"} for i in range(4)]}
    values = [(0.8, 0.2), (0.5, 0.8), (0.5, 0.5), (0.2, 0.5)]
    estimates = {"estimates": [{"id": f"R{i + 1}", "p1_event": f"CHALLENGE_EVENT_{i + 1}",
                    "p1": p1, "p1_reason": f"CHALLENGE_REASON_{i + 1}",
                    "p2_event": f"FUTURE_EVENT_{i + 1}", "p2": p2,
                    "p2_reason": f"FUTURE_REASON_{i + 1}"} for i, (p1, p2) in enumerate(values)]}
    return sources, review, estimates


def selected(review, estimates, threshold=0.5):
    """Hand-specified expected order for the fixed crossing/tie test above."""
    by_id = {item["id"]: item for item in review["points"]}
    frozen = {item["id"]: item for item in estimates["estimates"]}
    if threshold == 0.5:
        body_ids, future_ids = ["R1", "R2", "R3"], ["R2", "R3", "R4"]
    elif threshold == 0.6:
        body_ids, future_ids = ["R1"], ["R2"]
    else:
        raise AssertionError("Unexpected test threshold")
    def objectives(ids, probability, label):
        return [{"id": identifier, "probability": frozen[identifier][probability],
                 "writing_objective": f"{label} APPROVED_OBJECTIVE_{identifier}",
                 "source_refs": deepcopy(by_id[identifier]["source_basis"])} for identifier in ids]
    return {"audit": {"threshold": threshold, "threshold_reason": "Synthetic test threshold",
                      "records": deepcopy(estimates["estimates"])},
            "body_points": objectives(body_ids, "p1", "Explain"),
            "future_points": objectives(future_ids, "p2", "Explore")}


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.sources, self.review, self.estimates = data()
        self.selection = selected(self.review, self.estimates)

    def check(self, stage, result):
        validate_schema(result, read_json(pipeline.REFERENCES / "schemas" / (stage + ".json")))
        if stage == "review":
            pipeline.validate_review(result, self.sources)
        elif stage == "probabilities":
            pipeline.validate_probabilities(result, self.review)
        else:
            pipeline.validate_selection(result, self.review, self.estimates, self.sources)

    def test_template_values_are_never_reinterpreted(self):
        actual = pipeline.render("Source={{SOURCE}}; task={{USER_PROMPT}}", {
            "SOURCE": "literal {{USER_PROMPT}} and {{SOURCE}}", "USER_PROMPT": "USER_SECRET"})
        self.assertEqual(actual, "Source=literal {{USER_PROMPT}} and {{SOURCE}}; task=USER_SECRET")
        with self.assertRaises(ValidationError):
            pipeline.render("{{MISSING}}", {})

    def test_review_projection_is_exact_whitelist(self):
        projected = pipeline.project_review(self.review)
        self.assertEqual(len(projected), 4)
        for original, projected_item in zip(self.review["points"], projected):
            self.assertEqual(projected_item, {key: original[key] for key in ("id", "question", "source_basis", "answer_basis")})

    def test_evidence_must_quote_the_named_source_exactly(self):
        self.check("review", self.review)
        for bad_id, bad_quote in [("S099", "Alpha finding."), ("S002", "Alpha finding."), ("S001", "alpha finding.")]:
            with self.subTest(source_id=bad_id, quote=bad_quote):
                bad = deepcopy(self.review)
                bad["points"][0]["source_basis"] = [{"source_id": bad_id, "quote": bad_quote}]
                with self.assertRaises(ValidationError):
                    self.check("review", bad)

    def test_b1_requires_all_ids_in_order_and_finite_nonboolean_probabilities(self):
        self.check("probabilities", self.estimates)
        for mutation in ("missing", "duplicate", "reversed", "nan", "infinity", "boolean", "negative", "extra"):
            with self.subTest(mutation=mutation):
                bad = deepcopy(self.estimates)
                if mutation == "missing": bad["estimates"].pop()
                elif mutation == "duplicate": bad["estimates"][1]["id"] = "R1"
                elif mutation == "reversed": bad["estimates"].reverse()
                elif mutation == "extra": bad["estimates"][0]["task"] = "forbidden"
                else: bad["estimates"][0]["p1"] = {"nan": float("nan"), "infinity": float("inf"), "boolean": True, "negative": -0.01}[mutation]
                with self.assertRaises(ValidationError):
                    self.check("probabilities", bad)

    def test_b2_copies_every_frozen_field_and_uses_inclusive_independent_gates(self):
        self.check("selection", self.selection)
        self.assertEqual([p["id"] for p in self.selection["body_points"]], ["R1", "R2", "R3"])
        self.assertEqual([p["id"] for p in self.selection["future_points"]], ["R2", "R3", "R4"])
        for key in self.estimates["estimates"][0]:
            with self.subTest(changed_frozen_field=key):
                bad = deepcopy(self.selection)
                bad["audit"]["records"][0][key] = 0.75 if key in ("p1", "p2") else "CHANGED"
                with self.assertRaises(ValidationError):
                    self.check("selection", bad)
        for mutation in ("omit_qualified", "include_low", "tie_order", "wrong_probability", "wrong_source_refs", "audit_order"):
            with self.subTest(mutation=mutation):
                bad = deepcopy(self.selection)
                if mutation == "omit_qualified": bad["body_points"].pop()
                elif mutation == "include_low": bad["body_points"].append(deepcopy(bad["future_points"][-1]))
                elif mutation == "tie_order": bad["body_points"][1:] = reversed(bad["body_points"][1:])
                elif mutation == "wrong_probability": bad["future_points"][0]["probability"] = 0.79
                elif mutation == "wrong_source_refs": bad["body_points"][0]["source_refs"] = deepcopy(bad["body_points"][1]["source_refs"])
                else: bad["audit"]["records"].reverse()
                with self.assertRaises(ValidationError):
                    self.check("selection", bad)

    def test_threshold_interval_and_explicit_override_are_enforced(self):
        overridden = selected(self.review, self.estimates, 0.6)
        pipeline.validate_selection(overridden, self.review, self.estimates, self.sources, threshold_override=0.6)
        with self.assertRaises(ValidationError):
            pipeline.validate_selection(self.selection, self.review, self.estimates, self.sources, threshold_override=0.6)
        for invalid in (0.3299, 0.6701, float("nan"), float("inf"), True):
            with self.subTest(threshold=invalid):
                bad = deepcopy(self.selection)
                bad["audit"]["threshold"] = invalid
                with self.assertRaises(ValidationError):
                    self.check("selection", bad)
        for boundary, base in ((0.33, self.selection), (0.67, overridden)):
            with self.subTest(valid_boundary=boundary):
                valid = deepcopy(base)
                valid["audit"]["threshold"] = boundary
                self.check("selection", valid)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sources, self.review, self.estimates = data()
        self.selection = selected(self.review, self.estimates)
        self.paths = []
        for source in self.sources:
            path = self.root / source["name"]
            path.write_text(source["text"], encoding="utf-8")
            self.paths.append(str(path))
        self.task = "USER_TASK_SECRET_6731: Write an affirmative article. Keep {{SOURCE}} literal."
        self.task_path = self.root / "task.txt"
        self.task_path.write_text(self.task, encoding="utf-8")
        self.args = argparse.Namespace(source=self.paths, task_file=str(self.task_path), out=str(self.root / "output"),
            threshold=None, future_work="auto", word_budget=500, future_words=80, backend="fixture",
            model=None, effort=None, timeout=60, codex_bin=None)
        self.calls = []
        self.main = "# Final Body\n\nThe α finding is stated clearly.\n"
        self.appendix = "## Future Work\n\nExplore the approved prospective objective.\n"

    def invoke(self, *, fail=None, no_future=False, empty_appendix=False, empty_review=False):
        test = self
        class RecordingBackend(FixtureBackend):
            def run(self, stage, prompt, schema, stage_dir, context):
                test.calls.append({"stage": stage, "prompt": prompt, "context": deepcopy(context), "directory": stage_dir})
                if stage == fail:
                    raise RuntimeError("Deliberate offline stage failure")
                if stage == "review": result = {"points": []} if empty_review else deepcopy(test.review)
                elif stage == "probabilities": result = {"estimates": []} if empty_review else deepcopy(test.estimates)
                elif stage == "selection":
                    result = deepcopy(test.selection)
                    if empty_review:
                        result = {"audit": {"threshold": 0.5, "threshold_reason": "Empty synthetic test", "records": []}, "body_points": [], "future_points": []}
                    if no_future:
                        # Use genuinely ineligible future probabilities, then update the full frozen audit.
                        result["future_points"] = []
                    if fail == "invalid_selection": result["audit"]["records"][0]["p1_reason"] = "CHANGED"
                elif stage == "body": result = {"document": test.main}
                elif stage == "future": result = {"appendix": "" if empty_appendix else test.appendix}
                else: raise AssertionError(stage)
                return result, {"backend": "fixture", "real_model_call": False, "usage": None, "elapsed_seconds": 0, "fresh_session": True}
        if no_future:
            for item in self.estimates["estimates"]: item["p2"] = 0.1
            self.selection["audit"]["records"] = deepcopy(self.estimates["estimates"])
        with patch("readeraware.fixture.FixtureBackend", RecordingBackend):
            return pipeline.run_pipeline(self.args)

    def test_actual_stage_packets_isolate_b1_and_writer_contexts(self):
        summary = self.invoke()
        self.assertEqual([c["stage"] for c in self.calls], EXPECTED_STAGES)
        self.assertEqual(summary["actual_backend_calls"], 5)
        self.assertEqual(summary["actual_real_model_calls"], 0)
        self.assertTrue(summary["synthetic"])
        self.assertIn("plumbing", summary["fixture_note"])
        self.assertFalse(summary["semantic_quality_guaranteed"])
        b1, b2, body, future = self.calls[1:]
        self.assertEqual(set(b1["context"]), {"source", "review_points"})
        self.assertNotIn("USER_TASK_SECRET_6731", b1["prompt"])
        self.assertNotIn("PRIVATE_RELEVANCE_SENTINEL", b1["prompt"])
        self.assertIn("{{USER_PROMPT}}", b1["prompt"])
        for call in (self.calls[0], b2, body, future):
            self.assertEqual(call["context"]["user_prompt"], self.task)
        self.assertEqual(b2["context"]["frozen_estimates"], self.estimates)
        self.assertEqual(set(body["context"]), {"source", "user_prompt", "body_points", "body_length_budget"})
        self.assertEqual(body["context"]["body_points"], self.selection["body_points"])
        self.assertEqual(body["context"]["body_length_budget"], 420)
        for sentinel in ("RAW_QUESTION_SENTINEL", "PRIVATE_RELEVANCE_SENTINEL", "CHALLENGE_REASON", "FUTURE_REASON", "Explore APPROVED_OBJECTIVE"):
            self.assertNotIn(sentinel, body["prompt"])
        self.assertEqual(future["context"]["frozen_body"], self.main)
        self.assertEqual(future["context"]["future_points"], self.selection["future_points"])
        for call in self.calls:
            self.assertEqual(read_json(call["directory"] / "input.json"), call["context"])
            self.assertEqual((call["directory"] / "request.txt").read_text(encoding="utf-8"), call["prompt"])
        out = Path(self.args.out)
        self.assertEqual(read_json(out / "frozen_estimates.json"), self.estimates)
        self.assertEqual(read_json(out / "selection_audit.json")["records"], self.estimates["estimates"])
        self.assertEqual((out / "outputs/main.md").read_bytes(), self.main.encode("utf-8"))
        self.assertEqual((out / "outputs/future.md").read_bytes(), self.appendix.encode("utf-8"))
        self.assertEqual((out / "outputs/document.md").read_bytes(), (self.main + "\n\n" + self.appendix).encode("utf-8"))
        self.assertEqual(summary["frozen_main_sha256_utf8"], hashlib.sha256(self.main.encode("utf-8")).hexdigest())

    def test_disabled_future_issues_four_calls_and_uses_full_body_budget(self):
        self.args.future_work = "off"
        summary = self.invoke()
        self.assertEqual([c["stage"] for c in self.calls], EXPECTED_STAGES[:-1])
        self.assertEqual(summary["actual_backend_calls"], 4)
        self.assertEqual(summary["body_length_budget"], 500)
        self.assertTrue(summary["future_call_skipped"])
        self.assertEqual((Path(self.args.out) / "outputs/document.md").read_bytes(), self.main.encode("utf-8"))

    def test_empty_future_list_skips_call_even_when_enabled(self):
        self.args.future_work = "on"
        summary = self.invoke(no_future=True)
        self.assertEqual(summary["actual_backend_calls"], 4)
        self.assertEqual(summary["body_length_budget"], 500)
        self.assertTrue(summary["future_call_skipped"])

    def test_empty_review_is_valid_and_writer_still_runs_once(self):
        summary = self.invoke(empty_review=True)
        self.assertEqual(summary["actual_backend_calls"], 4)
        self.assertEqual(self.calls[-1]["context"]["body_points"], [])
        self.assertEqual(summary["review_points"], 0)

    def test_empty_appendix_does_not_change_body_or_add_separator(self):
        summary = self.invoke(empty_appendix=True)
        self.assertEqual(summary["actual_backend_calls"], 5)
        self.assertEqual((Path(self.args.out) / "outputs/document.md").read_bytes(), self.main.encode("utf-8"))

    def test_failed_stage_aborts_and_existing_attempt_is_never_reused(self):
        with self.assertRaisesRegex(RuntimeError, "Deliberate offline"):
            self.invoke(fail="selection")
        out = Path(self.args.out)
        self.assertEqual([c["stage"] for c in self.calls], ["review", "probabilities", "selection"])
        self.assertEqual(read_json(out / "manifest.json")["status"], "failed")
        self.assertFalse((out / "summary.json").exists())
        self.assertFalse((out / "outputs/document.md").exists())
        before = {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()}
        with self.assertRaisesRegex(ValidationError, "already exists"):
            self.invoke()
        self.assertEqual(len(self.calls), 3)
        after = {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()}
        self.assertEqual(after, before)

    def test_invalid_b2_is_preserved_and_never_sent_to_writer(self):
        with self.assertRaises(ValidationError):
            self.invoke(fail="invalid_selection")
        self.assertEqual([c["stage"] for c in self.calls], ["review", "probabilities", "selection"])
        directory = self.calls[-1]["directory"]
        self.assertEqual(read_json(directory / "response.json")["audit"]["records"][0]["p1_reason"], "CHANGED")
        self.assertFalse((directory / "receipt.json").exists())
        self.assertEqual(read_json(Path(self.args.out) / "manifest.json")["status"], "failed")

    def test_failed_future_preserves_frozen_body_without_final_success(self):
        with self.assertRaisesRegex(RuntimeError, "Deliberate offline"):
            self.invoke(fail="future")
        out = Path(self.args.out)
        self.assertEqual([c["stage"] for c in self.calls], EXPECTED_STAGES)
        self.assertEqual((out / "outputs/main.md").read_bytes(), self.main.encode("utf-8"))
        self.assertFalse((out / "outputs/document.md").exists())
        self.assertFalse((out / "summary.json").exists())
        self.assertEqual(read_json(out / "manifest.json")["status"], "failed")

    def test_completed_and_empty_existing_directories_are_both_rejected(self):
        self.invoke()
        calls = len(self.calls)
        out = Path(self.args.out)
        main = (out / "outputs/main.md").read_bytes()
        with self.assertRaisesRegex(ValidationError, "already exists"):
            self.invoke()
        self.assertEqual(len(self.calls), calls)
        self.assertEqual((out / "outputs/main.md").read_bytes(), main)
        empty = self.root / "empty-existing"
        empty.mkdir()
        self.args.out = str(empty)
        with self.assertRaisesRegex(ValidationError, "already exists"):
            self.invoke()
        self.assertEqual(list(empty.iterdir()), [])
        self.assertEqual(len(self.calls), calls)

    def test_credential_bearing_input_rejected_before_artifacts_or_calls(self):
        secret = "sk-" + "z" * 32
        Path(self.paths[0]).write_text("Accidental credential " + secret, encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "credential"):
            self.invoke()
        self.assertEqual(self.calls, [])
        self.assertFalse(Path(self.args.out).exists())


if __name__ == "__main__":
    unittest.main()
