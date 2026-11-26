"""The step model every engine replays.

A UI case stores `extensions.automation = {start_url, actions, assertions}`.
Each step carries a list of locator candidates, best first; the runner uses the
first one that matches and reports when it had to fall back. The older single
`selector` / `placeholder` / `role`+`name` fields are still accepted.
"""
from __future__ import annotations

import re
from contextvars import ContextVar
from math import isfinite
from urllib.parse import urljoin, urlsplit

LOCATOR_KINDS = ("testid", "role", "label", "placeholder", "text", "css", "xpath")
# action -> (needs a target element, required fields)
ACTIONS = {
    "goto": (False, ("url",)),
    "reload": (False, ()),
    "tab_new": (False, ("tab", "url")),
    "tab_switch": (False, ("tab",)),
    "tab_close": (False, ("tab", "active")),
    "tab_sync": (False, ("tabs",)),
    "fill": (True, ("value",)),
    "type": (True, ("value",)),
    "click": (True, ()),
    "dblclick": (True, ()),
    "hover": (True, ()),
    "press": (False, ("key",)),
    "check": (True, ()),
    "uncheck": (True, ()),
    "select": (True, ("value",)),
    "wait_visible": (True, ()),
    "wait_hidden": (True, ()),
    "captcha": (True, ("image",)),
}
# assertion -> (needs a target element, required fields)
ASSERTIONS = {
    "visible": (True, ()),
    "hidden": (True, ()),
    "count": (True, ("expected",)),
    "min_count": (True, ("expected",)),
    "text": (True, ("expected",)),
    "contains": (True, ("expected",)),
    "value": (True, ("expected",)),
    "checked": (True, ()),
    "attribute": (True, ("attribute", "expected")),
    "url": (False, ("expected",)),
    "url_contains": (False, ("expected",)),
    "title": (False, ("expected",)),
}
ACTION_NAMES = {
    "goto": "打开", "reload": "刷新页面", "fill": "输入", "type": "逐字输入", "click": "点击",
    "dblclick": "双击", "hover": "悬停", "press": "按键", "check": "勾选", "uncheck": "取消勾选",
    "select": "选择", "wait_visible": "等待出现", "wait_hidden": "等待消失",
    "tab_new": "新建标签页", "tab_switch": "切换标签页", "tab_close": "关闭标签页", "tab_sync": "跟随新窗口或关闭返回",
    "captcha": "识别验证码并填入",
}
ASSERTION_NAMES = {
    "visible": "可见", "hidden": "不可见", "count": "数量为", "min_count": "数量至少为", "text": "文本为", "contains": "文本包含",
    "value": "输入值为", "checked": "勾选状态为", "attribute": "属性", "url": "地址为",
    "url_contains": "地址包含", "title": "页面标题为",
}
LOCATOR_LABELS = {"testid": "测试标识", "label": "标签", "placeholder": "占位提示", "text": "文字", "css": "选择器", "xpath": "XPath"}


RAND = "{{RAND}}"
VARIABLE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
_rand = ContextVar("autoskill_rand", default="")
_variables: ContextVar[dict | None] = ContextVar("autoskill_variables", default=None)


def set_rand(value: str) -> None:
    _rand.set(value)


def set_variables(values: dict | None) -> None:
    """Values a managed run may substitute into page steps: inputs, extracted values, environment."""
    _variables.set(values or None)


def expand(value):
    """Fill {{RAND}} and {{name}}: a short text that is new on every run, so data a case creates never
    collides with an earlier run, plus the run's environment and input values."""
    token, variables = _rand.get(), _variables.get() or {}
    if isinstance(value, dict):
        return {key: expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand(item) for item in value]
    if not isinstance(value, str):
        return value
    if token:
        value = value.replace(RAND, token)
    if variables:
        value = VARIABLE.sub(lambda match: str(variables[match[1]]) if match[1] in variables else match[0], value)
    return value


