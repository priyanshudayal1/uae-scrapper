import json
import os

TRACKING_FILE = "downloaded_files.json"

def main():
    if not os.path.exists(TRACKING_FILE):
        print(f"Tracking file '{TRACKING_FILE}' not found.")
        return

    try:
        with open(TRACKING_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Try to get from stats
        stats = data.get("stats", {})
        total_downloaded = stats.get("total_downloaded")
        
        # If stats not reliable or missing, count files
        files = data.get("files", {})
        file_count = len(files)
        
        print(f"\nTracking File: {os.path.abspath(TRACKING_FILE)}")
        print("-" * 40)
        
        if total_downloaded is not None:
             print(f"Total Downloaded (from stats): {total_downloaded}")
        
        print(f"Total Files Tracked (actual count): {file_count}")
        
        # Also show upload stats if available
        total_uploaded = stats.get("total_uploaded")
        if total_uploaded is not None:
            print(f"Total Uploaded to S3: {total_uploaded}")
        
        last_updated = data.get("last_updated")
        if last_updated:
            print(f"Last Updated: {last_updated}")
            
        print("-" * 40)

    except Exception as e:
        print(f"Error reading tracking file: {e}")

if __name__ == "__main__":
    main()
