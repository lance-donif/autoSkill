"""The bundled example project: Ticket Desk, with a login and business cases ready to replay."""
from __future__ import annotations

from . import builder, projects, secrets

PROJECT_ID = "ticket-desk"
DEFAULT_URL = "http://127.0.0.1:8790"
AUTH = {"Authorization": "Bearer {{token}}"}


def _loc(**kw) -> list[dict]:
    return [kw]


def cases() -> list[dict]:
    testid = lambda value: _loc(by="testid", value=value)  # noqa: E731
    label = lambda value: _loc(by="label", value=value)  # noqa: E731
    button = lambda name: _loc(by="role", role="button", name=name)  # noqa: E731
    ui = [
        builder.ui_case("TC-UI-001", "管理员登录", {
            "start_url": "/",
            "actions": [
                {"action": "fill", "locators": testid("login-username") + label("账号"), "value": "qa_admin"},
                {"action": "fill", "locators": testid("login-password") + label("密码"), "value": "${ENV:TD_PASSWORD}"},
                {"action": "click", "locators": testid("login-submit") + button("登录")},
            ],
            "assertions": [{"assertion": "text", "locators": testid("current-user"), "expected": "测试管理员"}],
        }, module="登录", category="登录"),
        builder.ui_case("TC-UI-002", "新建高优先级工单", {
            "start_url": "/",
            "actions": [
                {"action": "click", "locators": testid("new-ticket") + button("新建工单")},
                {"action": "fill", "locators": testid("ticket-title") + label("标题"), "value": "回放创建的工单"},
                {"action": "select", "locators": testid("ticket-priority") + label("优先级"), "value": "high"},
                {"action": "click", "locators": testid("ticket-save") + button("保存")},
            ],
            "assertions": [
                {"assertion": "contains", "locators": testid("toast"), "expected": "已创建"},
                {"assertion": "contains", "locators": testid("ticket-rows"), "expected": "回放创建的工单"},
            ],
        }, module="工单", depends_on=["TC-UI-001"]),
        builder.ui_case("TC-UI-003", "标题为空时不能保存", {
            "start_url": "/",
            "actions": [{"action": "click", "locators": testid("new-ticket")}, {"action": "click", "locators": testid("ticket-save") + button("保存")}],
            "assertions": [{"assertion": "text", "locators": testid("title-error"), "expected": "请填写标题"}],
        }, module="工单", depends_on=["TC-UI-001"], risk="read"),
        builder.ui_case("TC-UI-004", "按状态筛选已解决工单", {
            "start_url": "/",
            "actions": [{"action": "select", "locators": testid("status-filter") + label("状态"), "value": "resolved"}],
            "assertions": [
                {"assertion": "contains", "locators": testid("ticket-rows"), "expected": "帮助文档链接失效"},
                {"assertion": "count", "locators": _loc(by="css", value="[data-testid=ticket-row] .badge.open"), "expected": 0},
            ],
        }, module="工单", depends_on=["TC-UI-001"], risk="read"),
        builder.ui_case("TC-UI-005", "未登录时显示登录表单", {
            "start_url": "/", "actions": [],
            "assertions": [{"assertion": "visible", "locators": testid("login-submit")}],
        }, module="登录", risk="read"),
    ]
    api = [
        builder.api_case("TC-API-001", "登录接口", "POST", "/api/login",
                         {"status": 200, "json_equals": {"/user/role": "admin"}}, module="登录", category="登录",
                         json={"username": "qa_admin", "password": "${ENV:TD_PASSWORD}"}, extract={"token": "/token"}),
        builder.api_case("TC-API-002", "查询当前用户", "GET", "/api/me",
                         {"status": 200, "json_equals": {"/username": "qa_admin"}}, module="登录",
                         headers=AUTH, depends_on=["TC-API-001"]),
        builder.api_case("TC-API-003", "新建工单接口", "POST", "/api/tickets",
                         {"status": 201, "json_equals": {"/status": "open", "/priority": "high"}}, module="工单",
                         headers=AUTH, json={"title": "接口回放工单", "priority": "high"},
                         extract={"ticket_id": "/id"}, depends_on=["TC-API-001"]),
        builder.api_case("TC-API-004", "工单详情接口", "GET", "/api/tickets/{{ticket_id}}",
                         {"status": 200, "json_equals": {"/title": "接口回放工单"}}, module="工单",
                         headers=AUTH, depends_on=["TC-API-003"]),
        builder.api_case("TC-API-005", "标题为空被拒绝", "POST", "/api/tickets",
                         {"status": 422, "json_equals": {"/code": "VALIDATION"}}, module="工单",
                         headers=AUTH, json={"title": ""}, depends_on=["TC-API-001"]),
        builder.api_case("TC-API-006", "未登录访问被拒绝", "GET", "/api/tickets",
                         {"status": 401, "json_equals": {"/code": "UNAUTHENTICATED"}}, module="登录"),
    ]
    return ui + api


def install(base_url: str = DEFAULT_URL, project_id: str = PROJECT_ID) -> dict:
    """Create the example project (once) and store the demo account's password locally."""
    existing = [p for p in projects.list_all() if p["id"] == project_id]
    if existing:
        return existing[0]
    projects.create("示例工单系统", base_url, category="示例", project_id=project_id)
    secrets.set_secret("TD_PASSWORD", "Passw0rd!demo")  # the published demo-site account, not a real credential
    projects.save_cases(project_id, cases())
    projects.set_auth(project_id, {"ui_case": "TC-UI-001", "api_case": "TC-API-001", "api_check_case": "TC-API-002"})
    return projects.get(project_id)
