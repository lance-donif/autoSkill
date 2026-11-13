# 失败与阻塞清单

本清单记录外置模型实验中的失败与阻塞，不把小模型误选直接认定为 autoSkill 产品缺陷。较早失败不因后续成功而删除。

## laya-browser/history

- **FT-MMYXLK-001** `02_password_select/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`select_two → select_two → select_two`。 [截图](证据/results/screenshots/laya-browser-history-02_password_select-en-11.png)
- **FT-FXKYDXK-001** `03_checkbox_radio/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`radio_default → radio_default → radio_default`。 [截图](证据/results/screenshots/laya-browser-history-03_checkbox_radio-en-11.png)
- **FT-DTSRK-001** `04_dynamic/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`reveal → reveal → reveal`。 [截图](证据/results/screenshots/laya-browser-history-04_dynamic-en-11.png)
- **FT-TCQRYQX-001** `05_dialogs/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`accept_alert → accept_alert`。 [截图](证据/results/screenshots/laya-browser-history-05_dialogs-en-11.png)
- **FT-QSYSCL-001** `07_missing/en`；选择导致执行错误；连续两次动作未改变观测状态，实验停止。动作：`fill_text → fill_absent → fill_absent`。 错误：StepError: 页面上找不到：选择器 #customer-id, [name=customer-id] [截图](证据/results/screenshots/laya-browser-history-07_missing-en-11.png)
- **FT-WZBD-001** `08_full_form/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`select_two → select_two → select_two`。 [截图](证据/results/screenshots/laya-browser-history-08_full_form-en-11.png)
- **FT-MMYXLK-002** `02_password_select/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`select_two → select_two → select_two`。 [截图](证据/results/screenshots/laya-browser-history-02_password_select-zh-11.png)
- **FT-FXKYDXK-002** `03_checkbox_radio/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`check_default → check_default → check_default`。 [截图](证据/results/screenshots/laya-browser-history-03_checkbox_radio-zh-11.png)
- **FT-DTSRK-002** `04_dynamic/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`reveal → reveal → reveal`。 [截图](证据/results/screenshots/laya-browser-history-04_dynamic-zh-11.png)
- **FT-TCQRYQX-002** `05_dialogs/zh`；任务结果未满足；将可执行任务错误报告为缺少元素。动作：`cancel_confirm → cancel_confirm → cancel_confirm → missing`。 [截图](证据/results/screenshots/laya-browser-history-05_dialogs-zh-11.png)
- **FT-QSYSCL-002** `07_missing/zh`；选择导致执行错误；连续两次动作未改变观测状态，实验停止。动作：`fill_text → fill_absent → fill_absent`。 错误：StepError: 页面上找不到：选择器 #customer-id, [name=customer-id] [截图](证据/results/screenshots/laya-browser-history-07_missing-zh-11.png)
- **FT-WZBD-002** `08_full_form/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`select_two → select_two → select_two`。 [截图](证据/results/screenshots/laya-browser-history-08_full_form-zh-11.png)

## laya-browser/planned

- **FT-TCQRYQX-003** `05_dialogs/en`；任务结果未满足；计划已执行完，但原任务目标尚未满足。动作：`cancel_confirm → accept_alert`。 [截图](证据/results/screenshots/laya-browser-planned-05_dialogs-en-11.png)
- **FT-WZBD-003** `08_full_form/en`；任务结果未满足；提前提交或提交数据与目标不符。动作：`fill_text → fill_area → fill_area → select_two → check_default → submit`。 [截图](证据/results/screenshots/laya-browser-planned-08_full_form-en-11.png)
- **FT-KJNSR-004** `06_iframe/zh`；选择导致执行错误；连续两次动作未改变观测状态，实验停止。动作：`fill_main → fill_main`。 错误：StepError: 页面上找不到：选择器 #working [截图](证据/results/screenshots/laya-browser-planned-06_iframe-zh-11.png)
- **FT-WZBD-004** `08_full_form/zh`；任务结果未满足；提前提交或提交数据与目标不符。动作：`fill_text → fill_area → fill_area → select_two → check_default → submit`。 [截图](证据/results/screenshots/laya-browser-planned-08_full_form-zh-11.png)

## laya-browser/native

- **FT-WBSRYTJ-005** `01_text_submit/en`；任务结果未满足；提前提交或提交数据与目标不符。动作：`submit`。 [截图](证据/results/screenshots/laya-browser-native-01_text_submit-en-11.png)
- **FT-MMYXLK-005** `02_password_select/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`select_two → __done__ → __done__`。 [截图](证据/results/screenshots/laya-browser-native-02_password_select-en-11.png)
- **FT-FXKYDXK-005** `03_checkbox_radio/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`radio_default → __done__ → __done__`。 [截图](证据/results/screenshots/laya-browser-native-03_checkbox_radio-en-11.png)
- **FT-DTSRK-005** `04_dynamic/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`reveal → __done__ → __done__ → reveal`。 [截图](证据/results/screenshots/laya-browser-native-04_dynamic-en-11.png)
- **FT-TCQRYQX-005** `05_dialogs/en`；任务结果未满足；尚未按要求取消第一次弹窗就确认跳转。动作：`accept_confirm`。 [截图](证据/results/screenshots/laya-browser-native-05_dialogs-en-11.png)
- **FT-QSYSCL-005** `07_missing/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`reload → __done__`。 [截图](证据/results/screenshots/laya-browser-native-07_missing-en-11.png)
- **FT-WZBD-005** `08_full_form/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`select_two → __done__ → __done__`。 [截图](证据/results/screenshots/laya-browser-native-08_full_form-en-11.png)
- **FT-WBSRYTJ-006** `01_text_submit/zh`；任务结果未满足；提前提交或提交数据与目标不符。动作：`submit`。 [截图](证据/results/screenshots/laya-browser-native-01_text_submit-zh-11.png)
- **FT-MMYXLK-006** `02_password_select/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`select_two → __done__ → __done__`。 [截图](证据/results/screenshots/laya-browser-native-02_password_select-zh-11.png)
- **FT-FXKYDXK-006** `03_checkbox_radio/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`radio_default → __done__ → __done__`。 [截图](证据/results/screenshots/laya-browser-native-03_checkbox_radio-zh-11.png)
- **FT-DTSRK-006** `04_dynamic/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`reveal → __done__ → __done__ → reveal`。 [截图](证据/results/screenshots/laya-browser-native-04_dynamic-zh-11.png)
- **FT-TCQRYQX-006** `05_dialogs/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`cancel_confirm → __done__ → __done__`。 [截图](证据/results/screenshots/laya-browser-native-05_dialogs-zh-11.png)
- **FT-QSYSCL-006** `07_missing/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`reload → __done__`。 [截图](证据/results/screenshots/laya-browser-native-07_missing-zh-11.png)
- **FT-WZBD-006** `08_full_form/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`select_two → __done__ → __done__`。 [截图](证据/results/screenshots/laya-browser-native-08_full_form-zh-11.png)

