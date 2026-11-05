# Dockerized edge light-node + local VPP prototype

This is a new local prototype. It does not change the historical `src.run_all A/B` experiments and does not send transactions to FISCO BCOS. Compose also starts the local Chinese web console described in [README-web-console.md](README-web-console.md).

## Topology

`edge` is one logical light-node container running five Go device goroutines and an HTTP health/status endpoint. The devices generate active-power records in UTC using the existing record fields. Every UTC five-minute window, the edge seals all records from that window into a batch, signs its canonical JSON payload with its own persisted secp256k1 ECDSA key, and stores the signed batch in a MySQL outbox.

`vpp` is a separate Go HTTP receiver. It reads only the edge public-key volume, verifies the signature and schema, stores the full payload in MySQL, computes the existing domain-separated Merkle root over individual records, and saves the root plus metadata in its MySQL schema. The simulated adapter is explicitly **not a blockchain**. VPP returns a receipt marked `SIMULATED_REGISTERED`; the edge recomputes the Merkle root from its durable payload and marks the outbox batch `VERIFIED` only when the acknowledgement matches.

The shared JSON payload fields are `protocolVersion`, `packetId`, `edgeId`, `windowStart`, `windowEnd`, `recordCount`, and `records`. The signature is sent alongside the payload and does not sign itself. `POST /v1/batches` is idempotent for an identical packet and rejects a reused packet ID with different content. VPP lists recent batch summaries at `GET /v1/batches?edgeId=EDGE-001`, exposes stored full batches at `GET /v1/batches/{packetId}`, and exposes simulated summary records at `GET /v1/mock-chain/batches/{batchId}`. Both runtime services expose `GET /healthz`; edge also exposes `GET /v1/status`.

## Run with Compose

Copy `.env.example` to `.env` if you want to adjust settings, then run:

```sh
docker compose -f compose.yaml up --build
```

The default sampling interval is 10–60 seconds, while the batch window is five minutes. Use `SAMPLE_INTERVAL_MIN_SECONDS=1`, `SAMPLE_INTERVAL_MAX_SECONDS=2`, and `BATCH_WINDOW_SECONDS=10` for a short local demonstration. Override only in the local `.env`; do not put private keys there.

Check health and queue counts:

```sh
curl http://127.0.0.1:18080/healthz
curl http://127.0.0.1:18080/v1/status
curl http://127.0.0.1:18081/healthz
curl http://127.0.0.1:18082/healthz
```

Open the local console at `http://127.0.0.1:18082`. It does not require a browser login. Privileged edge/VPP management endpoints use a private internal token shared only inside Compose and the web server; the token is not delivered to the browser. Keep all published host ports bound to loopback as configured.

The VPP logs show `packetId`, `batchId`, record count, Merkle root, and `SIMULATED_REGISTERED`. To inspect a full persisted batch, use its packet ID with `GET /v1/batches/{packetId}`.

The edge retries connection errors, server errors, and rate limits with exponential backoff capped at 60 seconds. Its MySQL outbox, unsealed records, generated key, and VPP batches survive container recreation in named volumes. Permanent VPP 4xx validation responses are retained as `REJECTED` for investigation instead of retried forever. To reset development state, stop Compose and run `docker compose -f compose.yaml down -v`; this deletes local generated keys and data.

The Compose stack starts MySQL 8.4 with separate `edge_data` and `vpp_data` schemas. Set `MYSQL_ROOT_PASSWORD` and `MYSQL_PASSWORD` in `.env` for local use; the values in `.env.example` are development-only credentials. The MySQL host port is bound to `127.0.0.1`.

## Test

```sh
go test ./...
```

## Configuration

The five default devices are `PV-01` and `PV-02` (0–100 kW), `BESS-01` and `BESS-02` (−50–50 kW; positive means discharge, negative means charge), and `EVSE-01` (0–22 kW). Each device independently samples after a uniform random delay. `DEVICE_PV_01_MIN_POWER_KW` / `DEVICE_PV_01_MAX_POWER_KW`, and corresponding `BESS_01`, `PV_02`, `BESS_02`, and `EVSE_01` variables, override individual ranges.

Use a distinct `EDGE_ID` and distinct volumes for every additional logical edge node. Provision each corresponding `{EDGE_ID}.pub` in the VPP trusted-public-key directory before accepting its batches. The first Compose example runs one edge and one local VPP.

## Scope of chain validation

VPP's simulated summary persistence is the replacement point for a future FISCO BCOS adapter. This first pass has no RPC connection, deployed-contract verification, consensus participation, blockchain receipt, or on-chain proof query. The public history's FISCO BCOS topology has five consensus nodes; this Compose file does not launch or emulate that consensus network.

