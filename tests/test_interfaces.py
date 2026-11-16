import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from adaptive_rag.api import create_api
from adaptive_rag.app import RagApplication
from adaptive_rag.cli import main
from adaptive_rag.config import Principal, Settings
from adaptive_rag.domain import AccessContext, IngestRequest, QueryRequest, ValidationError
from adaptive_rag.evaluation import load_cases, retrieval_metrics, run_evaluation


ADMIN_TOKEN = "admin-test-token-0000000000000001"
READER_TOKEN = "reader-test-token-000000000000001"
OTHER_TOKEN = "other-test-token-0000000000000001"


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = Settings(data_dir=self.root / "data", api_tokens={
            ADMIN_TOKEN: Principal("alpha", "admin", admin=True),
            READER_TOKEN: Principal("alpha", "reader"),
            OTHER_TOKEN: Principal("beta", "admin", admin=True),
        })
        self.client = TestClient(create_api(self.settings), raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def headers(self, token=ADMIN_TOKEN):
        return {"Authorization": f"Bearer {token}"}

    def publish_document(self):
        response = self.client.post("/documents", headers=self.headers(), json={
            "document_id": "policy", "version": "v1",
            "content": "# Returns\nThe amber warranty policy provides twelve months of coverage.",
        })
        self.assertEqual(response.status_code, 201, response.text)
        job = response.json()
        response = self.client.post("/publish", headers=self.headers(), json={"job_id": job["job_id"]})
        self.assertEqual(response.status_code, 200, response.text)
        return job, response.json()

    def test_api_authentication_and_roles(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/capabilities").status_code, 401)
        self.assertEqual(self.client.get("/capabilities", headers=self.headers("incorrect")).status_code, 401)
        self.assertEqual(self.client.get("/capabilities", headers=self.headers(READER_TOKEN)).status_code, 200)
        denied = self.client.post("/documents", headers=self.headers(READER_TOKEN), json={
            "document_id": "p", "version": "v1", "content": "warranty",
        })
        self.assertEqual(denied.status_code, 403, denied.text)
        with TestClient(create_api(Settings(data_dir=self.root / "no-tokens"))) as client:
            self.assertEqual(client.get("/console/session").status_code, 401)
            self.assertEqual(client.post("/auth/login", json={"username": "ragadmin", "password": "ragadmin123"}).status_code, 200)

    def test_api_identity_is_trusted_and_inputs_are_strict(self):
        for extra in ({"tenant_id": "beta"}, {"subject_id": "admin"}, {"groups": ["admin"]}):
            response = self.client.post("/query", headers=self.headers(), json={"question": "warranty", **extra})
            self.assertEqual(response.status_code, 422, response.text)
        for top_k in (True, 0, 21, "5"):
            response = self.client.post("/query", headers=self.headers(), json={"question": "warranty", "top_k": top_k})
            self.assertEqual(response.status_code, 422, response.text)
        response = self.client.post("/query", headers=self.headers(), json={"question": " "})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertNotIn("Traceback", response.text)

    def test_api_publish_query_evidence_and_tenant_boundary(self):
        job, snapshot = self.publish_document()
        response = self.client.post("/query", headers=self.headers(READER_TOKEN), json={"question": "amber warranty"})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["status"], "evidence_preview")
        self.assertIsNone(result["answer"])
        self.assertTrue(result["evidence"])
        evidence_id = result["evidence"][0]["evidence_id"]
        url = f"/evidence/{snapshot['generation']}/{evidence_id}"
        self.assertEqual(self.client.get(url, headers=self.headers(READER_TOKEN)).status_code, 200)
        self.assertIn(self.client.get(url, headers=self.headers(OTHER_TOKEN)).status_code, (403, 404))
        self.assertEqual(self.client.get(f"/jobs/{job['job_id']}", headers=self.headers(OTHER_TOKEN)).status_code, 404)
        self.assertEqual(self.client.get(f"/traces/{result['trace_id']}", headers=self.headers(READER_TOKEN)).status_code, 200)
        other = self.client.post("/query", headers=self.headers(OTHER_TOKEN), json={"question": "amber warranty"})
        self.assertEqual(other.status_code, 200, other.text)
        self.assertEqual(other.json()["evidence"], [])
        revoked = self.client.post("/access", headers=self.headers(), json={"document_id": "policy", "revoked": True})
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertIn(self.client.get(url, headers=self.headers(READER_TOKEN)).status_code, (403, 404))

    def invoke(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--data-dir", str(self.root / "cli-data"), *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_local_workflow_and_json_errors(self):
        source = self.root / "source.md"
        source.write_text("# Warranty\nThe amber warranty lasts twelve months.", encoding="utf-8")
        code, out, err = self.invoke("ingest", "--tenant", "cli", "--document", "warranty", "--version", "v1", "--file", str(source))
        self.assertEqual((code, err), (0, ""), err)
        job_id = json.loads(out)["job_id"]
        code, out, err = self.invoke("publish", "--tenant", "cli", "--job", job_id)
        self.assertEqual((code, err), (0, ""), err)
        code, out, err = self.invoke("query", "--tenant", "cli", "--question", "amber warranty")
        self.assertEqual((code, err), (0, ""), err)
        self.assertEqual(json.loads(out)["status"], "evidence_preview")
        code, out, err = self.invoke("query", "--tenant", "cli", "--question", "warranty", "--top-k", "invalid")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(json.loads(err)["error"]["code"], "validation_error")
        self.assertNotIn("Traceback", err)
        with patch("uvicorn.run") as run:
            code, out, err = self.invoke("serve")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["status"], "stopped")
        run.assert_called_once()

    def test_evaluation_denominators_and_saved_parameters(self):
        application = RagApplication(self.settings)
        staged = application.ingest(IngestRequest("alpha", "policy", "v1", "# Warranty\nThe amber warranty lasts twelve months."))
        generation = application.publish("alpha", staged.job_id, None).generation
        fixture = self.root / "eval.jsonl"
        fixture.write_text(
            json.dumps({"id": "positive", "question": "amber warranty", "gold_document_ids": ["policy"]}) + "\n" +
            json.dumps({"id": "unlabelled", "question": "xylophoneasteroid", "gold_document_ids": []}) + "\n",
            encoding="utf-8",
        )
        output = self.root / "report.json"
        report = asyncio.run(run_evaluation(application, fixture, AccessContext("alpha"), output_path=output))
        self.assertEqual(report["positive_case_count"], 1)
        self.assertEqual(report["unlabelled_case_count"], 1)
        self.assertEqual(report["metrics"]["hit_at_k"], 1.0)
        self.assertEqual(report["metrics"]["recall_at_k"], 1.0)
        self.assertEqual(report["retrieval_empty_rate"], 1.0)
        self.assertEqual(report["model_calls"], 0)
        self.assertEqual(report["parameters"]["generation"], generation)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["case_count"], 2)

    def test_evaluation_rejects_malformed_gold_and_has_correct_ranking_math(self):
        fixture = self.root / "bad.jsonl"
        for case in (
            {"question": "warranty", "gold_chunk_ids": [], "gold_document_ids": []},
            {"question": "warranty", "gold_document_ids": "policy"},
            {"question": "warranty", "gold_document_ids": ["policy", "policy"]},
            {"question": "warranty", "unexpected": 1},
        ):
            fixture.write_text(json.dumps(case), encoding="utf-8")
            with self.assertRaises(ValidationError):
                load_cases(fixture)
        scores = retrieval_metrics(["wrong", "right", "right"], ["right", "missing"], 3)
        self.assertEqual(scores["hit_at_k"], 1)
        self.assertEqual(scores["recall_at_k"], 0.5)
        self.assertEqual(scores["mrr_at_k"], 0.5)
        self.assertGreater(scores["ndcg_at_k"], 0)
        self.assertLess(scores["ndcg_at_k"], 1)

    def test_evaluation_uses_real_chunk_ids_and_requires_a_snapshot(self):
        application = RagApplication(self.settings)
        fixture = self.root / "chunks.jsonl"
        fixture.write_text(json.dumps({"question": "amber warranty", "gold_chunk_ids": []}), encoding="utf-8")
        with self.assertRaises(ValidationError):
            asyncio.run(run_evaluation(application, fixture, AccessContext("alpha")))
        staged = application.ingest(IngestRequest("alpha", "policy", "v1", "# Warranty\nThe amber warranty lasts twelve months."))
        application.publish("alpha", staged.job_id, None)
        result = asyncio.run(application.query(QueryRequest(AccessContext("alpha"), "amber warranty")))
        trace = application.get_trace("alpha", result.trace_id)
        chunk_id = trace["retrieved_chunk_ids"][0]
        self.assertNotEqual(chunk_id, result.evidence[0].parent_id)
        fixture.write_text(json.dumps({"question": "amber warranty", "gold_chunk_ids": [chunk_id]}), encoding="utf-8")
        report = asyncio.run(run_evaluation(application, fixture, AccessContext("alpha")))
        self.assertEqual(report["cases"][0]["gold_type"], "chunk")
        self.assertEqual(report["metrics"]["hit_at_k"], 1.0)


if __name__ == "__main__":
    unittest.main()
