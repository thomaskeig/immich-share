# immich-share

A Python script that synchronizes photos and videos between two Immich accounts while respecting deletions and maintaining sync state.

## Features

- **Bidirectional Sync**: Synchronizes assets between two Immich accounts in both directions
- **Date Range Filtering**: Only syncs assets within a specified date range
- **Deletion Awareness**: Respects deletions - won't re-sync assets that were intentionally deleted
- **Duplicate Prevention**: Tracks synced assets to avoid duplicate uploads
- **Continuous Operation**: Can run continuously with configurable intervals
- **Comprehensive Logging**: Detailed logging to both file and console
- **SQLite Database**: Maintains sync state and deletion history in a local database
- **Asset Type Support**: Handles both images and videos
- **Error Handling**: Robust error handling with retry logic

## Prerequisites

- Python 3.7+
- Access to an Immich instance
- Two Immich accounts with API keys (Must be on the same Immich instance)
- Required Python packages (see `requirements.txt`)

## Installation

1. Clone this repository:
```bash
git clone https://github.com/thomaskeig/immich-share.git
cd immich-share
```

2. Install required dependencies:
```bash
pip install -r requirements.txt
```

3. Configure the script by editing the configuration variables in `main.py`:

```python
# Configuration
IMMICH_BASE_URL = "https://your-immich-instance.com"
API_KEY_2 = "your-second-account-api-key"
API_KEY_1 = "your-first-account-api-key"
SYNC_START_DATE = "2025-08-09"  # Start date for sync range
SYNC_END_DATE = "2025-08-20"    # End date for sync range
SYNC_INTERVAL = 3600            # Sync interval in seconds (1 hour)
```

## Configuration

### Required Configuration Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `IMMICH_BASE_URL` | Base URL of your Immich instance | `"https://photos.example.com"` |
| `API_KEY_1` | API key for the first Immich account | `"your-api-key-here"` |
| `API_KEY_2` | API key for the second Immich account | `"your-api-key-here"` |
| `SYNC_START_DATE` | Start date for syncing assets (YYYY-MM-DD) | `"2025-01-01"` |
| `SYNC_END_DATE` | End date for syncing assets (YYYY-MM-DD) | `"2025-12-31"` |
| `SYNC_INTERVAL` | Time between sync cycles in seconds | `3600` (1 hour) |

### Getting API Keys

1. Log in to your Immich web interface
2. Go to **Account Settings** → **API Keys**
3. Create a new API key
4. Copy the generated key and paste it into the configuration

## Usage

### Run Continuous Sync

To run the sync continuously with the configured interval:

```bash
python main.py
```

### Stop Continuous Sync

Press `Ctrl+C` to gracefully stop the continuous sync process.

## Sync Process

1. **Asset Discovery**: Fetches all assets from both accounts within the specified date range
2. **Deletion Detection**: Identifies assets that were previously synced but are now missing (indicating deletion)
3. **Bidirectional Sync**: 
   - Syncs new assets from Account 1 to Account 2
   - Syncs new assets from Account 2 to Account 1
4. **State Tracking**: Records all sync operations in a local SQLite database
5. **Duplicate Prevention**: Skips assets that have already been synced
6. **Deletion Respect**: Won't re-sync assets that were intentionally deleted

## Safety Features

- **Non-Destructive**: Never deletes assets, only adds them
- **Deletion Awareness**: Respects user deletions and won't re-sync deleted items
- **State Persistence**: Maintains sync state across script restarts
- **Duplicate Prevention**: Avoids creating duplicate assets

## Limitations

- Only syncs assets within the specified date range
- Requires manual configuration of API keys and date ranges
- Does not sync metadata like albums, tags, or favorites
- Limited to IMAGE and VIDEO asset types

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## Disclaimer

This script is not officially affiliated with Immich. Use at your own risk and always backup your photos before running sync operations.

## Support

For issues and questions:
1. Check the troubleshooting section above
2. Review the log files for detailed error messages
3. Open an issue on GitHub with relevant log excerpts