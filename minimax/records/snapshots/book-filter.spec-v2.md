# 图书过滤器规格（Book Filter Specification）

Version: v2
Status: active
Owner: catalogue-team

## R1 关键词匹配

`filter_books(books, keyword=...)` 返回 `title` 中包含关键词的图书，大小写不敏感；
`keyword` 为 `None` 或空字符串时不做过滤。结果保持输入顺序。

## R2 最高价格（包含边界）

传入 `max_price` 时，仅保留 `price <= max_price` 的图书，边界值必须包含。
`price` 缺失或不是数字的图书在启用该过滤时一律排除。

## R3 标签全匹配

传入 `tags` 时，图书必须包含全部请求标签（all-of 语义）。
`tags` 为 `None` 或空序列时不做过滤；缺少 `tags` 字段的图书在启用该过滤时排除。

## R4 库存可见性

默认排除 `in_stock` 显式为 `False` 的图书；`in_stock` 缺失视为有货。
调用方传入 `include_out_of_stock=True` 时保留缺货图书。

## R5 最低价格（包含边界，v2 新增）

传入 `min_price` 时，仅保留 `price >= min_price` 的图书，边界值必须包含。
`price` 缺失或不是数字的图书在启用该过滤时一律排除。
`min_price` 与 `max_price` 同时存在时构成闭区间 `[min_price, max_price]`，
闭区间内没有图书时返回空列表。

## N1 通用约束

- 不修改输入对象，返回结果为浅拷贝（保留调用方附加字段）。
- 结果顺序与输入顺序一致。
- 不修改测试与规格文件来“适配”实现；实现必须满足上述规则。
