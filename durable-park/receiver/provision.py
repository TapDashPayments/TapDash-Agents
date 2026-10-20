#!/usr/bin/env python3
"""Provision the withHuman AAP instance for this receiver (run once, as root).

    sudo /opt/durable-park/bin/python /opt/durable-park/receiver/provision.py \
        --provisioner-token-file /etc/durable-park/provisioner_token \
        --url https://proto-agents.taila95862.ts.net/aap/events --name proto-agents-1

withHuman verifies the receiver BEFORE it creates the instance: it POSTs a signed
``webhook.verification`` to ``--url`` and needs the challenge echoed within 10 s, which the live
``durable-park-receiver`` service can only do once it holds the signing secret and registration id.
So, in order:

1. write a *pending* ``receiver.json`` (0600 aaprecv): new ``whsec_`` signing secret, registration id,
   idempotency key, url, name; restart the receiver so it loads them;
2. self-test: POST our own signed verification message to the local and the public URL and expect the
   challenge back (nothing is sent to withHuman if this fails);
3. ``Receiver.provision`` (SDK): ``POST {aap}/v1/instances`` with the provisioner token, the
   ``Idempotency-Key`` and ``delivery = {webhook, url, registration_id, signing_secret}``;
4. write the final ``receiver.json`` (+ ``instance_id`` and the receiver's copy of the instance token),
   then ``instance_token`` (0640 root:hermes), restart the receiver and self-test it again.

Prints only the instance id. Idempotent: a pending registration is reused as is (same secret, same
registration id, same idempotency key, so withHuman replays the original result instead of creating a
second instance), and a finished one is never re-provisioned (``instance_token`` is repaired from
receiver.json if it went missing; that needs no provisioner token, so its file can be deleted after the
first success). Secrets are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import grp
import json
import os
import pwd
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from agentapprovalprotocol import ProtocolError, ProvisionError, Receiver, WebhookVerificationFailed
from agentapprovalprotocol.receiver import new_secret, sign

DEFAULT_ETC_DIR = "/etc/durable-park"
DEFAULT_AAP_BASE_URL = "https://app.withhuman.ai/api/aap"
DEFAULT_LOCAL_URL = "http://127.0.0.1:8790/aap/events"
SERVICE = "durable-park-receiver"
RECEIVER_OWNER = ("aaprecv", "aaprecv")
TOKEN_OWNER = ("root", "hermes")
SELF_TEST_ATTEMPTS = 20
_IDENTITY = ("url", "instance_name", "aap_base_url")


class Refused(Exception):
    """Exit 2: wrong invocation or state; nothing was sent to withHuman."""


class Failed(Exception):
    """Exit 1: a step failed; re-running is safe."""


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Provision the withHuman AAP instance for durable-park's receiver")
    p.add_argument("--provisioner-token-file",
                   help="file holding the whp_ provisioner token; required to provision, not needed once "
                        "provisioned (a re-run then only repairs the files and never calls withHuman)")
    p.add_argument("--url", required=True, help="public HTTPS URL of the receiver (Tailscale Funnel)")
    p.add_argument("--name", required=True, help="instance name, e.g. proto-agents-1")
    p.add_argument("--aap-base-url", default=DEFAULT_AAP_BASE_URL)
    p.add_argument("--etc-dir", default=DEFAULT_ETC_DIR, help="where receiver.json and instance_token live")
    p.add_argument("--local-url", default=DEFAULT_LOCAL_URL, help="the receiver's loopback URL (self-test)")
    p.add_argument("--skip-public-check", action="store_true",
                   help="self-test only the local URL (when the public URL is not reachable from this host)")
    p.add_argument("--new-registration", action="store_true",
                   help="discard an unfinished registration made for another url/name and start a new one")
    return p.parse_args(argv)


def _err(message: str) -> None:
    print(f"provision: {message}", file=sys.stderr)


def _read_provisioner_token(path: str) -> str:
    try:
        raw = Path(path).read_text()
    except (OSError, UnicodeError) as exc:
        raise Refused(f"cannot read the provisioner token file {path} ({type(exc).__name__})") from None
    token = raw.strip()
    if not token or len(token.split()) != 1:
        raise Refused(f"the provisioner token file {path} must hold exactly one token on one line")
    return token


def _load_receiver_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise Refused(f"{path} is unreadable ({type(exc).__name__}); move it away to start over") from None
    if not isinstance(data, dict) or not isinstance(data.get("signing_secret"), str):
        raise Refused(f"{path} is not a durable-park receiver.json; move it away to start over")
    return data


def _write(path: Path, data: str, mode: int, owner: tuple[str, str], chown) -> None:
    """Atomically replace *path* (temp file in the same directory, fsync, rename), then set its owner."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.fchmod(fd, mode)
        os.write(fd, data.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    chown(path, *owner)


def _default_chown(path: Path, user: str, group: str) -> None:
    os.chown(path, pwd.getpwnam(user).pw_uid, grp.getgrnam(group).gr_gid)


def _default_restart() -> None:
    subprocess.run(["systemctl", "restart", SERVICE], check=True, stdout=subprocess.DEVNULL)


async def self_test(http: httpx.AsyncClient, url: str, secret: str, registration_id: str, *,
                    attempts: int = SELF_TEST_ATTEMPTS, delay: float = 0.5) -> bool:
    """POST our own signed ``webhook.verification`` to *url*; True when the challenge comes back."""
    for attempt in range(attempts):
        message_id, challenge = f"selftest-{uuid.uuid4()}", str(uuid.uuid4())
        body = json.dumps({"id": message_id, "type": "webhook.verification", "registration_id": registration_id,
                           "instance_id": "00000000-0000-0000-0000-000000000000",
                           "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "challenge": challenge}).encode()
        stamp = int(time.time())
        headers = {"content-type": "application/json", "webhook-id": message_id, "webhook-timestamp": str(stamp),
                   "webhook-signature": sign(secret, message_id, stamp, body)}
        try:
            response = await http.post(url, headers=headers, content=body, timeout=10.0)
            if response.status_code == 200 and response.json() == {"challenge": challenge}:
                return True
        except (httpx.HTTPError, ValueError):
            pass
        if attempt + 1 < attempts:
            await asyncio.sleep(delay)
    return False


def _token_file_text(token: str) -> str:
    return token + "\n"


def _repair(final: dict, etc: Path, args, chown) -> str:
    """Already provisioned: never call withHuman again; make sure both files are in place."""
    for key, value in (("url", args.url), ("instance_name", args.name), ("aap_base_url", args.aap_base_url)):
        if final.get(key) != value:
            raise Refused(f"already provisioned as instance {final['instance_id']} with a different {key}; "
                          "refusing to provision again (move receiver.json away to start over)")
    token = final.get("instance_token")
    if not isinstance(token, str) or not token:
        raise Refused("receiver.json has an instance_id but no instance_token; move it away to start over")
    receiver_json = etc / "receiver.json"
    os.chmod(receiver_json, 0o600)
    chown(receiver_json, *RECEIVER_OWNER)
    token_file = etc / "instance_token"
    try:
        current = token_file.read_text()
    except FileNotFoundError:
        current = None
    if current != _token_file_text(token):
        _write(token_file, _token_file_text(token), 0o640, TOKEN_OWNER, chown)
    else:
        os.chmod(token_file, 0o640)
        chown(token_file, *TOKEN_OWNER)
    return final["instance_id"]


async def run(args, *, etc_dir=None, chown=_default_chown, restart=_default_restart, receiver_http=None,
              provider_http=None, retry_delay: float = 0.5) -> int:
    etc = Path(etc_dir or args.etc_dir)
    own_receiver_http = receiver_http is None
    if own_receiver_http:  # loopback + our own public URL only: no env proxies, no redirects
        receiver_http = httpx.AsyncClient(trust_env=False, follow_redirects=False)
    try:
        instance_id = await _provision(args, etc, chown, restart, receiver_http, provider_http, retry_delay)
    except Refused as exc:
        _err(str(exc))
        return 2
    except Failed as exc:
        _err(str(exc))
        return 1
    finally:
        if own_receiver_http:
            await receiver_http.aclose()
    print(instance_id)
    return 0


async def _provision(args, etc: Path, chown, restart, receiver_http, provider_http, retry_delay) -> str:
    if not args.url.startswith("https://"):
        raise Refused("--url must be the receiver's public https:// URL")
    receiver_json = etc / "receiver.json"
    existing = _load_receiver_json(receiver_json)
    if existing is not None and existing.get("instance_id"):
        return _repair(existing, etc, args, chown)  # no withHuman call: the provisioner token is not needed
    if not args.provisioner_token_file:
        raise Refused("not provisioned yet: --provisioner-token-file is required")
    provisioner_token = _read_provisioner_token(args.provisioner_token_file)

    identity = {"url": args.url, "instance_name": args.name, "aap_base_url": args.aap_base_url}
    if existing is not None and not args.new_registration:
        changed = [key for key in _IDENTITY if existing.get(key) != identity[key]]
        if changed:
            raise Refused(f"an unfinished registration for another {', '.join(changed)} exists in {receiver_json}; "
                          "re-run with the same values, or pass --new-registration to discard it")
        pending = existing
    else:
        registration_id = str(uuid.uuid4())
        pending = {"signing_secret": new_secret(), "registration_id": registration_id,
                   "idempotency_key": registration_id, **identity}
    pending = {**pending, "instance_id": None, "instance_token": None, "expires_at": None}
    _write(receiver_json, json.dumps(pending, indent=2) + "\n", 0o600, RECEIVER_OWNER, chown)

    # The live receiver must answer withHuman's challenge with this secret before we ask for the instance.
    try:
        restart()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise Failed(f"could not restart {SERVICE} ({type(exc).__name__})") from None
    secret, registration_id = pending["signing_secret"], pending["registration_id"]
    if not await self_test(receiver_http, args.local_url, secret, registration_id, delay=retry_delay):
        raise Failed(f"the receiver did not answer the self-test at {args.local_url}; "
                     f"check `systemctl status {SERVICE}`. Nothing was sent to withHuman")
    if not args.skip_public_check and not await self_test(receiver_http, args.url, secret, registration_id,
                                                          delay=retry_delay):
        raise Failed(f"the receiver did not answer the self-test at {args.url}; check `tailscale funnel status` "
                     "(or pass --skip-public-check if this host cannot reach its own public URL). "
                     "Nothing was sent to withHuman")

    receiver = Receiver(resumers={}, secret=secret, registration_id=registration_id)
    try:
        provisioned = await receiver.provision(provider_url=args.aap_base_url, provisioner_token=provisioner_token,
                                               url=args.url, instance_name=args.name,
                                               idempotency_key=pending["idempotency_key"], http=provider_http)
    except WebhookVerificationFailed:
        raise Failed(f"withHuman could not verify the receiver at {args.url} (HTTP 422 webhook_verification_failed); "
                     "the pending registration is kept: fix the receiver/Funnel and re-run "
                     "(same idempotency key, no duplicate instance)") from None
    except ProvisionError as exc:  # message carries only the HTTP status and error code
        raise Failed(f"{exc}; the pending registration is kept, re-run to retry") from None
    except (ProtocolError, ValueError) as exc:
        raise Failed(f"provisioning failed: invalid answer or input ({type(exc).__name__})") from None

    # instance_name stays the name we asked for (our identity for re-runs); withHuman's spelling is kept apart.
    final = {**pending, "instance_id": provisioned.instance_id, "provider_instance_name": provisioned.instance_name,
             "instance_token": provisioned.token, "expires_at": provisioned.expires_at,
             "provisioned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    _write(receiver_json, json.dumps(final, indent=2) + "\n", 0o600, RECEIVER_OWNER, chown)
    _write(etc / "instance_token", _token_file_text(provisioned.token), 0o640, TOKEN_OWNER, chown)
    try:
        restart()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise Failed(f"provisioned instance {provisioned.instance_id}, but could not restart {SERVICE} "
                     f"({type(exc).__name__}); restart it by hand") from None
    if not await self_test(receiver_http, args.local_url, secret, registration_id, delay=retry_delay):
        raise Failed(f"provisioned instance {provisioned.instance_id}, but the restarted receiver did not answer "
                     f"the self-test; check `systemctl status {SERVICE}`")
    return provisioned.instance_id


def main(argv=None) -> int:
    args = parse_args(argv)
    if os.geteuid() != 0:
        _err("must run as root (sudo): it writes /etc/durable-park and restarts the receiver")
        return 2
    os.umask(0o077)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
