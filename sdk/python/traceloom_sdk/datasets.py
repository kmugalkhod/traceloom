"""Datasets for the SDK: `get_dataset`, `Dataset`, `DatasetItem` and create helpers.

Unlike tracing calls, these raise on failure (`DatasetError`): the caller asked for data and
has nothing to run without it. Only the client's own httpx client is used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    from traceloom_sdk.client import Traceloom

DATASETS_PATH = "/api/public/datasets"
RUN_ITEMS_PATH = "/api/public/dataset-run-items"
PAGE_SIZE = 100


class DatasetError(Exception):
    """A datasets API call failed."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class DatasetItem:
    def __init__(
        self,
        id: str,
        input: Any = None,
        expected_output: Any = None,
        metadata: Any = None,
        version: int | None = None,
        status: str = "ACTIVE",
        source_trace_id: str | None = None,
        source_observation_id: str | None = None,
    ) -> None:
        self.id = id
        self.input = input
        self.expected_output = expected_output
        self.metadata = metadata
        self.version = version
        self.status = status
        self.source_trace_id = source_trace_id
        self.source_observation_id = source_observation_id

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DatasetItem:
        return cls(
            data["id"],
            data.get("input"),
            data.get("expectedOutput"),
            data.get("metadata"),
            data.get("version"),
            data.get("status", "ACTIVE"),
            data.get("sourceTraceId"),
            data.get("sourceObservationId"),
        )

    def __repr__(self) -> str:
        return f"<DatasetItem {self.id}>"


class Dataset:
    def __init__(
        self,
        id: str,
        name: str,
        description: str = "",
        metadata: Any = None,
        items: list[DatasetItem] | None = None,
        client: Traceloom | None = None,
    ) -> None:
        self.id = id
        self.name = name
        self.description = description
        self.metadata = metadata
        self.items = items or []
        self._client = client

    def __repr__(self) -> str:
        return f"<Dataset {self.name!r} {len(self.items)} items>"


def resolve_client(client: Traceloom | None) -> Traceloom:
    if client is not None:
        return client
    from traceloom_sdk.client import get_client

    return get_client()


def request(
    client: Traceloom, method: str, path: str, *, json: Any = None, params: Any = None
) -> Any:
    """One call to the public API; raises `DatasetError` on a network error or non-2xx reply."""
    try:
        response = client._client().request(method, client.host + path, json=json, params=params)
    except httpx.HTTPError as exc:
        raise DatasetError(f"{method} {path} failed: {exc}") from exc
    if response.status_code >= 300:
        raise DatasetError(
            f"{method} {path} returned {response.status_code}: {response.text}",
            response.status_code,
        )
    return response.json()


def get_dataset(name: str, client: Traceloom | None = None) -> Dataset:
    """Fetch a dataset and all its active items (latest versions)."""
    client = resolve_client(client)
    base = f"{DATASETS_PATH}/{quote(name, safe='')}"
    head = request(client, "GET", base)
    items: list[DatasetItem] = []
    page = 1
    while True:
        body = request(client, "GET", f"{base}/items", params={"page": page, "limit": PAGE_SIZE})
        rows = body.get("data", [])
        items.extend(DatasetItem.from_dict(r) for r in rows)
        if len(rows) < PAGE_SIZE:
            break
        page += 1
    return Dataset(
        head["id"], head["name"], head.get("description", ""), head.get("metadata"), items, client
    )


def create_dataset(
    name: str,
    description: str = "",
    metadata: dict[str, Any] | None = None,
    client: Traceloom | None = None,
) -> Dataset:
    client = resolve_client(client)
    body = request(
        client,
        "POST",
        DATASETS_PATH,
        json={"name": name, "description": description, "metadata": metadata},
    )
    return Dataset(
        body["id"], body["name"], body.get("description", ""), body.get("metadata"), client=client
    )


def create_item(
    dataset: Dataset | str,
    input: Any = None,
    expected_output: Any = None,
    metadata: Any = None,
    source_trace_id: str | None = None,
    source_observation_id: str | None = None,
    client: Traceloom | None = None,
) -> DatasetItem:
    name = dataset.name if isinstance(dataset, Dataset) else dataset
    if client is None and isinstance(dataset, Dataset):
        client = dataset._client
    client = resolve_client(client)
    body = request(
        client,
        "POST",
        f"{DATASETS_PATH}/{quote(name, safe='')}/items",
        json={
            "input": input,
            "expectedOutput": expected_output,
            "metadata": metadata,
            "sourceTraceId": source_trace_id,
            "sourceObservationId": source_observation_id,
        },
    )
    item = DatasetItem.from_dict(body)
    if isinstance(dataset, Dataset):
        dataset.items.append(item)
    return item
