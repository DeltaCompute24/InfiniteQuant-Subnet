// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title ISlashTarget — disposition sink for slashed collateral
/// @notice Abstracts WHAT happens to seized collateral away from the decision
/// to seize it. The slash vote only moves a miner's collateral into the
/// contract-held `seized` pool; this target decides where that pool ultimately
/// goes when governance disburses it.
///
///   - v1: `BurnTarget` — destroy it (reproduces the current SN89 burn policy).
///   - v2: an SN8 staking adapter — turn seized TAO into subnet-8 trading
///         capital once an eliminated SN89 miner's collateral is finalised.
///
/// Switching from burn to stake-into-SN8 is a single governed `setSlashTarget`
/// call. `CollateralVoting` itself never changes.
interface ISlashTarget {
    /// @notice Receive and dispose of `amount` wei of seized collateral. Called
    /// by `CollateralVoting` with `msg.value == amount`.
    /// @param miner    the miner the seized funds originated from (audit trail;
    ///                 `address(0)` when disbursing an aggregate pool).
    /// @param amount   wei being disbursed; equals `msg.value`.
    /// @param evidenceUrl  off-chain evidence pointer carried for the log.
    /// @param evidenceMd5  md5 checksum of the evidence content.
    function onSlashDisbursed(
        address miner,
        uint256 amount,
        string calldata evidenceUrl,
        bytes16 evidenceMd5
    ) external payable;
}
