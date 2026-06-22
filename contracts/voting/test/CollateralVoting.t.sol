// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {CollateralVoting} from "../CollateralVoting.sol";
import {ValidatorVotingPower} from "../ValidatorVotingPower.sol";
import {BurnTarget} from "../BurnTarget.sol";
import {ISlashTarget} from "../ISlashTarget.sol";

/// Stand-in for the future SN8 staking adapter: records what it received so we
/// can prove the burn -> stake-into-SN8 switch routes funds to a new target.
contract MockStakeTarget is ISlashTarget {
    uint256 public totalStaked;
    uint256 public lastAmount;
    address public lastMiner;

    function onSlashDisbursed(address miner, uint256 amount, string calldata, bytes16) external payable {
        require(msg.value == amount, "value mismatch");
        totalStaked += amount;
        lastAmount = amount;
        lastMiner = miner;
    }
}

contract CollateralVotingTest is Test {
    CollateralVoting cv;
    ValidatorVotingPower vp;
    BurnTarget burnTarget;

    address governance = makeAddr("governance");
    address miner = makeAddr("miner");
    address v1 = makeAddr("v1");
    address v2 = makeAddr("v2");
    address v3 = makeAddr("v3");

    uint64 constant TIMEOUT = 3 days;
    uint16 constant QUORUM = 6667; // two-thirds supermajority
    uint256 constant MIN_INC = 1 ether;

    function setUp() public {
        vp = new ValidatorVotingPower(address(this));
        // stakes 40/40/20 of 100: any two clear the 2/3 quorum (>=80%),
        // any one alone (<=40%) does not.
        vp.setVotingPower(v1, 40);
        vp.setVotingPower(v2, 40);
        vp.setVotingPower(v3, 20);

        burnTarget = new BurnTarget();
        cv = new CollateralVoting(89, MIN_INC, TIMEOUT, QUORUM, governance, vp, burnTarget);

        vm.deal(miner, 1000 ether);
    }

    // --- custody ----------------------------------------------------------

    function _deposit(address who, uint256 amt) internal {
        vm.prank(who);
        cv.deposit{value: amt}();
    }

    function test_DepositEscrowsRealValue() public {
        _deposit(miner, 100 ether);
        assertEq(cv.balanceOf(miner), 100 ether);
        assertEq(cv.getTotalCollateral(), 100 ether);
        assertEq(address(cv).balance, 100 ether); // contract truly holds funds
    }

    function test_DepositBelowMinReverts() public {
        vm.prank(miner);
        vm.expectRevert(CollateralVoting.InsufficientAmount.selector);
        cv.deposit{value: 0.5 ether}();
    }

    function test_RawSendReverts() public {
        vm.prank(miner);
        (bool ok,) = address(cv).call{value: 1 ether}("");
        assertFalse(ok); // receive() reverts -> forces deposit()
    }

    // --- reclaim happy path ----------------------------------------------

    function test_ReclaimPaysAfterTimeout() public {
        _deposit(miner, 100 ether);
        vm.prank(miner);
        uint256 rid = cv.reclaimCollateral(40 ether, "", bytes16(0));
        assertEq(cv.reclaimableOf(miner), 60 ether);

        // cannot finalize before the window elapses
        vm.expectRevert(CollateralVoting.BeforeDenyTimeout.selector);
        cv.finalizeReclaim(rid);

        vm.warp(block.timestamp + TIMEOUT + 1);
        uint256 before = miner.balance;
        cv.finalizeReclaim(rid);
        assertEq(miner.balance - before, 40 ether);
        assertEq(cv.balanceOf(miner), 60 ether);
        assertEq(cv.getTotalCollateral(), 60 ether);
    }

    // --- governed slash ---------------------------------------------------

    function test_SlashRequiresQuorum_OneVoteNotEnough() public {
        _deposit(miner, 100 ether);
        vm.prank(v1);
        uint256 sid = cv.proposeSlash(miner, 30 ether, "ipfs://evidence", bytes16(0));
        // only v1 (100/300 = 33%) — below 2/3, no seizure yet
        assertEq(cv.getSeized(), 0);
        assertEq(cv.balanceOf(miner), 100 ether);
        // reclaim finalize is blocked while the slash is live
        vm.prank(miner);
        uint256 rid = cv.reclaimCollateral(50 ether, "", bytes16(0));
        vm.warp(block.timestamp + TIMEOUT + 1);
        vm.expectRevert(CollateralVoting.SlashPending.selector);
        cv.finalizeReclaim(rid);
        sid; // silence
    }

    function test_SlashExecutesAtQuorum_MovesToSeized() public {
        _deposit(miner, 100 ether);
        vm.prank(v1);
        uint256 sid = cv.proposeSlash(miner, 30 ether, "ipfs://evidence", bytes16(0));
        vm.prank(v2);
        cv.voteSlash(sid); // 200/300 = 66.7% -> passes

        assertEq(cv.getSeized(), 30 ether);
        assertEq(cv.getSlashedCollateral(), 30 ether);
        assertEq(cv.balanceOf(miner), 70 ether);
        assertEq(cv.getTotalCollateral(), 70 ether);
        // funds still custodied by the contract, just reclassified
        assertEq(address(cv).balance, 100 ether);
        assertEq(cv.activeSlashCount(miner), 0);
    }

    function test_DoubleVoteRejected() public {
        _deposit(miner, 100 ether);
        vm.prank(v1);
        uint256 sid = cv.proposeSlash(miner, 30 ether, "", bytes16(0));
        vm.prank(v1);
        vm.expectRevert(CollateralVoting.AlreadyVoted.selector);
        cv.voteSlash(sid);
    }

    function test_NonValidatorCannotPropose() public {
        _deposit(miner, 100 ether);
        vm.prank(miner);
        vm.expectRevert(CollateralVoting.NotValidator.selector);
        cv.proposeSlash(miner, 10 ether, "", bytes16(0));
    }

    function test_ExpiredSlashUnblocksReclaim() public {
        _deposit(miner, 100 ether);
        vm.prank(v1);
        uint256 sid = cv.proposeSlash(miner, 30 ether, "", bytes16(0));
        vm.prank(miner);
        uint256 rid = cv.reclaimCollateral(50 ether, "", bytes16(0));

        vm.warp(block.timestamp + TIMEOUT + 1);
        cv.expireSlash(sid); // clears the stale proposal
        assertEq(cv.activeSlashCount(miner), 0);
        uint256 before = miner.balance;
        cv.finalizeReclaim(rid);
        assertEq(miner.balance - before, 50 ether);
    }

    // --- governed deny ----------------------------------------------------

    function test_DenyVoteCancelsReclaim() public {
        _deposit(miner, 100 ether);
        vm.prank(miner);
        uint256 rid = cv.reclaimCollateral(40 ether, "", bytes16(0));

        vm.prank(v1);
        uint256 did = cv.proposeDeny(rid);
        vm.prank(v2);
        cv.voteDeny(did); // 2/3 -> reclaim denied

        // hold released, reclaim gone
        assertEq(cv.reclaimableOf(miner), 100 ether);
        vm.warp(block.timestamp + TIMEOUT + 1);
        vm.expectRevert(CollateralVoting.ReclaimNotFound.selector);
        cv.finalizeReclaim(rid);
    }

    // --- disposition switch: burn now, SN8 stake later -------------------

    function test_DisburseBurnsByDefault() public {
        _deposit(miner, 100 ether);
        _slashTo(miner, 30 ether);

        vm.prank(governance);
        cv.disburseSeized(30 ether, "", bytes16(0));

        assertEq(cv.getSeized(), 0);
        assertEq(address(0).balance, 30 ether); // burned
        assertEq(address(cv).balance, 70 ether);
    }

    function test_SwitchToSN8StakeTarget_NoCollateralChange() public {
        _deposit(miner, 100 ether);
        _slashTo(miner, 30 ether);

        // governance flips the disposition target — the only change needed.
        MockStakeTarget sn8 = new MockStakeTarget();
        vm.prank(governance);
        cv.setSlashTarget(sn8);

        vm.prank(governance);
        cv.disburseSeized(30 ether, "elim:hotkeyX", bytes16(0));

        // seized capital was staked into SN8, not burned
        assertEq(sn8.totalStaked(), 30 ether);
        assertEq(address(sn8).balance, 30 ether);
        assertEq(cv.getSeized(), 0);
        assertEq(address(0).balance, 0); // nothing burned this time
    }

    function test_OnlyGovernanceDisburses() public {
        _deposit(miner, 100 ether);
        _slashTo(miner, 30 ether);
        vm.prank(v1);
        vm.expectRevert(CollateralVoting.NotGovernance.selector);
        cv.disburseSeized(30 ether, "", bytes16(0));
    }

    function test_OnlyGovernanceSwapsTarget() public {
        MockStakeTarget sn8 = new MockStakeTarget();
        vm.prank(v1);
        vm.expectRevert(CollateralVoting.NotGovernance.selector);
        cv.setSlashTarget(sn8);
    }

    // --- invariant: contract balance == totalCollateral + seized ----------

    function test_BalanceInvariantHolds() public {
        _deposit(miner, 100 ether);
        _slashTo(miner, 25 ether);
        assertEq(address(cv).balance, cv.getTotalCollateral() + cv.getSeized());
        vm.prank(governance);
        cv.disburseSeized(25 ether, "", bytes16(0));
        assertEq(address(cv).balance, cv.getTotalCollateral() + cv.getSeized());
    }

    // helper: pass a slash to seize `amount` from `who`
    function _slashTo(address who, uint256 amount) internal {
        vm.prank(v1);
        uint256 sid = cv.proposeSlash(who, amount, "", bytes16(0));
        vm.prank(v2);
        cv.voteSlash(sid);
    }
}
