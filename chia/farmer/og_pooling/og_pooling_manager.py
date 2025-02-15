from __future__ import annotations

import asyncio
import traceback
from asyncio import Task, gather, sleep
from logging import Logger, getLogger
from time import time
from typing import Any, Dict, List, Optional

from chia_rs import AugSchemeMPL, G1Element, G2Element, PrivateKey

from chia.consensus.constants import ConsensusConstants
from chia.consensus.pot_iterations import calculate_iterations_quality, calculate_sp_interval_iters
from chia.farmer.og_pooling.og_pool_protocol import PartialPayload, SubmitPartial
from chia.farmer.og_pooling.og_pool_state import OgPoolState
from chia.farmer.og_pooling.pool_api_client import PoolApiClient
from chia.harvester.harvester_api import HarvesterAPI
from chia.protocols import harvester_protocol
from chia.protocols.harvester_protocol import SignatureRequestSourceData, SigningDataKind
from chia.server.ws_connection import WSChiaConnection
from chia.types.blockchain_format.proof_of_space import generate_plot_public_key
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint8, uint64, uint128
from chia.util.task_referencer import create_referenced_task

DEFAULT_OG_POOL_URL: str = "https://farmer-chia-og.foxypool.io"


class OgPoolingManager:
    @property
    def is_pooling_enabled(self):
        return not self._is_og_pooling_disabled

    @property
    def pool_target_puzzle_hash(self) -> bytes32:
        return self._pool_target_puzzle_hash

    @property
    def current_difficulty(self) -> uint64:
        return self._pool_state.difficulty

    _logger: Logger = getLogger("og_pooling_manager")
    _iters_limit: uint64
    _difficulty_constant_factor: uint128
    _pool_url: str
    _pool_payout_address: str
    _pool_target_puzzle_hash: bytes32
    _is_og_pooling_disabled: bool
    _pool_minimum_difficulty: uint64
    _pool_state: OgPoolState
    _pool_api_client: PoolApiClient
    _pool_and_farmer_private_keys: List[PrivateKey]
    _pool_and_farmer_private_keys_map: Dict[bytes, PrivateKey]
    _pool_public_keys: List[G1Element]
    _pool_var_diff_target_in_seconds: int = 5 * 60
    _shut_down: bool = False
    _update_pool_info_task: Optional[Task] = None
    _update_pool_difficulty_task: Optional[Task] = None

    def __init__(
        self,
        consensus_constants: ConsensusConstants,
        farmer_config: Dict[str, Any],
        farmer_reward_target_address: str,
        pool_xch_reward_target_puzzle_hash: bytes32,
        pool_and_farmer_private_keys: List[PrivateKey],
        pool_public_keys: List[G1Element],
    ):
        self._iters_limit = calculate_sp_interval_iters(consensus_constants, consensus_constants.POOL_SUB_SLOT_ITERS)
        self._difficulty_constant_factor = consensus_constants.DIFFICULTY_CONSTANT_FACTOR
        self._pool_url = farmer_config.get("pool_url", DEFAULT_OG_POOL_URL)
        self.update_pool_payout_address(farmer_config=farmer_config, farmer_reward_target_address=farmer_reward_target_address)
        self._is_og_pooling_disabled = farmer_config.get("disable_og_pooling", False)
        self._pool_minimum_difficulty = uint64(1)
        self._pool_state = OgPoolState(difficulty=self._pool_minimum_difficulty)
        self._pool_target_puzzle_hash = pool_xch_reward_target_puzzle_hash
        self._pool_api_client = PoolApiClient(self._pool_url)
        self._pool_and_farmer_private_keys = pool_and_farmer_private_keys
        self._pool_and_farmer_private_keys_map = {bytes(key.get_g1()): key for key in self._pool_and_farmer_private_keys}
        self._pool_public_keys = pool_public_keys

    async def initialize_pooling(self):
        if self._is_og_pooling_disabled:
            self._logger.info(f"Not OG pooling as 'disable_og_pooling' is set to true in your config")

            return

        self._shut_down = False
        self._logger.debug(f"Connecting to OG pool {self._pool_url} ..")
        is_connected_to_og_pool = False
        while not is_connected_to_og_pool:
            try:
                pool_info = await self._pool_api_client.get_pool_info()
                await self._update_pool_info(pool_info)
                pool_name = pool_info["name"]
                self._logger.info(f"Connected to OG pool {pool_name} ({self._pool_url}) using payout address {self._pool_payout_address}")
                is_connected_to_og_pool = True
            except asyncio.TimeoutError:
                self._logger.error(f"Timed out while retrieving OG pool info")
                await sleep(5)
            except Exception as e:
                tb = traceback.format_exc()
                self._logger.error(f"Error connecting to the OG pool: {e} {tb}")
                await sleep(5)

        self._pool_state.difficulty = self._pool_minimum_difficulty
        await self._update_pool_difficulty()

        self._update_pool_difficulty_task = create_referenced_task(self._periodically_update_pool_difficulty_task())
        self._update_pool_info_task = create_referenced_task(self._periodically_update_pool_info_task())

    async def process_new_proof_of_space_for_og_pool(
        self,
        new_proof_of_space: harvester_protocol.NewProofOfSpace,
        peer: WSChiaConnection,
        pool_public_key: G1Element,
        computed_quality_string: bytes32
    ):
        # Otherwise, send the proof of space to the pool
        # When we win a block, we also send the partial to the pool
        required_iters = calculate_iterations_quality(
            self._difficulty_constant_factor,
            computed_quality_string,
            new_proof_of_space.proof.size,
            self._pool_state.difficulty,
            new_proof_of_space.sp_hash,
        )
        if required_iters >= self._iters_limit:
            self._logger.debug(
                f"Proof of space not good enough for OG pool difficulty of {self._pool_state.difficulty}"
            )
            return

        # Submit partial to pool
        is_eos = new_proof_of_space.signage_point_index == 0
        payload = PartialPayload(
            new_proof_of_space.proof,
            new_proof_of_space.sp_hash,
            is_eos,
            self._pool_payout_address,
            peer.peer_node_id,
        )

        # The plot key is 2/2 so we need the harvester's half of the signature
        m_to_sign = payload.get_hash()
        m_src_data: Optional[List[Optional[SignatureRequestSourceData]]] = None

        if (  # pragma: no cover
                new_proof_of_space.include_source_signature_data
                or new_proof_of_space.farmer_reward_address_override is not None
        ):
            m_src_data = [SignatureRequestSourceData(uint8(SigningDataKind.PARTIAL), bytes(payload))]

        request = harvester_protocol.RequestSignatures(
            new_proof_of_space.plot_identifier,
            new_proof_of_space.challenge_hash,
            new_proof_of_space.sp_hash,
            [m_to_sign],
            message_data=m_src_data,
            rc_block_unfinished=None,
        )
        response: Any = await peer.call_api(HarvesterAPI.request_signatures, request)
        if not isinstance(response, harvester_protocol.RespondSignatures):
            self._logger.error(f"Invalid response from harvester: {response}")
            return

        assert len(response.message_signatures) == 1

        plot_signature: Optional[G2Element] = None
        for sk in self._pool_and_farmer_private_keys:
            pk = sk.get_g1()
            if pk == response.farmer_pk:
                agg_pk = generate_plot_public_key(response.local_pk, pk)
                assert agg_pk == new_proof_of_space.proof.plot_public_key
                sig_farmer = AugSchemeMPL.sign(sk, m_to_sign, agg_pk)
                plot_signature = AugSchemeMPL.aggregate([sig_farmer, response.message_signatures[0][1]])
                assert AugSchemeMPL.verify(agg_pk, m_to_sign, plot_signature)
        pool_sk = self._pool_and_farmer_private_keys_map[bytes(pool_public_key)]
        authentication_signature = AugSchemeMPL.sign(pool_sk, m_to_sign)

        assert plot_signature is not None
        agg_sig: G2Element = AugSchemeMPL.aggregate([plot_signature, authentication_signature])

        submit_partial = SubmitPartial(payload, agg_sig, self._pool_state.difficulty)
        self._logger.debug("Submitting partial to OG pool ..")
        self._pool_state.last_partial_submit_timestamp = time()
        submit_partial_response: Dict[str, Any]
        try:
            submit_partial_response = await self._pool_api_client.submit_partial(submit_partial)
        except asyncio.TimeoutError:
            self._logger.error(f"Timed out while submitting partial to OG pool")
            return
        except Exception as e:
            self._logger.error(f"Error submitting partial to OG pool: {e}")
            return
        self._logger.debug(f"OG pool response: {submit_partial_response}")
        if "error_code" in submit_partial_response:
            if submit_partial_response["error_code"] == 5:
                self._logger.info(
                    "Local OG pool difficulty too low, adjusting to OG pool difficulty "
                    f"({submit_partial_response['current_difficulty']})"
                )
                self._pool_state.difficulty = uint64(submit_partial_response["current_difficulty"])
            else:
                self._logger.error(
                    f"Error in OG pooling: {submit_partial_response['error_code'], submit_partial_response['error_message']}"
                )
        else:
            self._logger.info("The partial submitted to the OG pool was accepted")
            self._pool_state.difficulty = uint64(submit_partial_response["current_difficulty"])

    def update_pool_payout_address(self, farmer_config: Dict[str, Any], farmer_reward_target_address: str):
        self._pool_payout_address = farmer_config.get("pool_payout_address", farmer_reward_target_address)

    async def shutdown(self):
        self._shut_down = True
        if self._update_pool_info_task is not None:
            await self._update_pool_info_task
            self._update_pool_info_task = None
        if self._update_pool_difficulty_task is not None:
            await self._update_pool_difficulty_task
            self._update_pool_difficulty_task = None

    async def _update_pool_info(self, pool_info: Optional[Dict[str, Any]] = None):
        did_request_new_pool_info = False
        if pool_info is None:
            try:
                pool_info = await self._pool_api_client.get_pool_info()
                did_request_new_pool_info = True
            except asyncio.TimeoutError:
                self._logger.error(f"Timed out while retrieving OG pool info")
                return
            except Exception as e:
                self._logger.error(f"Error retrieving OG pool info: {e}")
                return

        assert pool_info.get("var_diff_target_in_seconds") is not None
        self._pool_var_diff_target_in_seconds = pool_info["var_diff_target_in_seconds"]
        assert pool_info.get("minimum_difficulty") is not None
        self._pool_minimum_difficulty = uint64(pool_info["minimum_difficulty"])
        assert pool_info.get("target_puzzle_hash") is not None
        pool_target_puzzle_hash = bytes32.from_hexstr(pool_info["target_puzzle_hash"])
        assert len(pool_target_puzzle_hash) == 32
        self._pool_target_puzzle_hash = pool_target_puzzle_hash

        if did_request_new_pool_info:
            self._logger.info(f"Updated the OG pool_info successfully")

    async def _update_pool_difficulty(self):
        difficulties_with_none = await gather(*[self._get_og_farmer_difficulty(pool_public_key) for pool_public_key in self._pool_public_keys])
        difficulties = list(filter(lambda difficulty: difficulty is not None, difficulties_with_none))
        if len(difficulties) == 0:
            return
        difficulty_to_use = max(difficulties)
        if self._pool_state.difficulty != difficulty_to_use:
            self._logger.info(f"Updating OG pool difficulty from {self._pool_state.difficulty} to {difficulty_to_use}")
            self._pool_state.difficulty = difficulty_to_use

    async def _get_og_farmer_difficulty(self, pool_public_key: G1Element) -> Optional[uint64]:
        self._logger.debug(f"Trying to obtain difficulty for pool public key {pool_public_key}")
        try:
            result: Dict = await self._pool_api_client.get_farmer(f"{pool_public_key}")
            if "current_difficulty" not in result:
                return
            difficulty = uint64(result["current_difficulty"])
            self._logger.debug(f"Obtained difficulty {difficulty} for pool public key {pool_public_key}")

            return difficulty
        except Exception:
            pass

    async def _periodically_update_pool_info_task(self):
        time_slept = 0
        while not self._shut_down:
            # Sleep in 1 sec intervals to quickly exit outer loop, but effectively sleep 1h between actual code runs
            await sleep(1)
            time_slept += 1
            if time_slept < 60 * 60:
                continue
            time_slept = 0
            try:
                await self._update_pool_info()
            except Exception as e:
                tb = traceback.format_exc()
                self._logger.error(f"Exception in update_og_pool_info, {e} {tb}")

    async def _periodically_update_pool_difficulty_task(self):
        time_slept = 0
        while not self._shut_down:
            # Sleep in 1 sec intervals to quickly exit outer loop, but effectively sleep 60 sec between actual code runs
            await sleep(1)
            time_slept += 1
            if time_slept < 5 * 60:
                continue
            time_slept = 0
            if (time() - self._pool_state.last_partial_submit_timestamp) < self._pool_var_diff_target_in_seconds:
                continue
            await self._update_pool_difficulty()
