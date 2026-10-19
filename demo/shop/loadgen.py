"""Sends steady traffic to the shop so there is always something to measure."""

from __future__ import annotations

import asyncio
import os
import random

import httpx

RPS = float(os.environ.get("RPS", "10"))
TARGETS = [
    # (url, share of traffic)
    (os.environ.get("CHECKOUT_URL", "http://checkout:8000") + "/checkout", 0.45),
    (os.environ.get("CHECKOUT_URL", "http://checkout:8000") + "/coupon", 0.1),
    (os.environ.get("SEARCH_URL", "http://search:8000") + "/search", 0.45),
]


async def hit(client: httpx.AsyncClient, url: str) -> None:
    try:
        await client.get(url)
    except httpx.HTTPError:
        pass  # the services record their own errors


async def main() -> None:
    urls = [u for u, _ in TARGETS]
    weights = [w for _, w in TARGETS]
    print(f"loadgen: {RPS * 3:g} requests/s across {len(urls)} endpoints", flush=True)
    async with httpx.AsyncClient(timeout=10.0, limits=httpx.Limits(max_connections=200)) as client:
        while True:
            # Each endpoint gets RPS * 3 * share requests per second.
            for url in random.choices(urls, weights, k=int(RPS * 3 / 10) or 1):
                asyncio.create_task(hit(client, url))
            await asyncio.sleep(0.1)


if __name__ == "__main__":
    asyncio.run(main())
