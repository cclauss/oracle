import asyncio
import json
import logging
from typing import Dict, TypedDict, Union

import backoff
import boto3
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount
from web3 import Web3
from web3.types import BlockNumber, Timestamp, Wei

from oracle.networks import NETWORKS
from oracle.oracle.clients import (
    execute_single_gql_query,
    execute_sw_gql_query,
    with_consensus,
)
from oracle.oracle.graphql_queries import (
    FINALIZED_BLOCK_QUERY,
    LATEST_BLOCK_QUERY,
    SYNC_BLOCK_QUERY,
    VOTING_PARAMETERS_QUERY,
)
from oracle.settings import CONFIRMATION_BLOCKS

from .distributor.types import DistributorVote, DistributorVotingParameters
from .rewards.types import RewardsVotingParameters, RewardVote
from .validators.types import ValidatorsVote, ValidatorVotingParameters

logger = logging.getLogger(__name__)


class Block(TypedDict):
    block_number: BlockNumber
    timestamp: Timestamp


class VotingParameters(TypedDict):
    rewards: RewardsVotingParameters
    distributor: DistributorVotingParameters
    validator: ValidatorVotingParameters


async def get_finalized_block(network: str) -> Block:
    """Gets the finalized block number and its timestamp."""
    results = await asyncio.gather(
        *[
            execute_single_gql_query(
                subgraph_url,
                query=FINALIZED_BLOCK_QUERY,
                variables=dict(
                    confirmation_blocks=CONFIRMATION_BLOCKS,
                ),
            )
            for subgraph_url in NETWORKS[network]["ETHEREUM_SUBGRAPH_URLS"]
        ]
    )
    result = _find_max_consensus(results, func=lambda x: int(x["blocks"][0]["id"]))

    return Block(
        block_number=BlockNumber(int(result["blocks"][0]["id"])),
        timestamp=Timestamp(int(result["blocks"][0]["timestamp"])),
    )


async def get_latest_block_number(network: str) -> BlockNumber:
    """Gets the latest block number and its timestamp."""
    results = await asyncio.gather(
        *[
            execute_single_gql_query(
                subgraph_url,
                query=LATEST_BLOCK_QUERY,
                variables=dict(),
            )
            for subgraph_url in NETWORKS[network]["ETHEREUM_SUBGRAPH_URLS"]
        ]
    )
    result = _find_max_consensus(results, func=lambda x: int(x["blocks"][0]["id"]))

    return BlockNumber(int(result["blocks"][0]["id"]))


async def has_synced_block(network: str, block_number: BlockNumber) -> bool:
    results = await asyncio.gather(
        *[
            execute_single_gql_query(
                subgraph_url,
                query=SYNC_BLOCK_QUERY,
                variables={},
            )
            for subgraph_url in NETWORKS[network]["STAKEWISE_SUBGRAPH_URLS"]
        ]
    )
    result = _find_max_consensus(
        results, func=lambda x: int(x["_meta"]["block"]["number"])
    )
    return block_number <= int(result["_meta"]["block"]["number"])


@with_consensus
async def get_voting_parameters(
    network: str, block_number: BlockNumber
) -> VotingParameters:
    """Fetches rewards voting parameters."""
    result: Dict = await execute_sw_gql_query(
        network=network,
        query=VOTING_PARAMETERS_QUERY,
        variables=dict(
            block_number=block_number,
        ),
    )
    network = result["networks"][0]
    reward_token = result["rewardEthTokens"][0]

    try:
        distributor = result["merkleDistributors"][0]
    except IndexError:
        distributor = {
            "rewardsUpdatedAtBlock": 0,
            "updatedAtBlock": 0,
            "merkleRoot": None,
            "merkleProofs": None,
        }

    rewards = RewardsVotingParameters(
        rewards_nonce=int(network["oraclesRewardsNonce"]),
        total_rewards=Wei(int(reward_token["totalRewards"])),
        rewards_updated_at_timestamp=Timestamp(int(reward_token["updatedAtTimestamp"])),
    )
    distributor = DistributorVotingParameters(
        rewards_nonce=int(network["oraclesRewardsNonce"]),
        from_block=BlockNumber(int(distributor["rewardsUpdatedAtBlock"])),
        to_block=BlockNumber(int(reward_token["updatedAtBlock"])),
        last_updated_at_block=BlockNumber(int(distributor["updatedAtBlock"])),
        last_merkle_root=distributor["merkleRoot"],
        last_merkle_proofs=distributor["merkleProofs"],
        protocol_reward=Wei(int(reward_token["protocolPeriodReward"])),
        distributor_reward=Wei(int(reward_token["distributorPeriodReward"])),
    )
    network = result["networks"][0]
    pool = result["pools"][0]
    validator = ValidatorVotingParameters(
        validators_nonce=int(network["oraclesValidatorsNonce"]),
        pool_balance=Wei(int(pool["balance"])),
    )

    return VotingParameters(
        rewards=rewards, distributor=distributor, validator=validator
    )


@backoff.on_exception(backoff.expo, Exception, max_time=900)
def submit_vote(
    network: str,
    oracle: LocalAccount,
    encoded_data: bytes,
    vote: Union[RewardVote, DistributorVote, ValidatorsVote],
    name: str,
) -> None:
    """Submits vote to the votes' aggregator."""
    network_config = NETWORKS[network]
    aws_bucket_name = network_config["AWS_BUCKET_NAME"]
    s3_client = boto3.client(
        "s3",
        aws_access_key_id=network_config["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=network_config["AWS_SECRET_ACCESS_KEY"],
    )
    # generate candidate ID
    candidate_id: bytes = Web3.keccak(primitive=encoded_data)
    message = encode_defunct(primitive=candidate_id)
    signed_message = oracle.sign_message(message)
    vote["signature"] = signed_message.signature.hex()

    # TODO: support more aggregators (GCP, Azure, etc.)
    bucket_key = f"{oracle.address}/{name}"
    s3_client.put_object(
        Bucket=aws_bucket_name,
        Key=bucket_key,
        Body=json.dumps(vote),
        ACL="public-read",
    )
    s3_client.get_waiter("object_exists").wait(Bucket=aws_bucket_name, Key=bucket_key)


def _find_max_consensus(items, func):
    majority = len(items) // 2 + 1
    maximum = 0
    result = None
    for item in items:
        if (
            func(item) > maximum
            and len([x for x in items if func(x) >= func(item)]) >= majority
        ):
            maximum = func(item)
            result = item
    return result
