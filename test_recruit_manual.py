"""Offline tests: no browser, external API or applicant submissions."""
import tempfile
import io
import json
import unittest
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import recruit_manual as flow


class CacheTests(unittest.TestCase):
    def test_manual_and_parsed_applicant_snapshot_routes(self):
        self.assertTrue(flow.is_applicant_snapshot("https://tenant/ApplicantProfiles/snapshot/abc"))
        self.assertTrue(flow.is_applicant_snapshot("https://tenant/applicant_profiles/snapshot/abc"))
        self.assertFalse(flow.is_applicant_snapshot("https://tenant/applicant_profiles/add_applicant"))

    def test_bad_store_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text("[]")
            with self.assertRaises(ValueError):
                flow.read_store(path)


class LocalAITests(unittest.TestCase):
    def test_auto_selects_available_key_or_local(self):
        from url import extraction_settings
        for key, expected in (("", "ollama"), ("your_key_here", "ollama"), ("test-key", "openai")):
            with patch.dict("os.environ", {"AI_PROVIDER": "auto", "OPENAI_API_KEY": key}, clear=True):
                self.assertEqual(extraction_settings()[0], expected)

    def test_openai_failure_falls_back_to_local_in_auto_mode(self):
        import sys
        from url import build_skill_set
        extracted = {"first_name": "Test", "last_name": "Person", "city": None,
                     "state": None, "skills": []}
        response = {"message": {"content": json.dumps(extracted)}}
        failing_client = SimpleNamespace(OpenAI=Mock(side_effect=RuntimeError("service unavailable")))
        with patch.dict("os.environ", {"AI_PROVIDER": "auto", "OPENAI_API_KEY": "test-key"}, clear=True), \
                patch.dict(sys.modules, {"openai": failing_client}), \
                patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())):
            self.assertEqual(build_skill_set({"public_sections": ["Test Person"]}), extracted)

    def test_local_provider_requires_no_key_and_validates_evidence(self):
        from url import build_skill_set
        extracted = {
            "first_name": "Test", "last_name": "Person", "city": None, "state": None,
            "skills": [{"skill": "Example engineering", "basis": "explicit",
                        "source": "summary", "evidence": "Example engineering"}],
        }
        response = {"message": {"content": json.dumps(extracted)}}
        with patch.dict("os.environ", {"AI_PROVIDER": "ollama"}, clear=True), \
                patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as call:
            result = build_skill_set({"public_sections": ["Test Person: Example engineering"]})
            self.assertEqual(result, extracted)
            self.assertEqual(call.call_args.args[0].full_url, "http://127.0.0.1:11434/api/chat")

    def test_local_hallucinated_evidence_rejected(self):
        from url import build_skill_set
        extracted = {
            "first_name": "Test", "last_name": "Person", "city": None, "state": None,
            "skills": [{"skill": "Imagined skill", "basis": "explicit",
                        "source": "summary", "evidence": "Not present in input"}],
        }
        response = {"message": {"content": json.dumps(extracted)}}
        with patch.dict("os.environ", {"AI_PROVIDER": "ollama"}, clear=True), \
                patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())):
            with self.assertRaises(ValueError):
                build_skill_set({"public_sections": ["Test Person"]})


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_collector_finishes_while_consumer_waits(self):
        queue = asyncio.Queue()
        jobs = {}
        api = SimpleNamespace(discover_jobs=AsyncMock(return_value=["one", "two", "three"]),
                              job_code_for=lambda url: url, job_title=AsyncMock(return_value="Title"),
                              job_location=AsyncMock(return_value="City"), save_state=Mock())
        args = SimpleNamespace(job_url=None, jobs_limit=25, search_only=False)
        blocked = asyncio.Event()
        async def slow_consumer():
            await queue.get()
            await blocked.wait()
        consumer = asyncio.create_task(slow_consumer())
        try:
            await asyncio.wait_for(flow.produce_jobs(api, args, None, jobs, {},
                                                    Path("unused"), queue, []), timeout=2)
            self.assertEqual(len(jobs), 3)
            self.assertFalse(consumer.done())
        finally:
            blocked.set()
            await consumer

    async def test_collector_continues_after_one_job_error(self):
        jobs, failures = {}, []
        api = SimpleNamespace(discover_jobs=AsyncMock(return_value=["bad", "good"]),
                              job_code_for=lambda url: url,
                              job_title=AsyncMock(side_effect=[ValueError("bad title"), "Title"]),
                              job_location=AsyncMock(return_value="City"), save_state=Mock())
        args = SimpleNamespace(job_url=None, jobs_limit=25, search_only=False)
        await flow.produce_jobs(api, args, None, jobs, {}, Path("unused"), asyncio.Queue(), failures)
        self.assertEqual(list(jobs), ["good"])
        self.assertEqual(failures, ["bad"])

    async def test_stored_job_is_reused_without_re_extraction(self):
        api = SimpleNamespace(discover_jobs=AsyncMock(return_value=["j1"]),
                              job_code_for=lambda url: url,
                              job_title=AsyncMock(), job_location=AsyncMock(), save_state=Mock())
        jobs = {"j1": {"url": "j1", "code": "j1", "title": "Stored Title",
                       "location": "Stored City", "extracted_at": 1}}
        queue = asyncio.Queue()
        args = SimpleNamespace(job_url=None, jobs_limit=25, search_only=False)
        await flow.produce_jobs(api, args, None, jobs, {}, Path("unused"), queue, [])
        api.job_title.assert_not_called()
        api.job_location.assert_not_called()
        self.assertEqual((await queue.get())["title"], "Stored Title")

    def setUp(self):
        self.url = "https://www.linkedin.com/talent/profile/test-person"
        self.args = SimpleNamespace(limit=20, search_only=False, location=None)
        self.api = SimpleNamespace(save_state=Mock(), ROOT=Path("."))
        self.job = {"code": "J1", "url": "https://example.test/job/1"}
        self.history = {"J1": {"status": "pending", "profiles": [self.url]}}
        self.profiles = {self.url: {
            "url": self.url, "job_codes": ["J1"], "status": "extracted",
            "raw": {"public_sections": ["Test Person"]},
            "profile": {"first_name": "Test", "last_name": "Person", "skills": []},
            "resume_path": str(Path(__file__).resolve()),
        }}
        self.persist = Mock()

    async def run_job(self):
        await flow.process_job(self.api, self.args, None, None, None,
                               self.job, self.profiles, self.history,
                               Path("unused_history.json"), self.persist)

    async def test_cached_profile_is_not_scraped_or_sent_to_ai_again(self):
        async def saved(page, row, api, persist):
            row["status"] = "saved"
        with patch.object(flow, "extract_profile", AsyncMock()) as extract, \
                patch.object(flow, "create_applicant", AsyncMock(side_effect=saved)), \
                patch("url.build_skill_set") as ai:
            await self.run_job()
            extract.assert_not_called()
            ai.assert_not_called()
        self.assertEqual(self.history["J1"]["status"], "completed")

    async def test_uncertain_save_is_not_retried(self):
        self.profiles[self.url]["status"] = "review_required"
        with patch.object(flow, "create_applicant", AsyncMock()) as create:
            with self.assertRaises(RuntimeError):
                await self.run_job()
            create.assert_not_called()
        self.assertEqual(self.history["J1"]["status"], "pending")

    async def test_error_after_save_stays_uncertain(self):
        async def timeout(page, row, api, persist):
            row["status"] = "saving"
            raise RuntimeError("save timed out")
        with patch.object(flow, "create_applicant", AsyncMock(side_effect=timeout)):
            with self.assertRaises(RuntimeError):
                await self.run_job()
        self.assertEqual(self.profiles[self.url]["status"], "review_required")

    async def test_missing_resume_file_is_rebuilt_from_the_stored_profile(self):
        row = self.profiles[self.url]
        row.pop("resume_path")

        async def saved(page, row, api, persist):
            row["status"] = "saved"

        page = SimpleNamespace(context=object())
        with patch.object(flow, "extract_profile", AsyncMock()) as extract, \
                patch("url.build_skill_set") as ai, \
                patch.object(flow, "write_resume_pdf", AsyncMock()) as pdf, \
                patch.object(flow, "create_applicant", AsyncMock(side_effect=saved)):
            await flow.process_job(self.api, self.args, None, page, None,
                                   self.job, self.profiles, self.history,
                                   Path("unused_history.json"), self.persist)
            extract.assert_not_called()
            ai.assert_not_called()
            pdf.assert_awaited_once()
        self.assertTrue(self.profiles[self.url]["resume_path"].endswith(".pdf"))

    async def test_confirmed_applicant_is_skipped_across_jobs(self):
        self.profiles[self.url]["status"] = "saved"
        self.profiles[self.url]["job_codes"] = ["J0"]
        with patch.object(flow, "create_applicant", AsyncMock()) as create:
            await self.run_job()
            create.assert_not_called()
        self.assertEqual(self.profiles[self.url]["job_codes"], ["J0", "J1"])


