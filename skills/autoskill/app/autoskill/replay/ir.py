"""The step model every engine replays.

A UI case stores `extensions.automation = {start_url, actions, assertions}`.
Each step carries a list of locator candidates, best first; the runner uses the
first one that matches and reports when it had to fall back. The older single
`selector` / `placeholder` / `role`+`name` fields are still accepted.
"""
from __future__ import annotations

import math
import re
from urllib.parse import urljoin, urlsplit

LOCATOR_KINDS = ("testid", "role", "label", "placeholder", "text", "css")
# action -> (needs a target element, required fields)
ACTIONS = {
    "goto": (False, ("url",)),
    "reload": (False, ()),
    "back": (False, ()),
    "forward": (False, ()),
    "tab_new": (False, ("tab", "url")),
    "tab_switch": (False, ("tab",)),
    "tab_close": (False, ("tab", "active")),
    "tab_sync": (False, ("tabs",)),
    "fill": (True, ("value",)),
    "type": (True, ("value",)),
    "click": (True, ()),
    "dblclick": (True, ()),
    "hover": (True, ()),
    "focus": (True, ()),
    "drag": (True, ("target_locators",)),
    "scroll_into_view": (True, ()),
    "frame": (False, ("selector",)),
    "dialog": (False, ("mode",)),
    "eval": (False, ("script",)),
    "keyboard": (False, ("operation", "value")),
    "mouse": (False, ("operation",)),
    "scroll": (False, ("direction", "pixels")),
    "wait": (False, ()),
    "upload": (True, ("files",)),
    "download": (True, ("filename",)),
    "clipboard": (False, ("operation", "text")),
    "browser_command": (False, ("command", "args")),
    # Keep unsupported mutations visible in saved cases, never silently replay them.
    "native_step": (False, ("command", "args", "reason")),
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
    "back": "后退", "forward": "前进", "focus": "聚焦", "drag": "拖动",
    "scroll_into_view": "滚动到元素", "frame": "切换框架", "dialog": "准备下一次对话框",
    "eval": "执行脚本", "keyboard": "键盘", "mouse": "鼠标", "scroll": "滚动",
    "wait": "等待", "upload": "上传文件", "download": "下载文件", "clipboard": "写入剪贴板",
    "native_step": "原生命令（不可回放）",
    "browser_command": "浏览器设置",
}
ASSERTION_NAMES = {
    "visible": "可见", "hidden": "不可见", "count": "数量为", "min_count": "数量至少为", "text": "文本为", "contains": "文本包含",
    "value": "输入值为", "checked": "勾选状态为", "attribute": "属性", "url": "地址为",
    "url_contains": "地址包含", "title": "页面标题为",
}
LOCATOR_LABELS = {"testid": "测试标识", "label": "标签", "placeholder": "占位提示", "text": "文字", "css": "选择器"}


RAND = "{{RAND}}"
_rand = ""


def set_rand(value: str) -> None:
    global _rand
    _rand = value


def expand(value):
    """Fill {{RAND}}: a short text that is new on every run, so data a case creates never collides with an earlier run."""
    return value.replace(RAND, _rand) if isinstance(value, str) and _rand else value


def locators(step: dict) -> list[dict]:
    """All locator candidates of a step, best first."""
    found = [dict(item) for item in step.get("locators", [])]
    if "selector" in step:
        found.append({"by": "css", "value": step["selector"]})
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
            return f'{text} {step.get("key")}' + (f"（{where}）" if where else "")
        if op == "drag":
            target = step.get("target_locators", [])
            return f'{text} {where} 到 {describe_locator(target[0]) if target else ""}'
        if op == "frame":
            return f'{text} {step.get("selector")}'
        if op == "dialog":
            return f'{text} {step.get("mode")}' + (f'「{step["text"]}」' if "text" in step else "")
        if op == "eval":
            return text  # A script can contain credentials; keep them out of comments/reports.
        if op == "keyboard":
            return f'{text} {step.get("operation")}「{step.get("value")}」'
        if op == "mouse":
            values = ", ".join(f"{field}={step[field]}" for field in ("x", "y", "button", "delta_x", "delta_y") if field in step)
            return f'{text} {step.get("operation")} {values}'.strip()
        if op == "scroll":
            return f'{text} {step.get("direction")} {step.get("pixels")}px {where}'.strip()
        if op == "wait":
            return f'{text} {step["milliseconds"]}ms' if "milliseconds" in step else f'{text} {where} {step.get("state", "visible")}'
        if op == "upload":
            return f'{text} {", ".join(step.get("files", []))} 到 {where}'
        if op == "download":
            return f'{text} {where} 为 {step.get("filename")}'
        if op == "clipboard":
            return text
        if op == "native_step":
            return f'{text} {step.get("command")}：{step.get("reason")}'
        if op == "browser_command":
            return f'{text} {step.get("command")} {" ".join(step.get("args", [])[:2])}'
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