def locators(step: dict) -> list[dict]:
    """All locator candidates of a step, best first."""
    found = [dict(item) for item in step.get("locators", [])]
    if "selector" in step:
        selector = step["selector"]
        found.append({"by": "xpath", "value": selector[6:]} if selector.startswith("xpath=") else {"by": "css", "value": selector})
    if "placeholder" in step:
        found.append({"by": "placeholder", "value": step["placeholder"]})
    if "role" in step:
        found.append({"by": "role", "role": step["role"], "name": step.get("name")})
    return found


def describe_locator(locator: dict) -> str:
    kind = locator.get("by")
    suffix = f' 第{locator["nth"] + 1}个' if isinstance(locator.get("nth"), int) else ""
    if kind == "role":
        name = locator.get("name")
        return (f'{locator.get("role")}「{name}」' if name else str(locator.get("role"))) + suffix
    return f'{LOCATOR_LABELS.get(kind, kind)} {locator.get("value")}{suffix}'


def describe(step: dict) -> str:
    """One readable line for a step; secrets stay as ${ENV:NAME} references."""
    target = locators(step)
    where = describe_locator(target[0]) if target else ""
    if "action" in step:
        op = step["action"]
        text = ACTION_NAMES.get(op, op)
        if op.startswith("tab_"):
            return f"{text} {step.get('tab', step.get('tabs', {}).get('active', ''))}".strip()
        if op == "goto":
            return f'{text} {step.get("url")}'
        if op in ("fill", "type", "select"):
            return f'在 {where} {text}「{step.get("value")}」'
        if op == "press":
            if not where and step.get("key_target") == "active":
                return f'{text} {step.get("key")}（当前焦点）'
            return f'{text} {step.get("key")}' + (f"（{where}）" if where else "")
        return f"{text} {where}".strip()
    op = step.get("assertion")
    text = ASSERTION_NAMES.get(op, op)
    if op in ("url", "url_contains", "title"):
        return f'{text}「{step.get("expected")}」'
    if op == "attribute":
        return f'{where} 的属性 {step.get("attribute")} 为「{step.get("expected")}」'
    if op in ("visible", "hidden"):
        return f"{where} {text}"
    if op == "checked":
        return f'{where} {"已勾选" if step.get("expected", True) else "未勾选"}'
    return f'{where} {text}「{step.get("expected")}」'


