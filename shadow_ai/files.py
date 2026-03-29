"""
Slack file download and conversion to Claude-compatible content blocks.
"""

import base64
import logging

import httpx

logger = logging.getLogger("slack-claude-code")


def download_slack_files(
    files: list[dict],
    slack_client,
    slack_bot_token: str,
    max_file_size: int = 10 * 1024 * 1024,
    supported_image_types: set | None = None,
) -> list[dict]:
    """Download files from Slack and convert to Claude-compatible content blocks.

    Args:
        files: List of Slack file metadata dicts (from event payload).
        slack_client: Slack WebClient instance.
        slack_bot_token: Bot token for Authorization header.
        max_file_size: Maximum file size in bytes (default 10MB).
        supported_image_types: Set of MIME types treated as images.
            Defaults to {"image/jpeg", "image/png", "image/gif", "image/webp"}.

    Returns:
        List of content block dicts (image or text) suitable for the Claude SDK.
    """
    if supported_image_types is None:
        supported_image_types = {"image/jpeg", "image/png", "image/gif", "image/webp"}

    file_blocks = []
    for f in files:
        file_id = f.get("id")
        file_size = f.get("size", 0)
        file_name = f.get("name", "unknown")
        mimetype = f.get("mimetype", "")

        if file_size > max_file_size:
            logger.warning(f"[FILES] Skipping {file_name}: {file_size} bytes exceeds {max_file_size} limit")
            continue

        try:
            info = slack_client.files_info(file=file_id)
            url = info["file"].get("url_private")
            if not url:
                logger.warning(f"[FILES] No url_private for {file_name}")
                continue

            resp = httpx.get(url, headers={"Authorization": f"Bearer {slack_bot_token}"}, timeout=30, follow_redirects=True)
            resp.raise_for_status()
            data = resp.content

            if mimetype in supported_image_types:
                b64 = base64.standard_b64encode(data).decode("ascii")
                file_blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": mimetype, "data": b64},
                })
                logger.info(f"[FILES] Added image: {file_name} ({mimetype}, {len(data)} bytes)")
            else:
                try:
                    text_content = data.decode("utf-8")
                    file_blocks.append({
                        "type": "text",
                        "text": f"--- File: {file_name} ---\n{text_content}\n--- End: {file_name} ---",
                    })
                    logger.info(f"[FILES] Added text file: {file_name} ({len(text_content)} chars)")
                except UnicodeDecodeError:
                    logger.warning(f"[FILES] Skipping binary file: {file_name} ({mimetype})")

        except Exception as e:
            logger.error(f"[FILES] Failed to download {file_name}: {type(e).__name__}: {e}")

    return file_blocks
