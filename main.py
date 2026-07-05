#!/usr/bin/env python3
"""
Immich Photo Sync Script
Synchronizes photos between two Immich accounts while respecting deletions.
"""

import requests
import json
import time
import logging
import sqlite3
import tempfile
from datetime import datetime, timedelta
from typing import List, Dict, Set, Optional
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from requests_toolbelt.multipart.encoder import MultipartEncoder

# Load configuration from a .env file (see .env.example)
load_dotenv()


def _require_env(name: str) -> str:
    """Read a required environment variable or exit with a helpful message."""
    value = os.getenv(name)
    if not value:
        sys.stderr.write(
            f"Missing required configuration: {name}. "
            f"Copy .env.example to .env and fill it in.\n"
        )
        sys.exit(1)
    return value


# Configuration
IMMICH_BASE_URL = _require_env("IMMICH_BASE_URL")
API_KEY_1 = _require_env("API_KEY_1")
API_KEY_2 = _require_env("API_KEY_2")
SYNC_START_DATE = _require_env("SYNC_START_DATE")
SYNC_END_DATE = _require_env("SYNC_END_DATE")
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "3600"))  # seconds

# Database file for tracking sync state
DB_FILE = os.getenv("DB_FILE", "immich_sync.db")

# Streaming/transfer tuning. Assets are streamed straight from the source
# account into the target account so the whole file never has to be held in
# RAM. Only in the rare case where the download has no Content-Length do we
# fall back to buffering the file (in a SpooledTemporaryFile that stays in RAM
# up to STREAM_SPOOL_MAX_BYTES and then rolls over to disk in STREAM_TEMP_DIR).
STREAM_CHUNK_SIZE = int(os.getenv("STREAM_CHUNK_SIZE", str(1024 * 1024)))  # bytes
STREAM_SPOOL_MAX_BYTES = int(os.getenv("STREAM_SPOOL_MAX_BYTES", str(8 * 1024 * 1024)))  # bytes
STREAM_TEMP_DIR = os.getenv("STREAM_TEMP_DIR") or None  # None -> system default

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('immich_sync.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

class _LengthedStream:
    """A minimal read-only file-like wrapper that advertises a known length.

    ``MultipartEncoder`` needs to know the size of each part up front so it can
    send an accurate ``Content-Length`` (and thus avoid chunked transfer
    encoding, which some Immich/reverse-proxy setups reject). A raw network
    download stream doesn't expose its length, so we attach it explicitly here
    from the download's ``Content-Length`` header. Data is read lazily, so the
    file is never fully materialised in memory.
    """

    def __init__(self, stream, length: int):
        self._stream = stream
        self._total = length
        self._pos = 0

    @property
    def len(self) -> int:
        # ``requests_toolbelt`` reads this to size the multipart body. It sizes
        # the part once at construction (when pos == 0, giving the full length,
        # hence the correct Content-Length) and then polls it while streaming to
        # decide when the part is exhausted, so it must report *remaining* bytes.
        return max(self._total - self._pos, 0)

    def read(self, amt: int = -1) -> bytes:
        data = self._stream.read(amt)
        self._pos += len(data)
        return data

    def tell(self) -> int:
        return self._pos


class ImmichAPI:
    """Wrapper for Immich API operations"""
    
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            'X-API-KEY': api_key,
            'Content-Type': 'application/json'
        })
    
    def get_assets(self, start_date: str, end_date: str) -> List[Dict]:
        """Get all assets between specified dates"""
        try:
            # Use the search/metadata endpoint - the correct current API
            url = f"{self.base_url}/api/search/metadata"
            # Immich v3 changed the default visibility of search results to
            # include all assets (archived, hidden, etc.) instead of only
            # "timeline" assets. Pin it to "timeline" to preserve the previous
            # behaviour of only syncing regular timeline photos/videos.
            payload = {
                "takenAfter": f"{start_date}T00:00:00.000Z",
                "takenBefore": f"{end_date}T23:59:59.999Z",
                "visibility": "timeline"
            }
            
            logger.info(f"Searching assets with payload: {payload}")
            response = self.session.post(url, json=payload)
            
            logger.info(f"Search response status: {response.status_code}")
            if response.status_code != 200:
                logger.error(f"Search failed with status {response.status_code}: {response.text}")
                return []
            
            data = response.json()
            logger.info(f"Search response keys: {list(data.keys()) if isinstance(data, dict) else 'Not a dict'}")
            
            # According to the API documentation, assets are in data.assets.items
            if not isinstance(data, dict):
                logger.error(f"Response is not a dict: {type(data)}")
                return []
            
            if 'assets' not in data:
                logger.warning("No 'assets' field in response")
                return []
            
            assets_section = data['assets']
            if not isinstance(assets_section, dict):
                logger.error(f"Assets section is not a dict: {type(assets_section)}")
                return []
            
            if 'items' not in assets_section:
                logger.warning("No 'items' field in assets section")
                logger.info(f"Assets section keys: {list(assets_section.keys())}")
                return []
            
            assets = assets_section['items']
            logger.info(f"Found {len(assets)} assets in items array")
            logger.info(f"Total assets available: {assets_section.get('total', 'unknown')}")
            
            # Validate that assets are actually dict objects with IDs
            valid_assets = []
            for i, asset in enumerate(assets):
                if isinstance(asset, dict) and 'id' in asset:
                    valid_assets.append(asset)
                    if i == 0:  # Log first asset details
                        logger.info(f"First asset ID: {asset.get('id')}")
                        logger.info(f"First asset filename: {asset.get('originalFileName', 'NO_FILENAME')}")
                        logger.info(f"First asset created: {asset.get('fileCreatedAt', 'NO_DATE')}")
                else:
                    logger.warning(f"Asset {i} is invalid: {type(asset)} - missing ID or not dict")
            
            logger.info(f"Returning {len(valid_assets)} valid assets")
            return valid_assets
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching assets via search: {e}")
        except Exception as e:
            logger.error(f"Unexpected error fetching assets: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            
        return []
    
    def get_asset_info(self, asset_id: str) -> Optional[Dict]:
        """Get detailed information about a specific asset"""
        try:
            url = f"{self.base_url}/api/assets/{asset_id}"
            response = self.session.get(url)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching asset info for {asset_id}: {e}")
            return None
    
    def open_asset_stream(self, asset_id: str) -> Optional[requests.Response]:
        """Open a streaming download of an asset's original file.

        Returns the ``requests.Response`` (opened with ``stream=True``) so the
        caller can pipe ``response.raw`` straight into an upload without ever
        holding the whole file in memory. The caller owns the response and must
        close it (ideally via a ``with`` block). Returns ``None`` on error.
        """
        try:
            url = f"{self.base_url}/api/assets/{asset_id}/original"
            response = self.session.get(url, stream=True)
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            logger.error(f"Error opening download stream for asset {asset_id}: {e}")
            return None

    def upload_asset_stream(self, file_stream, content_length: int, filename: str,
                            file_created_at: str) -> Optional[str]:
        """Upload an asset by streaming from a file-like object.

        ``file_stream`` only needs to support ``read(size)``; ``content_length``
        is the exact number of bytes it will yield, which lets us send a real
        ``Content-Length`` instead of chunked transfer encoding. The body is
        streamed, so a large asset never has to be buffered fully in memory.
        """
        try:
            # Wrap the stream so the multipart encoder knows the part's size.
            part = _LengthedStream(file_stream, content_length)
            encoder = MultipartEncoder(fields={
                'assetData': (filename, part, 'application/octet-stream'),
                # Immich v3 removed the deviceAssetId and deviceId properties
                # from POST /assets, so they are no longer sent (see the v3
                # migration guide).
                'fileCreatedAt': file_created_at,
                'fileModifiedAt': file_created_at,
                'isFavorite': 'false',
            })

            headers = {
                'X-API-KEY': self.api_key,
                'Content-Type': encoder.content_type,
            }

            url = f"{self.base_url}/api/assets"
            response = requests.post(url, headers=headers, data=encoder)

            logger.debug(f"Upload response status: {response.status_code}")
            logger.debug(f"Upload response: {response.text[:500]}")
            
            if response.status_code == 201:
                result = response.json()
                asset_id = result.get('id')
                if asset_id:
                    logger.info(f"Successfully uploaded {filename} with ID {asset_id}")
                return asset_id
            elif response.status_code == 200:
                # Handle duplicate case
                result = response.json()
                if result.get('duplicate', False):
                    logger.info(f"Asset {filename} already exists in target account")
                    return result.get('id')
                return result.get('id')
            else:
                logger.error(f"Upload failed with status {response.status_code}: {response.text}")
                return None
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Error uploading asset {filename}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error uploading asset {filename}: {e}")
            return None

class SyncDatabase:
    """Database to track sync state and deleted assets"""
    
    def __init__(self, db_file: str):
        self.db_file = db_file
        self.init_db()
    
    def init_db(self):
        """Initialize the database with required tables"""
        conn = sqlite3.connect(self.db_file)
        try:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS synced_assets (
                    source_id TEXT,
                    target_id TEXT,
                    source_account INTEGER,
                    target_account INTEGER,
                    sync_timestamp DATETIME,
                    PRIMARY KEY (source_id, target_account)
                )
            ''')
            
            conn.execute('''
                CREATE TABLE IF NOT EXISTS deleted_assets (
                    asset_id TEXT,
                    account INTEGER,
                    delete_timestamp DATETIME,
                    PRIMARY KEY (asset_id, account)
                )
            ''')
            
            conn.commit()
        finally:
            conn.close()
    
    def record_sync(self, source_id: str, target_id: str, source_account: int, target_account: int):
        """Record a successful sync operation"""
        conn = sqlite3.connect(self.db_file)
        try:
            conn.execute('''
                INSERT OR REPLACE INTO synced_assets 
                (source_id, target_id, source_account, target_account, sync_timestamp)
                VALUES (?, ?, ?, ?, ?)
            ''', (source_id, target_id, source_account, target_account, datetime.now()))
            conn.commit()
        finally:
            conn.close()
    
    def is_synced(self, source_id: str, target_account: int) -> bool:
        """Check if an asset has already been synced to target account"""
        conn = sqlite3.connect(self.db_file)
        try:
            cursor = conn.execute('''
                SELECT 1 FROM synced_assets 
                WHERE source_id = ? AND target_account = ?
            ''', (source_id, target_account))
            return cursor.fetchone() is not None
        finally:
            conn.close()
    
    def record_deletion(self, asset_id: str, account: int):
        """Record that an asset was deleted from an account"""
        conn = sqlite3.connect(self.db_file)
        try:
            conn.execute('''
                INSERT OR REPLACE INTO deleted_assets 
                (asset_id, account, delete_timestamp)
                VALUES (?, ?, ?)
            ''', (asset_id, account, datetime.now()))
            conn.commit()
            logger.info(f"Recorded deletion of asset {asset_id} from account {account}")
        finally:
            conn.close()
    
    def was_deleted(self, asset_id: str, account: int) -> bool:
        """Check if an asset was previously deleted from an account"""
        conn = sqlite3.connect(self.db_file)
        try:
            cursor = conn.execute('''
                SELECT 1 FROM deleted_assets 
                WHERE asset_id = ? AND account = ?
            ''', (asset_id, account))
            return cursor.fetchone() is not None
        finally:
            conn.close()
    
    def get_synced_assets_for_account(self, account: int) -> Set[str]:
        """Get all asset IDs that were synced to this account"""
        conn = sqlite3.connect(self.db_file)
        try:
            cursor = conn.execute('''
                SELECT target_id FROM synced_assets WHERE target_account = ?
            ''', (account,))
            return set(row[0] for row in cursor.fetchall())
        finally:
            conn.close()

class ImmichSyncManager:
    """Main sync manager"""
    
    def __init__(self):
        self.api1 = ImmichAPI(IMMICH_BASE_URL, API_KEY_1)
        self.api2 = ImmichAPI(IMMICH_BASE_URL, API_KEY_2)
        self.db = SyncDatabase(DB_FILE)

    def _transfer_asset(self, source_api: ImmichAPI, target_api: ImmichAPI,
                        asset_id: str, filename: str,
                        file_created_at: str) -> Optional[str]:
        """Move one asset from source to target with minimal memory use.

        The download is opened as a stream and piped straight into the upload,
        so the asset is never fully held in RAM. If the download doesn't report
        a Content-Length (so we can't set one on the upload), we fall back to
        buffering it in a SpooledTemporaryFile, which stays in RAM only up to
        STREAM_SPOOL_MAX_BYTES before spilling to disk. Returns the new target
        asset id, or None on failure.
        """
        response = source_api.open_asset_stream(asset_id)
        if response is None:
            return None

        try:
            content_length = response.headers.get('Content-Length')

            if content_length is not None:
                # Preferred path: stream the download straight into the upload.
                # Read the raw (undecoded) body so the byte count we forward
                # exactly equals the Content-Length we advertise on the upload.
                # Immich serves originals without content-encoding, so raw bytes
                # are the real file.
                response.raw.decode_content = False
                return target_api.upload_asset_stream(
                    response.raw, int(content_length), filename, file_created_at)

            # Fallback: no Content-Length, so spool the download (RAM up to a
            # threshold, then disk) to learn its size before uploading.
            logger.info(
                f"Asset {asset_id} has no Content-Length; buffering via spool file")
            with tempfile.SpooledTemporaryFile(
                    max_size=STREAM_SPOOL_MAX_BYTES, dir=STREAM_TEMP_DIR) as spool:
                for chunk in response.iter_content(chunk_size=STREAM_CHUNK_SIZE):
                    if chunk:
                        spool.write(chunk)
                size = spool.tell()
                spool.seek(0)
                return target_api.upload_asset_stream(
                    spool, size, filename, file_created_at)
        finally:
            response.close()

    def detect_deletions(self):
        """Detect assets that were deleted from accounts"""
        logger.info("Detecting deleted assets...")
        
        try:
            # Get current assets from both accounts
            assets_1 = self.api1.get_assets(SYNC_START_DATE, SYNC_END_DATE)
            assets_2 = self.api2.get_assets(SYNC_START_DATE, SYNC_END_DATE)
            
            current_assets_1 = set()
            current_assets_2 = set()
            
            # Safely extract asset IDs
            for asset in assets_1:
                if isinstance(asset, dict) and 'id' in asset:
                    current_assets_1.add(asset['id'])
            
            for asset in assets_2:
                if isinstance(asset, dict) and 'id' in asset:
                    current_assets_2.add(asset['id'])
            
            logger.info(f"Account 1 has {len(current_assets_1)} current assets")
            logger.info(f"Account 2 has {len(current_assets_2)} current assets")
            
            # Debug: Log some asset IDs if available
            if current_assets_1:
                logger.debug(f"Account 1 sample asset IDs: {list(current_assets_1)[:3]}")
            if current_assets_2:
                logger.debug(f"Account 2 sample asset IDs: {list(current_assets_2)[:3]}")
            
            # Get previously synced assets
            synced_to_1 = self.db.get_synced_assets_for_account(1)
            synced_to_2 = self.db.get_synced_assets_for_account(2)
            
            # Detect deletions
            deleted_from_1 = synced_to_1 - current_assets_1
            deleted_from_2 = synced_to_2 - current_assets_2
            
            # Record deletions
            for asset_id in deleted_from_1:
                self.db.record_deletion(asset_id, 1)
            
            for asset_id in deleted_from_2:
                self.db.record_deletion(asset_id, 2)
            
            if deleted_from_1 or deleted_from_2:
                logger.info(f"Detected {len(deleted_from_1)} deletions from account 1, {len(deleted_from_2)} deletions from account 2")
            
        except Exception as e:
            logger.error(f"Error detecting deletions: {e}")
    
    def sync_assets(self, source_api: ImmichAPI, target_api: ImmichAPI, 
                   source_account: int, target_account: int):
        """Sync assets from source to target account"""
        logger.info(f"Syncing from account {source_account} to account {target_account}")
        
        # Get assets from source account
        assets = source_api.get_assets(SYNC_START_DATE, SYNC_END_DATE)
        
        synced_count = 0
        skipped_count = 0
        error_count = 0
        
        logger.info(f"Processing {len(assets)} assets from account {source_account}")
        
        # Debug: Show what types of assets we have
        asset_types = {}
        for asset in assets:
            if isinstance(asset, dict):
                asset_type = asset.get('type', 'UNKNOWN')
                asset_types[asset_type] = asset_types.get(asset_type, 0) + 1
        
        logger.info(f"Asset types found: {asset_types}")
        
        # Debug: Show some sample filenames and dates
        for i, asset in enumerate(assets[:5]):  # First 5 assets
            if isinstance(asset, dict):
                filename = asset.get('originalFileName', 'NO_FILENAME')
                created = asset.get('fileCreatedAt', 'NO_DATE')
                asset_type = asset.get('type', 'UNKNOWN')
                logger.info(f"Sample asset {i+1}: {filename} ({asset_type}) created {created}")
        
        for asset in assets:
            if not isinstance(asset, dict):
                logger.warning(f"Skipping invalid asset: {asset}")
                error_count += 1
                continue
                
            asset_id = asset.get('id')
            if not asset_id:
                logger.warning(f"Asset missing ID: {asset}")
                error_count += 1
                continue
            
            # Skip if already synced
            if self.db.is_synced(asset_id, target_account):
                logger.debug(f"Skipping {asset_id} - already synced")
                skipped_count += 1
                continue
            
            # Skip if this asset was deleted from target account
            if self.db.was_deleted(asset_id, target_account):
                logger.info(f"Skipping asset {asset_id} - was previously deleted from account {target_account}")
                skipped_count += 1
                continue
            
            # Filter by asset type - only sync images and videos
            asset_type = asset.get('type', 'UNKNOWN')
            if asset_type not in ['IMAGE', 'VIDEO']:
                logger.debug(f"Skipping asset {asset_id} - type {asset_type} not supported")
                skipped_count += 1
                continue
            
            try:
                # Get asset details
                asset_info = source_api.get_asset_info(asset_id)
                if not asset_info:
                    logger.warning(f"Could not get info for asset {asset_id}")
                    error_count += 1
                    continue
                
                filename = asset_info.get('originalFileName', f"asset_{asset_id}")
                file_created_at = asset_info.get('fileCreatedAt', asset_info.get('createdAt'))

                # Stream the asset straight from source to target so the file
                # is never fully buffered in RAM.
                logger.info(f"Transferring asset {asset_id} ({filename}) to account {target_account}...")
                target_id = self._transfer_asset(
                    source_api, target_api, asset_id, filename, file_created_at)
                if not target_id:
                    logger.warning(f"Could not transfer asset {asset_id}")
                    error_count += 1
                    continue

                self.db.record_sync(asset_id, target_id, source_account, target_account)
                synced_count += 1
                logger.info(f"Successfully synced {filename} (source: {asset_id} -> target: {target_id})")

                # Small delay to avoid overwhelming the API
                time.sleep(0.5)
                
            except Exception as e:
                logger.error(f"Error processing asset {asset_id}: {e}")
                error_count += 1
                continue
        
        logger.info(f"Sync from account {source_account} to {target_account} complete: {synced_count} synced, {skipped_count} skipped, {error_count} errors")
    
    def run_sync_cycle(self):
        """Run a complete sync cycle"""
        logger.info("Starting sync cycle...")
        
        try:
            # First, detect any deletions
            self.detect_deletions()
            
            # Sync from account 1 to account 2
            self.sync_assets(self.api1, self.api2, 1, 2)
            
            # Sync from account 2 to account 1
            self.sync_assets(self.api2, self.api1, 2, 1)
            
            logger.info("Sync cycle completed successfully")
            
        except Exception as e:
            logger.error(f"Error during sync cycle: {e}")
    
    def run_continuous(self):
        """Run continuous sync with specified interval"""
        logger.info(f"Starting continuous sync (interval: {SYNC_INTERVAL} seconds)")
        
        while True:
            try:
                self.run_sync_cycle()
                logger.info(f"Waiting {SYNC_INTERVAL} seconds until next sync...")
                time.sleep(SYNC_INTERVAL)
                
            except KeyboardInterrupt:
                logger.info("Received interrupt signal, stopping sync...")
                break
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                logger.info("Waiting 5 minutes before retrying...")
                time.sleep(300)  # Wait 5 minutes before retrying

def main():
    """Main entry point"""
    logger.info("Immich Photo Sync Script starting...")
    logger.info(f"Sync date range: {SYNC_START_DATE} to {SYNC_END_DATE}")
    logger.info(f"Sync interval: {SYNC_INTERVAL} seconds")
    
    # Create sync manager and run
    sync_manager = ImmichSyncManager()
    
    if len(sys.argv) > 1 and sys.argv[1] == '--once':
        # Run once and exit
        logger.info("Running single sync cycle...")
        sync_manager.run_sync_cycle()
    else:
        # Run continuously
        sync_manager.run_continuous()

if __name__ == "__main__":
    main()
