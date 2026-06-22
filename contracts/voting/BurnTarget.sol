// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ISlashTarget} from "./ISlashTarget.sol";

/// @title BurnTarget — v1 disposition: destroy seized collateral
/// @notice Forwards disbursed collateral to the zero address, reproducing the
/// `burn_alpha` semantics of the current SN89 slash policy: a slash removes the
/// capital from circulation rather than redistributing it.
///
/// To stop burning and instead repurpose seized collateral as SN8 trading
/// capital, deploy an SN8 staking adapter implementing {ISlashTarget} and point
/// `CollateralVoting` at it with `setSlashTarget` — no change here, no change to
/// the collateral contract.
///
/// NOTE: sending to `address(0)` makes the value unspendable on the EVM. If the
/// Bittensor EVM exposes a true supply-reducing burn precompile, swap the sink
/// in `onSlashDisbursed` for a call to it; the interface is unchanged.
contract BurnTarget is ISlashTarget {
    event Burned(address indexed miner, uint256 amount, string evidenceUrl, bytes16 evidenceMd5);

    error TransferFailed();

    function onSlashDisbursed(
        address miner,
        uint256 amount,
        string calldata evidenceUrl,
        bytes16 evidenceMd5
    ) external payable {
        (bool ok,) = payable(address(0)).call{value: amount}("");
        if (!ok) revert TransferFailed();
        emit Burned(miner, amount, evidenceUrl, evidenceMd5);
    }
}
