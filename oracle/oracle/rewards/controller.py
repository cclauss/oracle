import asyncio
import logging
from datetime import datetime, timezone
from typing import Union

from aiohttp import ClientSession
from eth_account.signers.local import LocalAccount
from eth_typing import BlockNumber, HexStr
from web3 import Web3
from web3.types import Timestamp, Wei

from oracle.networks import GNOSIS_CHAIN
from oracle.oracle.rewards.types import RewardsVotingParameters, RewardVote
from oracle.oracle.utils import save
from oracle.oracle.vote import submit_vote
from oracle.settings import (
    MGNO_RATE,
    NETWORK,
    NETWORK_CONFIG,
    REWARD_VOTE_FILENAME,
    WAD,
)

from .eth1 import get_registered_validators_public_keys
from .eth2 import (
    PENDING_STATUSES,
    ValidatorStatus,
    get_finality_checkpoints,
    get_validators,
)

logger = logging.getLogger(__name__)
w3 = Web3()


class RewardsController(object):
    """Updates total rewards and activated validators number."""

    def __init__(
        self,
        aiohttp_session: ClientSession,
        genesis_timestamp: int,
        oracle: LocalAccount,
    ) -> None:
        self.deposit_amount: Wei = Web3.toWei(32, "ether")
        self.aiohttp_session = aiohttp_session
        self.genesis_timestamp = genesis_timestamp
        self.oracle = oracle
        self.sync_period = NETWORK_CONFIG["SYNC_PERIOD"]
        self.slots_per_epoch = NETWORK_CONFIG["SLOTS_PER_EPOCH"]
        self.seconds_per_epoch = (
            self.slots_per_epoch * NETWORK_CONFIG["SECONDS_PER_SLOT"]
        )
        self.deposit_token_symbol = NETWORK_CONFIG["DEPOSIT_TOKEN_SYMBOL"]
        self.last_vote_total_rewards = None

    @save
    async def process(
        self,
        voting_params: RewardsVotingParameters,
        current_block_number: BlockNumber,
        current_timestamp: Timestamp,
    ) -> None:
        """Submits vote for the new total rewards and activated validators to the IPFS."""
        # check whether it's voting time
        last_update_time = datetime.utcfromtimestamp(
            voting_params["rewards_updated_at_timestamp"]
        )
        next_update_time: datetime = last_update_time + self.sync_period
        current_time: datetime = datetime.utcfromtimestamp(current_timestamp)
        while next_update_time + self.sync_period <= current_time:
            next_update_time += self.sync_period

        # skip submitting vote if too early or vote has been already submitted
        if next_update_time > current_time:
            return

        # fetch pool validator BLS public keys
        public_keys = await get_registered_validators_public_keys(current_block_number)

        # calculate current ETH2 epoch
        update_timestamp = int(
            next_update_time.replace(tzinfo=timezone.utc).timestamp()
        )
        update_epoch: int = (
            update_timestamp - self.genesis_timestamp
        ) // self.seconds_per_epoch

        logger.info(
            f"Voting for new total rewards with parameters:"
            f" timestamp={update_timestamp}, epoch={update_epoch}"
        )

        # wait for the epoch to get finalized
        checkpoints = await get_finality_checkpoints(self.aiohttp_session)
        while update_epoch > int(checkpoints["finalized"]["epoch"]):
            logger.info(f"Waiting for the epoch {update_epoch} to finalize...")
            await asyncio.sleep(360)
            checkpoints = await get_finality_checkpoints(self.aiohttp_session)

        state_id = str(update_epoch * self.slots_per_epoch)
        total_rewards: Wei = voting_params["total_fees"]
        activated_validators = 0
        chunk_size = NETWORK_CONFIG["VALIDATORS_FETCH_CHUNK_SIZE"]

        # fetch balances in chunks
        for i in range(0, len(public_keys), chunk_size):
            validators = await get_validators(
                session=self.aiohttp_session,
                public_keys=public_keys[i : i + chunk_size],
                state_id=state_id,
            )
            for validator in validators:
                if ValidatorStatus(validator["status"]) in PENDING_STATUSES:
                    continue

                activated_validators += 1
                validator_reward = (
                    Web3.toWei(validator["balance"], "gwei") - self.deposit_amount
                )
                if NETWORK == GNOSIS_CHAIN:
                    # apply mGNO <-> GNO exchange rate
                    validator_reward = Wei(int(validator_reward * WAD // MGNO_RATE))
                total_rewards += validator_reward

        pretty_total_rewards = self.format_ether(total_rewards)
        logger.info(
            f"Retrieved pool validator rewards:"
            f" total={pretty_total_rewards},"
            f" fees={self.format_ether(voting_params['total_fees'])}"
        )
        if not total_rewards:
            logger.info("No staking rewards, waiting for validators to be activated...")
            return

        if total_rewards < voting_params["total_rewards"]:
            # rewards were reduced -> don't mint new ones
            logger.warning(
                f"Total rewards decreased since the previous update:"
                f" current={pretty_total_rewards},"
                f' previous={self.format_ether(voting_params["total_rewards"])}'
            )
            total_rewards = voting_params["total_rewards"]
            pretty_total_rewards = self.format_ether(total_rewards)

        # submit vote
        logger.info(
            f"Submitting rewards vote:"
            f" nonce={voting_params['rewards_nonce']},"
            f" total rewards={pretty_total_rewards},"
            f" activated validators={activated_validators}"
        )

        current_nonce = voting_params["rewards_nonce"]
        encoded_data: bytes = w3.codec.encode_abi(
            ["uint256", "uint256", "uint256"],
            [current_nonce, activated_validators, total_rewards],
        )
        vote = RewardVote(
            signature=HexStr(""),
            nonce=current_nonce,
            activated_validators=activated_validators,
            total_rewards=str(total_rewards),
        )
        submit_vote(
            oracle=self.oracle,
            encoded_data=encoded_data,
            vote=vote,
            name=REWARD_VOTE_FILENAME,
        )
        logger.info("Rewards vote has been successfully submitted")

        self.last_vote_total_rewards = total_rewards

    def format_ether(self, value: Union[str, int, Wei]) -> str:
        """Converts Wei value."""
        _value = int(value)
        if _value < 0:
            formatted_value = f'-{Web3.fromWei(abs(_value), "ether")}'
        else:
            formatted_value = f'{Web3.fromWei(_value, "ether")}'

        return f"{formatted_value} {self.deposit_token_symbol}"
