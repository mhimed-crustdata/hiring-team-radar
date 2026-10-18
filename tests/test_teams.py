import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import teams  # noqa: E402
from crustdata_api import API_VERSION, Crustdata  # noqa: E402


def job(jid, cid, name, title, posted="2026-10-14", reposted=False, country="United States"):
    return {"crustdata_job_id": jid,
            "job_details": {"title": title, "url": f"https://jobs.example/{jid}", "reposted_job": reposted},
            "company": {"basic_info": {"crustdata_company_id": cid, "name": name, "primary_domain": f"{name.lower()}.com"}},
            "location": {"country": country},
            "metadata": {"date_added": posted + "T10:00:00"}}


JOBS = [
    job(1, 10, "Acme", "Machine Learning Engineer"),
    job(2, 10, "Acme", "Senior ML Engineer", posted="2026-10-16"),
    job(3, 10, "Acme", "Backend Engineer"),
    job(4, 10, "Acme", "Solutions Engineer"),
    job(5, 10, "Acme", "Machine Learning Engineer", reposted=True),
    job(6, 20, "Beta", "Security Engineer"),
    job(7, 20, "Beta", "Application Security Engineer"),
    job(1, 10, "Acme", "Machine Learning Engineer"),  # duplicate id
    job(8, None, "Ghost", "Data Engineer"),
]

COMPANIES = [
    {"crustdata_company_id": 10, "headcount": {"total": 1200, "growth_percent": {"qoq": 9.84, "yoy": 30.0}}},
    {"crustdata_company_id": 20, "headcount": {"total": 300, "growth_percent": {"qoq": 1.2}}},
]


class ClassifyTests(unittest.TestCase):
    def test_team_buckets(self):
        self.assertEqual(teams.classify_team("Staff Machine Learning Engineer"), "AI & ML")
        self.assertEqual(teams.classify_team("Data Engineer"), "Data")
        self.assertEqual(teams.classify_team("Security Engineer"), "Security")
        self.assertEqual(teams.classify_team("Site Reliability Engineer"), "Infrastructure & Platform")
        self.assertEqual(teams.classify_team("Android Engineer"), "Frontend & Mobile")
        self.assertEqual(teams.classify_team("Partner Solutions Engineer"), "Sales & Solutions Engineering")
        self.assertEqual(teams.classify_team("Software Engineer"), "Software Engineering")


class GroupTests(unittest.TestCase):
    def test_groups_and_drops(self):
        out = teams.group_teams(JOBS, [teams.DEFAULT_EXCLUDE_TEAMS])
        keys = [(t["company"], t["team"], t["posts"]) for t in out["teams"]]
        self.assertIn(("Acme", "AI & ML", 2), keys)
        self.assertIn(("Acme", "Software Engineering", 1), keys)
        self.assertIn(("Beta", "Security", 2), keys)
        self.assertEqual(len(keys), 3)
        self.assertEqual(out["dropped"], {"excluded_team": 1, "reposts": 1, "duplicates": 1, "no_company": 1})
        ml = [t for t in out["teams"] if t["team"] == "AI & ML"][0]
        self.assertEqual(ml["latest_post"], "2026-10-16")

    def test_keep_all_teams(self):
        out = teams.group_teams(JOBS, [])
        self.assertIn("Sales & Solutions Engineering", [t["team"] for t in out["teams"]])


class FlagTests(unittest.TestCase):
    def setUp(self):
        self.result = teams.group_teams(JOBS, [teams.DEFAULT_EXCLUDE_TEAMS])
        teams.add_headcount(self.result["teams"], COMPANIES, "qoq")

    def test_flags_fast_growth_with_enough_posts(self):
        teams.flag_teams(self.result["teams"], min_growth=5, min_posts=2)
        flagged = [(t["company"], t["team"]) for t in self.result["teams"] if t["flagged"]]
        self.assertEqual(flagged, [("Acme", "AI & ML")])
        self.assertTrue(self.result["teams"][0]["flagged"])  # flagged sort first

    def test_slow_growth_is_not_flagged(self):
        teams.flag_teams(self.result["teams"], min_growth=5, min_posts=1)
        beta = [t for t in self.result["teams"] if t["company"] == "Beta"][0]
        self.assertFalse(beta["flagged"])
        self.assertEqual(beta["growth_pct"], 1.2)

    def test_missing_growth_is_not_flagged(self):
        teams.add_headcount(self.result["teams"], [], "qoq")
        teams.flag_teams(self.result["teams"], min_growth=0, min_posts=1)
        self.assertFalse(any(t["flagged"] for t in self.result["teams"]))

    def test_window_selects_growth_key(self):
        teams.add_headcount(self.result["teams"], COMPANIES, "yoy")
        acme = [t for t in self.result["teams"] if t["company"] == "Acme"][0]
        self.assertEqual(acme["growth_pct"], 30.0)


