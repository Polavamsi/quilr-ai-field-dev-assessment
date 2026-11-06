"""Issue a test bearer token for the gateway.

    GATEWAY_JWT_SECRET=... python -m task2_mcp_gateway.mint_token --role viewer --sub bob
"""

import argparse
import os
import time

import jwt

from task2_mcp_gateway.auth import ROLES


def mint(secret: str, role: str, subject: str, ttl_s: int = 3600) -> str:
    now = int(time.time())
    return jwt.encode({"sub": subject, "role": role, "iat": now, "exp": now + ttl_s}, secret, algorithm="HS256")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=sorted(ROLES))
    parser.add_argument("--sub", default="demo-user")
    parser.add_argument("--ttl", type=int, default=3600, help="lifetime in seconds")
    args = parser.parse_args()

    secret = os.environ.get("GATEWAY_JWT_SECRET")
    if not secret:
        parser.error("GATEWAY_JWT_SECRET is not set")
    print(mint(secret, args.role, args.sub, args.ttl))


if __name__ == "__main__":
    main()
