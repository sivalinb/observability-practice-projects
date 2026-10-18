import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("investigator_project", ROOT / "projects/08-ai-incident-investigator/project.py")
project = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(project)


class InvestigatorTests(unittest.TestCase):
    def test_evaluation_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            result = project.run_demo(Path(directory))
            self.assertTrue(all(row["passed"] for row in result["checks"]))

    def test_missing_evidence_abstains(self):
        self.assertIsNone(project.investigate([])["conclusion"])
        self.assertIsNone(project.investigate(project.fixture()[:1])["conclusion"])

    def test_validator_rejects_unsupported_model_conclusion(self):
        result = project.investigate(project.fixture())
        result["conclusion"] = "bad deployment"
        with self.assertRaisesRegex(ValueError, "sufficiency"):
            project.validate_model_response(result, project.fixture())

    def test_duplicate_evidence_is_not_independent_support(self):
        with self.assertRaisesRegex(ValueError, "unique"):
            project.EvidenceStore(project.fixture() + [project.fixture()[0]])

    def test_valid_model_shaped_result(self):
        result = project.investigate(project.fixture())
        self.assertEqual(project.validate_model_response(result, project.fixture())["conclusion"], "dependency failure")

    def test_duplicate_hypotheses_are_not_competing_explanations(self):
        result = project.investigate(project.fixture())
        result["hypotheses"] = [result["hypotheses"][0]] * 2
        with self.assertRaisesRegex(ValueError, "distinct"):
            project.validate_model_response(result, project.fixture())

    def test_wrong_sign_evidence_is_rejected(self):
        result = project.investigate(project.fixture())
        result["hypotheses"][0]["supporting_evidence"] = ["E03"]
        with self.assertRaisesRegex(ValueError, "claimed observation"):
            project.validate_model_response(result, project.fixture())

    def test_model_schema_constrains_actions_and_citation_signs(self):
        schema = project.model_response_schema(project.fixture())
        supported = schema["oneOf"][0]
        self.assertEqual(set(supported["properties"]["next_action"]["enum"]), project.SAFE_ACTIONS)
        self.assertEqual(set(supported["properties"]["conclusion"]["enum"]), set(project.HYPOTHESES))
        alternatives = supported["properties"]["hypotheses"]["items"]["oneOf"]
        dependency = next(row for row in alternatives if row["properties"]["name"]["const"] == "dependency failure")
        self.assertEqual(set(dependency["properties"]["supporting_evidence"]["items"]["enum"]), {"E01", "E02", "E05"})
        self.assertEqual(dependency["properties"]["contradicting_evidence"]["maxItems"], 0)

    def test_duplicate_citations_are_rejected(self):
        result = project.investigate(project.fixture())
        result["hypotheses"][0]["supporting_evidence"].append("E01")
        with self.assertRaisesRegex(ValueError, "duplicated"):
            project.validate_model_response(result, project.fixture())


if __name__ == "__main__":
    unittest.main()
