// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.6.10;

/** Versioned registry: each immutable root is bound to its transaction signer. */
contract RootHashRegistryV2 {
    struct BatchRecord {
        bytes32 root;
        uint64 recordCount;
        uint64 submittedAt;
        string subjectId;
        string schemaVersion;
        address publisher;
    }
    address public owner;
    mapping(address => bool) public authorized;
    mapping(address => string) public subjects;
    mapping(string => BatchRecord) private batches;
    event BatchRegistered(string batchId, bytes32 root, uint64 recordCount,
        uint64 submittedAt, string subjectId, string schemaVersion, address publisher);
    event SubjectAuthorized(address indexed account, string subjectId);

    constructor() public { owner=msg.sender; }
    function authorizeSubject(address account, string memory subjectId) public {
        require(msg.sender==owner,"ONLY_OWNER");
        require(account!=address(0) && bytes(subjectId).length>0,"BAD_SUBJECT");
        require(!authorized[account],"SUBJECT_ALREADY_BOUND");
        authorized[account]=true;
        subjects[account]=subjectId;
        emit SubjectAuthorized(account,subjectId);
    }
    function registerBatch(string memory batchId,bytes32 root,uint64 recordCount,
        uint64 submittedAt,string memory subjectId,string memory schemaVersion) public returns(bool) {
        require(authorized[msg.sender],"NOT_AUTHORIZED");
        require(keccak256(bytes(subjectId))==keccak256(bytes(subjects[msg.sender])),"SUBJECT_MISMATCH");
        require(bytes(batchId).length>0 && bytes(batchId).length<=128,"BAD_BATCH_ID");
        require(root!=bytes32(0) && recordCount>0,"BAD_ROOT_OR_COUNT");
        require(bytes(schemaVersion).length>0,"BAD_SCHEMA");
        require(batches[batchId].publisher==address(0),"BATCH_EXISTS");
        batches[batchId]=BatchRecord(root,recordCount,submittedAt,subjectId,schemaVersion,msg.sender);
        emit BatchRegistered(batchId,root,recordCount,submittedAt,subjectId,schemaVersion,msg.sender);
        return true;
    }
    function getBatch(string memory batchId) public view returns(bool,bytes32,uint64,uint64,string memory,string memory) {
        BatchRecord storage b=batches[batchId];
        return(b.publisher!=address(0),b.root,b.recordCount,b.submittedAt,b.subjectId,b.schemaVersion);
    }
    function getPublisher(string memory batchId) public view returns(address) { return batches[batchId].publisher; }
    function existsOf(string memory batchId) public view returns(bool) { return batches[batchId].publisher!=address(0); }
}