def _is_number(value: object, *, nonnegative: bool = False) -> bool:
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and (not nonnegative or value >= 0))
    except OverflowError:
        return False


def _relative_file(value: object) -> bool:
    """Portable artifact paths never include machine paths or runtime substitutions."""
    return (isinstance(value, str) and bool(value) and not value.startswith("/")
            and not re.match(r"^[A-Za-z]:", value) and "\\" not in value and "\x00" not in value
            and all(part not in ("", ".", "..") for part in value.split("/"))
            and "${ENV:" not in value and RAND not in value)


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


def _check_step(step: object, table: dict, key: str, where: str) -> None:
    kind = "动作" if key == "action" else "断言"
    if not isinstance(step, dict) or step.get(key) not in table:
        raise ValueError(f"{where}：不支持的{kind}；只接受 {' / '.join(table)}")
    needs_target, required = table[step[key]]
    for field in required:
        if field not in step:
            raise ValueError(f"{where}（{step[key]}）缺少 {field}")
    if "locators" in step and not isinstance(step["locators"], list):
        raise ValueError(f"{where}：locators 必须是数组")
    for field in ("timeout_seconds", "stable_for_seconds"):
        if field in step and (not _is_number(step[field], nonnegative=True)
                              or (field == "timeout_seconds" and step[field] == 0)):
            raise ValueError(f"{where}：{field} 必须是{'正' if field == 'timeout_seconds' else '非负'}有限数值")
    target = locators(step)
    if needs_target and not target:
        raise ValueError(f"{where}（{step[key]}）缺少定位")
    for locator in target:
        _check_locator(locator, where)
    if key == "action":
        _check_action_fields(step, where)
    if step[key] in ("count", "min_count") and not _is_index(step["expected"]):
        raise ValueError(f"{where}：count 断言需要非负整数")
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


def _check_action_fields(step: dict, where: str) -> None:
    op = step["action"]

    def text(field: str, *, nonempty: bool = False) -> None:
        if not isinstance(step.get(field), str) or (nonempty and not step[field]):
            raise ValueError(f"{where}（{op}）：{field} 必须是{'非空' if nonempty else ''}字符串")

    if op == "drag":
        targets = step["target_locators"]
        if not isinstance(targets, list) or not targets:
            raise ValueError(f"{where}：drag 缺少目标定位 target_locators")
        for locator in targets:
            _check_locator(locator, where + " 目标")
    elif op == "frame":
        text("selector", nonempty=True)
    elif op == "dialog":
        if step["mode"] not in ("accept", "dismiss"):
            raise ValueError(f"{where}：dialog mode 只能是 accept / dismiss")
        if "text" in step:
            text("text")
            if step["mode"] != "accept":
                raise ValueError(f"{where}：只有接受对话框时才能提供 text")
    elif op == "eval":
        text("script", nonempty=True)
    elif op == "keyboard":
        if step["operation"] not in ("type", "inserttext", "press", "down", "up"):
            raise ValueError(f"{where}：不支持的 keyboard operation")
        text("value")
        if step["operation"] in ("press", "down", "up") and not step["value"]:
            raise ValueError(f"{where}：键名不能为空")
    elif op == "mouse":
        operation = step["operation"]
        if operation not in ("move", "down", "up", "wheel"):
            raise ValueError(f"{where}：不支持的 mouse operation")
        if operation == "move" and not all(field in step for field in ("x", "y")):
            raise ValueError(f"{where}：mouse move 缺少 x / y")
        if "button" in step and step["button"] not in ("left", "middle", "right"):
            raise ValueError(f"{where}：鼠标按钮只能是 left / middle / right")
        for field in ("x", "y", "delta_x", "delta_y"):
            if field in step and not _is_number(step[field]):
                raise ValueError(f"{where}：{field} 必须是有限数值")
    elif op == "scroll":
        if step["direction"] not in ("up", "down", "left", "right") or not _is_number(step["pixels"], nonnegative=True):
            raise ValueError(f"{where}：scroll 需要 up / down / left / right 与非负 pixels")
    elif op == "wait":
        if "milliseconds" in step:
            if not _is_number(step["milliseconds"], nonnegative=True) or locators(step) or "state" in step:
                raise ValueError(f"{where}：wait 毫秒模式需要非负数且不能混用定位或 state")
        elif not locators(step) or step.get("state", "visible") not in ("visible", "hidden", "attached", "detached"):
            raise ValueError(f"{where}：wait 需要 milliseconds 或定位与有效 state")
    elif op == "upload":
        if not isinstance(step["files"], list) or not step["files"] or not all(_relative_file(name) for name in step["files"]):
            raise ValueError(f"{where}：upload files 必须是非空的项目相对文件路径数组")
    elif op == "download":
        if not _relative_file(step["filename"]):
            raise ValueError(f"{where}：download filename 必须是输出目录内的相对文件路径")
    elif op == "clipboard":
        if step["operation"] != "write":
            raise ValueError(f"{where}：clipboard 只记录 write；读取是观察操作")
        text("text")
    elif op in ("native_step", "browser_command"):
        text("command", nonempty=True)
        if not isinstance(step["args"], list) or not all(isinstance(arg, str) for arg in step["args"]):
            raise ValueError(f"{where}：{op} args 必须是字符串数组")
        if op == "native_step":
            text("reason", nonempty=True)
            if "replayable" in step and step["replayable"] is not False:
                raise ValueError(f"{where}：native_step 不能标记为可回放")
        else:
            from .primitives import normalize_browser_command

            # Templates substitute values, never the command/flag vocabulary.
            args = list(step["args"])
            templated = ["${ENV:" in arg or RAND in arg for arg in args]
            for index, dynamic in enumerate(templated):
                if not dynamic:
                    continue
                replacement = "value"
                if step["command"] == "set" and index > 0:
                    setting = args[0]
                    replacement = {"viewport": "1", "offline": "off", "headers": "{}", "geo": "0", "geolocation": "0",
                                   "media": "light" if index == 1 else "no-preference"}.get(setting, "value")
                elif step["command"] == "cookies" and index > 0:
                    replacement = {"--expires": "1", "--same-site": "Lax", "--url": "https://example.test/",
                                   "--domain": "example.test", "--path": "/"}.get(args[index - 1], "value")
                args[index] = replacement
            normalize_browser_command(step["command"], args)


