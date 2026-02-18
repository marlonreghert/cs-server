"""S3 client for uploading and managing venue menu photos.

Uses boto3 with asyncio.to_thread to avoid blocking the event loop.
Photos are stored at: places/<venue_id>/photos/menu/<photo_id>.jpg
"""
import asyncio
import logging
import time
import uuid

import boto3
from botocore.exceptions import ClientError

from app.metrics import (
    S3_UPLOADS_TOTAL,
    S3_UPLOAD_DURATION_SECONDS,
)

logger = logging.getLogger(__name__)


class S3Client:
    """Async-friendly S3 client for menu photo storage."""

    def __init__(
        self,
        bucket: str,
        region: str,
        access_key_id: str,
        secret_access_key: str,
    ):
        self.bucket = bucket
        self.region = region
        self._s3 = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )

    async def close(self):
        """Close the S3 client."""
        pass  # boto3 client doesn't need explicit close

    async def upload_photo_bytes(
        self,
        venue_id: str,
        photo_bytes: bytes,
        content_type: str = "image/jpeg",
    ) -> tuple[str, str, str]:
        """Upload photo bytes to S3.

        Args:
            venue_id: Venue identifier
            photo_bytes: Raw photo bytes
            content_type: MIME type of the photo

        Returns:
            Tuple of (photo_id, s3_key, s3_url)
        """
        photo_id = str(uuid.uuid4())
        ext = "jpg" if "jpeg" in content_type or "jpg" in content_type else "png"
        s3_key = f"places/{venue_id}/photos/menu/{photo_id}.{ext}"
        s3_url = f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{s3_key}"

        start_time = time.perf_counter()
        try:
            await asyncio.to_thread(
                self._s3.put_object,
                Bucket=self.bucket,
                Key=s3_key,
                Body=photo_bytes,
                ContentType=content_type,
            )
            duration = time.perf_counter() - start_time
            S3_UPLOAD_DURATION_SECONDS.observe(duration)
            S3_UPLOADS_TOTAL.labels(status="success").inc()
            logger.debug(f"[S3Client] Uploaded {s3_key} ({len(photo_bytes)} bytes)")
            return photo_id, s3_key, s3_url

        except ClientError as e:
            duration = time.perf_counter() - start_time
            S3_UPLOAD_DURATION_SECONDS.observe(duration)
            S3_UPLOADS_TOTAL.labels(status="error").inc()
            logger.error(f"[S3Client] Failed to upload {s3_key}: {e}")
            raise

    async def generate_presigned_url(
        self, s3_key: str, expires_in: int = 3600
    ) -> str:
        """Generate a presigned URL for temporary read access to an S3 object.

        Args:
            s3_key: S3 object key
            expires_in: URL expiration time in seconds (default: 1 hour)

        Returns:
            Presigned URL string
        """
        url = await asyncio.to_thread(
            self._s3.generate_presigned_url,
            "get_object",
            Params={"Bucket": self.bucket, "Key": s3_key},
            ExpiresIn=expires_in,
        )
        return url
