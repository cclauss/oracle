from typing import Any, Dict, List, Union

import backoff
import boto3
import ipfshttpclient
from aiohttp import ClientSession, ClientTimeout
from gql import Client
from gql.transport.aiohttp import AIOHTTPTransport
from graphql import DocumentNode

from oracle.oracle.settings import (
    AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY,
    ETHEREUM_SUBGRAPH_URL,
    IPFS_FETCH_ENDPOINTS,
    IPFS_PIN_ENDPOINTS,
    STAKEWISE_SUBGRAPH_URL,
    UNISWAP_V3_SUBGRAPH_URL,
)

s3_client = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
)

# set default GQL query execution timeout to 30 seconds
EXECUTE_TIMEOUT = 30
AIOHTTP_SESSION_ARGS = {
    "timeout": ClientTimeout(total=None, sock_connect=5, sock_read=5)
}


@backoff.on_exception(backoff.expo, Exception, max_time=300)
async def execute_sw_gql_query(query: DocumentNode, variables: Dict) -> Dict:
    """Executes GraphQL query."""
    transport = AIOHTTPTransport(
        url=STAKEWISE_SUBGRAPH_URL, client_session_args=AIOHTTP_SESSION_ARGS
    )
    async with Client(transport=transport, execute_timeout=EXECUTE_TIMEOUT) as session:
        return await session.execute(query, variable_values=variables)


@backoff.on_exception(backoff.expo, Exception, max_time=300)
async def execute_uniswap_v3_gql_query(query: DocumentNode, variables: Dict) -> Dict:
    """Executes GraphQL query."""
    transport = AIOHTTPTransport(
        url=UNISWAP_V3_SUBGRAPH_URL, client_session_args=AIOHTTP_SESSION_ARGS
    )
    async with Client(transport=transport, execute_timeout=EXECUTE_TIMEOUT) as session:
        return await session.execute(query, variable_values=variables)


@backoff.on_exception(backoff.expo, Exception, max_time=300)
async def execute_ethereum_gql_query(query: DocumentNode, variables: Dict) -> Dict:
    """Executes GraphQL query."""
    transport = AIOHTTPTransport(
        url=ETHEREUM_SUBGRAPH_URL, client_session_args=AIOHTTP_SESSION_ARGS
    )
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
