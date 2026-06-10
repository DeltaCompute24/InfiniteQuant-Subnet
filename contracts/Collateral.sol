// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {Ownable2StepUpgradeable} from "@openzeppelin/contracts-upgradeable/access/Ownable2StepUpgradeable.sol";
import {UUPSUpgradeable} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";

/// @title SN89 caller-collateral ledger
/// @notice Public, auditable ledger of caller collateral on the Bittensor EVM.
///
/// This contract holds no funds. The collateral itself is SN89 alpha staked on
/// the subnet vault coldkey (moved there by a caller-signed
/// SubtensorModule.transfer_stake). The owner mirrors every custody event into
/// this ledger so that anyone can verify, per hotkey:
///   - how much collateral is posted        (balanceOf)
///   - how much has ever been slashed       (getSlashedCollateral)
///   - the full event history               (Deposited/Withdrawn/Slashed logs)
///
/// A slash recorded here is paired with a SubtensorModule.burn_alpha extrinsic
/// from the vault stake, so the destruction of the underlying alpha is itself
/// verifiable on-chain. Amounts are denominated in rao of SN89 alpha.
///
/// Account keys are the first 20 bytes of the hotkey's 32-byte AccountId
/// (the standard subtensor ss58 -> H160 truncation).
///
/// The external API is kept identical to the SN8 (Taoshi/Vanta) collateral
/// ledger so existing tooling conventions transfer.
contract Collateral is Initializable, Ownable2StepUpgradeable, UUPSUpgradeable {
    mapping(address => uint256) public collateralBalances;
    uint256 public totalCollateral;
    uint256 public slashedCollateral;

    event CollateralDeposited(address indexed account, uint256 amount);
    event CollateralWithdrawn(address indexed account, uint256 amount);
    event CollateralSlashed(address indexed account, uint256 amount);

    error InvalidAddress();
    error InvalidAmount();
    error InsufficientBalance();

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    function initialize(address initialOwner) public initializer {
        __Ownable_init(initialOwner);
        __Ownable2Step_init();
        __UUPSUpgradeable_init();
    }

    function balanceOf(address account) external view returns (uint256) {
        return collateralBalances[account];
    }

    function getTotalCollateral() external view returns (uint256) {
        return totalCollateral;
    }

    function getSlashedCollateral() external view returns (uint256) {
        return slashedCollateral;
    }

    function deposit(address account, uint256 amount) external onlyOwner {
        if (account == address(0)) revert InvalidAddress();
        if (amount == 0) revert InvalidAmount();
        collateralBalances[account] += amount;
        totalCollateral += amount;
        emit CollateralDeposited(account, amount);
    }

    function withdraw(address account, uint256 amount) external onlyOwner {
        if (account == address(0)) revert InvalidAddress();
        if (amount == 0) revert InvalidAmount();
        if (collateralBalances[account] < amount) revert InsufficientBalance();
        collateralBalances[account] -= amount;
        totalCollateral -= amount;
        emit CollateralWithdrawn(account, amount);
    }

    function slash(address account, uint256 amount) external onlyOwner {
        if (account == address(0)) revert InvalidAddress();
        if (amount == 0) revert InvalidAmount();
        if (collateralBalances[account] < amount) revert InsufficientBalance();
        collateralBalances[account] -= amount;
        totalCollateral -= amount;
        slashedCollateral += amount;
        emit CollateralSlashed(account, amount);
    }

    function _authorizeUpgrade(address newImplementation) internal override onlyOwner {}
}
