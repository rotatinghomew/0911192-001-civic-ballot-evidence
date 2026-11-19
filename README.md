# 社区微更新偏好的稳健组合决议

社区议事会的意见回收册。旧版项目与当季建设候选并存。
这里的记录全部为合成资料。日期按当地日历解释，未附时区的时间使用东八区。

## 交接资料

CSV 使用 UTF-8 和逗号分隔。值留空表示这条资料未给出该字段，不等于零。
`data/schema.json` 登记表头与跨表编号关系；`data/policy.json` 保存本期业务配置。

- `data/residents.csv`：每行一名虚构居民；eligible 为名册确认标志。 字段：`resident`, `district`, `eligible`。
- `data/projects.csv`：费用单位千元，access 为是否计入无障碍目标；多个依赖用分号。replaces 表示改名或合并的旧提案，旧提案本身不可签。 字段：`project`, `booklet`, `label`, `cost`, `access`, `requires`, `excludes`, `replaces`, `kind`。
- `data/ballots.csv`：同档用分号，档之间用大于号；unknown 为明确未比较。确认件替换整票，不将两份排序拼接。 字段：`receipt`, `resident`, `booklet`, `received`, `state`, `ranked`, `unknown`。
- `data/commitments.csv`：手工承诺按登记生效；撤回同决议号。 字段：`decision`, `project`, `received`, `state`。

## 资料校验

使用 Python 3.11.2 标准库，不需要安装扩展包。资料入口只核查交接格式，不计算业务结果。

```sh
python3 inspect_inputs.py
python3 -m unittest discover -s tests -v
```

## 业务决议（`council` 包）

- `council/core.py`：有效意见认定、联审映射、成对偏好、可行组合与组合比较。
- `council/workbench.py`：工作台报告；`public_report` 生成无票-个人对应关系的公开版。
- `council/report.py`：命令行入口。

```sh
python3 -m council.report            # 内部工作稿 JSON（含票册编号，供存档核对）
python3 -m council.report --public   # 公开版 JSON（仅片区级聚合，匿名剔除原因）
python3 -m council.report --text     # 紧凑文本摘要
```

### 落实的议事规则

1. **有效意见唯一**：每名资格居民以议事期间最后确认的整份意见为准，确认件不与旧排序拼接；
   代录未确认件不覆盖原意见；逾期票与不具资格居民的票册剔除并记录原因。
2. **权重不重分**：片区代表性权重按预定值全额计入；旧票只回答部分内容时，该居民权重保留，
   不转移给回答完整的人。
3. **改名与合并分口径**：改名（seat→bench）承接原顺位；实质合并（ramp/light→path）不继承
   整包支持，旧票对合并方案的比较记为「口径不同」，旧表明确比较过的内容仅作留痕。
4. **未知不等于反对**：票面未比较或明确标注 unknown 的两两关系一律计入未知，汇总时区分
   `robust_win/robust_loss`（最坏未知补足后仍领先/落后）、`contestable`（可被合规补足翻转）
   与 `incomparable`（无同口径证据）。
5. **可行组合四道闸**：费用上限、成套依赖、工程互斥、无障碍覆盖；已签约未撤回的承诺强制
   入选并占用预算，撤回的承诺释放预算；每个被否组合保留全部暂缓理由。
6. **组合比较口径**：只比较两个组合各自独有组成项的最佳明确偏好，同档并列不加票，每个居民
   每次比较只用一次权重；包含关系标记为结构问题，不伪装成偏好胜负。
7. **结论可重审**：证据无法区分时保留全部竞争组合与最能帮助确认的提问范围，不指定单一排序；
   新增确认意见后重新计算；公开报告不出现居民编号与票册编号。

