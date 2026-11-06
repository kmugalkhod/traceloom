import json

import pytest

from tests.unit.providers.openai_helpers import (
    EXPECTED_MESSAGES,
    EXPECTED_TOOLS,
    MESSAGES,
    PARAMS,
    SCHEMA,
    TOOLS,
    stream_reply,
)
from traceloom.llm import providers
from traceloom.llm.providers import DoneDelta, ErrorDelta, ProviderConfig, TextDelta
from traceloom.llm.providers.openai_compatible import OpenAICompatibleProvider
from traceloom.llm.ssrf import BlockedAddress

KEY = "gateway-token-0123456789"
HEADER_SECRET = "tenant-secret-abcdef"


def resolver(*addresses):
    return lambda host, port: list(addresses)


def make(
    openai_script, base_url="http://gateway.test/v1", allow_private=True, ips=("93.184.216.34",)
):
    cfg = ProviderConfig(
        api_key=KEY,
        base_url=base_url,
        config={"headers": {"X-Tenant": HEADER_SECRET}},
        allow_private=allow_private,
    )
    return OpenAICompatibleProvider(cfg, http_client=openai_script.http(), resolver=resolver(*ips))


def test_request_mapping_and_custom_headers(openai_script):
    provider = make(openai_script)
    openai_script.reply(**stream_reply())
    list(provider.stream(MESSAGES, "llama3.3", PARAMS, TOOLS, SCHEMA))
    request = openai_script.requests[-1]
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["x-tenant"] == HEADER_SECRET
    body = json.loads(request.content)
    assert body["model"] == "llama3.3" and body["max_tokens"] == 300
    assert "max_completion_tokens" not in body
    assert body["messages"] == EXPECTED_MESSAGES and body["tools"] == EXPECTED_TOOLS


def test_streams_and_reports_usage(openai_script):
    provider = make(openai_script)
    openai_script.reply(**stream_reply())
    out = list(provider.stream([{"role": "user", "content": "hi"}], "m"))
    assert (
        out[0] == TextDelta("Hel")
        and out[-2].total_tokens == 18
        and out[-1] == DoneDelta("tool_calls")
    )


def test_errors_scrub_key_and_header_values(openai_script):
    provider = make(openai_script)
    openai_script.reply(status=403, json={"error": {"message": f"denied {KEY} {HEADER_SECRET}"}})
    out = list(provider.stream([{"role": "user", "content": "hi"}], "m"))
    assert isinstance(out[0], ErrorDelta) and "denied" in out[0].message
    assert KEY not in out[0].message and HEADER_SECRET not in out[0].message


@pytest.mark.parametrize("url", ["http://169.254.169.254/v1", "http://metadata.test/v1"])
def test_metadata_address_is_refused(openai_script, url):
    with pytest.raises(BlockedAddress):
        make(openai_script, base_url=url, ips=("169.254.169.254",))


def test_private_refused_when_policy_says_so(openai_script):
    with pytest.raises(BlockedAddress):
        make(openai_script, allow_private=False, ips=("10.0.0.5",))
    make(openai_script, allow_private=True, ips=("10.0.0.5",))


def test_dns_rebinding_is_caught_at_stream_time(openai_script):
    answers = [["93.184.216.34"], ["169.254.169.254"]]
    cfg = ProviderConfig(api_key=KEY, base_url="http://gateway.test/v1")
    provider = OpenAICompatibleProvider(
        cfg, http_client=openai_script.http(), resolver=lambda h, p: answers.pop(0)
    )
    out = list(provider.stream([{"role": "user", "content": "hi"}], "m"))
    assert isinstance(out[0], ErrorDelta) and not openai_script.requests


def test_base_url_is_required():
    with pytest.raises(ValueError):
        OpenAICompatibleProvider(ProviderConfig(api_key="k"))


def test_registered_by_name():
    assert providers.get_factory("openai_compatible") is OpenAICompatibleProvider
