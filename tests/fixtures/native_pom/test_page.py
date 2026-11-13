import os

import pytest
from pages.home import Home


@pytest.mark.parametrize("iteration", [1, 2])
def test_original_page_object(browser, data, iteration):
    page = Home(browser)
    page.open()
    assert iteration > 0
    assert page.title() == os.environ.get("EXPECTED_TITLE", data["title"])
