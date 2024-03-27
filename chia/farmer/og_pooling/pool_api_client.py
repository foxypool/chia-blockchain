from __future__ import annotations

from typing import Any, Dict

from aiohttp import ClientSession, ClientTimeout

from chia import __version__
from chia.farmer.og_pooling.og_pool_protocol import SubmitPartial
from chia.server.server import ssl_context_for_root
from chia.ssl.create_ssl import get_mozilla_ca_crt

default_timeout = ClientTimeout(total=30)
get_farmer_timeout = ClientTimeout(total=15)


class PoolApiClient:
    base_url: str

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.ssl_context = ssl_context_for_root(get_mozilla_ca_crt())

    async def get_pool_info(self) -> Dict[str, Any]:
        async with ClientSession(timeout=default_timeout) as client:
            async with client.get(f"{self.base_url}/pool_info", ssl=self.ssl_context) as res:
                return await res.json()

    async def submit_partial(self, submit_partial: SubmitPartial) -> Dict[str, Any]:
        async with ClientSession(timeout=default_timeout) as client:
            async with client.post(
                    f"{self.base_url}/partial",
                    json=submit_partial.to_json_dict(),
                    ssl=self.ssl_context,
                    headers={"User-Agent": f"Chia Blockchain/{__version__}"},
            ) as res:
                return await res.json()

    async def get_farmer(self, pool_public_key: str) -> Dict[str, Any]:
        async with ClientSession(timeout=get_farmer_timeout) as client:
            async with client.get(
                    f"{self.base_url}/farmer",
                    params={"pool_public_key": pool_public_key},
                    ssl=self.ssl_context,
                    headers={"User-Agent": f"Chia Blockchain/{__version__}"},
            ) as res:
                return await res.json()
