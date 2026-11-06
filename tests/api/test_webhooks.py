"""AC-6: signed, queued, retried webhook delivery."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, text

from traceloom.auth.permissions import Role
from traceloom.db.base import utcnow
from traceloom.db.models import AuditLog, Webhook, WebhookDelivery
from traceloom.llm.crypto import SecretBox
from traceloom.llm.ssrf import BlockedAddress
from traceloom.prompts import service as prompts
from traceloom.prompts.service import Actor, NotAllowed
from traceloom.webhooks import deliver, events
from traceloom.webhooks import service as webhooks
from traceloom.webhooks.service import WebhookError, WebhookPolicy

URL = "https://hooks.example.test/in"
PUBLIC = "93.184.216.34"


class Receiver:
    """An httpx transport that replies with scripted statuses and records each request."""

    def __init__(self, *statuses):
        self.statuses = list(statuses)
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.statuses.pop(0) if self.statuses else 200)

    @property
    def transport(self):
        return httpx.MockTransport(self)


@pytest.fixture
def admin(world):
    return Actor.for_user(world.users[Role.ADMIN], Role.ADMIN)


@pytest.fixture
def member(world):
    return Actor.for_user(world.users[Role.MEMBER], Role.MEMBER)


@pytest.fixture
def policy(settings):
    return WebhookPolicy.from_settings(settings, resolver=lambda host, port: [PUBLIC])


@pytest.fixture
def setup(container, db, world, admin, policy):
    container.webhook_resolver = policy.resolver
    hook, secret = webhooks.create(
        db,
        world.project,
        admin,
        policy,
        url=URL,
        event_names=[events.LABEL_MOVED, events.VERSION_CREATED],
    )
    db.commit()
    return hook, secret


def pending(db):
    return db.scalars(select(WebhookDelivery)).all()


def move_label(db, world, admin):
    for text_ in ("one", "two"):
        prompts.create_version(
            db, world.project, admin, name="p", type="text", content=text_, commit_message=text_
        )
    prompts.move_label(db, world.project, admin, "p", "staging", 1)
    db.commit()


def test_retry_then_success_signed(container, db, world, admin, setup):
    hook, secret = setup
    move_label(db, world, admin)
    receiver = Receiver(500, 500, 200)
    delivery = db.scalars(
        select(WebhookDelivery).where(WebhookDelivery.event == events.LABEL_MOVED)
    ).one()
    now = utcnow()
    for _attempt in range(3):
        deliver.handle(
            container, deliver.message(delivery.id), now=now, transport=receiver.transport
        )
        db.refresh(delivery)
        now = now + timedelta(minutes=10)  # past any backoff
    assert (delivery.status, delivery.attempts, delivery.last_status_code) == ("succeeded", 3, 200)
    assert delivery.delivered_at is not None and len(receiver.requests) == 3
    last = receiver.requests[-1]
    body = last.content
    stamp = dict(p.split("=") for p in last.headers["x-traceloom-signature"].split(","))
    expected = hmac.new(secret.encode(), f"{stamp['t']}.".encode() + body, hashlib.sha256)
    assert hmac.compare_digest(stamp["v1"], expected.hexdigest())
    assert last.headers["x-traceloom-delivery"] == delivery.id
    assert last.headers["host"] == "hooks.example.test" and last.url.host == PUBLIC
    payload = json.loads(body)
    assert payload["event"] == events.LABEL_MOVED and payload["data"]["label"] == "staging"


def test_attempts_are_logged_without_secrets(container, db, world, admin, setup, caplog):
    hook, secret = setup
    move_label(db, world, admin)
    delivery = pending(db)[0]
    caplog.set_level(logging.DEBUG)
    deliver.handle(container, deliver.message(delivery.id), transport=Receiver(200).transport)
    lines = [r.getMessage() for r in caplog.records if "attempt" in r.getMessage()]
    assert lines and "attempt 1" in lines[0]
    assert secret not in caplog.text and "v1=" not in caplog.text


def test_gives_up_after_five_attempts(container, db, world, admin, setup):
    move_label(db, world, admin)
    delivery = pending(db)[0]
    receiver, now = Receiver(*[503] * 10), utcnow()
    for _ in range(8):
        deliver.handle(
            container, deliver.message(delivery.id), now=now, transport=receiver.transport
        )
        now += timedelta(hours=1)
    db.refresh(delivery)
    assert (delivery.status, delivery.attempts, len(receiver.requests)) == ("failed", 5, 5)
    assert delivery.last_error == "HTTP 503"


def test_client_errors_and_timeouts(container, db, world, admin, setup):
    move_label(db, world, admin)
    first, second = pending(db)[:2]
    deliver.handle(container, deliver.message(first.id), transport=Receiver(404).transport)
    db.refresh(first)
    assert first.status == "failed" and first.attempts == 1  # a 4xx is not retried

    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    deliver.handle(container, deliver.message(second.id), transport=httpx.MockTransport(boom))
    db.refresh(second)
    assert second.status == "pending" and second.last_error == "timeout"
    assert second.next_attempt_at > utcnow()


def test_duplicate_message_does_not_send_twice(container, db, world, admin, setup):
    move_label(db, world, admin)
    delivery = pending(db)[0]
    receiver = Receiver(500, 200)
    now = utcnow()
    deliver.handle(container, deliver.message(delivery.id), now=now, transport=receiver.transport)
    deliver.handle(container, deliver.message(delivery.id), now=now, transport=receiver.transport)
    assert len(receiver.requests) == 1  # the second message found the retry not yet due


def test_private_url_refused_at_save(db, world, admin, settings):
    loopback = WebhookPolicy.from_settings(settings, resolver=lambda h, p: ["127.0.0.1"])
    with pytest.raises(WebhookError, match="not allowed"):
        webhooks.create(
            db, world.project, admin, loopback, url=URL, event_names=[events.LABEL_MOVED]
        )
    allowed = WebhookPolicy(loopback.box, True, True, loopback.resolver)
    webhooks.create(db, world.project, admin, allowed, url=URL, event_names=[events.LABEL_MOVED])
    metadata = WebhookPolicy(loopback.box, True, True, lambda h, p: ["169.254.169.254"])
    with pytest.raises(WebhookError):
        webhooks.create(
            db, world.project, admin, metadata, url=URL, event_names=[events.LABEL_MOVED]
        )


def test_url_is_checked_again_at_send(container, db, world, admin, setup):
    move_label(db, world, admin)
    delivery = pending(db)[0]
    container.webhook_resolver = lambda h, p: ["10.0.0.9"]  # DNS changed since save
    receiver = Receiver()
    deliver.handle(container, deliver.message(delivery.id), transport=receiver.transport)
    db.refresh(delivery)
    assert delivery.status == "failed" and "blocked" in delivery.last_error
    assert not receiver.requests


@pytest.mark.parametrize("url", ["ftp://x.test/", "javascript:alert(1)", "", "https://"])
def test_bad_urls(db, world, admin, policy, url):
    with pytest.raises((WebhookError, BlockedAddress)):
        webhooks.create(db, world.project, admin, policy, url=url, event_names=[events.LABEL_MOVED])


def test_http_is_refused_in_production(db, world, admin, policy):
    strict = WebhookPolicy(policy.box, False, False, policy.resolver)
    with pytest.raises(WebhookError, match="https"):
        webhooks.create(
            db, world.project, admin, strict, url="http://x.test/", event_names=[events.LABEL_MOVED]
        )


def test_failed_delivery_never_blocks_a_label_move(container, db, world, admin, setup):
    hook, _ = setup
    hook.url = "https://unreachable.invalid/"  # nothing ever listens
    db.commit()
    move_label(db, world, admin)  # commits without any delivery being attempted inline
    assert prompts.get(db, world.project, admin, "p", label="staging").version == 1
    assert pending(db)[0].status == "pending"


def test_pending_deliveries_are_redelivered_after_restart(container, db, world, admin, setup):
    move_label(db, world, admin)
    container.queue.discard()  # the process died with the queue contents
    receiver = Receiver()
    container.webhook_transport = receiver.transport
    container.sweeper.run_once()  # the startup sweep replays due deliveries
    container.queue.drain()
    db.expire_all()
    statuses = {d.status for d in db.scalars(select(WebhookDelivery))}
    assert statuses == {"succeeded"}
    assert len(receiver.requests) == len(pending(db))  # each delivered exactly once


def test_secret_is_encrypted_at_rest_and_shown_once(db, world, admin, policy, settings):
    hook, secret = webhooks.create(
        db, world.project, admin, policy, url=URL, event_names=[events.LABEL_MOVED]
    )
    db.commit()
    stored = db.execute(text("select secret_ciphertext from webhooks")).scalar_one()
    assert secret.startswith("whsec_") and secret not in stored
    assert SecretBox(settings.encryption_key).decrypt(stored) == secret
    detail = json.dumps([a.detail for a in db.scalars(select(AuditLog))])
    assert secret not in detail and stored not in detail


def test_admin_only_audited_crud(db, world, admin, member, policy):
    with pytest.raises(NotAllowed):
        webhooks.create(
            db, world.project, member, policy, url=URL, event_names=[events.LABEL_MOVED]
        )
    hook, _ = webhooks.create(
        db, world.project, admin, policy, url=URL, event_names=[events.LABEL_MOVED]
    )
    webhooks.update(
        db,
        world.project,
        admin,
        policy,
        hook.id,
        enabled=False,
        event_names=[events.VERSION_CREATED],
    )
    assert (hook.enabled, hook.events) == (False, [events.VERSION_CREATED])
    with pytest.raises(WebhookError):
        webhooks.update(db, world.project, admin, policy, hook.id, event_names=["nope"])
    webhooks.delete(db, world.project, admin, hook.id)
    db.commit()
    actions = [a.action for a in db.scalars(select(AuditLog).order_by(AuditLog.created_at))]
    assert [a for a in actions if a.startswith("webhook.")] == [
        "webhook.created",
        "webhook.updated",
        "webhook.deleted",
    ]
    assert db.scalars(select(Webhook)).all() == []


def test_send_test_delivers_a_signed_test_event(container, db, world, admin, setup):
    hook, _ = setup
    delivery = webhooks.send_test(db, world.project, admin, hook.id)
    db.commit()
    receiver = Receiver()
    deliver.handle(container, deliver.message(delivery.id), transport=receiver.transport)
    db.refresh(delivery)
    assert delivery.status == "succeeded" and delivery.event == webhooks.TEST_EVENT
    assert receiver.requests[0].headers["x-traceloom-event"] == webhooks.TEST_EVENT


def test_other_projects_webhook_is_not_found(db, world, admin, policy, make_project):
    other = make_project(world.org)
    hook, _ = webhooks.create(db, other, admin, policy, url=URL, event_names=[events.LABEL_MOVED])
    with pytest.raises(webhooks.WebhookNotFound):
        webhooks.get(db, world.project, hook.id)


# --- review fixes ------------------------------------------------------------------------------


def test_send_test_works_on_a_disabled_webhook(container, db, world, admin, setup, policy):
    hook, _ = setup
    webhooks.update(db, world.project, admin, policy, hook.id, enabled=False)
    delivery = webhooks.send_test(db, world.project, admin, hook.id)
    db.commit()
    receiver = Receiver()
    deliver.handle(container, deliver.message(delivery.id), transport=receiver.transport)
    db.refresh(delivery)
    assert delivery.status == "succeeded" and len(receiver.requests) == 1


def test_real_events_on_a_disabled_webhook_still_fail(container, db, world, admin, setup, policy):
    hook, _ = setup
    move_label(db, world, admin)
    webhooks.update(db, world.project, admin, policy, hook.id, enabled=False)
    db.commit()
    receiver = Receiver()
    delivery = pending(db)[0]
    deliver.handle(container, deliver.message(delivery.id), transport=receiver.transport)
    db.refresh(delivery)
    assert delivery.status == "failed" and receiver.requests == []


@pytest.mark.parametrize("status", [408, 429])
def test_408_and_429_are_retried(container, db, world, admin, setup, status):
    move_label(db, world, admin)
    delivery = pending(db)[0]
    deliver.handle(container, deliver.message(delivery.id), transport=Receiver(status).transport)
    db.refresh(delivery)
    assert delivery.status == "pending" and delivery.attempts == 1
    assert delivery.last_status_code == status and delivery.next_attempt_at is not None


def test_claim_is_atomic_across_sessions(container, db, world, admin, setup):
    """Two workers that both read the row as due: only one of them may claim it."""
    move_label(db, world, admin)
    delivery_id = pending(db)[0].id
    now = utcnow()
    with container.session_factory() as a, container.session_factory() as b:
        a.get(WebhookDelivery, delivery_id), b.get(WebhookDelivery, delivery_id)  # both see it due
        assert deliver.claim(a, delivery_id, now) is True
        a.commit()
        assert deliver.claim(b, delivery_id, now) is False


def test_requests_use_a_short_connect_timeout(container, db, world, admin, setup):
    move_label(db, world, admin)
    receiver = Receiver()
    deliver.handle(container, deliver.message(pending(db)[0].id), transport=receiver.transport)
    timeout = receiver.requests[0].extensions["timeout"]
    assert timeout == {"connect": 3, "read": 10, "write": 10, "pool": 10}


def test_a_sweep_hands_out_a_bounded_number_of_deliveries(container, db, world, admin, setup):
    hook, _ = setup
    for _ in range(deliver.DUE_BATCH + 5):
        webhooks.send_test(db, world.project, admin, hook.id)
    db.commit()
    assert len(deliver.due_messages(container.session_factory)) == deliver.DUE_BATCH
    assert deliver.DUE_BATCH <= 25


def test_a_hanging_webhook_receiver_does_not_delay_ingestion(
    client, container, db, world, admin, setup, make_key
):
    import base64
    import threading
    import time

    release = threading.Event()
    entered = threading.Event()

    def hang(request):
        entered.set()
        release.wait(10)
        return httpx.Response(200)

    container.webhook_transport = httpx.MockTransport(hang)
    try:
        move_label(db, world, admin)
        deliver.kick(container)
        assert entered.wait(5)  # a delivery is now stuck on the slow endpoint
        _, public, secret = make_key(world.project)
        token = base64.b64encode(f"{public}:{secret}".encode()).decode()
        batch = [{"id": "e1", "type": "trace-create", "timestamp": utcnow().isoformat(),
                  "body": {"id": "t-hang", "name": "run"}}]  # fmt: skip
        started = time.monotonic()
        r = client.post(
            "/api/public/ingestion",
            json={"batch": batch},
            headers={"Authorization": f"Basic {token}"},
        )
        assert r.status_code == 207
        while time.monotonic() - started < 3:
            got = container.trace_store.get_existing(world.project.id, {"trace": ["t-hang"]})
            if got["trace"].get("t-hang"):
                break
            time.sleep(0.02)
        assert got["trace"].get("t-hang"), "ingestion waited behind the webhook delivery"
    finally:
        release.set()


@pytest.fixture
def receiver(container, setup):
    got = Receiver()
    container.webhook_transport = got.transport
    return got


def _events_sent(container, receiver):
    container.queue.drain()  # only what was kicked: the sweeper runs once a minute
    return [r.headers["x-traceloom-event"] for r in receiver.requests]


def test_public_api_saves_and_label_moves_deliver_right_away(
    client, container, world, make_key, receiver
):
    import base64

    _, public, secret = make_key(world.project)
    auth = {"Authorization": "Basic " + base64.b64encode(f"{public}:{secret}".encode()).decode()}
    body = {"name": "p", "type": "text", "prompt": "hi", "commitMessage": "c"}
    assert client.post("/api/public/v2/prompts", json=body, headers=auth).status_code == 201
    assert _events_sent(container, receiver) == [events.VERSION_CREATED]
    r = client.patch(
        "/api/public/v2/prompts/p/versions/1", json={"newLabels": ["staging"]}, headers=auth
    )
    assert r.status_code == 200
    assert _events_sent(container, receiver) == [events.VERSION_CREATED, events.LABEL_MOVED]


def test_web_saves_and_label_moves_deliver_right_away(
    client, container, world, login, receiver
):
    web = login(client, world.users[Role.MEMBER])
    project = world.project.id
    r = web.post(
        f"/project/{project}/prompts/new",
        data={"name": "p", "type": "text", "text": "hi", "commit_message": "c"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert _events_sent(container, receiver) == [events.VERSION_CREATED]
    r = web.post(
        f"/project/{project}/prompts/p/labels",
        data={"label": "staging", "version": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert _events_sent(container, receiver)[1:] == [events.LABEL_MOVED]
    r = web.post(
        f"/project/{project}/playground/save",
        json={"mode": "new", "name": "chatp", "commit_message": "from playground",
              "messages": [{"role": "user", "content": "x"}]},  # fmt: skip
    )
    assert r.status_code == 201
    assert _events_sent(container, receiver)[2:] == [events.VERSION_CREATED]
