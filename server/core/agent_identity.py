"""Verified agent identity: signed requests in the Web Bot Auth profile of HTTP Message Signatures.

An agent signs its requests with an Ed25519 key (RFC 9421, `tag="web-bot-auth"`, the scheme
behind Cloudflare's Web Bot Auth). The gateway checks the signature against a directory of
known agent keys. A verified agent gets a stable identity, so its history follows it:

- an agent with a clean record pays a smaller deposit,
- an agent whose deposit was kept for breaking the rules is refused,
- an unsigned or unknown agent pays the full deposit (and the site can require signatures).

A rogue operator can still make a new unverified identity per visit, but it then always pays
the full price and never earns the discount; a known identity cannot shed its record.

Signed request headers::

    Signature-Input: sig1=("@method" "@authority" "@path");created=1731000000;expires=1731000300;keyid="<JWK thumbprint>";alg="ed25519";tag="web-bot-auth"
    Signature: sig1=:<base64 signature>:
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

TAG = "web-bot-auth"
COMPONENTS = ("@method", "@authority", "@path")
MAX_VALIDITY_SECONDS = 600
CLOCK_SKEW_SECONDS = 60


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def public_jwk(key: Ed25519PublicKey) -> dict[str, str]:
    return {"kty": "OKP", "crv": "Ed25519", "x": _b64url(key.public_bytes(Encoding.Raw, PublicFormat.Raw))}


def jwk_thumbprint(jwk: Mapping[str, str]) -> str:
    """RFC 7638 thumbprint of an OKP key: SHA-256 over the canonical required members."""
    canonical = json.dumps({"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"]}, separators=(",", ":"), sort_keys=True)
    return _b64url(hashlib.sha256(canonical.encode()).digest())


def _component_value(name: str, method: str, authority: str, path: str) -> str:
    return {"@method": method.upper(), "@authority": authority.lower(), "@path": path}[name]


def signature_base(params: str, components: tuple[str, ...], method: str, authority: str, path: str) -> bytes:
    lines = [f'"{c}": {_component_value(c, method, authority, path)}' for c in components]
    lines.append(f'"@signature-params": {params}')
    return "\n".join(lines).encode()


def sign_request(private_key: Ed25519PrivateKey, method: str, authority: str, path: str, now: Optional[int] = None, ttl: int = 300) -> dict[str, str]:
    """Headers that sign this request as the agent holding `private_key`."""
    created = int(now if now is not None else time.time())
    keyid = jwk_thumbprint(public_jwk(private_key.public_key()))
    comps = " ".join(f'"{c}"' for c in COMPONENTS)
    params = f'({comps});created={created};expires={created + ttl};keyid="{keyid}";alg="ed25519";tag="{TAG}"'
    sig = private_key.sign(signature_base(params, COMPONENTS, method, authority, path))
    return {"Signature-Input": f"sig1={params}", "Signature": f"sig1=:{base64.b64encode(sig).decode()}:"}


@dataclass
class DirectoryEntry:
    name: str
    operator: str
    keyid: str
    public_key: Ed25519PublicKey


@dataclass
class AgentDirectory:
    """Known agent keys, keyed by JWK thumbprint (a site's list of agents it can recognise)."""

    entries: dict[str, DirectoryEntry] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Optional[Path]) -> "AgentDirectory":
        directory = cls()
        if path and path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for agent in data.get("agents", []):
                directory.add(agent["name"], agent.get("operator", ""), agent["jwk"])
        return directory

    def add(self, name: str, operator: str, jwk: Mapping[str, str]) -> DirectoryEntry:
        if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
            raise ValueError("Only Ed25519 (OKP) agent keys are supported")
        key = Ed25519PublicKey.from_public_bytes(_b64url_decode(jwk["x"]))
        entry = DirectoryEntry(name=name, operator=operator, keyid=jwk_thumbprint(jwk), public_key=key)
        self.entries[entry.keyid] = entry
        return entry


@dataclass
class AgentIdentity:
    signed: bool
    verified: bool
    keyid: Optional[str] = None
    name: Optional[str] = None
    operator: Optional[str] = None
    error: Optional[str] = None

    @property
    def crawler_id(self) -> Optional[str]:
        """Stable crawler ID for a verified agent, so its history follows its key."""
        if not self.verified or not self.name:
            return None
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", self.name.lower()).strip("-")[:80] or "agent"
        return f"verified:{slug}"

    def public(self) -> dict[str, Any]:
        return {"signed": self.signed, "verified": self.verified, "agent": self.name, "operator": self.operator, "keyid": self.keyid}


_PARAM = re.compile(r';\s*([a-z]+)=("([^"]*)"|-?\d+)')


def verify_request(headers: Mapping[str, str], method: str, authority: str, path: str, directory: AgentDirectory, now: Optional[float] = None) -> AgentIdentity:
    """Check a request's Web Bot Auth signature. Unsigned requests return an unverified identity."""
    lower = {k.lower(): v for k, v in headers.items()}
    sig_input, sig = lower.get("signature-input"), lower.get("signature")
    if not sig_input and not sig:
        return AgentIdentity(signed=False, verified=False)

    def fail(reason: str, keyid: Optional[str] = None) -> AgentIdentity:
        return AgentIdentity(signed=True, verified=False, keyid=keyid, error=reason)

    m = re.match(r'\s*([A-Za-z0-9_-]+)=\(([^)]*)\)(.*)$', sig_input or "")
    if not m or not sig:
        return fail("Malformed Signature-Input or missing Signature header")
    label, comp_text, rest = m.groups()
    components = tuple(c.strip('"') for c in comp_text.split())
    params = {k: (q if q is not None and v.startswith('"') else v) for k, v, q in _PARAM.findall(rest)}
    keyid = params.get("keyid")
    if params.get("tag") != TAG:
        return fail('Signature tag must be "web-bot-auth"', keyid)
    if params.get("alg", "ed25519") != "ed25519":
        return fail("Only ed25519 signatures are supported", keyid)
    if not {"@authority", "@path"} <= set(components) or not set(components) <= set(COMPONENTS):
        return fail("Signature must cover @authority and @path", keyid)
    try:
        created, expires = int(params["created"]), int(params["expires"])
    except (KeyError, ValueError):
        return fail("Signature needs created and expires", keyid)
    t = time.time() if now is None else now
    if created > t + CLOCK_SKEW_SECONDS or expires < t or expires - created > MAX_VALIDITY_SECONDS:
        return fail("Signature is expired or not yet valid", keyid)
    entry = directory.entries.get(keyid or "")
    if entry is None:
        return fail("Unknown agent key", keyid)
    sm = re.match(rf"\s*{re.escape(label)}=:([A-Za-z0-9+/=]+):", sig)
    if not sm:
        return fail("Signature header does not match the Signature-Input label", keyid)
    params_text = f"({comp_text}){rest.strip()}"
    try:
        entry.public_key.verify(base64.b64decode(sm.group(1)), signature_base(params_text, components, method, authority, path))
    except (InvalidSignature, ValueError):
        return fail("Signature does not verify", keyid)
    return AgentIdentity(signed=True, verified=True, keyid=entry.keyid, name=entry.name, operator=entry.operator)
