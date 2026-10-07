"""Initialize the development bucket. Run explicitly, never on public API requests."""

import json

from botocore.exceptions import ClientError

from app.config import get_settings
from app.storage import storage


def initialize():
    client, bucket = storage(), get_settings().storage_bucket
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in {"404", "NoSuchBucket", "NotFound"}:
            raise
        client.create_bucket(Bucket=bucket)
    client.put_bucket_policy(
        Bucket=bucket,
        Policy=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": "*",
                        "Action": ["s3:GetObject"],
                        "Resource": [f"arn:aws:s3:::{bucket}/public/*"],
                    }
                ],
            }
        ),
    )
    client.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "expire-staging",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "private/"},
                    "Expiration": {"Days": 7},
                }
            ],
        },
    )


if __name__ == "__main__":
    initialize()
