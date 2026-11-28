"""Observe a real external mutation and the dashboard's ensuing revision and shell reads."""
import uuid
from urllib.parse import urlsplit

from playwright.sync_api import expect


def change_project_and_wait_for_ui(test, project_id):
    before = test.client.get('/api/state-revision').json()['revision']
    after_revision = None
    observed = False

    def revision_changed(response):
        nonlocal observed
        if after_revision is None or response.status != 200 or urlsplit(response.url).path != '/api/state-revision':
            return False
        if response.json()['revision'] != after_revision:
            return False
        observed = True
        return True

    def shell_refreshed(response):
        return observed and urlsplit(response.url).path == '/api/status'

    category = 'External revision ' + uuid.uuid4().hex
    with test.page.expect_response(shell_refreshed), test.page.expect_response(revision_changed):
        response = test.client.patch('/api/projects/' + project_id, json={'category': category})
        test.assertEqual(response.status_code, 200, response.text)
        after_revision = test.client.get('/api/state-revision').json()['revision']
        test.assertNotEqual(after_revision, before)
    expect(test.page.locator('#view')).not_to_have_attribute('aria-busy', 'true')
    return category
