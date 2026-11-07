"""Sign an agent's HTTP requests (Web Bot Auth / RFC 9421) so a SARA gateway can verify who it is."""

from __future__ import annotations

from pathlib import Path
from typing import Generator, Optional

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from server.core.agent_identity import public_jwk, sign_request


class WebBotAuth(httpx.Auth):
    """httpx auth that adds Signature-Input and Signature headers to every request."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self.private_key = private_key

    @property
    def jwk(self) -> dict[str, str]:
        return public_jwk(self.private_key.public_key())

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        path = request.url.raw_path.split(b"?", 1)[0].decode()
        authority = request.headers.get("host") or request.url.netloc.decode()
        request.headers.update(sign_request(self.private_key, request.method, authority, path))
        yield request


def load_signer(path: Optional[str]) -> Optional[WebBotAuth]:
    """Load an Ed25519 PEM key (for example from SARA_AGENT_KEY); None when no key is set."""
    if not path:
        return None
    key = load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("SARA_AGENT_KEY must be an Ed25519 private key")
    return WebBotAuth(key)


def main() -> None:
    """Create an agent key: `python -m client.web_bot_auth new-key agent.pem "My agent" "My company"`.

    Prints the directory entry a site adds to data/agent_directory.json to recognise the agent.
    """
    import json
    import sys

    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

    if len(sys.argv) < 3 or sys.argv[1] != "new-key":
        raise SystemExit(main.__doc__)
    key = Ed25519PrivateKey.generate()
    Path(sys.argv[2]).write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    name = sys.argv[3] if len(sys.argv) > 3 else "My agent"
    operator = sys.argv[4] if len(sys.argv) > 4 else ""
    print(json.dumps({"name": name, "operator": operator, "jwk": public_jwk(key.public_key())}, indent=2))


if __name__ == "__main__":
    main()
