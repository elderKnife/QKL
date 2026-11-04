// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.6.10;

/**
 * @title RootHashRegistry
 * @notice 最小验证实验的链上锚定合约：VPP 把一批记录的 Merkle 根哈希写入链上，
 *         GRID 通过自己的链客户端读取同一批次的根哈希进行独立核验。
 *
 * 设计约束（对应任务书 6.5）：
 *  - 批次编号非空且唯一，重复提交必须被拒绝（不可静默覆盖既有证据）；
 *  - 根哈希必须非零，记录条数必须为正；
 *  - 只有被授权的业务账户（VPP）可以写入；
 *  - 链上只保存根哈希与必要的定位元数据，**不保存原始记录**。
 */
contract RootHashRegistry {
    struct BatchRecord {
        bytes32 root;          // Merkle root (SHA-256, 32 bytes)
        uint64 recordCount;    // number of records committed by this root
        uint64 submittedAt;    // application-submitted timestamp (ms, UTC)
        string vppId;          // business subject that submitted the batch
        string schemaVersion;  // canonicalization / Merkle rule version
    }

    address public owner;
    mapping(address => bool) public authorized;
    mapping(string => BatchRecord) private batches;
    mapping(string => bool) private batchExists;

    event BatchRegistered(
        string batchId,
        bytes32 root,
        uint64 recordCount,
        uint64 submittedAt,
        string vppId,
        string schemaVersion
    );
    event AuthorizationChanged(address indexed account, bool allowed);

    constructor() public {
        owner = msg.sender;
        authorized[msg.sender] = true;
        emit AuthorizationChanged(msg.sender, true);
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "ONLY_OWNER");
        _;
    }

    modifier onlyAuthorized() {
        require(authorized[msg.sender], "NOT_AUTHORIZED");
        _;
    }

    function setAuthorized(address account, bool allowed) public onlyOwner {
        authorized[account] = allowed;
        emit AuthorizationChanged(account, allowed);
    }

    function registerBatch(
        string memory batchId,
        bytes32 root,
        uint64 recordCount,
        uint64 submittedAt,
        string memory vppId,
        string memory schemaVersion
    ) public onlyAuthorized returns (bool) {
        require(bytes(batchId).length > 0 && bytes(batchId).length <= 128, "BAD_BATCH_ID");
        require(root != bytes32(0), "BAD_ROOT");
        require(recordCount > 0, "BAD_RECORD_COUNT");
        require(!batchExists[batchId], "BATCH_EXISTS");

        batches[batchId] = BatchRecord(root, recordCount, submittedAt, vppId, schemaVersion);
        batchExists[batchId] = true;
        emit BatchRegistered(batchId, root, recordCount, submittedAt, vppId, schemaVersion);
        return true;
    }

    function getBatch(string memory batchId)
        public
        view
        returns (
            bool exists,
            bytes32 root,
            uint64 recordCount,
            uint64 submittedAt,
            string memory vppId,
            string memory schemaVersion
        )
    {
        BatchRecord storage b = batches[batchId];
        return (batchExists[batchId], b.root, b.recordCount, b.submittedAt, b.vppId, b.schemaVersion);
    }

    function existsOf(string memory batchId) public view returns (bool) {
        return batchExists[batchId];
    }
}
