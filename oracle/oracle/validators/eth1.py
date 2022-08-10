from typing import Dict, Set, Union

from eth_typing import HexStr
from web3 import Web3
from web3.types import BlockNumber

from oracle.oracle.common.clients import (
    execute_ethereum_gql_query,
    execute_sw_gql_query,
)
from oracle.oracle.common.graphql_queries import (
    OPERATORS_QUERY,
    VALIDATOR_REGISTRATIONS_LATEST_INDEX_QUERY,
    VALIDATOR_REGISTRATIONS_QUERY,
)
from oracle.oracle.common.ipfs import ipfs_fetch
from oracle.settings import NETWORK

from .types import ValidatorDepositData


async def select_validator(
    block_number: BlockNumber, used_pubkeys: Set[HexStr]
) -> Union[None, ValidatorDepositData]:
    """Selects the next validator to register."""
    result: Dict = await execute_sw_gql_query(
        network=NETWORK,
        query=OPERATORS_QUERY,
        variables=dict(block_number=block_number),
    )
    operators = result["operators"]
    for operator in operators:
        merkle_proofs = operator["depositDataMerkleProofs"]
        if not merkle_proofs:
            continue

        operator_address = Web3.toChecksumAddress(operator["id"])
        deposit_data_index = int(operator["depositDataIndex"])
        deposit_datum = await ipfs_fetch(merkle_proofs)

        max_deposit_data_index = len(deposit_datum) - 1
        if deposit_data_index > max_deposit_data_index:
            continue

        selected_deposit_data = deposit_datum[deposit_data_index]
        public_key = selected_deposit_data["public_key"]
        can_register = public_key not in used_pubkeys and await can_register_validator(
            block_number, public_key
        )
        while deposit_data_index < max_deposit_data_index and not can_register:
            # the edge case when the validator was registered in previous merkle root
            # and the deposit data is presented in the same.
            deposit_data_index += 1
            selected_deposit_data = deposit_datum[deposit_data_index]
            public_key = selected_deposit_data["public_key"]
            can_register = (
                public_key not in used_pubkeys
                and await can_register_validator(block_number, public_key)
            )

        if can_register:
            return ValidatorDepositData(
                operator=operator_address,
                public_key=selected_deposit_data["public_key"],
                withdrawal_credentials=selected_deposit_data["withdrawal_credentials"],
                deposit_data_root=selected_deposit_data["deposit_data_root"],
                deposit_data_signature=selected_deposit_data["signature"],
                proof=selected_deposit_data["proof"],
            )
    return None


async def can_register_validator(block_number: BlockNumber, public_key: HexStr) -> bool:
    """Checks whether it's safe to register the validator."""
    result: Dict = await execute_ethereum_gql_query(
        network=NETWORK,
        query=VALIDATOR_REGISTRATIONS_QUERY,
        variables=dict(block_number=block_number, public_key=public_key),
    )
    registrations = result["validatorRegistrations"]

    return len(registrations) == 0


async def get_validators_deposit_root(block_number: BlockNumber) -> HexStr:
    """Fetches validators deposit root for protecting against operator submitting deposit prior to registration."""
    result: Dict = await execute_ethereum_gql_query(
        network=NETWORK,
        query=VALIDATOR_REGISTRATIONS_LATEST_INDEX_QUERY,
        variables=dict(block_number=block_number),
    )
    return result["validatorRegistrations"][0]["validatorsDepositRoot"]