## laya-multilingual/history

- **FT-WBSRYTJ-007** `01_text_submit/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`fill_text → fill_text → fill_text`。 [截图](证据/results/screenshots/laya-multilingual-history-01_text_submit-en-11.png)
- **FT-MMYXLK-007** `02_password_select/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`fill_password → fill_password → fill_password`。 [截图](证据/results/screenshots/laya-multilingual-history-02_password_select-en-11.png)
- **FT-FXKYDXK-007** `03_checkbox_radio/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`check_default → check_default → check_default`。 [截图](证据/results/screenshots/laya-multilingual-history-03_checkbox_radio-en-11.png)
- **FT-DTSRK-007** `04_dynamic/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`add_box → add_box`。 [截图](证据/results/screenshots/laya-multilingual-history-04_dynamic-en-11.png)
- **FT-TCQRYQX-007** `05_dialogs/en`；任务结果未满足；尚未按要求取消第一次弹窗就确认跳转。动作：`accept_alert → accept_confirm`。 [截图](证据/results/screenshots/laya-multilingual-history-05_dialogs-en-11.png)
- **FT-QSYSCL-007** `07_missing/en`；选择导致执行错误；连续两次动作未改变观测状态，实验停止。动作：`fill_absent → fill_absent`。 错误：StepError: 页面上找不到：选择器 #customer-id, [name=customer-id] [截图](证据/results/screenshots/laya-multilingual-history-07_missing-en-11.png)
- **FT-WZBD-007** `08_full_form/en`；任务结果未满足；将可执行任务错误报告为缺少元素。动作：`missing`。 [截图](证据/results/screenshots/laya-multilingual-history-08_full_form-en-11.png)
- **FT-WBSRYTJ-008** `01_text_submit/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`fill_text → fill_text → fill_text`。 [截图](证据/results/screenshots/laya-multilingual-history-01_text_submit-zh-11.png)
- **FT-MMYXLK-008** `02_password_select/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`fill_password → fill_password → fill_password`。 [截图](证据/results/screenshots/laya-multilingual-history-02_password_select-zh-11.png)
- **FT-FXKYDXK-008** `03_checkbox_radio/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`check_default → check_default → check_default`。 [截图](证据/results/screenshots/laya-multilingual-history-03_checkbox_radio-zh-11.png)
- **FT-DTSRK-008** `04_dynamic/zh`；任务结果未满足；将可执行任务错误报告为缺少元素。动作：`missing`。 [截图](证据/results/screenshots/laya-multilingual-history-04_dynamic-zh-11.png)
- **FT-TCQRYQX-008** `05_dialogs/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`accept_alert → accept_alert`。 [截图](证据/results/screenshots/laya-multilingual-history-05_dialogs-zh-11.png)
- **FT-QSYSCL-008** `07_missing/zh`；选择导致执行错误；连续两次动作未改变观测状态，实验停止。动作：`fill_absent → fill_absent`。 错误：StepError: 页面上找不到：选择器 #customer-id, [name=customer-id] [截图](证据/results/screenshots/laya-multilingual-history-07_missing-zh-11.png)
- **FT-WZBD-008** `08_full_form/zh`；任务结果未满足；将可执行任务错误报告为缺少元素。动作：`missing`。 [截图](证据/results/screenshots/laya-multilingual-history-08_full_form-zh-11.png)

