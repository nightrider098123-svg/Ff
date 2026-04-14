# How to run this bot in Google Colab

1. Create a new Google Colab notebook.
2. In the first cell, install the necessary dependencies:

   ```bash
   !apt-get update
   !apt-get install -y aria2 ffmpeg unrar unzip
   !pip install pyrogram tgcrypto nest_asyncio
   ```

3. In the second cell, copy the contents of `telegram_torrent_bot.py` or upload the file and run it. You will need to provide your API_ID, API_HASH, BOT_TOKEN, and target GROUP_ID.

   ```bash
   # Run the script via command line
   !python telegram_torrent_bot.py --api-id YOUR_API_ID --api-hash YOUR_API_HASH --bot-token YOUR_BOT_TOKEN --group-id YOUR_GROUP_ID
   ```

   If it prompts you to authorize Google Drive access, please allow it.
