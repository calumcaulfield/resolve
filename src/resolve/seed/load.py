"""Load the synthetic dataset into a running stack.

    make seed          # or: python -m resolve.seed.load

Posts every synthetic message to the ingest API so the whole pipeline —
ingest, bus, worker, workflow, agent, approvals — is exercised end to end and
the console has something to show.
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx

from resolve.logging import configure_logging, get_logger
from resolve.seed.generate import build_dataset

log = get_logger(__name__)

API_URL = os.getenv("RESOLVE_API_URL", "http://localhost:8000")
API_KEY = os.getenv("RESOLVE_API_KEY", "dev-local-key")


async def main() -> int:
    configure_logging("INFO", json_output=False)
    dataset = build_dataset()

    async with httpx.AsyncClient(base_url=API_URL, timeout=30.0) as client:
        try:
            await client.get("/healthz")
        except httpx.HTTPError as exc:
            log.error("seed.api_unreachable", url=API_URL, error=str(exc))
            return 1

        accepted = duplicates = 0
        for message in dataset.messages:
            response = await client.post(
                "/v1/tickets",
                headers={"x-api-key": API_KEY, "Idempotency-Key": message.external_id},
                json={
                    "external_id": message.external_id,
                    "from_email": message.from_email,
                    "subject": message.subject,
                    "body": message.body,
                    "channel": message.channel.value,
                },
            )
            response.raise_for_status()
            if response.json().get("duplicate"):
                duplicates += 1
            else:
                accepted += 1

    log.info("seed.complete", accepted=accepted, duplicates=duplicates, api=API_URL)
    print(f"Seeded {accepted} tickets ({duplicates} already present) into {API_URL}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
