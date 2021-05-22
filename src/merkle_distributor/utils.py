import logging
from typing import Tuple, Set, Dict, Union, List, NamedTuple, TypedDict

import ipfshttpclient
from ens.constants import EMPTY_ADDR_HEX
from eth_typing import HexStr, ChecksumAddress
from gql import Client, gql
from gql.transport.requests import RequestsHTTPTransport
from tenacity import retry, Retrying
from tenacity.before_sleep import before_sleep_log
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import ContractLogicError
from web3.types import Wei, BlockNumber

from src.staking_rewards.utils import backoff, stop_attempts
from src.utils import wait_for_transaction

logger = logging.getLogger(__name__)

# 1e18
ETHER: int = Web3.toWei(1, "ether")
IPFS_PREFIX: str = "ipfs://"


class Distribution(NamedTuple):
    beneficiary: ChecksumAddress
    token: ChecksumAddress
    value: Wei


class OraclesSettings(TypedDict):
    # Number of blocks between checking the balances again for allocating rewards
    snapshot_interval_in_blocks: int

    # set of balancer staked ETH pool IDs
    balancer_staked_eth_pool_ids: Set[HexStr]

    # mapping between balancer pool address and its ID
    balancer_pools: Dict[ChecksumAddress, HexStr]

    # set of Uniswap V2 supported pairs
    uniswap_v2_pairs: Set[ChecksumAddress]

    # set of Uniswap V3 supported pairs
    uniswap_v3_pairs: Dict[ChecksumAddress, BlockNumber]

    # dictionary of supported ERC-20 tokens and the block number when they were created
    erc20_tokens: Dict[ChecksumAddress, BlockNumber]