## laya-multilingual/planned

- **FT-FXKYDXK-009** `03_checkbox_radio/en`；任务结果未满足；计划已执行完，但原任务目标尚未满足。动作：`check_default → check_default`。 [截图](证据/results/screenshots/laya-multilingual-planned-03_checkbox_radio-en-11.png)
- **FT-DTSRK-009** `04_dynamic/en`；选择导致执行错误；连续两次动作未改变观测状态，实验停止。动作：`add_box → wait`。 错误：StepError: 等待超时：等待出现 选择器 #revealed [截图](证据/results/screenshots/laya-multilingual-planned-04_dynamic-en-11.png)
- **FT-TCQRYQX-009** `05_dialogs/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`accept_alert → accept_alert`。 [截图](证据/results/screenshots/laya-multilingual-planned-05_dialogs-en-11.png)
- **FT-FXKYDXK-010** `03_checkbox_radio/zh`；任务结果未满足；计划已执行完，但原任务目标尚未满足。动作：`check_default → check_default`。 [截图](证据/results/screenshots/laya-multilingual-planned-03_checkbox_radio-zh-11.png)
- **FT-DTSRK-010** `04_dynamic/zh`；选择导致执行错误；连续两次动作未改变观测状态，实验停止。动作：`add_box → wait`。 错误：StepError: 等待超时：等待出现 选择器 #revealed [截图](证据/results/screenshots/laya-multilingual-planned-04_dynamic-zh-11.png)
- **FT-TCQRYQX-010** `05_dialogs/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`accept_alert → accept_alert`。 [截图](证据/results/screenshots/laya-multilingual-planned-05_dialogs-zh-11.png)
- **FT-WZBD-010** `08_full_form/zh`；任务结果未满足；提前提交或提交数据与目标不符。动作：`fill_password → fill_password → fill_area → select_two → check_default → radio_default → submit`。 [截图](证据/results/screenshots/laya-multilingual-planned-08_full_form-zh-11.png)

## verdict-small/history

