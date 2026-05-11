"""Create or recreate the `library_vectors` Milvus collection.

Schema and index choices are documented in:
- ARCHITECTURE.md §3 — fields, dim=1024, partition key on `book_id`.
- ARCHITECTURE.md §7.1 — why the partition key is `book_id` (Option B).

Connection settings come from the infra `.env` file via the `SERMON_MILVUS_*`
environment variables; defaults are the docker-compose ports.
"""
# pymilvus 2.6 ships without `py.typed`; client methods declare **kwargs as
# Unknown and a few sync methods are mis-annotated as returning coroutines.
# Relax the affected rules locally rather than globally — runtime behaviour is
# exercised by `make bootstrap-milvus` and the §3 schema spot-check.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnnecessaryComparison=false

from __future__ import annotations

import argparse
import os
import sys

from pymilvus import DataType, MilvusClient

COLLECTION_NAME = "library_vectors"
VECTOR_DIM = 1024


def make_client() -> MilvusClient:
    host = os.environ.get("SERMON_MILVUS_HOST", "localhost")
    port = os.environ.get("SERMON_MILVUS_PORT", "19530")
    return MilvusClient(uri=f"http://{host}:{port}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the library_vectors collection.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Drop the collection if it exists, then recreate it.",
    )
    args = parser.parse_args()

    client = make_client()

    if client.has_collection(collection_name=COLLECTION_NAME):
        if not args.force:
            print(
                f"Collection '{COLLECTION_NAME}' already exists; "
                f"skipping (use --force to recreate)."
            )
            return 0
        print(f"Dropping existing collection '{COLLECTION_NAME}' (--force).")
        client.drop_collection(collection_name=COLLECTION_NAME)

    schema = client.create_schema(
        auto_id=True,
        enable_dynamic_field=False,
        partition_key_field="book_id",
    )
    schema.add_field(field_name="id", datatype=DataType.INT64, is_primary=True)
    schema.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=VECTOR_DIM)
    schema.add_field(field_name="book_id", datatype=DataType.VARCHAR, max_length=64)
    schema.add_field(field_name="content_chunk", datatype=DataType.VARCHAR, max_length=65535)
    schema.add_field(field_name="metadata", datatype=DataType.JSON)

    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="vector",
        index_type="FLAT",
        metric_type="COSINE",
    )

    client.create_collection(
        collection_name=COLLECTION_NAME,
        schema=schema,
        index_params=index_params,
    )
    print(f"Created collection '{COLLECTION_NAME}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
