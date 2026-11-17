"""The page shells stay usable when one of their script or stylesheet requests fails."""

from html.parser import HTMLParser
from pathlib import Path
import unittest

STATIC = Path(__file__).resolve().parents[1] / "adaptive_rag" / "static"
PAGES = ("index.html", "images.html", "knowledge.html", "evaluation.html")


class Tags(HTMLParser):
    """Start tags in document order as (tag, attributes)."""

    def __init__(self, text):
        super().__init__()
        self.tags = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def with_id(self, identifier):
        found = [attrs for _, attrs in self.tags if attrs.get("id") == identifier]
        assert len(found) == 1, (identifier, found)
        return found[0]


def tags(page="index.html"):
    return Tags((STATIC / page).read_text(encoding="utf-8"))


class PageShellTests(unittest.TestCase):
    def test_the_login_form_stays_hidden_until_the_session_is_known(self):
        page = tags()
        # A signed-in user must not see the login form flash up, nor be left on it when one request fails.
        self.assertIn("hidden", page.with_id("login-page"))
        self.assertIn("hidden", page.with_id("app-shell"))
        self.assertNotIn("hidden", page.with_id("boot-page"))

    def test_a_script_that_never_loads_leaves_a_way_to_reload(self):
        page = tags()
        # A plain link works without JavaScript; app.css reveals it after a delay, and app.js right away on failure.
        self.assertEqual(page.with_id("boot-reload").get("href"), "/")
        self.assertNotIn("hidden", page.with_id("boot-actions"))

    def test_the_login_form_never_falls_back_to_a_get_request(self):
        # Submitted natively as GET, the form would put the password into the address bar and the history.
        self.assertEqual(tags().with_id("login-form").get("method"), "post")

    def test_brand_marks_have_a_size_without_the_stylesheet(self):
        # An inline SVG that only has a viewBox grows to the width of the page when app.css is missing.
        for page in PAGES:
            marks = [attrs for tag, attrs in tags(page).tags if tag == "svg" and "brand-mark" in attrs.get("class", "")]
            self.assertTrue(marks, page)
            for attrs in marks:
                self.assertTrue(attrs.get("width") and attrs.get("height"), (page, attrs))


if __name__ == "__main__":
    unittest.main()
