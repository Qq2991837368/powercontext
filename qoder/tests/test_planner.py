"""查询规划测试：领域术语识别、跨语言扩展与变体上限。"""

from __future__ import annotations

from paperlens.planner import find_terms, plan_query


def test_plan_extracts_terms_and_variants() -> None:
    plan = plan_query("HNSW 的 ef 参数如何影响召回率和查询延迟？")
    assert plan.question.startswith("HNSW")
    assert "HNSW" in plan.terms
    assert "召回率" in plan.terms
    assert len(plan.variants) == 3
    assert plan.variants[0] == plan.question
    assert any("graph index" in variant for variant in plan.variants)


def test_plan_variants_never_exceed_three() -> None:
    plan = plan_query("PQ 乘积量化在十亿级内积检索中的压缩率与召回率如何权衡，是否需要精排？")
    assert len(plan.variants) <= 3
    assert len(set(plan.variants)) == len(plan.variants)
    assert "乘积量化" in plan.terms


def test_plan_handles_plain_question() -> None:
    plan = plan_query("向量检索的召回率")
    assert plan.question == "向量检索的召回率"
    assert len(plan.variants) >= 1
    assert all(variant.strip() for variant in plan.variants)


def test_find_terms_keeps_input_order() -> None:
    matched = find_terms("HNSW 用 ef 控制候选队列", ("ef", "HNSW", "PQ"))
    assert matched == ("ef", "HNSW")
