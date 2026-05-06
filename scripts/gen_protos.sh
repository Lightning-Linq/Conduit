#!/bin/bash
# Generate Python gRPC stubs from LND proto files.
#
# Prerequisites:
#   pip install grpcio-tools
#
# Usage:
#   ./scripts/gen_protos.sh
#
# This downloads the latest LND proto definitions and compiles them
# into Python modules that the LndClient can import.

set -euo pipefail

PROTO_DIR="src/conduit/services/protos"
OUT_DIR="src/conduit/services/proto_generated"

mkdir -p "$PROTO_DIR" "$OUT_DIR"

# Download LND proto files (pin to a release tag for stability)
LND_VERSION="v0.18.0-beta"
BASE_URL="https://raw.githubusercontent.com/lightningnetwork/lnd/${LND_VERSION}/lnrpc"

echo "Downloading LND proto files (${LND_VERSION})..."
curl -sL "${BASE_URL}/lightning.proto" -o "${PROTO_DIR}/lightning.proto"
curl -sL "${BASE_URL}/invoicesrpc/invoices.proto" -o "${PROTO_DIR}/invoices.proto"
curl -sL "${BASE_URL}/routerrpc/router.proto" -o "${PROTO_DIR}/router.proto"

# Generate Python stubs
echo "Generating Python gRPC stubs..."
python -m grpc_tools.protoc \
    --proto_path="${PROTO_DIR}" \
    --python_out="${OUT_DIR}" \
    --grpc_python_out="${OUT_DIR}" \
    "${PROTO_DIR}/lightning.proto"

echo "Done! Generated stubs in ${OUT_DIR}"
echo "Next step: update src/conduit/services/lnd.py to import from proto_generated"
