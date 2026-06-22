// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title IVotingPower — stake-weighted voting power source
/// @notice Abstracts WHERE validator voting power comes from, so the governance
/// rule (how a slash/deny passes) is independent of how power is measured.
///
/// The default `ValidatorVotingPower` is an owner-synced mirror of the SN89
/// validator set's stake, updated each weights tempo. It can later be replaced
/// — without touching `CollateralVoting` — by a source that reads validator
/// stake live from the Subtensor Metagraph precompile, once that integration
/// is settled.
interface IVotingPower {
    /// @notice Voting weight of `validator`. Zero means "not a validator / may
    /// not vote". Units are arbitrary (e.g. rao of stake); only ratios matter.
    function votingPowerOf(address validator) external view returns (uint256);

    /// @notice Sum of every validator's voting power. Quorum is measured as a
    /// fraction of this total, snapshotted when a proposal is created.
    function totalVotingPower() external view returns (uint256);
}
