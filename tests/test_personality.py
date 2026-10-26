from __future__ import annotations

from services.personality import (
    PERSONAS,
    PERSONA_PROMPTS,
    affection_level,
    growth_stage,
    persona_prompt,
)


def test_three_personas_have_distinct_prompts():
    assert set(PERSONA_PROMPTS) == set(PERSONAS)
    assert len(set(PERSONA_PROMPTS.values())) == 3
    assert "贪吃" in persona_prompt("标准贪吃鲸娘")
    assert "主动" in persona_prompt("活力鲸娘")
    assert "克制" in persona_prompt("安静鲸娘")


def test_affection_levels_and_growth_unlocks():
    assert [affection_level(value) for value in (0, 20, 40, 60, 80)] == [
        "陌生",
        "熟悉",
        "亲近",
        "喜爱",
        "专属",
    ]
    assert growth_stage(49).level == 4
    assert growth_stage(50).title == "小鲸"
    assert growth_stage(499).level == 49
    assert growth_stage(500).level == 50
    assert growth_stage(500).title == "👑"
