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
python3 -m unittest -v
```
