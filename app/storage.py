"""Private originals and immutable public image derivatives in S3-compatible storage."""

import io
import warnings

import boto3
from botocore.config import Config
from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import get_settings

MAX_BYTES = 8 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 20_000_000


def storage():
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.storage_endpoint or None,
        region_name=settings.storage_region,
        aws_access_key_id=settings.storage_access_key,
        aws_secret_access_key=settings.storage_secret_key,
        config=Config(
            signature_version="s3v4",
            connect_timeout=5,
            read_timeout=10,
            retries={"max_attempts": 2},
            s3={"addressing_style": "path"},
        ),
    )


def put_object(key, content, content_type="application/octet-stream"):
    storage().put_object(
        Bucket=get_settings().storage_bucket,
        Key=key,
        Body=content,
        ContentType=content_type,
        CacheControl="public, max-age=31536000, immutable"
        if key.startswith("public/")
        else "private, no-store",
    )


def delete_object(key):
    storage().delete_object(Bucket=get_settings().storage_bucket, Key=key)


def get_object(key):
    response = storage().get_object(Bucket=get_settings().storage_bucket, Key=key)
    with response["Body"] as stream:
        content = stream.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ValueError("Image exceeds 8 MiB")
    return content


def public_url(key):
    return f"{get_settings().media_public_url.rstrip('/')}/{key}" if key else None


def validate_image(content):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                if image.format not in {"JPEG", "PNG", "WEBP"} or image.is_animated:
                    raise ValueError("Upload a still JPEG, PNG or WebP image")
                if image.width < 32 or image.height < 32:
                    raise ValueError("Image must be at least 32 pixels in each dimension")
                image.verify()
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("Upload a valid image with at most 20 million pixels") from exc


def image_variants(content):
    validate_image(content)
    try:
        with Image.open(io.BytesIO(content)) as original:
            image = ImageOps.exif_transpose(original).convert("RGB")
            result = []
            for bounds in ((1920, 1920), (480, 480)):
                variant = image.copy()
                variant.thumbnail(bounds, Image.Resampling.LANCZOS)
                output = io.BytesIO()
                # Metadata is discarded when writing the fresh RGB image.
                variant.save(output, "WEBP", quality=85)
                result.append(output.getvalue())
            return result
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("Image pixels could not be decoded. Upload another image.") from exc
