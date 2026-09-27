"""Unit tests for the Internshala connector and multi-source dispatcher."""

import unittest
from unittest.mock import MagicMock, patch

from job_scraper.sources import (
    Listing,
    fetch_detail_for_listing,
    fetch_from_sources,
)
from job_scraper.sources.internshala import (
    _build_search_urls,
    _parse_listings,
    _slugify,
    fetch,
    fetch_detail,
)


class TestInternshalaHelpers(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(_slugify("Full Stack Developer"), "full-stack-developer")
        self.assertEqual(_slugify("  Bangalore, India "), "bangalore-india")
        self.assertEqual(_slugify("AI/ML"), "ai-ml")

    def test_build_search_urls_city(self):
        urls = _build_search_urls("full stack developer", "bangalore")
        self.assertIn("https://internshala.com/jobs/full-stack-developer-jobs-in-bangalore/", urls)

    def test_build_search_urls_remote(self):
        urls = _build_search_urls("python developer", "remote")
        self.assertIn("https://internshala.com/jobs/work-from-home-python-developer-jobs/", urls)


class TestInternshalaParsing(unittest.TestCase):
    MOCK_HTML = """
    <div class="container-fluid individual_internship" id="individual_internship_998877" data-href="/job/detail/test-job-998877">
        <h2 class="job-internship-name">
            <a class="job-title-href" href="/job/detail/test-job-998877">Full Stack Engineer</a>
        </h2>
        <div class="company_and_premium">
            <p class="company-name">Acme Tech Solutions</p>
        </div>
        <p class="row-1-item locations">
            <a>Bangalore</a>
        </p>
    </div>
    """

    MOCK_DETAIL_HTML = """
    <html>
        <title>Full Stack Engineer at Acme Tech Solutions</title>
        <div class="text-container">
            We are looking for a skilled React and Python developer to join Acme Tech.
        </div>
    </html>
    """

    def test_parse_listings(self):
        listings = _parse_listings(self.MOCK_HTML, limit=5)
        self.assertEqual(len(listings), 1)
        item = listings[0]
        self.assertEqual(item["id"], "998877")
        self.assertEqual(item["title"], "Full Stack Engineer")
        self.assertEqual(item["company"], "Acme Tech Solutions")
        self.assertEqual(item["url"], "https://internshala.com/job/detail/test-job-998877")
        self.assertEqual(item["source"], "internshala")

    @patch("job_scraper.sources.internshala._fetch_html")
    def test_fetch_detail(self, mock_fetch):
        mock_fetch.return_value = self.MOCK_DETAIL_HTML
        res = fetch_detail("https://internshala.com/job/detail/test-job-998877")
        self.assertIsNotNone(res)
        self.assertIn("React and Python developer", res["description"])

    @patch("job_scraper.sources.internshala._fetch_html")
    def test_fetch(self, mock_fetch):
        mock_fetch.return_value = self.MOCK_HTML
        items = fetch({"role": "developer", "location": "bangalore"}, limit=2)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "internshala")


class TestMultiSourceDispatcher(unittest.TestCase):
    @patch("job_scraper.sources.internshala.fetch")
    @patch("job_scraper.sources.linkedin.fetch")
    def test_fetch_from_sources_all(self, mock_li, mock_is):
        mock_li.return_value = [
            Listing(id="li1", title="Dev LI", company="A", url="http://li", description=None, posted_date=None, source="linkedin")
        ]
        mock_is.return_value = [
            Listing(id="is1", title="Dev IS", company="B", url="http://is", description=None, posted_date=None, source="internshala")
        ]

        res = fetch_from_sources("all", {"role": "developer", "location": "Bangalore"})
        self.assertEqual(len(res), 2)
        sources = {r["source"] for r in res}
        self.assertEqual(sources, {"linkedin", "internshala"})

    @patch("job_scraper.sources.internshala.fetch")
    @patch("job_scraper.sources.linkedin.fetch")
    def test_fetch_from_sources_single(self, mock_li, mock_is):
        mock_li.return_value = [
            Listing(id="li1", title="Dev LI", company="A", url="http://li", description=None, posted_date=None, source="linkedin")
        ]
        mock_is.return_value = [
            Listing(id="is1", title="Dev IS", company="B", url="http://is", description=None, posted_date=None, source="internshala")
        ]

        res_is = fetch_from_sources("internshala", {"role": "developer", "location": "Bangalore"})
        self.assertEqual(len(res_is), 1)
        self.assertEqual(res_is[0]["source"], "internshala")

    @patch("job_scraper.sources.internshala.fetch_detail")
    @patch("job_scraper.sources.linkedin.fetch_detail")
    def test_fetch_detail_for_listing(self, mock_li, mock_is):
        mock_li.return_value = {"description": "LI details"}
        mock_is.return_value = {"description": "IS details"}

        li_res = fetch_detail_for_listing({"id": "1", "source": "linkedin", "title": "t", "url": "u", "company": "c", "description": None, "posted_date": None})
        self.assertEqual(li_res["description"], "LI details")

        is_res = fetch_detail_for_listing({"id": "2", "source": "internshala", "title": "t", "url": "https://internshala.com/job/2", "company": "c", "description": None, "posted_date": None})
        self.assertEqual(is_res["description"], "IS details")


if __name__ == "__main__":
    unittest.main()
