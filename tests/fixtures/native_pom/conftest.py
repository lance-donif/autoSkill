import json
import os
from pathlib import Path

import pytest
from selenium import webdriver
from selenium.webdriver.chrome.service import Service


@pytest.fixture(scope="session")
def browser():
    options = webdriver.ChromeOptions()
    options.binary_location = os.environ["NATIVE_BROWSER"]
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    events = Path(os.environ["FIXTURE_EVENTS"])
    events.write_text("setup\n")
    driver = webdriver.Chrome(service=Service(os.environ["NATIVE_DRIVER"]), options=options)
    yield driver
    driver.quit()
    with events.open("a") as stream:
        stream.write("teardown\n")


@pytest.fixture
def data():
    return json.loads(Path("data/expected.json").read_text())