@retry(
    reraise=True,
    wait=backoff,
    stop=stop_attempts,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def get_merkle_root_voting_parameters(
    oracles: Contract,
    multicall: Contract,
    reward_eth_token: Contract,
    block_number: BlockNumber,
) -> Tuple[bool, bool, int, BlockNumber]:
    """Fetches merkle root voting parameters."""
    calls = [
        dict(target=oracles.address, callData=oracles.encodeABI("isMerkleRootVoting")),
        dict(target=oracles.address, callData=oracles.encodeABI("paused")),
        dict(target=oracles.address, callData=oracles.encodeABI("currentNonce")),
        dict(
            target=reward_eth_token.address,
            callData=reward_eth_token.encodeABI("lastUpdateBlockNumber"),
        ),
    ]
    response = multicall.functions.aggregate(calls).call(block_identifier=block_number)[
        1
    ]
    return (
        bool(Web3.toInt(response[0])),
        bool(Web3.toInt(response[1])),
        Web3.toInt(response[2]),
        Web3.toInt(response[3]),
    )


@retry(
    reraise=True,
    wait=backoff,
    stop=stop_attempts,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def get_prev_merkle_root_parameters(
    merkle_distributor: Contract,
    reward_eth_token: Contract,
    to_block: BlockNumber,
) -> Union[None, Tuple[HexStr, HexStr, BlockNumber, BlockNumber]]:
    """
    Fetches previous merkle root update parameters.
    """
    events = merkle_distributor.events.MerkleRootUpdated.getLogs(
        fromBlock=0, toBlock=to_block
    )
    if not events:
        # it's the first merkle root update
        return None

    # fetch block number of staking rewards update prior to merkle distributor update
    prev_merkle_root_update_block_number = events[-1]["blockNumber"]
    prev_merkle_root_staking_rewards_update_block_number: BlockNumber = (
        reward_eth_token.functions.lastUpdateBlockNumber().call(
            block_identifier=prev_merkle_root_update_block_number
        )
    )

    return (
        events[-1]["args"]["merkleRoot"],
        events[-1]["args"]["merkleProofs"],
        prev_merkle_root_update_block_number,
        prev_merkle_root_staking_rewards_update_block_number,
    )


def get_staked_eth_period_reward(
    reward_eth_token: Contract,
    new_rewards_block_number: BlockNumber,
    prev_merkle_root_update_block_number: BlockNumber = None,
    prev_merkle_root_staking_rewards_update_block_number: BlockNumber = None,
) -> Wei:
    """Calculates period reward of staked eth since the last update."""
    total_rewards: Wei = reward_eth_token.functions.balanceOf(EMPTY_ADDR_HEX).call(
        block_identifier=new_rewards_block_number
    )

    if prev_merkle_root_staking_rewards_update_block_number <= 0:
        # it's the first merkle root update -> no need to subtract previously claimed rewards
        return total_rewards

    # it's not the first merkle root update -> calculate unclaimed
    # rewards and subtract them from the total rewards
    prev_total_rewards: Wei = reward_eth_token.functions.balanceOf(EMPTY_ADDR_HEX).call(
        block_identifier=prev_merkle_root_staking_rewards_update_block_number
    )
    claimed_events = reward_eth_token.events.Transfer.getLogs(
        argument_filters={"from": EMPTY_ADDR_HEX},
        fromBlock=prev_merkle_root_update_block_number,
        toBlock="latest",
    )
    for event in claimed_events:
        prev_total_rewards -= event["args"]["value"]

    return Wei(total_rewards - prev_total_rewards)


@retry(
    reraise=True,
    wait=backoff,
    stop=stop_attempts,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def get_reth_disabled_accounts(
    reward_eth_token: Contract, to_block: BlockNumber
) -> Set[ChecksumAddress]:
    """
    Fetches accounts that have rETH2 distribution disabled from RewardEthToken contract.
    """
    events = reward_eth_token.events.RewardsToggled.getLogs(
        fromBlock=0, toBlock=to_block
    )
    reth_disabled_accounts: Set[ChecksumAddress] = set()
    for event in events:
        account = Web3.toChecksumAddress(event["args"]["account"])
        is_disabled = event["args"]["isDisabled"]

        if not is_disabled and account in reth_disabled_accounts:
            reth_disabled_accounts.remove(account)
        elif is_disabled and account not in reth_disabled_accounts:
            reth_disabled_accounts.add(account)

    return reth_disabled_accounts


@retry(
    reraise=True,
    wait=backoff,
    stop=stop_attempts,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def get_staked_eth_distributions(
    staked_eth_token: Contract,
    multicall: Contract,
    reward_eth_token_address: ChecksumAddress,
    reth_disabled_accounts: List[ChecksumAddress],
    staked_eth_period_reward: Wei,
    new_rewards_block_number: BlockNumber,
) -> List[Distribution]:
    """Creates staked eth reward distributions."""
    if staked_eth_period_reward <= 0:
        # cannot create distributions
        logger.warning(
            f"Cannot generate staked ETH distributions: period reward={staked_eth_period_reward} Wei"
        )
        return []

    # fetch staked eth balances
    staked_eth_balance_calls = [
        dict(
            target=staked_eth_token.address,
            callData=staked_eth_token.encodeABI("balanceOf", [account]),
        )
        for account in reth_disabled_accounts
    ]
    response = multicall.functions.aggregate(staked_eth_balance_calls).call(
        block_identifier=new_rewards_block_number
    )[1]

    # parse balances
    staked_eth_balances: List[Wei] = []
    total_staked_eth_balance: Wei = Wei(0)
    for bal in response:
        balance: Wei = Web3.toInt(bal)
        staked_eth_balances.append(balance)
        total_staked_eth_balance += balance

    # no total balance
    if total_staked_eth_balance <= 0:
        # cannot create distributions
        logger.warning(
            f"Cannot generate staked ETH distributions: total balance={total_staked_eth_balance} Wei"
        )
        return []

    # calculate staked eth rewards distribution
    distributions: List[Distribution] = []
    pairs: List[Tuple[ChecksumAddress, Wei]] = list(
        zip(reth_disabled_accounts, staked_eth_balances)
    )
    distributed: Wei = Wei(0)
    for beneficiary, staked_eth_balance in pairs:
        if beneficiary == reth_disabled_accounts[-1]:
            reward: Wei = Wei(staked_eth_period_reward - distributed)
            if reward > 0:
                distributions.append(
                    Distribution(beneficiary, reward_eth_token_address, reward)
                )
            break

        reward: Wei = Wei(
            staked_eth_period_reward * staked_eth_balance // total_staked_eth_balance
        )
        if reward <= 0:
            continue

        distributed += reward
        distributions.append(
            Distribution(beneficiary, reward_eth_token_address, reward)
        )

    return distributions


@retry(
    reraise=True,
    wait=backoff,
    stop=stop_attempts,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def get_oracles_config(
    node_id: bytes,
    ens_resolver: Contract,
    block_number: BlockNumber,
    ens_text_record: str,
    ipfs_endpoint: str,
) -> OraclesSettings:
    """Fetches oracles config from the DAO's ENS text record."""
    # fetch IPFS URL
    oracles_config_url = ens_resolver.functions.text(node_id, ens_text_record).call(
        block_identifier=block_number
    )
    if not oracles_config_url:
        # try for the latest block
        oracles_config_url = ens_resolver.functions.text(node_id, ens_text_record).call(
            block_identifier="latest"
        )

    if oracles_config_url.startswith(IPFS_PREFIX):
        oracles_config_url = oracles_config_url[7:]

    with ipfshttpclient.connect(ipfs_endpoint) as client:
        oracles_settings = client.get_json(oracles_config_url)

    return OraclesSettings(
        snapshot_interval_in_blocks=int(
            oracles_settings["snapshot_interval_in_blocks"]
        ),
        balancer_staked_eth_pool_ids=set(
            oracles_settings.get("balancer_staked_eth_pool_ids", [])
        ),
        balancer_pools={
            Web3.toChecksumAddress(pool_address): pool_id
            for pool_address, pool_id in oracles_settings.get(
                "balancer_pools", {}
            ).items()
        },
        uniswap_v2_pairs=set(
            [
                Web3.toChecksumAddress(pair)
                for pair in oracles_settings.get("uniswap_v2_pairs", [])
            ]
        ),
        uniswap_v3_pairs={
            Web3.toChecksumAddress(token_address): int(block_number)
            for token_address, block_number in oracles_settings.get(
                "uniswap_v3_pairs", {}
            ).items()
        },
        erc20_tokens={
            Web3.toChecksumAddress(token_address): int(block_number)
            for token_address, block_number in oracles_settings.get(
                "erc20_tokens", {}
            ).items()
        },
    )


@retry(
    reraise=True,
    wait=backoff,
    stop=stop_attempts,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def get_distributions(
    merkle_distributor: Contract,
    distribution_start_block: BlockNumber,
    distribution_end_block: BlockNumber,
    blocks_interval: int,
) -> Dict[BlockNumber, List[Distribution]]:
    """Creates rewards distributions for reward tokens with specific block intervals."""
    distribute_events = merkle_distributor.events.DistributionAdded.getLogs(
        fromBlock=0, toBlock="latest"
    )
    distributions: Dict[BlockNumber, List[Distribution]] = {}
    for event in distribute_events:
        token: ChecksumAddress = Web3.toChecksumAddress(event["args"]["token"])
        beneficiary: ChecksumAddress = Web3.toChecksumAddress(
            event["args"]["beneficiary"]
        )
        amount: Wei = event["args"]["amount"]
        start_block: BlockNumber = event["args"]["startBlock"]
        end_block: BlockNumber = event["args"]["endBlock"]

        if (
            end_block <= distribution_start_block
            or start_block >= distribution_end_block
        ):
            # distributions are out of current range
            continue

        # calculate reward distributions for spread of `block_interval`
        total_blocks = end_block - start_block
        if total_blocks <= 0:
            continue

        reward_per_block: Wei = Wei(amount // total_blocks)
        interval_reward: Wei = Wei(reward_per_block * blocks_interval)
        start: BlockNumber = max(distribution_start_block, start_block)
        end: BlockNumber = min(distribution_end_block, end_block)
        while start != end:
            if start + blocks_interval > end:
                interval = end - start
                reward: Wei = Wei(reward_per_block * interval)
                if end == end_block:
                    # collect left overs
                    reward += amount - (reward_per_block * total_blocks)

                if reward > 0:
                    distributions.setdefault(BlockNumber(start + interval), []).append(
                        Distribution(beneficiary, token, reward)
                    )
                break

            start += blocks_interval
            if interval_reward > 0:
                distributions.setdefault(start, []).append(
                    Distribution(beneficiary, token, interval_reward)
                )

    return distributions


@retry(
    reraise=True,
    wait=backoff,
    stop=stop_attempts,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def get_merkle_distributor_claimed_addresses(
    merkle_distributor: Contract, from_block: BlockNumber
) -> Set[ChecksumAddress]:
    """Fetches addresses that have claimed their tokens from `MerkleDistributor` contract."""
    events = merkle_distributor.events.Claimed.getLogs(
        fromBlock=from_block, toBlock="latest"
    )
    return set(Web3.toChecksumAddress(event["args"]["account"]) for event in events)


@retry(
    reraise=True,
    wait=backoff,
    stop=stop_attempts,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def get_unclaimed_balances(
    merkle_proofs_ipfs_url: str,
    claimed_accounts: Set[ChecksumAddress],
    ipfs_endpoint: str,
) -> Dict[ChecksumAddress, Dict[ChecksumAddress, Wei]]:
    """Fetches balances of previous merkle drop from IPFS and removes the accounts that have already claimed."""
    if merkle_proofs_ipfs_url.startswith(IPFS_PREFIX):
        merkle_proofs_ipfs_url = merkle_proofs_ipfs_url[7:]

    with ipfshttpclient.connect(ipfs_endpoint) as client:
        prev_balances: Dict[ChecksumAddress, Dict] = client.get_json(
            merkle_proofs_ipfs_url
        )

    unclaimed_balances: Dict[ChecksumAddress, Dict[ChecksumAddress, Wei]] = {}
    for account, data in prev_balances.items():
        account = Web3.toChecksumAddress(account)
        if account in claimed_accounts:
            continue

        unclaimed_balances[account] = {}
        for i, token in enumerate(data["tokens"]):
            unclaimed_balances[account][token] = Wei(int(data["amounts"][i]))

    return unclaimed_balances


@retry(
    reraise=True,
    wait=backoff,
    stop=stop_attempts,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def execute_graphql_query(client: Client, query: str, variables: Dict) -> Dict:
    """Executes GraphQL query."""
    return client.execute(query, variable_values=variables)


@retry(
    reraise=True,
    wait=backoff,
    stop=stop_attempts,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def get_balancer_vault_pool_shares(
    subgraph_url: str,
    token_address: ChecksumAddress,
    pool_ids: Set[HexStr],
    block_number: BlockNumber,
) -> Dict[ChecksumAddress, Wei]:
    """Fetches vault shares for specific token in balancer pools."""
    transport = RequestsHTTPTransport(url=subgraph_url)

    # create a GraphQL client using the defined transport
    client = Client(transport=transport, fetch_schema_from_transport=True)

    # provide a GraphQL query
    query = gql(
        """
        query getPools($block_number: Int, $pool_ids: [ID], $token_address: String) {
          pools(block: { number: $block_number }, where: { id_in: $pool_ids }) {
            address
            tokens(where: { address: $token_address }) {
              balance
            }
          }
        }
    """
    )

    # execute the query on the transport
    result: Dict = execute_graphql_query(
        client=client,
        query=query,
        variables=dict(
            block_number=block_number,
            pool_ids=list(pool_ids),
            token_address=token_address.lower(),
        ),
    )

    # extract pool shares
    shares: Dict[ChecksumAddress, Wei] = {}
    pools = result.get("pools", [])
    for pool in pools:
        pool_address: ChecksumAddress = Web3.toChecksumAddress(pool["address"])
        balances = pool.get("tokens", [])
        if not balances or len(balances) != 1:
            balance: Wei = Wei(0)
        else:
            balance: Wei = Web3.toWei((balances[0].get("balance", "0"), "ether"))

        shares[pool_address] = balance

    return shares


@retry(
    reraise=True,
    wait=backoff,
    stop=stop_attempts,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def get_balancer_pool_balances(
    subgraph_url: str,
    pool_id: HexStr,
    block_number: BlockNumber,
) -> Tuple[Dict[ChecksumAddress, Wei], Wei]:
    """Fetches users' balances of the Balancer V2 pool."""
    transport = RequestsHTTPTransport(url=subgraph_url)

    # create a GraphQL client using the defined transport
    client = Client(transport=transport, fetch_schema_from_transport=True)

    # provide a GraphQL query
    query = gql(
        """
        query getPoolShares($block_number: Int, $pool_id: String, $last_id: ID) {
          poolShares(
            first: 1000
            block: { number: $block_number }
            where: { poolId: $pool_id, id_gt: $last_id }
            orderBy: id
            orderDirection: asc
          ) {
            id
            userAddress {
              id
            }
            balance
          }
        }
    """
    )

    # execute the query on the transport in chunks of 1000 entities
    last_id = ""
    result: Dict = execute_graphql_query(
        client=client,
        query=query,
        variables=dict(
            block_number=block_number, pool_id=pool_id.lower(), last_id=last_id
        ),
    )
    pool_shares_chunk = result.get("poolShares", [])
    pool_shares = pool_shares_chunk

    # accumulate chunks of pool shares
    while len(pool_shares_chunk) >= 1000:
        last_id = pool_shares_chunk[-1]["id"]
        if not last_id:
            break

        result = execute_graphql_query(
            client=client,
            query=query,
            variables=dict(
                block_number=block_number, pool_id=pool_id.lower(), last_id=last_id
            ),
        )
        pool_shares_chunk = result.get("poolShares", [])
        pool_shares.extend(pool_shares_chunk)

    # extract balances and total supply
    balances: Dict[ChecksumAddress, Wei] = {}
    total_supply: Wei = Wei(0)
    for pool_share in pool_shares:
        user_address: ChecksumAddress = pool_share.get("userAddress", {}).get(
            "id", EMPTY_ADDR_HEX
        )
        if not user_address or user_address == EMPTY_ADDR_HEX:
            continue

        balance: Wei = Web3.toWei(pool_share.get("balance", "0"), "ether")
        if balance <= 0:
            continue

        user_address = Web3.toChecksumAddress(user_address)
        if user_address in balances:
            raise ValueError(
                f"Duplicated balance entry for the user with address {user_address}"
            )

        balances[user_address] = balance
        total_supply += balance

    return balances, total_supply


@retry(
    reraise=True,
    wait=backoff,
    stop=stop_attempts,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def get_uniswap_v2_balances(
    subgraph_url: str,
    pair_address: ChecksumAddress,
    block_number: BlockNumber,
) -> Tuple[Dict[ChecksumAddress, Wei], Wei]:
    """Fetches users' balances of the Uniswap V2 Pair."""
    transport = RequestsHTTPTransport(url=subgraph_url)

    # create a GraphQL client using the defined transport
    client = Client(transport=transport, fetch_schema_from_transport=True)

    # provide a GraphQL query
    query = gql(
        """
        query getLiquidityPositions($block_number: Int, $pair: String, $last_id: ID) {
          liquidityPositions(
            first: 1000
            block: { number: $block_number }
            where: { pair: $pair, id_gt: $last_id }
            orderBy: id
            orderDirection: asc
          ) {
            id
            user {
              id
            }
            liquidityTokenBalance
          }
        }
    """
    )

    # execute the query on the transport in chunks of 1000 entities
    last_id = ""
    result: Dict = execute_graphql_query(
        client=client,
        query=query,
        variables=dict(
            block_number=block_number, pair=pair_address.lower(), last_id=last_id
        ),
    )
    liquidity_positions_chunk = result.get("liquidityPositions", [])
    liquidity_positions = liquidity_positions_chunk

    # accumulate chunks of pool shares
    while len(liquidity_positions_chunk) >= 1000:
        last_id = liquidity_positions_chunk[-1]["id"]
        if not last_id:
            break

        result = execute_graphql_query(
            client=client,
            query=query,
            variables=dict(
                block_number=block_number, pair=pair_address.lower(), last_id=last_id
            ),
        )
        liquidity_positions_chunk = result.get("liquidityPositions", [])
        liquidity_positions.extend(liquidity_positions_chunk)

    # extract balances and total supply
    balances: Dict[ChecksumAddress, Wei] = {}
    total_supply: Wei = Wei(0)

    for liquidity_position in liquidity_positions:
        user_address: ChecksumAddress = liquidity_position.get("user", {}).get(
            "id", EMPTY_ADDR_HEX
        )
        if not user_address or user_address == EMPTY_ADDR_HEX:
            continue

        balance: Wei = Web3.toWei(
            liquidity_position.get("liquidityTokenBalance", "0"), "ether"
        )
        if balance <= 0:
            continue

        user_address = Web3.toChecksumAddress(user_address)
        if user_address in balances:
            raise ValueError(
                f"Duplicated balance entry for the user with address {user_address}"
            )

        balances[user_address] = balance
        total_supply += balance

    return balances, total_supply


@retry(
    reraise=True,
    wait=backoff,
    stop=stop_attempts,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def get_uniswap_v3_balances(
    subgraph_url: str,
    pool_address: ChecksumAddress,
    position_manager: Contract,
    from_block: BlockNumber,
    to_block: BlockNumber,
) -> Tuple[Dict[ChecksumAddress, Wei], Wei]:
    """Fetches users' balances of the Uniswap V3 Pair that provide liquidity for the current tick."""
    transport = RequestsHTTPTransport(url=subgraph_url)

    # create a GraphQL client using the defined transport
    client = Client(transport=transport, fetch_schema_from_transport=True)

    # fetch pool current tick and token addresses
    query = gql(
        """
        query getPools($block_number: Int, $pool_address: ID) {
          pools(block: { number: $block_number }, where: { id: $pool_address }) {
            tick
          }
        }
    """
    )

    # execute the query on the transport
    result: Dict = execute_graphql_query(
        client=client,
        query=query,
        variables=dict(block_number=to_block, pool_address=pool_address.lower()),
    )
    pools = result.get("pools", [])
    if not pools:
        return {}, Wei(0)

    current_tick: str = pools[0].get("tick", "")
    if current_tick == "":
        return {}, Wei(0)

    # fetch liquidity mints that cover the current tick
    query = gql(
        """
        query getMints(
          $block_number: Int
          $current_tick: BigInt
          $owner: Bytes
          $pool_address: String
          $last_id: ID
        ) {
          mints(
            first: 1000
            block: { number: $block_number }
            where: {
              tickLower_lte: $current_tick
              tickUpper_gte: $current_tick
              owner: $owner
              pool: $pool_address
              id_gt: $last_id
            }
            orderBy: id
            orderDirection: asc
          ) {
            id
            origin
            transaction {
              id
            }
          }
        }
    """
    )

    # execute the query on the transport in chunks of 1000 entities
    last_id = ""
    result: Dict = execute_graphql_query(
        client=client,
        query=query,
        variables=dict(
            block_number=to_block,
            current_tick=current_tick,
            pool_address=pool_address.lower(),
            owner=position_manager.address.lower(),
            last_id=last_id,
        ),
    )
    mints_chunk = result.get("mints", [])
    mints = mints_chunk

    # accumulate chunks
    while len(mints_chunk) >= 1000:
        last_id = mints_chunk[-1]["id"]
        if not last_id:
            break

        result = execute_graphql_query(
            client=client,
            query=query,
            variables=dict(
                block_number=to_block,
                current_tick=current_tick,
                pool_address=pool_address.lower(),
                owner=position_manager.address.lower(),
                last_id=last_id,
            ),
        )
        mints_chunk = result.get("mints", [])
        mints.extend(mints_chunk)

    # process mints
    account_transactions: Dict[ChecksumAddress, Set[HexStr]] = {}
    for mint in mints:
        tx_hash: HexStr = mint.get("transaction", {}).get("id", "")
        user_address: ChecksumAddress = mint.get("origin", EMPTY_ADDR_HEX)
        if not tx_hash or user_address in (EMPTY_ADDR_HEX, ""):
            continue

        user_address = Web3.toChecksumAddress(user_address)
        account_transactions.setdefault(user_address, set()).add(tx_hash)

    token_id_to_owner: Dict[int, ChecksumAddress] = get_uniswap_v3_token_ids(
        position_manager=position_manager,
        account_transactions=account_transactions,
        start_block=from_block,
        end_block=to_block,
    )
    owner_to_liquidity: Dict[
        ChecksumAddress, Wei
    ] = get_uniswap_v3_token_owner_liquidity(
        position_manager=position_manager,
        token_id_to_owner=token_id_to_owner,
        start_block=from_block,
        end_block=to_block,
    )

    return owner_to_liquidity, sum(owner_to_liquidity.values())


@retry(
    reraise=True,
    wait=backoff,
    stop=stop_attempts,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def get_uniswap_v3_token_owner_liquidity(
    position_manager: Contract,
    token_id_to_owner: Dict[int, ChecksumAddress],
    start_block: BlockNumber,
    end_block: BlockNumber,
) -> Dict[ChecksumAddress, Wei]:
    """Fetches liquidity for specific Uniswap V3 token IDs and aggregates based on the owner address."""
    # fetch liquidity positions for all the token IDs
    argument_filters = {"tokenId": list(token_id_to_owner.keys())}
    _start_block: BlockNumber = start_block
    blocks_spread = 200_000
    owner_to_liquidity: Dict[ChecksumAddress, Wei] = {}

    # process increase events
    while start_block < end_block:
        for attempt in Retrying(
            reraise=True,
            wait=backoff,
            stop=stop_attempts,
            before_sleep=before_sleep_log(logger, logging.WARNING),
        ):
            if start_block + blocks_spread >= end_block:
                to_block: BlockNumber = end_block
            else:
                to_block: BlockNumber = start_block + blocks_spread
            with attempt:
                try:
                    decrease_events = position_manager.events.IncreaseLiquidity.getLogs(
                        argument_filters=argument_filters,
                        fromBlock=start_block,
                        toBlock=to_block,
                    )
                    start_block = to_block + 1
                except Exception as e:
                    blocks_spread = blocks_spread // 2
                    logger.warning(
                        f"Failed to fetch liquidity increase events: from block={start_block}, to block={to_block},"
                        f" changing blocks spread to={blocks_spread}"
                    )
                    raise e

            # process increase liquidity events
            for event in decrease_events:
                liquidity: Wei = event["args"]["liquidity"]
                token_id: int = event["args"]["tokenId"]
                owner: ChecksumAddress = token_id_to_owner[token_id]
                owner_to_liquidity[owner] = Wei(
                    owner_to_liquidity.setdefault(owner, Wei(0)) + liquidity
                )

    # process decrease events
    blocks_spread = 200_000
    start_block = _start_block
    while start_block < end_block:
        for attempt in Retrying(
            reraise=True,
            wait=backoff,
            stop=stop_attempts,
            before_sleep=before_sleep_log(logger, logging.WARNING),
        ):
            if start_block + blocks_spread >= end_block:
                to_block: BlockNumber = end_block
            else:
                to_block: BlockNumber = start_block + blocks_spread
            with attempt:
                try:
                    decrease_events = position_manager.events.DecreaseLiquidity.getLogs(
                        argument_filters=argument_filters,
                        fromBlock=start_block,
                        toBlock=to_block,
                    )
                    start_block = to_block + 1
                except Exception as e:
                    blocks_spread = blocks_spread // 2
                    logger.warning(
                        f"Failed to fetch liquidity decrease events: from block={start_block}, to block={to_block},"
                        f" changing blocks spread to={blocks_spread}"
                    )
                    raise e

            # process decrease liquidity events
            for event in decrease_events:
                liquidity: Wei = event["args"]["liquidity"]
                token_id: int = event["args"]["tokenId"]
                owner: ChecksumAddress = token_id_to_owner[token_id]
                prev_liquidity: Wei = owner_to_liquidity.setdefault(owner, Wei(0))
                if prev_liquidity - liquidity <= 0:
                    del owner_to_liquidity[owner]
                else:
                    owner_to_liquidity[owner] = Wei(prev_liquidity - liquidity)

    return owner_to_liquidity


@retry(
    reraise=True,
    wait=backoff,
    stop=stop_attempts,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def get_uniswap_v3_token_ids(
    position_manager: Contract,
    account_transactions: Dict[ChecksumAddress, Set[HexStr]],
    start_block: BlockNumber,
    end_block: BlockNumber,
) -> Dict[int, ChecksumAddress]:
    """Fetches Uniswap V3 token IDs for specific accounts."""
    argument_filters = {"from": EMPTY_ADDR_HEX, "to": list(account_transactions.keys())}
    visited_mint_transactions: Set[HexStr] = set()
    duplicated_transactions: Set[HexStr] = set()
    _start_block: BlockNumber = start_block

    # fetch token IDs
    blocks_spread = 200_000
    owner_to_token_ids: Dict[ChecksumAddress, Set[int]] = {}
    while start_block < end_block:
        for attempt in Retrying(
            reraise=True,
            wait=backoff,
            stop=stop_attempts,
            before_sleep=before_sleep_log(logger, logging.WARNING),
        ):
            if start_block + blocks_spread >= end_block:
                to_block: BlockNumber = end_block
            else:
                to_block: BlockNumber = start_block + blocks_spread
            with attempt:
                try:
                    transfer_events = position_manager.events.Transfer.getLogs(
                        argument_filters=argument_filters,
                        fromBlock=start_block,
                        toBlock=to_block,
                    )
                    start_block = to_block + 1
                except Exception as e:
                    blocks_spread = blocks_spread // 2
                    logger.warning(
                        f"Failed to fetch transfer events: from block={start_block}, to block={to_block},"
                        f" changing blocks spread to={blocks_spread}"
                    )
                    raise e

            # process mint transactions to get a set of valid token IDs
            for transfer_event in transfer_events:
                tx_hash: HexStr = Web3.toHex(transfer_event["transactionHash"])
                to_address: ChecksumAddress = Web3.toChecksumAddress(
                    transfer_event["args"]["to"]
                )
                token_id: int = transfer_event["args"]["tokenId"]
                if (
                    to_address not in account_transactions
                    or tx_hash not in account_transactions[to_address]
                ):
                    continue

                if tx_hash in visited_mint_transactions:
                    duplicated_transactions.add(tx_hash)
                    continue

                visited_mint_transactions.add(tx_hash)
                owner_to_token_ids.setdefault(to_address, set()).add(token_id)

    # get rid of accounts that have multiple mints in one transaction
    for tx in duplicated_transactions:
        for to_address, mint_transactions in account_transactions.items():
            if tx in mint_transactions:
                del owner_to_token_ids[to_address]

    token_id_to_owner: Dict[int, ChecksumAddress] = {}
    for to_address, token_ids in owner_to_token_ids.items():
        for token_id in token_ids:
            token_id_to_owner[token_id] = to_address

    # check for updated owners
    argument_filters = {
        "tokenId": list(token_id_to_owner.keys()),
    }
    blocks_spread = 200_000
    start_block = _start_block
    while start_block < end_block:
        for attempt in Retrying(
            reraise=True,
            wait=backoff,
            stop=stop_attempts,
            before_sleep=before_sleep_log(logger, logging.WARNING),
        ):
            if start_block + blocks_spread >= end_block:
                to_block: BlockNumber = end_block
            else:
                to_block: BlockNumber = start_block + blocks_spread
            with attempt:
                try:
                    transfer_events = position_manager.events.Transfer.getLogs(
                        argument_filters=argument_filters,
                        fromBlock=start_block,
                        toBlock=to_block,
                    )
                    start_block = to_block + 1
                except Exception as e:
                    blocks_spread = blocks_spread // 2
                    logger.warning(
                        f"Failed to fetch transfer events: from block={start_block}, to block={to_block},"
                        f" changing blocks spread to={blocks_spread}"
                    )
                    raise e

            # process mint transactions to get a set of valid token IDs
            for transfer_event in transfer_events:
                to_address: ChecksumAddress = Web3.toChecksumAddress(
                    transfer_event["args"]["to"]
                )
                token_id: int = transfer_event["args"]["tokenId"]
                if to_address == EMPTY_ADDR_HEX:
                    token_id_to_owner.pop(token_id, None)
                else:
                    token_id_to_owner[token_id] = to_address

    return token_id_to_owner


def get_token_participated_accounts(
    token: Contract, start_block: BlockNumber, end_block: BlockNumber
) -> Set[ChecksumAddress]:
    """Fetches accounts that were in either `from` or `to` of the contract Transfer event."""
    blocks_spread = 200_000
    accounts: Set[ChecksumAddress] = set()
    while start_block < end_block:
        for attempt in Retrying(
            reraise=True,
            wait=backoff,
            stop=stop_attempts,
            before_sleep=before_sleep_log(logger, logging.WARNING),
        ):
            with attempt:
                if start_block + blocks_spread >= end_block:
                    to_block: BlockNumber = end_block
                else:
                    to_block: BlockNumber = start_block + blocks_spread
                try:
                    transfer_events = token.events.Transfer.getLogs(
                        fromBlock=start_block, toBlock=to_block
                    )
                    start_block = to_block + 1
                except Exception as e:
                    blocks_spread = blocks_spread // 2
                    logger.warning(
                        f"Failed to fetch transfer events: from block={start_block}, to block={to_block},"
                        f" changing blocks spread to={blocks_spread}"
                    )
                    raise e

                for transfer_event in transfer_events:
                    to_address: ChecksumAddress = Web3.toChecksumAddress(
                        transfer_event["args"]["to"]
                    )
                    from_address: ChecksumAddress = Web3.toChecksumAddress(
                        transfer_event["args"]["from"]
                    )
                    if to_address != EMPTY_ADDR_HEX:
                        accounts.add(to_address)
                    if from_address != EMPTY_ADDR_HEX:
                        accounts.add(from_address)

    return accounts


def get_erc20_token_balances(
    token: Contract, start_block: BlockNumber, end_block: BlockNumber
) -> Tuple[Dict[ChecksumAddress, Wei], Wei]:
    """Fetches balances of the ERC-20 token."""
    blocks_spread = 200_000
    balances: Dict[ChecksumAddress, Wei] = {}
    while start_block < end_block:
        for attempt in Retrying(
            reraise=True,
            wait=backoff,
            stop=stop_attempts,
            before_sleep=before_sleep_log(logger, logging.WARNING),
        ):
            with attempt:
                if start_block + blocks_spread >= end_block:
                    to_block: BlockNumber = end_block
                else:
                    to_block: BlockNumber = start_block + blocks_spread
                try:
                    transfer_events = token.events.Transfer.getLogs(
                        fromBlock=start_block, toBlock=to_block
                    )
                    start_block = to_block + 1
                except Exception as e:
                    blocks_spread = blocks_spread // 2
                    logger.warning(
                        f"Failed to fetch transfer events: from block={start_block}, to block={to_block},"
                        f" changing blocks spread to={blocks_spread}"
                    )
                    raise e

                for transfer_event in transfer_events:
                    to_address: ChecksumAddress = Web3.toChecksumAddress(
                        transfer_event["args"]["to"]
                    )
                    from_address: ChecksumAddress = Web3.toChecksumAddress(
                        transfer_event["args"]["from"]
                    )
                    value: Wei = transfer_event["args"]["value"]
                    if to_address != EMPTY_ADDR_HEX:
                        balances[to_address] = Wei(
                            balances.get(to_address, Wei(0)) + value
                        )
                    if from_address != EMPTY_ADDR_HEX:
                        balances[from_address] = Wei(
                            balances.get(from_address, Wei(0)) - value
                        )

    return balances, sum(balances.values())


@retry(
    reraise=True,
    wait=backoff,
    stop=stop_attempts,
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def get_reward_eth_token_balances(
    reward_eth_token: Contract,
    staked_eth_token: Contract,
    multicall: Contract,
    from_block: BlockNumber,
    to_block: BlockNumber,
) -> Tuple[Dict[ChecksumAddress, Wei], Wei]:
    """Fetches RewardEthToken balances and total supply excluding maintainer."""

    # fetch maintainer address and skip allocating reward to it
    maintainer = Web3.toChecksumAddress(
        reward_eth_token.functions.maintainer().call(block_identifier=to_block)
    )

    # fetch all the staked eth token addresses
    staked_eth_accounts: Set[ChecksumAddress] = get_token_participated_accounts(
        token=staked_eth_token,
        start_block=from_block,
        end_block=to_block,
    )

    # fetch all the reward eth token addresses
    reward_eth_accounts: Set[ChecksumAddress] = get_token_participated_accounts(
        token=reward_eth_token,
        start_block=from_block,
        end_block=to_block,
    )

    # fetch total supply, maintainer address
    all_accounts: List[ChecksumAddress] = list(
        staked_eth_accounts.union(reward_eth_accounts)
    )
    if not all_accounts:
        return {}, Wei(0)

    # fetch rETH2 balances for all accounts in batches
    balances: List[Wei] = []
    start_index = 0
    end_index = len(all_accounts)
    batch_size = 1000
    while start_index < end_index:
        for attempt in Retrying(
            reraise=True,
            wait=backoff,
            stop=stop_attempts,
            before_sleep=before_sleep_log(logger, logging.WARNING),
        ):
            with attempt:
                if start_index + batch_size >= end_index:
                    to_index = end_index
                else:
                    to_index = start_index + batch_size
                try:
                    accounts_batch = all_accounts[start_index:to_index]
                    # fetch rETH2 balance in batch call
                    calls = [
                        {
                            "target": reward_eth_token.address,
                            "callData": reward_eth_token.encodeABI(
                                fn_name="balanceOf", args=[account]
                            ),
                        }
                        for account in accounts_batch
                    ]
                    response = multicall.functions.aggregate(calls).call(
                        block_identifier=to_block
                    )
                    balances.extend(
                        [
                            reward_eth_token.web3.toInt(primitive=balance)
                            for balance in response[1]
                        ]
                    )
                    start_index = to_index
                except Exception as e:
                    prev_batch = batch_size
                    batch_size = prev_batch // 2
                    logger.warning(
                        f"Failed to fetch rETH2 balances for batch of {prev_batch} accounts,"
                        f" changing batch to={batch_size}"
                    )
                    raise e

    total_supply: Wei = Wei(0)
    all_balances: Dict[ChecksumAddress, Wei] = {}
    for account, balance in zip(all_accounts, balances):
        if account == maintainer or account == EMPTY_ADDR_HEX or balance <= 0:
            # count maintainer out to not assign rewards to it
            continue

        total_supply += balance
        all_balances[account] = balance

    return all_balances, total_supply


def get_ens_node_id(ens_name: str) -> bytes:
    """Calculates ENS node ID based on the domain name."""
    if not ens_name:
        return b"\0" * 32

    label, _, remainder = ens_name.partition(".")
    return Web3.keccak(primitive=get_ens_node_id(remainder) + Web3.keccak(text=label))


def get_merkle_node(
    w3: Web3,
    index: int,
    tokens: List[ChecksumAddress],
    account: ChecksumAddress,
    amounts: List[Wei],
) -> bytes:
    """Generates node for merkle tree."""
    encoded_data: bytes = w3.codec.encode_abi(
        ["uint256", "address[]", "address", "uint256[]"],
        [index, tokens, account, amounts],
    )
    return w3.keccak(primitive=encoded_data)


def pin_claims_to_ipfs(
    claims: Dict[ChecksumAddress, Dict],
    ipfs_endpoint: str,
) -> str:
    """Submits claims to the IPFS and pins the file."""
    with ipfshttpclient.connect(ipfs_endpoint) as client:
        ipfs_hash = client.add_json(claims)
        client.pin.add(ipfs_hash)

    if not ipfs_hash.startswith(IPFS_PREFIX):
        ipfs_hash = IPFS_PREFIX + ipfs_hash
    return ipfs_hash


def submit_oracle_merkle_root_vote(
    oracles: Contract,
    merkle_root: HexStr,
    merkle_proofs: str,
    current_nonce: int,
    transaction_timeout: int,
    gas: Wei,
    confirmation_blocks: int,
) -> None:
    """Submits new merkle root vote to `Oracles` contract."""
    tx_hash = None
    for attempt in Retrying(
        reraise=True,
        wait=backoff,
        stop=stop_attempts,
        before_sleep=before_sleep_log(logger, logging.WARNING),
    ):
        with attempt:
            try:
                # check whether gas price can be estimated for the the vote
                oracles.functions.voteForMerkleRoot(
                    current_nonce, merkle_root, merkle_proofs
                ).estimateGas({"gas": gas})
            except ContractLogicError as e:
                # check whether nonce has changed -> new merkle root was already submitted
                if current_nonce != oracles.functions.currentNonce().call():
                    return
                raise e

            if tx_hash is None:
                tx_hash = oracles.functions.voteForMerkleRoot(
                    current_nonce, merkle_root, merkle_proofs
                ).transact({"gas": gas})
            else:
                tx_hash = oracles.web3.eth.replace_transaction(tx_hash, {"gas": gas})

            wait_for_transaction(
                oracles=oracles,
                tx_hash=tx_hash,
                timeout=transaction_timeout,
                confirmation_blocks=confirmation_blocks,
            )
