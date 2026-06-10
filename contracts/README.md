# SN89 collateral ledger contract

`Collateral.sol` is the on-chain ledger for caller collateral — see
`docs/collateral.md` for the full system design. It is a bookkeeping contract:
the alpha itself sits staked on the subnet vault coldkey, and every
deposit/withdraw/slash the custodian executes is mirrored here so balances and
the slash history are publicly auditable.

## Deploy (Hardhat + OpenZeppelin upgrades)

The contract targets the Bittensor EVM and deploys behind a UUPS proxy.

```bash
mkdir collateral-deploy && cd collateral-deploy
npm init -y
npm install --save-dev hardhat @nomicfoundation/hardhat-toolbox @openzeppelin/hardhat-upgrades
npm install @openzeppelin/contracts-upgradeable
npx hardhat init   # empty TypeScript project; copy Collateral.sol into contracts/
```

`hardhat.config.ts` networks:

```ts
networks: {
  // Bittensor EVM mainnet (chainId 964)
  mainnet: { url: "https://lite.chain.opentensor.ai", accounts: [process.env.PRIVATE_KEY!] },
  // Bittensor EVM testnet (chainId 945)
  testnet: { url: "https://test.chain.opentensor.ai", accounts: [process.env.PRIVATE_KEY!] },
}
```

Deploy script:

```ts
import { ethers, upgrades } from "hardhat";

async function main() {
  const [deployer] = await ethers.getSigners();
  const Collateral = await ethers.getContractFactory("Collateral");
  const proxy = await upgrades.deployProxy(Collateral, [deployer.address], {
    initializer: "initialize",
  });
  await proxy.waitForDeployment();
  console.log("proxy:", await proxy.getAddress());
}
main();
```

After deploying:

1. Record the **proxy** address (not the implementation) as
   `SN89_COLLATERAL_CONTRACT` for validators and the custodian.
2. The deployer EOA is the contract owner — it signs every ledger write and
   authorizes upgrades. Custody of that key and of the vault coldkey is the
   trust root of the whole system; store both per the operational runbook,
   never in the repo.
3. Testnet first: deploy on chainId 945 against netuid 496 and run a full
   deposit → slash → withdraw cycle before mainnet.