- **FT-WBSRYTJ-011** `01_text_submit/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`fill_text → fill_text → fill_text`。 [截图](证据/results/screenshots/verdict-small-history-01_text_submit-en-11.png)
- **FT-MMYXLK-011** `02_password_select/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`fill_password → fill_password → fill_password`。 [截图](证据/results/screenshots/verdict-small-history-02_password_select-en-11.png)
- **FT-FXKYDXK-011** `03_checkbox_radio/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`radio_default → radio_default → radio_default`。 [截图](证据/results/screenshots/verdict-small-history-03_checkbox_radio-en-11.png)
- **FT-DTSRK-011** `04_dynamic/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`reveal → reveal → wait → reveal → wait`。 [截图](证据/results/screenshots/verdict-small-history-04_dynamic-en-11.png)
- **FT-TCQRYQX-011** `05_dialogs/en`；任务结果未满足；尚未按要求取消第一次弹窗就确认跳转。动作：`accept_confirm`。 [截图](证据/results/screenshots/verdict-small-history-05_dialogs-en-11.png)
- **FT-QSYSCL-011** `07_missing/en`；选择导致执行错误；连续两次动作未改变观测状态，实验停止。动作：`fill_absent → fill_absent`。 错误：StepError: 页面上找不到：选择器 #customer-id, [name=customer-id] [截图](证据/results/screenshots/verdict-small-history-07_missing-en-11.png)
- **FT-WZBD-011** `08_full_form/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`radio_default → radio_default → radio_default`。 [截图](证据/results/screenshots/verdict-small-history-08_full_form-en-11.png)
- **FT-WBSRYTJ-012** `01_text_submit/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`fill_text → fill_text → fill_text`。 [截图](证据/results/screenshots/verdict-small-history-01_text_submit-zh-11.png)
- **FT-MMYXLK-012** `02_password_select/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`fill_password → fill_password → fill_password`。 [截图](证据/results/screenshots/verdict-small-history-02_password_select-zh-11.png)
- **FT-FXKYDXK-012** `03_checkbox_radio/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`radio_default → radio_default → radio_default`。 [截图](证据/results/screenshots/verdict-small-history-03_checkbox_radio-zh-11.png)
- **FT-DTSRK-012** `04_dynamic/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`reveal → reveal → reveal`。 [截图](证据/results/screenshots/verdict-small-history-04_dynamic-zh-11.png)
- **FT-TCQRYQX-012** `05_dialogs/zh`；任务结果未满足；达到实验步数预算仍未满足目标。动作：`cancel_confirm → cancel_confirm → cancel_confirm → cancel_confirm`。 [截图](证据/results/screenshots/verdict-small-history-05_dialogs-zh-11.png)
- **FT-QSYSCL-012** `07_missing/zh`；选择导致执行错误；连续两次动作未改变观测状态，实验停止。动作：`fill_absent → fill_absent`。 错误：StepError: 页面上找不到：选择器 #customer-id, [name=customer-id] [截图](证据/results/screenshots/verdict-small-history-07_missing-zh-11.png)
- **FT-WZBD-012** `08_full_form/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`radio_default → radio_default → radio_default`。 [截图](证据/results/screenshots/verdict-small-history-08_full_form-zh-11.png)

## verdict-small/planned

- **FT-WBSRYTJ-013** `01_text_submit/en`；任务结果未满足；提前提交或提交数据与目标不符。动作：`fill_area → submit`。 [截图](证据/results/screenshots/verdict-small-planned-01_text_submit-en-11.png)
- **FT-FXKYDXK-013** `03_checkbox_radio/en`；任务结果未满足；计划已执行完，但原任务目标尚未满足。动作：`check_default → check_default`。 [截图](证据/results/screenshots/verdict-small-planned-03_checkbox_radio-en-11.png)
- **FT-WZBD-013** `08_full_form/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`fill_area → fill_area → fill_area`。 [截图](证据/results/screenshots/verdict-small-planned-08_full_form-en-11.png)
- **FT-WBSRYTJ-014** `01_text_submit/zh`；任务结果未满足；提前提交或提交数据与目标不符。动作：`fill_area → submit`。 [截图](证据/results/screenshots/verdict-small-planned-01_text_submit-zh-11.png)
- **FT-FXKYDXK-014** `03_checkbox_radio/zh`；任务结果未满足；计划已执行完，但原任务目标尚未满足。动作：`check_default → check_default`。 [截图](证据/results/screenshots/verdict-small-planned-03_checkbox_radio-zh-11.png)
- **FT-TCQRYQX-014** `05_dialogs/zh`；任务结果未满足；尚未按要求取消第一次弹窗就确认跳转。动作：`accept_confirm`。 [截图](证据/results/screenshots/verdict-small-planned-05_dialogs-zh-11.png)
- **FT-WZBD-014** `08_full_form/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`fill_area → fill_area → fill_area`。 [截图](证据/results/screenshots/verdict-small-planned-08_full_form-zh-11.png)

## qwen3.5-0.8b/history

