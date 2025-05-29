from __future__ import annotations

from dataclasses import dataclass

from chia_rs import G2Element

from chia.types.blockchain_format.proof_of_space import ProofOfSpace
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64
from chia.util.streamable import Streamable, streamable


@streamable
@dataclass(frozen=True)
class PartialPayload(Streamable):
    proof_of_space: ProofOfSpace
    sp_hash: bytes32
    end_of_sub_slot: bool
    payout_address: str  # The farmer can choose where to send the rewards. This can take a few minutes
    harvester_id: bytes32


@streamable
@dataclass(frozen=True)
class SubmitPartial(Streamable):
    payload: PartialPayload
    partial_aggregate_signature: G2Element  # Sig of partial by plot key and pool key
    difficulty: uint64