class PublicProfileTests(unittest.TestCase):
    def test_owner_public_url_wins_over_other_people(self):
        text = ("John Callum McDaniel\nVirginia Tech \u00b7 Richmond, Virginia, United States\n"
                "People also viewed\nhttps://www.linkedin.com/in/someone-else-1234\n"
                "Personal Information\nJohn Callum's profile\n"
                "https://www.linkedin.com/in/john-callum-mcdaniel-0aa02b1b7")
        self.assertEqual(flow.find_public_profile_url(text, "John Callum McDaniel"),
                         "https://www.linkedin.com/in/john-callum-mcdaniel-0aa02b1b7")

    def test_section_urls_are_not_profile_urls(self):
        text = "Jane Doe\nhttps://www.linkedin.com/in/jane-doe/details/experience"
        self.assertEqual(flow.find_public_profile_url(text, "Jane Doe"), "")

    def test_missing_name_still_yields_a_candidate(self):
        text = "https://www.linkedin.com/in/solo-1234"
        self.assertEqual(flow.find_public_profile_url(text, ""),
                         "https://www.linkedin.com/in/solo-1234")


class ResumeTests(unittest.TestCase):
    def test_resume_carries_the_required_applicant_details(self):
        profile = {"first_name": "John", "last_name": "McDaniel", "city": "Richmond",
                   "state": "Virginia", "job_title": "Security Analyst",
                   "skills": [{"skill": "Risk assessment"}],
                   "certifications": ["EXAM-123\nIssuer"]}
        document = flow.render_resume_html(
            profile, "https://www.linkedin.com/talent/profile/abc",
            "https://www.linkedin.com/in/john-mcdaniel")
        for expected in ("John", "McDaniel", "Richmond, Virginia",
                         "https://www.linkedin.com/in/john-mcdaniel",
                         "Risk assessment", "EXAM-123"):
            self.assertIn(expected, document)

    def test_public_url_is_preferred_over_the_talent_url(self):
        document = flow.render_resume_html(
            {"first_name": "A", "last_name": "B"},
            "https://www.linkedin.com/talent/profile/abc",
            "https://www.linkedin.com/in/a-b")
        self.assertIn("https://www.linkedin.com/in/a-b", document)
        self.assertNotIn("https://www.linkedin.com/talent/profile/abc", document)

    def test_resume_filename_is_safe_and_unique_per_profile(self):
        profile = {"first_name": "John", "last_name": "Mc Daniel"}
        self.assertEqual(
            flow.resume_filename(profile, "https://www.linkedin.com/talent/profile/abc"),
            "John-Mc-Daniel-abc.pdf")


class TalentOnlyTests(unittest.TestCase):
    """Public /in/ profile pages must never be visited."""

    def test_only_talent_profile_urls_are_accepted(self):
        import recruit as api
        self.assertEqual(
            api.canonical_url("https://www.linkedin.com/talent/profile/abc?trk=x"),
            "https://www.linkedin.com/talent/profile/abc")
        self.assertIsNone(api.canonical_url("https://www.linkedin.com/in/someone"))
        self.assertIsNone(api.canonical_url("https://www.linkedin.com/in/someone/details/experience"))


if __name__ == "__main__":
    unittest.main()
