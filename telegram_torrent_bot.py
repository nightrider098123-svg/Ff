import argparse
import os
import sys
import csv
import time

class StateManager:
    def __init__(self, logs_dir):
        self.logs_dir = logs_dir
        self.csv_path = os.path.join(logs_dir, "progress.csv")
        self.headers = ["ID", "Title", "Year", "Source", "Magnet", "Status", "Error_Message", "Created_At", "Updated_At"]
        self._init_csv()

    def _init_csv(self):
        if not os.path.exists(self.csv_path):
            with open(self.csv_path, mode='w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(self.headers)

    def read_all(self):
        records = []
        if not os.path.exists(self.csv_path):
            return records
        with open(self.csv_path, mode='r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                records.append(row)
        return records

    def write_all(self, records):
        with open(self.csv_path, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=self.headers)
            writer.writeheader()
            writer.writerows(records)

    def add_item(self, title, year, source, magnet):
        records = self.read_all()
        # Check if already exists by magnet
        for r in records:
            if r["Magnet"] == magnet:
                return r["ID"]

        new_id = str(int(time.time() * 1000))
        new_record = {
            "ID": new_id,
            "Title": title,
            "Year": year,
            "Source": source,
            "Magnet": magnet,
            "Status": "Pending",
            "Error_Message": "",
            "Created_At": time.strftime("%Y-%m-%d %H:%M:%S"),
            "Updated_At": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        records.append(new_record)
        self.write_all(records)
        return new_id

    def update_status(self, item_id, status, error_message=""):
        records = self.read_all()
        for r in records:
            if r["ID"] == item_id:
                r["Status"] = status
                r["Error_Message"] = error_message
                r["Updated_At"] = time.strftime("%Y-%m-%d %H:%M:%S")
                break
        self.write_all(records)

    def get_pending(self):
        return [r for r in self.read_all() if r["Status"] in ["Pending", "Downloading", "Processing", "Uploading"]]

import subprocess
import asyncio
import glob
import shutil
import math
from pyrogram import Client

VIDEO_EXTENSIONS = ['.mp4', '.mkv', '.avi', '.mov']
SUB_EXTENSIONS = ['.srt', '.vtt', '.ass']
ARCHIVE_EXTENSIONS = ['.zip', '.rar']
MAX_FILE_SIZE = 1950 * 1024 * 1024 # 1.95 GB to be safe

def get_files_by_ext(dir_path, extensions):
    found = []
    for root, dirs, files in os.walk(dir_path):
        for f in files:
            if any(f.lower().endswith(ext) for ext in extensions):
                found.append(os.path.join(root, f))
    return found

async def extract_archives(dir_path):
    archives = get_files_by_ext(dir_path, ARCHIVE_EXTENSIONS)
    for archive in archives:
        print(f"Extracting archive: {archive}")
        if archive.lower().endswith('.zip'):
            cmd = ["unzip", "-o", archive, "-d", os.path.dirname(archive)]
        elif archive.lower().endswith('.rar'):
            cmd = ["unrar", "x", "-o+", archive, os.path.dirname(archive)]
        else:
            continue

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await process.communicate()
            if process.returncode == 0:
                print(f"Extraction successful: {archive}")
                os.remove(archive)
            else:
                print(f"Failed to extract {archive} (return code {process.returncode})")
        except Exception as e:
            print(f"Failed to extract {archive}: {e}")

async def get_duration(video_path):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path]
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await process.communicate()
        if process.returncode == 0:
            return float(stdout.decode('utf-8').strip())
        return 0.0
    except Exception as e:
        print(f"Failed to get duration for {video_path}: {e}")
        return 0.0

async def split_video(video_path, output_dir):
    file_size = os.path.getsize(video_path)
    if file_size <= MAX_FILE_SIZE:
        return [video_path]

    print(f"Video {video_path} is larger than 1.95GB. Splitting...")
    duration = await get_duration(video_path)
    if duration == 0.0:
        return [video_path] # Fallback if duration fails

    num_parts = math.ceil(file_size / MAX_FILE_SIZE)
    part_duration = math.ceil(duration / num_parts)

    base_name = os.path.basename(video_path)
    name, ext = os.path.splitext(base_name)
    split_files = []

    for i in range(num_parts):
        start_time = i * part_duration
        out_file = os.path.join(output_dir, f"{name}_part{i+1}{ext}")
        cmd = [
            "ffmpeg", "-i", video_path, "-ss", str(start_time), "-t", str(part_duration),
            "-c", "copy", "-map", "0", out_file
        ]
        print(f"Creating part {i+1}...")
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await process.communicate()
            if process.returncode == 0:
                split_files.append(out_file)
            else:
                print(f"Failed to split video part {i+1} (return code {process.returncode})")
        except Exception as e:
            print(f"Failed to split video part {i+1}: {e}")

    if split_files:
        os.remove(video_path) # Remove original after split
    return split_files

async def process_media(dir_path, title):
    await extract_archives(dir_path)

    videos = get_files_by_ext(dir_path, VIDEO_EXTENSIONS)
    subs = get_files_by_ext(dir_path, SUB_EXTENSIONS)

    # Optional: You might want to pick the largest video if there are extras
    if not videos:
        return []

    videos.sort(key=lambda x: os.path.getsize(x), reverse=True)
    main_video = videos[0]

    if subs:
        # Soft merge first sub
        sub_file = subs[0]
        print(f"Merging subtitle {sub_file} into {main_video}...")
        out_file = os.path.join(dir_path, f"merged_{os.path.basename(main_video)}")
        # Use copy for MKV, mov_text for MP4/MOV, etc
        subtitle_codec = "copy" if main_video.lower().endswith(".mkv") else "mov_text"
        # Adding soft subtitle
        cmd = ["ffmpeg", "-i", main_video, "-i", sub_file, "-c", "copy", "-c:s", subtitle_codec, out_file]
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await process.communicate()
            if process.returncode == 0:
                os.remove(main_video)
                main_video = out_file
                print("Merge successful.")
            else:
                print(f"Subtitle merge failed (return code {process.returncode}). Falling back to original video.")
        except Exception as e:
            print(f"Subtitle merge failed: {e}. Falling back to original video.")

    # Rename to Title
    _, ext = os.path.splitext(main_video)
    safe_title = "".join([c for c in title if c.isalpha() or c.isdigit() or c==' ']).rstrip()
    renamed_video = os.path.join(dir_path, f"{safe_title}{ext}")
    os.rename(main_video, renamed_video)

    # Check size and split if needed
    final_files = await split_video(renamed_video, dir_path)
    return final_files

async def download_magnet(magnet, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    print(f"Starting download for magnet link...")

    cmd = [
        "aria2c",
        "--seed-time=0",
        "--max-upload-limit=1K",
        "--bt-require-crypto=true",
        "--bt-force-encryption=true",
        "--enable-dht=true",
        "--dht-listen-port=6881-6999",
        "--dir", output_dir,
        magnet
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await process.communicate()

        if process.returncode == 0:
            print("Download completed successfully.")
            return True, None
        else:
            err_msg = f"aria2c exited with code {process.returncode}. Stderr: {stderr.decode('utf-8')}"
            print(f"Download failed: {err_msg}")
            return False, err_msg

    except Exception as e:
        err_msg = f"Exception during download: {str(e)}"
        print(err_msg)
        return False, err_msg

def parse_txt_file(filepath, state_manager):
    if not os.path.exists(filepath):
        print(f"File not found: {filepath}")
        return

    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    blocks = content.split('--------------------------------------------------------------------------------')

    count = 0
    for block in blocks:
        block = block.strip()
        if not block:
            continue

        lines = block.split('\n')
        title = ""
        year = ""
        source = ""
        magnet = ""

        for line in lines:
            line = line.strip()
            if line.startswith("Title:"):
                title = line[len("Title:"):].strip()
            elif line.startswith("Year:"):
                year = line[len("Year:"):].strip()
            elif line.startswith("Source:"):
                source = line[len("Source:"):].strip()
            elif line.startswith("Magnet:"):
                magnet = line[len("Magnet:"):].strip()

        if title and magnet:
            state_manager.add_item(title, year, source, magnet)
            count += 1

    print(f"Parsed and added {count} new items from {filepath} to state manager.")

def mount_drive():
    try:
        from google.colab import drive
        print("Mounting Google Drive...")
        drive.mount('/content/drive')
        print("Google Drive mounted successfully.")
    except ImportError:
        print("Not running in Google Colab. Skipping Google Drive mount.")

def parse_args():
    parser = argparse.ArgumentParser(description="Telegram Magnet Downloader Bot")
    parser.add_argument("--api-id", required=True, type=int, help="Telegram API ID")
    parser.add_argument("--api-hash", required=True, type=str, help="Telegram API Hash")
    parser.add_argument("--bot-token", required=True, type=str, help="Telegram Bot Token")
    parser.add_argument("--group-id", required=True, type=int, help="Target Telegram Group ID (e.g., -1001234567890)")
    parser.add_argument("--logs-dir", default="/content/drive/MyDrive/logs", type=str, help="Directory to store logs and CSV state files")
    return parser.parse_args()

async def upload_files(client, group_id, files, title):
    for f in files:
        print(f"Uploading {f} to telegram...")
        try:
            await client.send_document(
                chat_id=group_id,
                document=f,
                caption=f"**{title}**\n\nUploaded by Colab Bot."
            )
            print(f"Uploaded {f} successfully.")
        except Exception as e:
            print(f"Failed to upload {f}: {e}")
            raise e

from pyrogram import filters

async def async_main(args, state_manager):
    app = Client(
        "bot_session",
        api_id=args.api_id,
        api_hash=args.api_hash,
        bot_token=args.bot_token
    )

    @app.on_message(filters.document & filters.private)
    async def handle_document(client, message):
        print(f"Received document: {message.document.file_name}")
        if not message.document.file_name.endswith('.txt') and not message.document.file_name.endswith('.csv'):
            await message.reply("Please send a .txt file containing the torrent info.")
            return

        await message.reply("Downloading document...")
        file_path = await message.download()

        await message.reply("Parsing document...")
        parse_txt_file(file_path, state_manager)

        os.remove(file_path)
        await message.reply("Document parsed and added to queue. Please check logs.")

        # Kick off processing logic
        asyncio.create_task(process_queue(client, args, state_manager))

    # A global lock so we don't process the queue concurrently and hit duplicate items
    processing_lock = asyncio.Lock()

    async def process_queue(client, args, state_manager):
        async with processing_lock:
            pending_items = state_manager.get_pending()
            if not pending_items:
                print("No pending items found.")
                return

            for item in pending_items:
                item_id = item["ID"]
                title = item["Title"]
                magnet = item["Magnet"]

                print(f"\nProcessing {title}...")

                state_manager.update_status(item_id, "Downloading")
                download_dir = os.path.join("/content/downloads", item_id)
                if not os.path.exists("/content"): # Local testing fallback
                    download_dir = os.path.join("./downloads", item_id)

                success, err = await download_magnet(magnet, download_dir)

                if not success:
                    state_manager.update_status(item_id, "Error", err)
                    continue

                state_manager.update_status(item_id, "Processing")
                final_files = await process_media(download_dir, title)

                if not final_files:
                    state_manager.update_status(item_id, "Error", "No media files found after download.")
                    continue

                state_manager.update_status(item_id, "Uploading")
                try:
                    await upload_files(client, args.group_id, final_files, title)
                    state_manager.update_status(item_id, "Completed")
                except Exception as e:
                    state_manager.update_status(item_id, "Error", f"Upload failed: {str(e)}")

                # Cleanup
                if os.path.exists(download_dir):
                    shutil.rmtree(download_dir)
                    print(f"Cleaned up {download_dir}")

    await app.start()
    print("Telegram client started. Waiting for messages...")

    # Run any existing queue items on startup
    asyncio.create_task(process_queue(app, args, state_manager))

    import pyrogram
    await pyrogram.idle()
    await app.stop()

def main():
    args = parse_args()
    mount_drive()

    # Ensure logs directory exists
    os.makedirs(args.logs_dir, exist_ok=True)
    print(f"Logs directory ready at: {args.logs_dir}")

    state_manager = StateManager(args.logs_dir)
    print("State manager initialized.")

    # In Colab, we need to apply nest_asyncio to run asyncio event loops inside notebook cells
    try:
        import nest_asyncio
        nest_asyncio.apply()
    except ImportError:
        pass

    asyncio.run(async_main(args, state_manager))

if __name__ == "__main__":
    main()