class TextTests(unittest.TestCase):
    def test_one_liner_and_slack(self):
        result = teams.group_teams(JOBS, [teams.DEFAULT_EXCLUDE_TEAMS])
        result["posts"] = len(JOBS)
        teams.add_headcount(result["teams"], COMPANIES, "qoq")
        teams.flag_teams(result["teams"], 5, 2)
        line = teams.one_liner(result["teams"][0], "qoq")
        self.assertEqual(line, "Acme AI & ML: 2 new posts, company headcount +9.8% (qoq), 1,200 people.")
        text = teams.slack_text(result, "2026-10-11", "qoq")
        self.assertIn("1 teams to recruit from first", text)
        self.assertIn(line, text)
        self.assertEqual(teams.summary(result)["flagged_by_team"], {"AI & ML": 1})

    def test_slack_when_nothing_flagged(self):
        text = teams.slack_text({"teams": [], "posts": 0, "dropped": {}}, "2026-10-11", "qoq")
        self.assertIn("No team met", text)


class StateTests(unittest.TestCase):
    def test_only_new_jobs(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s", "seen.json")
            self.assertEqual(len(teams.only_new_jobs(JOBS, path)), len(JOBS))
            result = teams.group_teams(JOBS, [])
            teams.save_state(result["teams"], path)
            left = teams.only_new_jobs(JOBS, path)
            self.assertEqual([j["crustdata_job_id"] for j in left], [5, 8])  # reposts and no-company were never alerted


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append((url, body, headers))
        return self.responses[url.replace("https://api.crustdata.com", "")]


class ApiTests(unittest.TestCase):
    def test_job_search_request(self):
        tr = FakeTransport({"/job/search": {"job_listings": JOBS[:2]}})
        client = Crustdata(api_key="k", transport=tr)
        f = teams.job_filters(["acme.com"], ["engineer"], "2026-10-11")
        rows = list(client.job_search(f, teams.JOB_FIELDS, max_results=50))
        self.assertEqual(len(rows), 2)
        url, body, headers = tr.calls[0]
        self.assertEqual(url, "https://api.crustdata.com/job/search")
        self.assertEqual(headers["x-api-version"], API_VERSION)
        self.assertEqual(body["filters"]["conditions"][0], {"field": "company.basic_info.primary_domain", "type": "in", "value": ["acme.com"]})
        self.assertEqual(body["filters"]["conditions"][1]["conditions"][0]["type"], "[.]")
        self.assertEqual(body["filters"]["conditions"][2]["field"], "metadata.date_added")

    def test_company_enrich_request(self):
        resp = [{"matches": [{"company_data": COMPANIES[0]}]}, {"matches": [{"company_data": COMPANIES[1]}]}]
        tr = FakeTransport({"/company/enrich": resp})
        client = Crustdata(api_key="k", transport=tr)
        out = client.company_enrich([10, 20], teams.COMPANY_FIELDS)
        self.assertEqual([c["crustdata_company_id"] for c in out], [10, 20])
        url, body, headers = tr.calls[0]
        self.assertEqual(url, "https://api.crustdata.com/company/enrich")
        self.assertEqual(body, {"crustdata_company_ids": [10, 20], "fields": ["basic_info", "headcount"]})
        self.assertEqual(headers["x-api-version"], API_VERSION)

    def test_create_watch_request(self):
        tr = FakeTransport({"/watch/job/search": {"id": 1}})
        client = Crustdata(api_key="k", transport=tr)
        f = teams.job_filters(["acme.com"], ["engineer"], "2026-10-11")
        client.create_job_watch(f)
        url, body, _ = tr.calls[0]
        self.assertEqual(url, "https://api.crustdata.com/watch/job/search")
        self.assertEqual(body["config"]["trigger"], {"type": "interval", "every_hours": 168})
        self.assertEqual(body["filters"], f)
        json.dumps(body)


class OutputTests(unittest.TestCase):
    def test_write_outputs(self):
        result = teams.group_teams(JOBS, [])
        result["posts"] = len(JOBS)
        teams.add_headcount(result["teams"], COMPANIES, "qoq")
        teams.flag_teams(result["teams"], 5, 2)
        with tempfile.TemporaryDirectory() as d:
            teams.write_outputs(result, d, "qoq")
            self.assertTrue(os.path.exists(os.path.join(d, "teams.csv")))
            with open(os.path.join(d, "teams.json")) as fh:
                self.assertEqual(json.load(fh)["summary"]["flagged_teams"], 1)


if __name__ == "__main__":
    unittest.main()
