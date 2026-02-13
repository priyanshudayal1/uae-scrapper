import os
import sys
from typing import Dict

import boto3
from botocore.exceptions import BotoCoreError, ClientError


DEFAULT_BUCKET = "uae-judgements"
PREFIXES = ("orders/", "judgments/")


def get_s3_client():
    return boto3.client(
        "s3",
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=os.getenv("AWS_REGION", "us-east-1"),
    )


def count_objects_in_prefix(s3_client, bucket: str, prefix: str) -> int:
    total = 0
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj.get("Key", "")
            # Skip "folder marker" keys if present
            if key == prefix:
                continue
            total += 1
    return total


def main() -> int:
    bucket = os.getenv("S3_BUCKET_NAME", DEFAULT_BUCKET)

    try:
        s3_client = get_s3_client()
        counts: Dict[str, int] = {}
        for prefix in PREFIXES:
            counts[prefix] = count_objects_in_prefix(s3_client, bucket, prefix)
    except (BotoCoreError, ClientError) as exc:
        print(f"Failed to count S3 objects: {exc}")
        return 1

    print(f"S3 bucket: {bucket}")
    for prefix in PREFIXES:
        print(f"{prefix}: {counts[prefix]}")
    print(f"total: {sum(counts.values())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
