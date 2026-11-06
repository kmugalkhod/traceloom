"""URL helpers shared by the prompt pages.

Prompt names contain `/`, so routes take them as paths. `{name:promptname}` is a path that is
never the literal `new` (that is the new-prompt editor) and never ends in `/metrics` (that is
the metrics tab), whatever order the routers are included in. A prompt whose name ends in
`/metrics` therefore has no page of its own; it is still reachable through the API.
"""

from __future__ import annotations

from urllib.parse import quote

from starlette.convertors import CONVERTOR_TYPES, StringConvertor


class PromptNameConvertor(StringConvertor):
    regex = r"(?!new$)(?!.*/metrics$)[^/].*"

    def convert(self, value: str) -> str:
        return str(value)

    def to_string(self, value: str) -> str:
        return str(value)


CONVERTOR_TYPES.setdefault("promptname", PromptNameConvertor())


def prompt_url(project_id: str, name: str, suffix: str = "") -> str:
    """`/project/<id>/prompts/<name>[/suffix]` with the name percent-encoded (`/` kept)."""
    return f"/project/{project_id}/prompts/{quote(name, safe='/')}{suffix}"