def _is_index(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check_field_identity(locator: dict, where: str) -> None:
    if locator.get("unique") is not True or "nth" in locator:
        raise ValueError(f"{where}：唯一控件定位需要带名称的角色，不能指定匹配序号")
    if not isinstance(locator.get("name"), str) or not locator["name"]:
        raise ValueError(f"{where}：唯一控件定位缺少名称")
    if "anchor" in locator:
        anchor = locator["anchor"]
        if (not isinstance(anchor, dict) or set(anchor) != {"by", "value"} or anchor["by"] not in ("css", "testid")
                or not isinstance(anchor["value"], str) or not anchor["value"]):
            raise ValueError(f"{where}：唯一控件的锚点需要有效的 CSS 或测试标识")


def _check_locator(locator: object, where: str) -> None:
    if not isinstance(locator, dict) or locator.get("by") not in LOCATOR_KINDS:
        raise ValueError(f"{where}：定位方式必须是 {' / '.join(LOCATOR_KINDS)} 之一")
    if locator["by"] == "role":
        if not isinstance(locator.get("role"), str) or not locator["role"]:
            raise ValueError(f"{where}：role 定位缺少 role")
    elif not isinstance(locator.get("value"), str) or not locator["value"]:
        raise ValueError(f"{where}：定位缺少 value")
    if "nth" in locator and not _is_index(locator["nth"]):
        raise ValueError(f"{where}：nth 必须是从 0 开始的整数")
    if "context" in locator:
        raise ValueError(f"{where}：context 应声明在步骤上，不能嵌入定位候选")
    if locator["by"] == "role" and "unique" in locator and locator["unique"] is not False:
        _check_field_identity(locator, where)
    if "xpath_anchor" in locator:
        if locator["by"] != "xpath" or not isinstance(locator["xpath_anchor"], str) or not locator["xpath_anchor"].strip():
            raise ValueError(f"{where}：xpath_anchor 必须是 XPath 的非空 CSS 元素锚点")
        if not locator["value"].startswith("."):
            raise ValueError(f"{where}：带元素锚点的 XPath 必须以 . 开始")


def _check_context(context: object, where: str) -> None:
    if not isinstance(context, list):
        raise ValueError(f"{where}：context 必须是有序的 frame / shadow 路径数组")
    for index, segment in enumerate(context, 1):
        if not isinstance(segment, dict) or segment.get("kind") not in ("frame", "shadow"):
            raise ValueError(f"{where}：第 {index} 层上下文必须是 frame 或 shadow")
        candidates = segment.get("locators")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError(f"{where}：第 {index} 层上下文缺少 locators")
        for locator in candidates:
            _check_locator(locator, where + f" 第 {index} 层上下文")


def _check_step(step: object, table: dict, key: str, where: str) -> None:
    kind = "动作" if key == "action" else "断言"
    if not isinstance(step, dict) or step.get(key) not in table:
        raise ValueError(f"{where}：不支持的{kind}；只接受 {' / '.join(table)}")
    needs_target, required = table[step[key]]
    for field in required:
        if field not in step:
            raise ValueError(f"{where}（{step[key]}）缺少 {field}")
    target = locators(step)
    if needs_target and not target:
        raise ValueError(f"{where}（{step[key]}）缺少定位")
    for locator in target:
        _check_locator(locator, where)
    _check_context(step.get("context", []), where)
    if step.get("action") == "captcha":
        if not isinstance(step["image"], list) or not step["image"]:
            raise ValueError(f"{where}：验证码图片必须包含定位候选")
        for locator in step["image"]:
            _check_locator(locator, where + " 验证码图片")
        _check_context(step.get("image_context", step.get("context", [])), where + " 验证码图片")
    dynamic = isinstance(step.get("expected"), str) and re.fullmatch(r"\$\{ENV:[A-Za-z_][A-Za-z0-9_]*\}", step["expected"])
    if step[key] in ("count", "min_count") and not (_is_index(step["expected"]) or dynamic):
        raise ValueError(f"{where}：count 断言需要非负整数")
    _check_timing(step, key, where)
    _check_tab_fields(step, where)


def _check_timing(step: dict, key: str, where: str) -> None:
    for field in ("timeout_seconds", "stable_for_seconds"):
        if field in step:
            value = step[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
                raise ValueError(f"{where}：{field} 必须是有限的非负秒数")
    if key != "assertion":
        return
    if step.get("mode", "eventually") not in ("eventually", "always"):
        raise ValueError(f"{where}：mode 只能是 eventually 或 always")
    if "transition_from" in step:
        source = step["transition_from"]
        if not isinstance(source, dict) or any(k in source for k in ("transition_from", "mode", "timeout_seconds", "stable_for_seconds")):
            raise ValueError(f"{where}：transition_from 必须是没有时间配置的普通断言")
        source = {"locators": locators(step), "context": step.get("context", []), **source}
        _check_step(source, ASSERTIONS, "assertion", where + " 的来源状态")


def _check_tab_fields(step: dict, where: str) -> None:
    """Validate recorded tab ids and changes after the main step fields."""
    def tab_id(value):
        return isinstance(value, str) and value.startswith("t") and value[1:].isascii() and value[1:].isdigit() and int(value[1:]) > 0
    for field in ("tab", "active"):
        if field in step and not tab_id(step[field]):
            raise ValueError(f"{where}：标签页编号必须是 t1、t2 这样的格式")
    if "tabs" in step:
        change = step["tabs"]
        if not isinstance(change, dict) or not tab_id(change.get("active")):
            raise ValueError(f"{where}：标签页变化缺少有效的 active")
        for field in ("opened", "closed"):
            if not isinstance(change.get(field, []), list):
                raise ValueError(f"{where}：{field} 必须为数组")
        for item in change.get("opened", []):
            if not isinstance(item, dict) or not tab_id(item.get("id")) or not isinstance(item.get("url", ""), str):
                raise ValueError(f"{where}：新标签页格式无效")
        if not all(tab_id(tab) for tab in change.get("closed", [])):
            raise ValueError(f"{where}：关闭标签页格式无效")


def validate(automation: object) -> None:
    """Reject anything outside the whitelist. Steps are data; nothing is ever evaluated."""
    if not isinstance(automation, dict):
        raise ValueError("用例没有可回放的自动化步骤")
    if not isinstance(automation.get("start_url"), str) or not automation["start_url"]:
        raise ValueError("自动化步骤缺少 start_url")
    actions, assertions = automation.get("actions", []), automation.get("assertions", [])
    if not isinstance(actions, list) or not isinstance(assertions, list):
        raise ValueError("actions 与 assertions 必须是数组")
    for index, step in enumerate(actions, 1):
        if isinstance(step, dict) and "assertion" in step:  # a check made mid-flow
            _check_step(step, ASSERTIONS, "assertion", f"第 {index} 步")
        else:
            _check_step(step, ACTIONS, "action", f"第 {index} 步")
    for index, step in enumerate(assertions, 1):
        _check_step(step, ASSERTIONS, "assertion", f"第 {index} 条断言")
    setup = automation.get("setup_cases", [])
    if not isinstance(setup, list):
        raise ValueError("setup_cases 必须是按执行顺序排列的已保存用例版本")
    for item in setup:
        if (not isinstance(item, dict) or not isinstance(item.get("case_id"), str) or not item["case_id"]
                or type(item.get("revision")) is not int or item["revision"] < 1
                or not isinstance(item.get("content_hash"), str) or not re.fullmatch(r"[a-f0-9]{64}", item["content_hash"])):
            raise ValueError("前置用例需要编号、版本和内容哈希")
        validate_setup(item.get("automation"))


def validate_setup(automation: object) -> None:
    """A setup stage has its own oracle and stays in the current tab."""
    if isinstance(automation, dict) and "setup_cases" in automation:
        raise ValueError("前置用例的所有阶段需按执行顺序保存")
    validate(automation)
    actions, assertions = automation.get("actions", []), automation.get("assertions", [])
    if any(step.get("action", "").startswith("tab_") or step.get("tabs") for step in actions):
        raise ValueError("同页前置用例暂只支持单标签页操作")
    if not any("assertion" in step and step.get("oracle", {}).get("source") != "observation"
               for step in [*actions, *assertions]):
        raise ValueError("前置用例需要独立页面断言")


def is_replayable(case: dict) -> bool:
    if case.get("kind") == "api":
        return True
    try:
        validate(case.get("extensions", {}).get("automation"))
    except ValueError:
        return False
    return True


def absolute_url(url: str, base_url: str, rebase: str | None = None) -> str:
    """Cases may store a path; the project's target address makes it absolute.

    With an environment whose address differs, recorded absolute URLs on the project's own
    origin move to that environment; URLs on other sites are left alone.
    """
    parts = urlsplit(url)
    if parts.scheme:
        recorded = urlsplit(rebase) if rebase else None
        if (recorded and base_url and parts.scheme in ("http", "https")
                and (parts.scheme, parts.netloc) == (recorded.scheme, recorded.netloc)):
            return urlsplit(base_url)._replace(path=parts.path, query=parts.query, fragment=parts.fragment).geturl()
        return url
    if not base_url:
        raise ValueError("用例使用了相对地址，但项目没有设置目标地址")
    return urljoin(base_url.rstrip("/") + "/", url.lstrip("/"))


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def check_origin(url: str, allowed: list[str]) -> None:  # noqa: ARG001  # Preserve the legacy keyword argument without restricting origins.
    """Validate URL shape; legacy allowed_origins no longer limits site traffic."""
    parts = urlsplit(url)
    if parts.scheme in ("about", "data", "blob"):
        return
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        shown = f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "") if parts.hostname else parts.scheme + ":"
        raise PermissionError(f"地址 {shown} 格式无效或含内嵌凭据")  # never echo userinfo
