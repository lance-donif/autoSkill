import os

from selenium.webdriver.common.by import By


class Home:
    def __init__(self, browser):
        self.browser = browser

    def open(self):
        self.browser.get(os.environ["FIXTURE_URL"] + "/page")

    def title(self):
        return self.browser.find_element(By.TAG_NAME, "h1").text
