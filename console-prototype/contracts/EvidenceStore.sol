// SPDX-License-Identifier: Apache-2.0
pragma solidity >=0.8.0 <0.9.0;

contract EvidenceStore {
    struct Batch {
        string batchId;
        string evidenceType;
        string dataType;
        string merkleRoot;
        uint256 recordCount;
        string submitter;
        string relatedBatchId;
        uint256 timestamp;
    }

    mapping(bytes32 => Batch) private _batches;

    event BatchRecorded(
        string indexed batchId,
        string evidenceType,
        string dataType,
        string merkleRoot,
        uint256 recordCount,
        string submitter,
        string relatedBatchId,
        uint256 timestamp
    );

    function recordBatch(
        string memory batchId,
        string memory evidenceType,
        string memory dataType,
        string memory merkleRoot,
        uint256 recordCount,
        string memory submitter,
        string memory relatedBatchId,
        uint256 timestamp
    ) public returns (bool) {
        bytes32 key = keccak256(abi.encodePacked(batchId));
        require(bytes(_batches[key].batchId).length == 0, "EvidenceStore: batch exists");
        _batches[key] = Batch(
            batchId,
            evidenceType,
            dataType,
            merkleRoot,
            recordCount,
            submitter,
            relatedBatchId,
            timestamp
        );
        emit BatchRecorded(
            batchId,
            evidenceType,
            dataType,
            merkleRoot,
            recordCount,
            submitter,
            relatedBatchId,
            timestamp
        );
        return true;
    }

    function getBatch(string memory batchId)
        public
        view
        returns (
            string memory batchIdOut,
            string memory evidenceType,
            string memory dataType,
            string memory merkleRoot,
            uint256 recordCount,
            string memory submitter,
            string memory relatedBatchId,
            uint256 timestamp
        )
    {
        Batch storage b = _batches[keccak256(abi.encodePacked(batchId))];
        return (
            b.batchId,
            b.evidenceType,
            b.dataType,
            b.merkleRoot,
            b.recordCount,
            b.submitter,
            b.relatedBatchId,
            b.timestamp
        );
    }
}
