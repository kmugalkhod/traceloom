"""Score names and category values are user content: the analytics page renders them escaped."""

from __future__ import annotations

from datetime import timedelta

from tests.api.test_score_analytics_page import NOW, add, page, paired_bools
from traceloom.auth.permissions import Role

XSS = "<script>alert(1)</script>"
IMG = '"><img src=x onerror=alert(1)>'


def test_score_name_is_escaped_everywhere(client, login, world, db):
    paired_bools(db, world, (17, 3, 4, 6), name=XSS)
    for i in range(12):
        add(
            db,
            world,
            f"n{i}",
            XSS + "num",
            f"nn{i}",
            "ANNOTATION",
            value=0.5,
            ago=timedelta(days=i % 3),
        )
    db.commit()
    for query in ("", f"?score={XSS}", f"?a={XSS}&as=ANNOTATION&b={XSS}&bs=EVAL"):
        text = page(client, login, world, query, role=Role.VIEWER).text
        assert "<script>alert(1)</script>" not in text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text


def test_attribute_breakout_is_escaped(client, login, world, db):
    paired_bools(db, world, (17, 3, 4, 6), name=IMG)
    text = page(client, login, world, f"?score={IMG}").text
    assert "<img src=x" not in text
    assert 'name="score" value="&#34;&gt;&lt;img' in text or "&#34;&gt;&lt;img" in text


def test_category_values_and_chart_json_are_escaped(client, login, world, db):
    for i in range(12):
        add(db, world, f"c{i}", "tone", f"c{i}", "ANNOTATION", text=XSS, kind="CATEGORICAL")
        add(db, world, f"v{i}", "helpfulness", f"c{i}", "EVAL", value=0.7, ago=timedelta(hours=i))
    db.commit()
    text = page(client, login, world, "?a=tone&as=ANNOTATION&b=helpfulness&bs=EVAL").text
    assert "<script>alert(1)</script>" not in text
    assert NOW  # seeded relative to now
