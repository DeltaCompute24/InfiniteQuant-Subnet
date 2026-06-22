// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IVotingPower} from "./IVotingPower.sol";
import {ISlashTarget} from "./ISlashTarget.sol";

/// @title CollateralVoting — escrowed SN89 caller collateral, slashed by a
///        stake-weighted validator vote, with a swappable disposition target.
///
/// @notice This is the trustless-custody redesign of `contracts/Collateral.sol`.
/// Where the current ledger holds no funds and mirrors owner-executed custody
/// events, this contract *escrows the collateral itself* (TAO, as native value)
/// and enforces three properties on-chain:
///
///   1. CUSTODY — `deposit` locks real value here; no off-chain vault coldkey.
///   2. GOVERNED SLASH — no single trustee can seize. A slash (and the denial
///      of a reclaim) only takes effect when validators holding at least
///      `quorumBps` of snapshotted {IVotingPower} vote for it. This answers the
///      "decentralizing the custodian" v2 question in docs/collateral.md.
///   3. DECOUPLED DISPOSITION — a slash moves collateral into the `seized` pool
///      and stops there. WHERE seized funds go (burn now; SN8 trading capital
///      later) is a separate, governance-controlled `disburseSeized` routed
///      through a swappable {ISlashTarget}. Switching burn -> stake-into-SN8 is
///      one `setSlashTarget` call and changes nothing in this contract.
///
/// Miners keep the church-style timeout exit: a reclaim pays out after
/// `DECISION_TIMEOUT` unless validators vote to deny it in the window. The
/// settlement lock (no exit while signals are open) stays enforced off-chain by
/// the validator set deciding when to deny; on-chain, deny is the only lever
/// that can stop a reclaim, so `DECISION_TIMEOUT` MUST exceed the realistic time
/// for a deny vote to reach quorum.
///
/// Roles:
///   - validators (votingPower > 0): propose/vote slashes and denials.
///   - governance (a multisig/timelock): tune params, swap the voting source
///     and the slash target, and disburse the seized pool. Governance cannot
///     seize a miner's collateral — only a vote can. Making disbursement and
///     parameter changes themselves vote-gated is the natural next step.
contract CollateralVoting {
    // --- immutable config -------------------------------------------------

    /// @notice SN89 netuid this collateral is for (informational; off-chain ref).
    uint16 public immutable NETUID;
    /// @notice Minimum deposit, and minimum partial-reclaim size.
    uint256 public immutable MIN_COLLATERAL_INCREASE;
    /// @notice Reclaim challenge window and slash/deny voting window (seconds).
    uint64 public immutable DECISION_TIMEOUT;

    // --- governed config --------------------------------------------------

    address public governance;
    IVotingPower public votingPower;
    ISlashTarget public slashTarget;
    /// @notice Basis points of snapshot total voting power required to pass a
    /// slash or deny. e.g. 6667 = a two-thirds supermajority.
    uint16 public quorumBps;

    // --- collateral ledger ------------------------------------------------

    mapping(address => uint256) public collaterals;
    mapping(address => uint256) public collateralUnderPendingReclaims;
    uint256 public totalCollateral; // == sum(collaterals); excludes `seized`
    uint256 public seized; // slashed, awaiting disposition
    uint256 public totalSlashed; // lifetime cumulative slashed

    // --- reclaims ---------------------------------------------------------

    struct Reclaim {
        address miner;
        uint256 amount;
        uint64 denyTimeout;
    }

    mapping(uint256 => Reclaim) public reclaims;
    uint256 public nextReclaimId;

    // --- slash proposals --------------------------------------------------

    struct SlashProposal {
        address miner;
        uint256 amount;
        uint64 expiry;
        uint256 snapshotTotalPower;
        uint256 yesPower;
        bool executed;
        bool cancelled;
    }

    mapping(uint256 => SlashProposal) public slashProposals;
    mapping(uint256 => mapping(address => bool)) public slashVoted;
    uint256 public nextSlashId;
    /// @notice Unresolved slash proposals per miner; blocks reclaim finalize.
    mapping(address => uint256) public activeSlashCount;

    // --- deny proposals ---------------------------------------------------

    struct DenyProposal {
        uint256 reclaimId;
        uint64 expiry;
        uint256 snapshotTotalPower;
        uint256 yesPower;
        bool executed;
    }

    mapping(uint256 => DenyProposal) public denyProposals;
    mapping(uint256 => mapping(address => bool)) public denyVoted;
    uint256 public nextDenyId;

    // --- reentrancy guard -------------------------------------------------

    uint256 private _lock = 1;

    modifier nonReentrant() {
        require(_lock == 1, "reentrant");
        _lock = 2;
        _;
        _lock = 1;
    }

    // --- events -----------------------------------------------------------

    event Deposit(address indexed account, uint256 amount);
    event ReclaimStarted(
        uint256 indexed reclaimId,
        address indexed miner,
        uint256 amount,
        uint64 expirationTime,
        string url,
        bytes16 urlMd5
    );
    event Reclaimed(uint256 indexed reclaimId, address indexed miner, uint256 amount);
    event ReclaimReleasedUnpaid(uint256 indexed reclaimId, address indexed miner, uint256 amount);
    event ReclaimDenied(uint256 indexed reclaimId, uint256 indexed denyId);

    event SlashProposed(
        uint256 indexed slashId,
        address indexed miner,
        uint256 amount,
        uint64 expiry,
        string url,
        bytes16 urlMd5
    );
    event SlashVote(uint256 indexed slashId, address indexed validator, uint256 weight, uint256 yesPower);
    event Slashed(uint256 indexed slashId, address indexed miner, uint256 amount, uint256 seizedTotal);
    event SlashExpired(uint256 indexed slashId);

    event DenyProposed(uint256 indexed denyId, uint256 indexed reclaimId, address indexed proposer);
    event DenyVote(uint256 indexed denyId, address indexed validator, uint256 weight, uint256 yesPower);

    event SeizedDisbursed(address indexed target, uint256 amount);

    event GovernanceTransferred(address indexed previousGovernance, address indexed newGovernance);
    event VotingPowerSourceChanged(address indexed previousSource, address indexed newSource);
    event SlashTargetChanged(address indexed previousTarget, address indexed newTarget);
    event QuorumChanged(uint16 previousQuorumBps, uint16 newQuorumBps);

    // --- errors -----------------------------------------------------------

    error ZeroAddress();
    error AmountZero();
    error InvalidQuorum();
    error InsufficientAmount();
    error ReclaimAmountTooLarge();
    error ReclaimAmountTooSmall();
    error ReclaimNotFound();
    error BeforeDenyTimeout();
    error PastDenyTimeout();
    error SlashPending();
    error TransferFailed();
    error NotGovernance();
    error NotValidator();
    error ProposalNotFound();
    error ProposalExpired();
    error NotExpired();
    error AlreadyResolved();
    error AlreadyVoted();
    error UseDeposit();

    // --- modifiers --------------------------------------------------------

    modifier onlyGovernance() {
        if (msg.sender != governance) revert NotGovernance();
        _;
    }

    modifier onlyValidator() {
        if (votingPower.votingPowerOf(msg.sender) == 0) revert NotValidator();
        _;
    }

    constructor(
        uint16 netuid,
        uint256 minCollateralIncrease,
        uint64 decisionTimeout,
        uint16 quorumBps_,
        address governance_,
        IVotingPower votingPower_,
        ISlashTarget slashTarget_
    ) {
        if (minCollateralIncrease == 0) revert AmountZero();
        if (decisionTimeout == 0) revert AmountZero();
        if (quorumBps_ == 0 || quorumBps_ > 10000) revert InvalidQuorum();
        if (governance_ == address(0)) revert ZeroAddress();
        if (address(votingPower_) == address(0)) revert ZeroAddress();
        if (address(slashTarget_) == address(0)) revert ZeroAddress();

        NETUID = netuid;
        MIN_COLLATERAL_INCREASE = minCollateralIncrease;
        DECISION_TIMEOUT = decisionTimeout;
        quorumBps = quorumBps_;
        governance = governance_;
        votingPower = votingPower_;
        slashTarget = slashTarget_;
    }

    // --- deposit ----------------------------------------------------------

    receive() external payable {
        revert UseDeposit();
    }

    fallback() external payable {
        revert UseDeposit();
    }

    /// @notice Escrow collateral crediting `msg.sender`.
    function deposit() external payable {
        depositFor(msg.sender);
    }

    /// @notice Escrow collateral crediting `account`. Anyone may fund an
    /// account, but only that account can ever reclaim its balance.
    function depositFor(address account) public payable {
        if (account == address(0)) revert ZeroAddress();
        if (msg.value < MIN_COLLATERAL_INCREASE) revert InsufficientAmount();
        collaterals[account] += msg.value;
        totalCollateral += msg.value;
        emit Deposit(account, msg.value);
    }

    // --- reclaim (miner exit) --------------------------------------------

    /// @notice Begin reclaiming `amount` of your own collateral. Pays out via
    /// `finalizeReclaim` after `DECISION_TIMEOUT`, unless validators vote-deny.
    function reclaimCollateral(uint256 amount, string calldata url, bytes16 urlMd5)
        external
        returns (uint256 reclaimId)
    {
        if (amount == 0) revert AmountZero();
        uint256 collateral = collaterals[msg.sender];
        uint256 pending = collateralUnderPendingReclaims[msg.sender];
        if (pending + amount > collateral) revert ReclaimAmountTooLarge();
        uint256 available = collateral - pending;
        // dust guard: small partials only allowed when draining the remainder.
        if (amount < MIN_COLLATERAL_INCREASE && available != amount) revert ReclaimAmountTooSmall();

        uint64 expiry = uint64(block.timestamp) + DECISION_TIMEOUT;
        reclaimId = ++nextReclaimId;
        reclaims[reclaimId] = Reclaim({miner: msg.sender, amount: amount, denyTimeout: expiry});
        collateralUnderPendingReclaims[msg.sender] = pending + amount;

        emit ReclaimStarted(reclaimId, msg.sender, amount, expiry, url, urlMd5);
    }

    /// @notice Finalize a matured reclaim. Pays the miner unless their balance
    /// was slashed below the held amount in the meantime, in which case the hold
    /// is simply released. Blocked while a slash vote against the miner is live.
    function finalizeReclaim(uint256 reclaimId) external nonReentrant {
        Reclaim memory r = reclaims[reclaimId];
        if (r.amount == 0) revert ReclaimNotFound();
        if (r.denyTimeout >= block.timestamp) revert BeforeDenyTimeout();
        if (activeSlashCount[r.miner] != 0) revert SlashPending();

        delete reclaims[reclaimId];
        collateralUnderPendingReclaims[r.miner] -= r.amount;

        if (collaterals[r.miner] < r.amount) {
            // funds were slashed out from under the hold; release without pay.
            emit ReclaimReleasedUnpaid(reclaimId, r.miner, r.amount);
            return;
        }

        collaterals[r.miner] -= r.amount;
        totalCollateral -= r.amount;
        emit Reclaimed(reclaimId, r.miner, r.amount);

        (bool ok,) = payable(r.miner).call{value: r.amount}("");
        if (!ok) revert TransferFailed();
    }

    // --- deny voting (stop a reclaim) ------------------------------------

    function proposeDeny(uint256 reclaimId) external onlyValidator returns (uint256 denyId) {
        Reclaim memory r = reclaims[reclaimId];
        if (r.amount == 0) revert ReclaimNotFound();
        if (r.denyTimeout < block.timestamp) revert PastDenyTimeout();

        denyId = ++nextDenyId;
        DenyProposal storage p = denyProposals[denyId];
        p.reclaimId = reclaimId;
        p.expiry = r.denyTimeout; // must conclude before the reclaim can finalize
        p.snapshotTotalPower = votingPower.totalVotingPower();

        emit DenyProposed(denyId, reclaimId, msg.sender);
        _castDeny(denyId, msg.sender);
    }

    function voteDeny(uint256 denyId) external onlyValidator {
        _castDeny(denyId, msg.sender);
    }

    function _castDeny(uint256 denyId, address voter) internal {
        DenyProposal storage p = denyProposals[denyId];
        if (p.reclaimId == 0) revert ProposalNotFound();
        if (p.executed) revert AlreadyResolved();
        if (block.timestamp > p.expiry) revert ProposalExpired();
        if (denyVoted[denyId][voter]) revert AlreadyVoted();

        uint256 w = votingPower.votingPowerOf(voter);
        if (w == 0) revert NotValidator();

        denyVoted[denyId][voter] = true;
        p.yesPower += w;
        emit DenyVote(denyId, voter, w, p.yesPower);

        if (_quorumReached(p.yesPower, p.snapshotTotalPower)) {
            Reclaim memory r = reclaims[p.reclaimId];
            if (r.amount == 0) revert ReclaimNotFound();
            p.executed = true;
            collateralUnderPendingReclaims[r.miner] -= r.amount;
            delete reclaims[p.reclaimId];
            emit ReclaimDenied(p.reclaimId, denyId);
        }
    }

    // --- slash voting -----------------------------------------------------

    /// @notice Propose seizing `amount` of `miner`'s collateral. Records the
    /// proposer's vote; executes immediately if that alone reaches quorum.
    function proposeSlash(address miner, uint256 amount, string calldata url, bytes16 urlMd5)
        external
        onlyValidator
        returns (uint256 slashId)
    {
        if (amount == 0) revert AmountZero();
        if (collaterals[miner] < amount) revert InsufficientAmount();

        slashId = ++nextSlashId;
        SlashProposal storage p = slashProposals[slashId];
        p.miner = miner;
        p.amount = amount;
        p.expiry = uint64(block.timestamp) + DECISION_TIMEOUT;
        p.snapshotTotalPower = votingPower.totalVotingPower();
        activeSlashCount[miner] += 1;

        emit SlashProposed(slashId, miner, amount, p.expiry, url, urlMd5);
        _castSlash(slashId, msg.sender);
    }

    function voteSlash(uint256 slashId) external onlyValidator {
        _castSlash(slashId, msg.sender);
    }

    function _castSlash(uint256 slashId, address voter) internal {
        SlashProposal storage p = slashProposals[slashId];
        if (p.miner == address(0)) revert ProposalNotFound();
        if (p.executed || p.cancelled) revert AlreadyResolved();
        if (block.timestamp > p.expiry) revert ProposalExpired();
        if (slashVoted[slashId][voter]) revert AlreadyVoted();

        uint256 w = votingPower.votingPowerOf(voter);
        if (w == 0) revert NotValidator();

        slashVoted[slashId][voter] = true;
        p.yesPower += w;
        emit SlashVote(slashId, voter, w, p.yesPower);

        if (_quorumReached(p.yesPower, p.snapshotTotalPower)) {
            _executeSlash(slashId);
        }
    }

    function _executeSlash(uint256 slashId) internal {
        SlashProposal storage p = slashProposals[slashId];
        uint256 amount = p.amount;
        // Balance may have dropped (a prior reclaim finalized). Re-propose for
        // the available amount rather than partially seizing under a stale vote.
        if (collaterals[p.miner] < amount) revert InsufficientAmount();

        p.executed = true;
        activeSlashCount[p.miner] -= 1;

        collaterals[p.miner] -= amount;
        totalCollateral -= amount;
        seized += amount;
        totalSlashed += amount;

        emit Slashed(slashId, p.miner, amount, seized);
    }

    /// @notice Clear an expired, unpassed slash proposal so it stops blocking
    /// the miner's reclaim finalization. Callable by anyone.
    function expireSlash(uint256 slashId) external {
        SlashProposal storage p = slashProposals[slashId];
        if (p.miner == address(0)) revert ProposalNotFound();
        if (p.executed || p.cancelled) revert AlreadyResolved();
        if (block.timestamp <= p.expiry) revert NotExpired();

        p.cancelled = true;
        activeSlashCount[p.miner] -= 1;
        emit SlashExpired(slashId);
    }

    // --- disposition (governance) ----------------------------------------

    /// @notice Route `amount` of the seized pool through the current
    /// {ISlashTarget}. This is the burn -> stake-into-SN8 switch point: change
    /// `slashTarget` first, then disburse to the new destination.
    function disburseSeized(uint256 amount, string calldata url, bytes16 urlMd5)
        external
        onlyGovernance
        nonReentrant
    {
        if (amount == 0) revert AmountZero();
        if (amount > seized) revert InsufficientAmount();

        seized -= amount;
        ISlashTarget target = slashTarget;
        emit SeizedDisbursed(address(target), amount);
        target.onSlashDisbursed{value: amount}(address(0), amount, url, urlMd5);
    }

    // --- governance setters ----------------------------------------------

    function setVotingPowerSource(IVotingPower newSource) external onlyGovernance {
        if (address(newSource) == address(0)) revert ZeroAddress();
        emit VotingPowerSourceChanged(address(votingPower), address(newSource));
        votingPower = newSource;
    }

    function setSlashTarget(ISlashTarget newTarget) external onlyGovernance {
        if (address(newTarget) == address(0)) revert ZeroAddress();
        emit SlashTargetChanged(address(slashTarget), address(newTarget));
        slashTarget = newTarget;
    }

    function setQuorumBps(uint16 newQuorumBps) external onlyGovernance {
        if (newQuorumBps == 0 || newQuorumBps > 10000) revert InvalidQuorum();
        emit QuorumChanged(quorumBps, newQuorumBps);
        quorumBps = newQuorumBps;
    }

    function transferGovernance(address newGovernance) external onlyGovernance {
        if (newGovernance == address(0)) revert ZeroAddress();
        emit GovernanceTransferred(governance, newGovernance);
        governance = newGovernance;
    }

    // --- views ------------------------------------------------------------

    function balanceOf(address account) external view returns (uint256) {
        return collaterals[account];
    }

    /// @notice Collateral not currently under a pending reclaim hold.
    function reclaimableOf(address account) external view returns (uint256) {
        return collaterals[account] - collateralUnderPendingReclaims[account];
    }

    function getTotalCollateral() external view returns (uint256) {
        return totalCollateral;
    }

    function getSeized() external view returns (uint256) {
        return seized;
    }

    function getSlashedCollateral() external view returns (uint256) {
        return totalSlashed;
    }

    function _quorumReached(uint256 yesPower, uint256 snapshotTotal) internal view returns (bool) {
        if (snapshotTotal == 0) return false;
        return yesPower * 10000 >= snapshotTotal * quorumBps;
    }
}
