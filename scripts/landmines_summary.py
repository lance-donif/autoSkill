"""Summarize the pinned Landmines rules and the other Oxlint diagnostics for CI."""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def summarize(report_path: Path) -> None:
    preset = ROOT / ".github/landmines/node_modules/@interfere/landmines/configs/recommended.json"
    rules = json.loads(preset.read_text(encoding="utf-8"))["rules"]
    configured = json.loads((ROOT / ".oxlintrc.json").read_text(encoding="utf-8"))["rules"]
    if any(configured.get(rule) not in {"warn", "error"} for rule in rules):
        raise SystemExit("Landmines 有上游规则未配置，请核对项目配置。")

    report = json.loads(report_path.read_text(encoding="utf-8"))
    files = report["number_of_files"]
    if files < 1:
        raise SystemExit("Landmines 未扫描到源文件。")
    counts = Counter(item.get("code") for item in report["diagnostics"])
    per_rule = {}
    for rule in rules:
        plugin, name = rule.split("/")
        per_rule[rule] = counts[f"{plugin}({name})"]
    total = sum(per_rule.values())
    print(f"检查了 {files} 个文件。Landmines 共 {len(rules)} 条规则，产生 {total} 条提示。"
          f"Oxlint 另有 {counts.total() - total} 条诊断。\n")
    print("| Landmines 规则 | 提示数 |\n| --- | ---: |")
    for rule, count in per_rule.items():
        print(f"| `{rule}` | {count} |")


if __name__ == "__main__":
    summarize(Path(sys.argv[1]))
