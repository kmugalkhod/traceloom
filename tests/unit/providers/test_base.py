import pytest

from traceloom.llm import providers
from traceloom.llm.models import DEFAULT_MODELS, models_for
from traceloom.llm.providers import (
    PROVIDERS,
    BaseProvider,
    DoneDelta,
    ErrorDelta,
    ProviderConfig,
    TextDelta,
    UnknownProvider,
    UsageDelta,
    scrub,
)
from traceloom.llm.providers.base import split_system


def test_registry_returns_adapter_by_name(fake_provider):
    assert providers.get_factory("fake") is fake_provider
    provider = providers.create("fake", ProviderConfig(api_key="k"))
    assert provider is fake_provider and provider.config.api_key == "k"


def test_unknown_provider():
    with pytest.raises(UnknownProvider):
        providers.get_factory("nope")


def test_fake_registration_is_removed_after_the_test():
    with pytest.raises(UnknownProvider):
        providers.get_factory("fake")


def test_fake_provider_yields_scripted_deltas(fake_provider):
    fake_provider.script(TextDelta("hel"), TextDelta("lo"), UsageDelta(3, 2), DoneDelta("stop"))
    out = list(fake_provider.stream([{"role": "user", "content": "hi"}], "m", {"temperature": 0}))
    assert out == [TextDelta("hel"), TextDelta("lo"), UsageDelta(3, 2), DoneDelta("stop")]
    assert out[2].total_tokens == 5
    assert fake_provider.calls[0]["model"] == "m" and fake_provider.calls[0]["params"] == {
        "temperature": 0
    }


def test_all_six_providers_have_default_models():
    assert set(PROVIDERS) == set(DEFAULT_MODELS) and len(PROVIDERS) == 6
    assert all(DEFAULT_MODELS[p] for p in PROVIDERS)
    assert models_for("anthropic", ["mine", "claude-sonnet-5-5"])[-1] == "mine"
    assert len(models_for("anthropic", ["claude-sonnet-5-5"])) == len(DEFAULT_MODELS["anthropic"])


class Boom(BaseProvider):
    def _stream(self, *args):
        yield TextDelta("partial")
        raise RuntimeError("401 bad key sk-live-abcdef123456 / hunter2-secret")


def test_errors_become_scrubbed_error_deltas():
    cfg = ProviderConfig(api_key="sk-live-abcdef123456", secrets={"x": "hunter2-secret"})
    out = list(Boom(cfg).stream([], "m"))
    assert out[0] == TextDelta("partial")
    assert isinstance(out[1], ErrorDelta)
    assert "sk-live" not in out[1].message and "hunter2" not in out[1].message
    assert "RuntimeError" in out[1].message


def test_scrub_shapes():
    text = (
        "Authorization: Bearer abc.def-123456 key=AKIAABCDEFGHIJKLMNOP AIzaSyA1234567890abcdefghij"
    )
    out = scrub(text)
    assert not any(s in out for s in ("abc.def", "AKIAABCD", "AIzaSy"))


def test_split_system():
    system, rest = split_system(
        [
            {"role": "system", "content": "a"},
            {"role": "user", "content": "u"},
            {"role": "system", "content": "b"},
        ]
    )
    assert system == "a\n\nb" and rest == [{"role": "user", "content": "u"}]