- **FT-MMYXLK-015** `02_password_select/en`；任务结果未满足；提前提交或提交数据与目标不符。动作：`select_two → submit`。 [截图](证据/results/screenshots/qwen3.5-0.8b-history-02_password_select-en-11.png)
- **FT-FXKYDXK-015** `03_checkbox_radio/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`check_default → check_default → check_default`。 [截图](证据/results/screenshots/qwen3.5-0.8b-history-03_checkbox_radio-en-11.png)
- **FT-DTSRK-015** `04_dynamic/en`；选择导致执行错误；连续两次动作未改变观测状态，实验停止。动作：`fill_revealed → fill_revealed`。 错误：TimeoutError: Locator.fill: Timeout 1600ms exceeded. [截图](证据/results/screenshots/qwen3.5-0.8b-history-04_dynamic-en-11.png)
- **FT-TCQRYQX-015** `05_dialogs/en`；任务结果未满足；尚未按要求取消第一次弹窗就确认跳转。动作：`accept_confirm`。 [截图](证据/results/screenshots/qwen3.5-0.8b-history-05_dialogs-en-11.png)
- **FT-WZBD-015** `08_full_form/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`fill_text → fill_text → fill_text`。 [截图](证据/results/screenshots/qwen3.5-0.8b-history-08_full_form-en-11.png)
- **FT-MMYXLK-016** `02_password_select/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`select_two → fill_password → fill_password → fill_password`。 [截图](证据/results/screenshots/qwen3.5-0.8b-history-02_password_select-zh-11.png)
- **FT-FXKYDXK-016** `03_checkbox_radio/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`radio_default → radio_default → radio_default`。 [截图](证据/results/screenshots/qwen3.5-0.8b-history-03_checkbox_radio-zh-11.png)
- **FT-DTSRK-016** `04_dynamic/zh`；选择导致执行错误；连续两次动作未改变观测状态，实验停止。动作：`fill_revealed → fill_revealed`。 错误：TimeoutError: Locator.fill: Timeout 1600ms exceeded. [截图](证据/results/screenshots/qwen3.5-0.8b-history-04_dynamic-zh-11.png)
- **FT-QSYSCL-016** `07_missing/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`fill_text → fill_text → fill_text`。 [截图](证据/results/screenshots/qwen3.5-0.8b-history-07_missing-zh-11.png)
- **FT-WZBD-016** `08_full_form/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`fill_text → fill_text → fill_text`。 [截图](证据/results/screenshots/qwen3.5-0.8b-history-08_full_form-zh-11.png)

## qwen3.5-0.8b/planned

- **FT-FXKYDXK-017** `03_checkbox_radio/en`；任务结果未满足；计划已执行完，但原任务目标尚未满足。动作：`check_default → uncheck_checked`。 [截图](证据/results/screenshots/qwen3.5-0.8b-planned-03_checkbox_radio-en-11.png)
- **FT-TCQRYQX-017** `05_dialogs/en`；任务结果未满足；尚未按要求取消第一次弹窗就确认跳转。动作：`accept_confirm`。 [截图](证据/results/screenshots/qwen3.5-0.8b-planned-05_dialogs-en-11.png)
- **FT-FXKYDXK-018** `03_checkbox_radio/zh`；任务结果未满足；计划已执行完，但原任务目标尚未满足。动作：`check_default → uncheck_checked`。 [截图](证据/results/screenshots/qwen3.5-0.8b-planned-03_checkbox_radio-zh-11.png)
- **FT-TCQRYQX-018** `05_dialogs/zh`；任务结果未满足；尚未按要求取消第一次弹窗就确认跳转。动作：`accept_confirm`。 [截图](证据/results/screenshots/qwen3.5-0.8b-planned-05_dialogs-zh-11.png)

## kev-0.8b/history

