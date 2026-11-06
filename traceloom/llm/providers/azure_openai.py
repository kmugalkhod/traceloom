"""Azure OpenAI: the OpenAI adapter on an Azure resource.

`base_url` is the resource endpoint. `config` holds `api_version` (required) and `deployments`,
a map from model name to deployment name (a model without an entry is used as the deployment).
"""

from __future__ import annotations

from typing import Any

from traceloom.llm.providers.base import ProviderConfig
from traceloom.llm.providers.openai import OpenAIProvider


class AzureOpenAIProvider(OpenAIProvider):
    name = "azure_openai"

    def __init__(self, config: ProviderConfig, *, http_client: Any = None) -> None:
        if not config.base_url:
            raise ValueError("Azure OpenAI needs the resource endpoint as its base URL")
        if not config.config.get("api_version"):
            raise ValueError("Azure OpenAI needs an API version")
        super().__init__(config, http_client=http_client)

    def _deployment(self, model: str) -> str:
        return (self.cfg.config.get("deployments") or {}).get(model, model)

    def _make_client(self) -> Any:
        import openai

        return openai.AzureOpenAI(
            azure_endpoint=self.cfg.base_url,
            api_version=self.cfg.config["api_version"],
            **self._client_kwargs(),
        )
