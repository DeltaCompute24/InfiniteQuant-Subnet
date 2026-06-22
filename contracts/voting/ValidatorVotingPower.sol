// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IVotingPower} from "./IVotingPower.sol";

/// @title ValidatorVotingPower — owner-synced mirror of SN89 validator stake
/// @notice The default {IVotingPower} source. The owner (the subnet custodian,
/// ideally a multisig) mirrors the active validator set's stake here each
/// weights tempo, the same cadence at which validators already recompute the
/// metagraph. Voting in `CollateralVoting` reads weights from this contract.
///
/// This keeps the on-chain governance weights faithful to real subnet stake
/// without requiring the Metagraph precompile on day one. When that precompile
/// integration is ready, deploy a precompile-backed {IVotingPower} and switch
/// `CollateralVoting` to it via `setVotingPowerSource`; this contract can be
/// retired without migrating any collateral.
contract ValidatorVotingPower is IVotingPower {
    address public owner;

    mapping(address => uint256) private _power;
    uint256 private _total;

    event OwnerTransferred(address indexed previousOwner, address indexed newOwner);
    event VotingPowerSet(address indexed validator, uint256 oldPower, uint256 newPower);

    error NotOwner();
    error ZeroAddress();
    error LengthMismatch();

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    constructor(address initialOwner) {
        if (initialOwner == address(0)) revert ZeroAddress();
        owner = initialOwner;
        emit OwnerTransferred(address(0), initialOwner);
    }

    function votingPowerOf(address validator) external view returns (uint256) {
        return _power[validator];
    }

    function totalVotingPower() external view returns (uint256) {
        return _total;
    }

    /// @notice Set one validator's voting power. `_total` is kept exact.
    function setVotingPower(address validator, uint256 power) public onlyOwner {
        if (validator == address(0)) revert ZeroAddress();
        uint256 old = _power[validator];
        if (old == power) return;
        _total = _total - old + power;
        _power[validator] = power;
        emit VotingPowerSet(validator, old, power);
    }

    /// @notice Mirror a whole validator set in one tempo update.
    function setVotingPowerBatch(address[] calldata validators, uint256[] calldata powers) external onlyOwner {
        if (validators.length != powers.length) revert LengthMismatch();
        for (uint256 i = 0; i < validators.length; i++) {
            setVotingPower(validators[i], powers[i]);
        }
    }

    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert ZeroAddress();
        emit OwnerTransferred(owner, newOwner);
        owner = newOwner;
    }
}