- **FT-WBSRYTJ-019** `01_text_submit/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`fill_text → fill_text → fill_text`。 [截图](证据/results/screenshots/kev-0.8b-history-01_text_submit-en-11.png)
- **FT-DTSRK-019** `04_dynamic/en`；选择导致执行错误；连续两次动作未改变观测状态，实验停止。动作：`wait → wait`。 错误：StepError: 等待超时：等待出现 选择器 #revealed [截图](证据/results/screenshots/kev-0.8b-history-04_dynamic-en-11.png)
- **FT-TCQRYQX-019** `05_dialogs/en`；任务结果未满足；尚未按要求取消第一次弹窗就确认跳转。动作：`accept_confirm`。 [截图](证据/results/screenshots/kev-0.8b-history-05_dialogs-en-11.png)
- **FT-WZBD-019** `08_full_form/en`；任务结果未满足；提前提交或提交数据与目标不符。动作：`submit`。 [截图](证据/results/screenshots/kev-0.8b-history-08_full_form-en-11.png)
- **FT-WBSRYTJ-020** `01_text_submit/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`fill_text → fill_text → fill_text`。 [截图](证据/results/screenshots/kev-0.8b-history-01_text_submit-zh-11.png)
- **FT-MMYXLK-020** `02_password_select/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`fill_password → fill_password → fill_password`。 [截图](证据/results/screenshots/kev-0.8b-history-02_password_select-zh-11.png)
- **FT-FXKYDXK-020** `03_checkbox_radio/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`check_default → check_default → check_default`。 [截图](证据/results/screenshots/kev-0.8b-history-03_checkbox_radio-zh-11.png)
- **FT-DTSRK-020** `04_dynamic/zh`；选择导致执行错误；连续两次动作未改变观测状态，实验停止。动作：`wait → wait`。 错误：StepError: 等待超时：等待出现 选择器 #revealed [截图](证据/results/screenshots/kev-0.8b-history-04_dynamic-zh-11.png)
- **FT-TCQRYQX-020** `05_dialogs/zh`；任务结果未满足；达到实验步数预算仍未满足目标。动作：`cancel_confirm → cancel_confirm → cancel_confirm → cancel_confirm`。 [截图](证据/results/screenshots/kev-0.8b-history-05_dialogs-zh-11.png)
- **FT-WZBD-020** `08_full_form/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`fill_text → fill_text → fill_text`。 [截图](证据/results/screenshots/kev-0.8b-history-08_full_form-zh-11.png)

## kev-0.8b/planned

- **FT-FXKYDXK-021** `03_checkbox_radio/en`；任务结果未满足；计划已执行完，但原任务目标尚未满足。动作：`uncheck_checked → radio_default`。 [截图](证据/results/screenshots/kev-0.8b-planned-03_checkbox_radio-en-11.png)
- **FT-FXKYDXK-022** `03_checkbox_radio/zh`；任务结果未满足；计划已执行完，但原任务目标尚未满足。动作：`uncheck_checked → radio_default`。 [截图](证据/results/screenshots/kev-0.8b-planned-03_checkbox_radio-zh-11.png)

## laya-browser/native_dom

- **FT-WBSRYTJ-027** `01_text_submit/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`fill_text → __done__ → __done__`。 [截图](证据/results/screenshots/laya-browser-native_dom-01_text_submit-en-11.png)
- **FT-MMYXLK-027** `02_password_select/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`select_two → __done__ → __done__`。 [截图](证据/results/screenshots/laya-browser-native_dom-02_password_select-en-11.png)
- **FT-FXKYDXK-027** `03_checkbox_radio/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`radio_default → __done__ → __done__`。 [截图](证据/results/screenshots/laya-browser-native_dom-03_checkbox_radio-en-11.png)
- **FT-DTSRK-027** `04_dynamic/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`reveal → __done__ → __done__ → reveal`。 [截图](证据/results/screenshots/laya-browser-native_dom-04_dynamic-en-11.png)
- **FT-TCQRYQX-027** `05_dialogs/en`；任务结果未满足；尚未按要求取消第一次弹窗就确认跳转。动作：`accept_confirm`。 [截图](证据/results/screenshots/laya-browser-native_dom-05_dialogs-en-11.png)
- **FT-QSYSCL-027** `07_missing/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`fill_text → __done__ → __done__`。 [截图](证据/results/screenshots/laya-browser-native_dom-07_missing-en-11.png)
- **FT-WZBD-027** `08_full_form/en`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`select_two → __done__ → __done__`。 [截图](证据/results/screenshots/laya-browser-native_dom-08_full_form-en-11.png)
- **FT-MMYXLK-028** `02_password_select/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`fill_password → select_two → __done__ → __done__`。 [截图](证据/results/screenshots/laya-browser-native_dom-02_password_select-zh-11.png)
- **FT-FXKYDXK-028** `03_checkbox_radio/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`radio_default → __done__ → __done__`。 [截图](证据/results/screenshots/laya-browser-native_dom-03_checkbox_radio-zh-11.png)
- **FT-TCQRYQX-028** `05_dialogs/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`cancel_confirm → __done__ → __done__`。 [截图](证据/results/screenshots/laya-browser-native_dom-05_dialogs-zh-11.png)
- **FT-QSYSCL-028** `07_missing/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`fill_text → __done__ → __done__`。 [截图](证据/results/screenshots/laya-browser-native_dom-07_missing-zh-11.png)
- **FT-WZBD-028** `08_full_form/zh`；任务结果未满足；连续两次动作未改变观测状态，实验停止。动作：`select_two → __done__ → __done__`。 [截图](证据/results/screenshots/laya-browser-native_dom-08_full_form-zh-11.png)

## 产品执行对照

工具控制 16/16；DeepSeek 计划直接执行 16/16；未从这些固定页面测试中确认需修改生产代码的执行器缺陷。
