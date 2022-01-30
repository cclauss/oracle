import copy
import logging
from typing import List, Set

from ens.constants import EMPTY_ADDR_HEX
from eth_typing import BlockNumber, ChecksumAddress

from oracle.oracle.settings import (
    DISTRIBUTOR_FALLBACK_ADDRESS,
    RARI_FUSE_POOL_ADDRESSES,
    REWARD_TOKEN_CONTRACT_ADDRESS,
    STAKED_TOKEN_CONTRACT_ADDRESS,
    SWISE_TOKEN_CONTRACT_ADDRESS,
)

from .rari import get_rari_fuse_liquidity_points
from .types import Balances, Rewards, UniswapV3Pools
from .uniswap_v3 import (
    get_uniswap_v3_liquidity_points,
    get_uniswap_v3_range_liquidity_points,
    get_uniswap_v3_single_token_balances,
)

logger = logging.getLogger(__name__)


class DistributorRewards(object):
    def __init__(
        self,
        uniswap_v3_pools: UniswapV3Pools,
        from_block: BlockNumber,
        to_block: BlockNumber,
        reward_token: ChecksumAddress,
        uni_v3_token: ChecksumAddress,
    ) -> None:
        self.uni_v3_staked_token_pools = uniswap_v3_pools["staked_token_pools"]
        self.uni_v3_reward_token_pools = uniswap_v3_pools["reward_token_pools"]
        self.uni_v3_swise_pools = uniswap_v3_pools["swise_pools"]
        self.uni_v3_pools = self.uni_v3_swise_pools.union(
            self.uni_v3_staked_token_pools
        ).union(self.uni_v3_reward_token_pools)
        self.from_block = from_block
        self.to_block = to_block
        self.uni_v3_token = uni_v3_token
        self.reward_token = reward_token

    def is_supported_contract(self, contract_address: ChecksumAddress) -> bool:
        """Checks whether the provided contract address is supported."""
        return (
            contract_address in self.uni_v3_pools
            or contract_address == SWISE_TOKEN_CONTRACT_ADDRESS
            or contract_address in RARI_FUSE_POOL_ADDRESSES
        )

    @staticmethod
    def add_value(
        rewards: Rewards,
        to: ChecksumAddress,
        reward_token: ChecksumAddress,
        amount: int,
    ) -> None:
        """Adds reward token to the beneficiary address."""
        prev_amount = rewards.setdefault(to, {}).setdefault(reward_token, "0")
        rewards[to][reward_token] = str(int(prev_amount) + amount)

    @staticmethod
    def merge_rewards(rewards1: Rewards, rewards2: Rewards) -> Rewards:
        """Merges two dictionaries into one."""
        merged_rewards: Rewards = copy.deepcopy(rewards1)
        for account, account_rewards in rewards2.items():
            for reward_token, value in account_rewards.items():
                DistributorRewards.add_value(
                    rewards=merged_rewards,
                    to=account,
                    reward_token=reward_token,
                    amount=int(value),
                )

        return merged_rewards

    async def get_rewards(
        self, contract_address: ChecksumAddress, reward: int
    ) -> Rewards:
        """Calculates reward for every account recursively and aggregates amounts."""
        if reward <= 0:
            return {}

        if self.is_supported_contract(contract_address):
            return await self._get_rewards(
                contract_address=contract_address,
                total_reward=reward,
                visited={contract_address},
            )

        # unknown allocation -> assign to the rescue address
        rewards: Rewards = {}
        self.add_value(
            rewards=rewards,
            to=DISTRIBUTOR_FALLBACK_ADDRESS,
            reward_token=self.reward_token,
            amount=reward,
        )

        return rewards

    async def get_balances(self, contract_address: ChecksumAddress) -> Balances:
        """Fetches balances and total supply of the contract."""
        if (
            self.uni_v3_token == STAKED_TOKEN_CONTRACT_ADDRESS
            and contract_address in self.uni_v3_staked_token_pools
        ):
            logger.info(
                f"Fetching Uniswap V3 staked token balances: pool={contract_address}"
            )
            return await get_uniswap_v3_single_token_balances(
                pool_address=contract_address,
                token=STAKED_TOKEN_CONTRACT_ADDRESS,
                block_number=self.to_block,
            )
        elif (
            self.uni_v3_token == REWARD_TOKEN_CONTRACT_ADDRESS
            and contract_address in self.uni_v3_reward_token_pools
        ):
            logger.info(
                f"Fetching Uniswap V3 reward token balances: pool={contract_address}"
            )
            return await get_uniswap_v3_single_token_balances(
                pool_address=contract_address,
                token=REWARD_TOKEN_CONTRACT_ADDRESS,
                block_number=self.to_block,
            )
        elif (
            self.uni_v3_token == SWISE_TOKEN_CONTRACT_ADDRESS
            and contract_address in self.uni_v3_swise_pools
        ):
            logger.info(f"Fetching Uniswap V3 SWISE balances: pool={contract_address}")
            return await get_uniswap_v3_single_token_balances(
                pool_address=contract_address,
                token=SWISE_TOKEN_CONTRACT_ADDRESS,
                block_number=self.to_block,
            )
        elif (
            self.uni_v3_token == EMPTY_ADDR_HEX
            and contract_address in self.uni_v3_swise_pools
        ):
            logger.info(
                f"Fetching Uniswap V3 full range liquidity points: pool={contract_address}"
            )
            return await get_uniswap_v3_range_liquidity_points(
                tick_lower=-887220,
                tick_upper=887220,
                pool_address=contract_address,
                block_number=self.to_block,
            )
        elif contract_address in self.uni_v3_pools:
            logger.info(
                f"Fetching Uniswap V3 liquidity points: pool={contract_address}"
            )
            return await get_uniswap_v3_liquidity_points(
                pool_address=contract_address,
                block_number=self.to_block,
            )
        elif contract_address in RARI_FUSE_POOL_ADDRESSES:
            logger.info(
                f"Fetching Rari Fuse Pool liquidity points: pool={contract_address}"
            )
            return await get_rari_fuse_liquidity_points(
                ctoken_address=contract_address,
                from_block=self.from_block,
                to_block=self.to_block,
            )

        raise ValueError(
            f"Cannot get balances for unsupported contract address {contract_address}"
        )

    async def _get_rewards(
        self,
        contract_address: ChecksumAddress,
        total_reward: int,
        visited: Set[ChecksumAddress],
    ) -> Rewards:
        rewards: Rewards = {}

        # fetch user balances and total supply for reward portions calculation
        result = await self.get_balances(contract_address)
        total_supply = result["total_supply"]
        if total_supply <= 0:
            # no recipients for the rewards -> assign reward to the rescue address
            self.add_value(
                rewards=rewards,
                to=DISTRIBUTOR_FALLBACK_ADDRESS,
                reward_token=self.reward_token,
                amount=total_reward,
            )
            return rewards

        balances = result["balances"]

        # distribute rewards to the users or recurse for the supported contracts
        total_distributed = 0
        accounts: List[ChecksumAddress] = sorted(balances.keys())
        last_account_index = len(accounts) - 1
        for i, account in enumerate(accounts):
            if i == last_account_index:
                account_reward = total_reward - total_distributed
            else:
                balance = balances[account]
                account_reward = (total_reward * balance) // total_supply

            if account_reward <= 0:
                continue

            if account == contract_address or account in visited:
                # failed to assign reward -> return it to rescue address
                self.add_value(
                    rewards=rewards,
                    to=DISTRIBUTOR_FALLBACK_ADDRESS,
                    reward_token=self.reward_token,
                    amount=account_reward,
                )
            elif self.is_supported_contract(account):
                # recurse into the supported contract
                new_rewards = await self._get_rewards(
                    contract_address=account,
                    total_reward=account_reward,
                    visited=visited.union({account}),
                )
                rewards = self.merge_rewards(rewards, new_rewards)
            else:
                self.add_value(
                    rewards=rewards,
                    to=account,
                    reward_token=self.reward_token,
                    amount=account_reward,
                )

            total_distributed += account_reward

        return rewards
