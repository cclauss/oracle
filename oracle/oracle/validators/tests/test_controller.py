from unittest.mock import patch

from web3 import Web3
from web3.types import BlockNumber

from oracle.oracle.tests.common import get_test_oracle
from oracle.oracle.tests.factories import faker

from ..controller import ValidatorsController
from ..types import ValidatorVotingParameters

w3 = Web3()
block_number = faker.random_int(150000, 250000)


def select_operators(operator, *args, **kwargs):
    return {
        "operators": [
            {
                "id": operator,  # operator
                "depositDataMerkleProofs": "/ipfs/" + faker.text(max_nb_chars=20),
                "depositDataIndex": "5",
            },
        ]
    }


def select_validators(*args, **kwargs):
    return {"validators": []}


def can_registor_validator(*args, **kwargs):
    return {"validatorRegistrations": []}


def ipfs_fetch(
    deposit_data_root,
    public_key,
    signature,
    withdrawal_credentials,
    proofs,
):
    return [
        {
            "amount": str(32 * 10**9),
            "deposit_data_root": deposit_data_root,
            "proof": proofs,
            "public_key": public_key,
            "signature": signature,
            "withdrawal_credentials": withdrawal_credentials,
        }
    ] * 6


def ipfs_fetch_query(
    deposit_data_root,
    public_key,
    signature,
    withdrawal_credentials,
    proofs,
):

    return [
        ipfs_fetch(
            deposit_data_root, public_key, signature, withdrawal_credentials, proofs
        )
    ]


def get_validators_deposit_root(validatorsDepositRoot, *args, **kwargs):
    return {
        "validatorRegistrations": [{"validatorsDepositRoot": validatorsDepositRoot}]
    }


def sw_gql_query(operator):
    return [
        select_operators(operator),
        select_validators(),
    ]


def ethereum_gql_query(validatorsDepositRoot, *args, **kwargs):
    return [
        can_registor_validator(),
        get_validators_deposit_root(validatorsDepositRoot),
    ]


class TestValidatorController:
    async def test_process_low_balance(self):
        with patch("oracle.oracle.vote.submit_vote", return_value=None) as vote_mock:
            controller = ValidatorsController(
                oracle=get_test_oracle(),
            )
            await controller.process(
                voting_params=ValidatorVotingParameters(
                    validators_nonce=faker.random_int(1000, 2000),
                    pool_balance=w3.toWei(31, "ether"),
                ),
                block_number=BlockNumber(14583706),
            )
            assert vote_mock.mock_calls == []

    async def test_process_success(self):
        validators_nonce = faker.random_int(1000, 2000)

        vote = {
            "signature": "",
            "nonce": validators_nonce,
            "validators_deposit_root": faker.eth_proof(),
            "deposit_data": [
                {
                    "operator": faker.eth_address(),
                    "public_key": faker.eth_public_key(),
                    "withdrawal_credentials": faker.eth_address(),
                    "deposit_data_root": faker.eth_proof(),
                    "deposit_data_signature": faker.eth_signature(),
                    "proof": [faker.eth_proof()] * 6,
                }
            ],
        }
        with patch(
            "oracle.oracle.validators.eth1.execute_sw_gql_query",
            side_effect=sw_gql_query(operator=vote["deposit_data"][0]["operator"]),
        ), patch(
            "oracle.oracle.validators.eth1.execute_ethereum_gql_query",
            side_effect=ethereum_gql_query(
                validatorsDepositRoot=vote["validators_deposit_root"]
            ),
        ), patch(
            "oracle.oracle.validators.eth1.ipfs_fetch",
            side_effect=ipfs_fetch_query(
                deposit_data_root=vote["deposit_data"][0]["deposit_data_root"],
                public_key=vote["deposit_data"][0]["public_key"],
                signature=vote["deposit_data"][0]["deposit_data_signature"],
                withdrawal_credentials=vote["deposit_data"][0][
                    "withdrawal_credentials"
                ],
                proofs=vote["deposit_data"][0]["proof"],
            ),
        ), patch(
            "oracle.oracle.validators.controller.NETWORK", "goerli"
        ), patch(
            "oracle.oracle.validators.controller.submit_vote", return_value=None
        ) as vote_mock:
            controller = ValidatorsController(
                oracle=get_test_oracle(),
            )
            await controller.process(
                voting_params=ValidatorVotingParameters(
                    validators_nonce=validators_nonce,
                    pool_balance=w3.toWei(33, "ether"),
                ),
                block_number=BlockNumber(14583706),
            )

            encoded_data: bytes = w3.codec.encode_abi(
                ["uint256", "(address,bytes32,bytes32,bytes,bytes)[]", "bytes32"],
                [
                    vote["nonce"],
                    [
                        (
                            vote["deposit_data"][0]["operator"],
                            vote["deposit_data"][0]["withdrawal_credentials"],
                            vote["deposit_data"][0]["deposit_data_root"],
                            vote["deposit_data"][0]["public_key"],
                            vote["deposit_data"][0]["deposit_data_signature"],
                        )
                    ],
                    vote["validators_deposit_root"],
                ],
            )

            vote_mock.assert_called()
            validator_vote = dict(
                oracle=get_test_oracle(),
                encoded_data=encoded_data,
                vote=vote,
                name="validator-vote.json",
            )
            vote_mock.assert_called_once_with(**validator_vote)