def validate(automation: object) -> None:
    """Validate saved step data, including explicit nonportable native mutations.

    eval is an authorized browser script action; native_step remains nonreplayable.
    """
    if not isinstance(automation, dict):
        raise ValueError("用例没有可回放的自动化步骤")
    if not isinstance(automation.get("start_url"), str) or not automation["start_url"]:
        raise ValueError("自动化步骤缺少 start_url")
    for field in ("timeout_seconds", "assertion_timeout_seconds"):
        if field in automation and (not _is_number(automation[field], nonnegative=True) or automation[field] == 0):
            raise ValueError(f"{field} 必须是正有限数值")
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


def is_replayable(case: dict) -> bool:
    if case.get("kind") == "api":
        return True
    try:
        automation = case.get("extensions", {}).get("automation")
        validate(automation)
    except ValueError:
        return False
    if replay_blockers(automation):
        return False
    return True


def replay_blockers(case_or_automation: object) -> list[str]:
    """Reasons a recorded UI flow must be shown as blocked rather than omitted."""
    if not isinstance(case_or_automation, dict):
        return []
    automation = case_or_automation.get("extensions", {}).get("automation", case_or_automation)
    if not isinstance(automation, dict) or not isinstance(automation.get("actions", []), list):
        return []
    return [f"{step.get('command', 'native')}：{step.get('reason', '没有可移植回放实现')}"
            for step in automation.get("actions", [])
            if isinstance(step, dict) and step.get("action") == "native_step"]


def absolute_url(url: str, base_url: str) -> str:
    """Cases may store a path; the project's target address makes it absolute."""
    if urlsplit(url).scheme:
        return url
    if not base_url:
        raise ValueError("用例使用了相对地址，但项目没有设置目标地址")
    return urljoin(base_url.rstrip("/") + "/", url.lstrip("/"))


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def check_origin(url: str, allowed: list[str]) -> None:
    """Validate URL shape; legacy allowed_origins no longer limits site traffic."""
    parts = urlsplit(url)
    if parts.scheme in ("about", "data", "blob"):
        return
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        shown = f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "") if parts.hostname else parts.scheme + ":"
        raise PermissionError(f"地址 {shown} 格式无效或含内嵌凭据")  # never echo userinfo
