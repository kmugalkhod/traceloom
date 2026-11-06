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
from traceloom.llm.providers.azure_openai import AzureOpenAIProvider

KEY = "azure-key-0123456789abcdef"


def config(**extra):
    return ProviderConfig(
        api_key=KEY,
        base_url="http://myres.openai.azure.test",
        config={
            "api_version": "2025-04-01-preview",
            "deployments": {"gpt-4o": "prod-gpt4o"},
            **extra,
        },
    )


@pytest.fixture
def provider(openai_script):
    return AzureOpenAIProvider(config(), http_client=openai_script.http())


def test_uses_the_deployment_and_api_version(provider, openai_script):
    openai_script.reply(**stream_reply())
    list(provider.stream(MESSAGES, "gpt-4o", PARAMS, TOOLS, SCHEMA))
    request = openai_script.requests[-1]
    assert request.url.path == "/openai/deployments/prod-gpt4o/chat/completions"
    assert request.url.params["api-version"] == "2025-04-01-preview"
    assert request.headers["api-key"] == KEY
    body = json.loads(request.content)
    assert body["messages"] == EXPECTED_MESSAGES and body["tools"] == EXPECTED_TOOLS
    assert body["max_completion_tokens"] == 300 and body["stream"] is True


def test_unmapped_model_is_its_own_deployment(provider, openai_script):
    openai_script.reply(**stream_reply())
    list(provider.stream([{"role": "user", "content": "x"}], "my-deploy"))
    assert openai_script.requests[-1].url.path == "/openai/deployments/my-deploy/chat/completions"


def test_streams_and_reports_usage(provider, openai_script):
    openai_script.reply(**stream_reply())
    out = list(provider.stream([{"role": "user", "content": "hi"}], "gpt-4o"))
    assert (
        out[0] == TextDelta("Hel")
        and out[-2].total_tokens == 18
        and out[-1] == DoneDelta("tool_calls")
    )


def test_errors_are_scrubbed(provider, openai_script):
    openai_script.reply(status=401, json={"error": {"message": f"Access denied for {KEY}"}})
    out = list(provider.stream([{"role": "user", "content": "hi"}], "gpt-4o"))
    assert isinstance(out[0], ErrorDelta) and "Access denied" in out[0].message
    assert KEY not in out[0].message


@pytest.mark.parametrize(
    "cfg",
    [
        ProviderConfig(api_key="k", config={"api_version": "v"}),
        ProviderConfig(api_key="k", base_url="http://x.test"),
    ],
)
def test_endpoint_and_version_are_required(cfg):
    with pytest.raises(ValueError):
        AzureOpenAIProvider(cfg)


def test_registered_by_name():
    assert providers.get_factory("azure_openai") is AzureOpenAIProvider
