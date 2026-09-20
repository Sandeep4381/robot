import unittest
from profile_rules import build_profile


class RuleTests(unittest.TestCase):
    def test_skills_and_header_location_without_job_address_leak(self):
        result = build_profile({"public_sections": [
            "John Callum McDaniel\nSchool · Richmond, Virginia, United States · Industry\n"
            "Experience\nPosition title\nSecurity Analyst\nPosition location\nBoston, Massachusetts, United States\n"
            "Position summary\nWork across governance, risk, and compliance.\n"
            "Skills (3)\nUnusual Tool\nTeamwork\nShow all 3 skills\nInterests\nUnrelated Person"]})
        self.assertEqual(result["first_name"], "John Callum")
        self.assertEqual(result["last_name"], "McDaniel")
        self.assertEqual(result["city"], "Richmond")
        self.assertEqual(result["job_title"], "Security Analyst")
        self.assertEqual([s["skill"] for s in result["skills"]],
                         ["Unusual Tool", "Teamwork", "governance", "risk", "compliance"])

    def test_school_segment_is_not_mistaken_for_the_location(self):
        result = build_profile({"public_sections": [
            "Rodney Daniels\nSenior Security Architect\n"
            "University of Maryland, Global Campus (Cum Laude, 2009) \u00b7 "
            "Richmond, Virginia, United States \u00b7 Computer and Network Security \u00b7 319\n"
            "319 connections\nExperience\nPosition title\nSecurity Engineer"]})
        self.assertEqual(result["city"], "Richmond")
        self.assertEqual(result["state"], "Virginia")

    def test_does_not_invent_skills_from_certification(self):
        result = build_profile({"public_sections": ["SingleName\nCertifications\nEXAM-123\nIssuer"]})
        self.assertIsNone(result["last_name"])
        self.assertEqual(result["skills"], [])
        self.assertEqual(result["certifications"], ["EXAM-123\nIssuer"])


if __name__ == "__main__":
    unittest.main()
