"""T-10 design fidelity: Scores column chips, sortable header and the filter tray Score section."""

from __future__ import annotations

import re

from tests.api.test_trace_list_page import NOW, H, add, ids_in, url
from traceloom.auth.permissions import Role
from traceloom.ports import ScoreFilter, ScoreRow


def _many(container, world):
    scores = [
        ScoreRow(id="a", trace_id="m1", name="quality", value=0.9, timestamp=NOW,
                 source="ANNOTATION", author_id="u"),
        ScoreRow(id="b", trace_id="m1", name="correct", value=1, data_type="BOOLEAN",
                 timestamp=NOW, source="EVAL", execution_id="x"),
        ScoreRow(id="c", trace_id="m1", name="latency_ok", value=0.5, timestamp=NOW),
        ScoreRow(id="d", trace_id="m1", name="tone", string_value="ok",
                 data_type="CATEGORICAL", timestamp=NOW),
        ScoreRow(id="e", trace_id="m1", name="extra", value=2, timestamp=NOW),
    ]  # fmt: skip
    add(container, world.project.id, "m1", scores=scores)


def test_scores_cell_caps_at_three_chips_with_source_icons(client, container, world, login):
    _many(container, world)
    login(client, world.user(Role.VIEWER))
    html = client.get(url(world), headers=H).text
    cell = re.search(r'<td class="c">(.*?)</td>', html, re.S).group(1)
    assert cell.count('<span class="sc ') == 3
    assert '<span class="sc-more"' in cell and "+2" in cell
    assert 'href="#i-user"' in cell and 'href="#i-judge"' in cell and 'href="#i-key"' in cell
    assert '<div class="scs">' in cell


def test_sorted_scores_header_shows_its_direction(client, container, world, login):
    _many(container, world)
    login(client, world.user(Role.VIEWER))
    q = "score=quality%3Aexists&sort=score%3Aquality%3Adesc"
    html = client.get(url(world, q), headers=H).text
    head = re.search(r'<th class="c" aria-sort="descending">.*?</th>', html, re.S).group(0)
    assert 'href="#i-down"' in head
    html = client.get(url(world, "score=quality%3Aexists"), headers=H).text
    head = re.search(r'<th class="c" aria-sort="none">.*?</th>', html, re.S).group(0)
    assert 'href="#i-sort"' in head


def test_tray_score_names_come_from_configs_and_rows_can_be_removed(
    client, container, world, login, make_score_config
):
    make_score_config(world.project, "helpfulness")
    _many(container, world)
    login(client, world.user(Role.MEMBER))
    html = client.get(url(world, "score=quality%3A%3E%3D%3A0.5"), headers=H).text
    tray = html[html.index('id="score-filters"') :]
    tray = tray[: tray.index("</fieldset>")]
    assert '<select class="select" name="sf_name" aria-label="Score name">' in tray
    assert '<option value="helpfulness">helpfulness</option>' in tray
    assert '<option value="quality" selected>quality</option>' in tray
    assert 'aria-label="Remove condition"' in tray
    assert "Add score condition" in tray and "<template" in tray
    assert ">≥</option>" in tray  # operators read as symbols
    assert "All conditions must match" in html


def test_invalid_condition_shows_an_inline_error_by_config_type(
    client, container, world, login, make_score_config
):
    make_score_config(world.project, "helpfulness")
    _many(container, world)
    login(client, world.user(Role.MEMBER))
    html = client.get(url(world, "score=helpfulness%3A%3E%3D%3Ahigh"), headers=H).text
    assert ids_in(html) == ["m1"]  # the bad condition is not applied
    tray = html[html.index('id="score-filters"') :]
    assert 'value="high" aria-invalid="true" aria-describedby="sf-err-0"' in tray
    assert re.search(
        r'<span class="field-error" id="sf-err-0" role="alert">.*helpfulness is numeric\. '
        r"Enter a number like 0\.5\.",
        tray,
        re.S,
    )
    assert "was ignored" not in html


def test_score_filter_accepts_word_operators():
    from traceloom.web.trace_list import parse_score_filter

    assert parse_score_filter("quality:gte:0.5") == ScoreFilter("quality", ">=", 0.5)
    assert parse_score_filter("quality:ne:1") == ScoreFilter("quality", "!=", 1.0)
