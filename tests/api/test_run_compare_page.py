from __future__ import annotations

import re

import pytest

from tests.api.compare_helpers import actor, make_run
from traceloom.auth.permissions import Role


@pytest.fixture
def env(db, world, container, make_dataset, make_dataset_item):
    member = actor(world, Role.MEMBER)
    dataset = make_dataset(world.project)
    items = [
        make_dataset_item(dataset, {"q": f"question-{n}"}, {"a": f"expected-{n}"}) for n in range(5)
    ]

    def mk(name, values):
        return make_run(db, world, container, dataset, items, member, name, values)

    v1 = mk("v1", [0.5] * 5)
    v2 = mk("v2", [0.9, 0.9, 0.9, 0.4, 0.3])  # wins on average; items 3 and 4 regress
    v3 = mk("v3", [0.5, 0.45, 0.5, 0.5, 0.5])
    return dataset, items, (v1, v2, v3)


def page(client, login, world, dataset, query, role=Role.VIEWER):
    login(client, world.users[role], csrf=False)
    return client.get(f"/project/{world.project.id}/datasets/{dataset.id}/compare?{query}")


def row_count(text):
    return len(re.findall(r"<tr data-reg=", text))


def test_best_marker_per_score(client, login, world, env):
    dataset, _, (v1, v2, _) = env
    r = page(client, login, world, dataset, f"runs={v1.id},{v2.id}")
    assert r.status_code == 200
    agg = r.text.split('aria-label="Items"')[0]
    row = re.search(r"<b>correct</b>.*?</tr>", agg, re.S).group(0)
    cells = re.findall(r"<td.*?</td>", row, re.S)  # baseline, v2
    assert "best-c" not in cells[0]
    assert "best-c" in cells[1] and "Best" in cells[1]
    assert "of 5" in agg and "Baseline" in agg


def test_only_regressed_lists_exact_items_and_threshold_hides(client, login, world, env):
    dataset, _, (v1, v2, _) = env
    q = f"runs={v1.id},{v2.id}"
    assert row_count(page(client, login, world, dataset, q).text) == 5
    only = page(client, login, world, dataset, q + "&regressed=1")
    assert row_count(only.text) == 2
    assert "question-3" in only.text and "question-4" in only.text
    assert "question-0" not in only.text
    hi = page(client, login, world, dataset, q + "&regressed=1&threshold=0.15")
    assert row_count(hi.text) == 1 and "question-4" in hi.text
    none = page(client, login, world, dataset, q + "&regressed=1&threshold=0.35")
    assert row_count(none.text) == 0
    assert "No items regressed by more than 0.35" in none.text


def test_sort_by_delta_worst_first(client, login, world, env):
    dataset, _, (v1, v2, _) = env
    r = page(client, login, world, dataset, f"runs={v1.id},{v2.id}&sort=delta")
    order = [int(n) for n in re.findall(r"question-(\d)", r.text)]
    assert order[:2] == [4, 3]


def test_third_run_and_regression_counts(client, login, world, env):
    dataset, _, (v1, v2, v3) = env
    r = page(client, login, world, dataset, f"runs={v1.id},{v2.id},{v3.id}")
    assert re.search(rf'data-regressed="{v2.id}"[^>]*>.*?2 of 5', r.text, re.S)
    assert re.search(rf'data-regressed="{v3.id}"[^>]*>.*?1 of 5', r.text, re.S)


def test_at_most_four_runs(client, login, world, env, db, container):
    dataset, items, (v1, v2, v3) = env
    member = actor(world, Role.MEMBER)
    extra = [
        make_run(db, world, container, dataset, items, member, f"extra{i}", [0.5] * 5)
        for i in range(2)
    ]
    ids = ",".join(r.id for r in (v1, v2, v3, *extra))
    r = page(client, login, world, dataset, f"runs={ids}")
    assert r.status_code == 200
    assert "at most 4 runs" in r.text
    header = r.text.split('aria-label="Aggregates"')[1].split("</thead>")[0]
    assert "extra0" in header and "extra1" not in header


def test_unknown_run_is_not_found(client, login, world, env):
    dataset, *_ = env
    assert page(client, login, world, dataset, "runs=nope").status_code == 404


def test_no_runs_shows_empty_state(client, login, world, env):
    dataset, *_ = env
    assert "Pick runs to compare" in page(client, login, world, dataset, "").text


def test_no_inline_styles_for_csp(client, login, world, env):
    dataset, _, (v1, v2, _) = env
    r = page(client, login, world, dataset, f"runs={v1.id},{v2.id}")
    assert r.status_code == 200
    assert "style=" not in r.text.split("<body", 1)[1]
    assert 'class="dlt up"' in r.text


def test_bare_page_subtitle_counts_the_datasets_runs(client, login, world, env):
    """With no runs picked, the subtitle counts the runs there are to pick from, not 0."""
    dataset, *_ = env
    text = page(client, login, world, dataset, "").text
    sub = re.search(r'<p class="sub">(.*?)</p>', text, re.S).group(1)
    assert "3 runs" in sub and "0 runs" not in sub


def test_picked_runs_subtitle_counts_the_picked_runs(client, login, world, env):
    dataset, _, (v1, v2, _) = env
    text = page(client, login, world, dataset, f"runs={v1.id},{v2.id}").text
    sub = re.search(r'<p class="sub">(.*?)</p>', text, re.S).group(1)
    assert "2 runs" in sub and "baseline is v1" in sub


def test_short_item_text_offers_no_show_more(client, login, world, env):
    """Show more appears only when the item text is long enough to be clamped."""
    dataset, _, (v1, v2, _) = env
    text = page(client, login, world, dataset, f"runs={v1.id},{v2.id}").text
    sticky = re.findall(r'<td class="stick"><p class="inp">.*?</td>', text, re.S)
    assert sticky and not any("data-expand" in td for td in sticky)
