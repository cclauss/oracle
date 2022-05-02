import logging
from typing import Any, Dict, List, Union

import backoff
import ipfshttpclient
from aiohttp import ClientSession
from gql import Client
from gql.transport.aiohttp import AIOHTTPTransport
from graphql import DocumentNode

from oracle.networks import NETWORKS
from oracle.settings import IPFS_FETCH_ENDPOINTS, IPFS_PIN_ENDPOINTS

gql_logger = logging.getLogger("gql_logger")
gql_handler = logging.StreamHandler()
gql_logger.addHandler(gql_handler)
gql_logger.setLevel(logging.ERROR)

# set default GQL query execution timeout to 30 seconds
EXECUTE_TIMEOUT = 30

# set default GQL pagination
PAGINATION_WINDOWS = 1000


async def execute_sw_gql_query(
    network: str, query: DocumentNode, variables: Dict
) -> Dict:
    return await execute_gql_query(
        subgraph_url=NETWORKS[network]["STAKEWISE_SUBGRAPH_URL"],
        query=query,
        variables=variables,
    )


async def execute_uniswap_v3_gql_query(
    network: str,
    query: DocumentNode,
    variables: Dict,
) -> Dict:
    """Executes GraphQL query."""
    return await execute_gql_query(
        subgraph_url=NETWORKS[network]["UNISWAP_V3_SUBGRAPH_URL"],
        query=query,
        variables=variables,
    )


async def execute_ethereum_gql_query(
    network: str, query: DocumentNode, variables: Dict
) -> Dict:
    """Executes GraphQL query."""
    return await execute_gql_query(
        subgraph_url=NETWORKS[network]["ETHEREUM_SUBGRAPH_URL"],
        query=query,
        variables=variables,
    )


async def execute_base_gql_paginated_query(
    subgraph_url: str, query: DocumentNode, variables: Dict, paginated_field: str
) -> List:
    """Executes GraphQL query."""
    chunks, result = [], []
    variables["last_id"] = ""

    while True:
        query_result: Dict = await execute_gql_query(
            subgraph_url=subgraph_url,
            query=query,
            variables=variables,
        )
        chunks = query_result.get(paginated_field, [])
        result.extend(chunks)
        if len(chunks) < PAGINATION_WINDOWS:
            return result

        variables["last_id"] = chunks[-1]["id"]


async def execute_sw_gql_paginated_query(
    network: str, query: DocumentNode, variables: Dict, paginated_field: str
) -> List:
    return await execute_base_gql_paginated_query(
        subgraph_url=NETWORKS[network]["STAKEWISE_SUBGRAPH_URL"],
        query=query,
        variables=variables,
        paginated_field=paginated_field,
    )


async def execute_uniswap_v3_paginated_gql_query(
    network: str, query: DocumentNode, variables: Dict, paginated_field: str
) -> List:
    """Executes GraphQL query."""
    return await execute_base_gql_paginated_query(
        subgraph_url=NETWORKS[network]["UNISWAP_V3_SUBGRAPH_URL"],
        query=query,
        variables=variables,
        paginated_field=paginated_field,
    )


async def execute_ethereum_paginated_gql_query(
    network: str, query: DocumentNode, variables: Dict, paginated_field: str
) -> List:
    """Executes ETH query."""
    return await execute_base_gql_paginated_query(
        subgraph_url=NETWORKS[network]["ETHEREUM_SUBGRAPH_URL"],
        query=query,
        variables=variables,
        paginated_field=paginated_field,
    )


@backoff.on_exception(backoff.expo, Exception, max_time=300, logger=gql_logger)
async def execute_gql_query(
    subgraph_url: str, query: DocumentNode, variables: Dict
) -> Dict:
    """Executes gql query."""
    transport = AIOHTTPTransport(url=subgraph_url)
    async with Client(transport=transport, execute_timeout=EXECUTE_TIMEOUT) as session:
        return await session.execute(query, variable_values=variables)


@backoff.on_exception(backoff.expo, Exception, max_time=900)
async def ipfs_fetch(ipfs_hash: str) -> Union[Dict[Any, Any], List[Dict[Any, Any]]]:
    """Tries to fetch IPFS hash from different sources."""
    _ipfs_hash = ipfs_hash.replace("ipfs://", "").replace("/ipfs/", "")
    for ipfs_endpoint in IPFS_PIN_ENDPOINTS:
        try:
            with ipfshttpclient.connect(ipfs_endpoint) as client:
                return client.get_json(_ipfs_hash)
        except ipfshttpclient.exceptions.TimeoutError:
            pass

    async with ClientSession() as session:
        for endpoint in IPFS_FETCH_ENDPOINTS:
            try:
                response = await session.get(
                    f"{endpoint.rstrip('/')}/ipfs/{_ipfs_hash}"
                )
                response.raise_for_status()
                return await response.json()
            except:  # noqa: E722
                pass

    raise RuntimeError(f"Failed to fetch IPFS data at {_ipfs_hash}")
