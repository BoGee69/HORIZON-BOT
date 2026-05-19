import logging
import asyncio
import aiohttp
import boto3
from bs4 import BeautifulSoup
from discord.ext import tasks, commands
from config import OPEN_DIRECTORY_URL, SYNC_INTERVAL_HOURS, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ACCOUNT_ID, R2_BUCKET_NAME

log = logging.getLogger(__name__)

class OpenDirSync(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        )
        self.sync_task.start()

    def cog_unload(self):
        self.sync_task.cancel()

    async def get_r2_files(self):
        """Ambil daftar file yang udah ada di R2"""
        paginator = self.s3.get_paginator('list_objects_v2')
        files = set()
        for page in paginator.paginate(Bucket=R2_BUCKET_NAME):
            for obj in page.get('Contents', []):
                files.add(obj['Key'])
        return files

    @tasks.loop(hours=SYNC_INTERVAL_HOURS)
    async def sync_task(self):
        log.info("🔄 Memulai sinkronisasi Open Directory...")
        r2_files = await self.get_r2_files()
        
        async with aiohttp.ClientSession() as session:
            # Ambil daftar folder abjad (A, B, C...)
            async with session.get(OPEN_DIRECTORY_URL) as resp:
                html = await resp.text()
                soup = BeautifulSoup(html, 'html.parser')
                folders = [link['href'] for link in soup.find_all('a') if link['href'].endswith('/') and link['href'] != '../']

            for folder in folders:
                folder_url = OPEN_DIRECTORY_URL + folder
                async with session.get(folder_url) as f_resp:
                    f_html = await f_html = await f_resp.text()
                    f_soup = BeautifulSoup(f_html, 'html.parser')
                    
                    for link in f_soup.find_all('a'):
                        filename = link['href']
                        if filename.endswith(('.zip', '.manifest', '.lua')):
                            r2_key = f"Database/{filename}"
                            if r2_key not in r2_files:
                                log.info(f"🆕 Menemukan file baru: {filename}. Mengupload...")
                                await self.upload_to_r2(session, folder_url + filename, r2_key)
                                r2_files.add(r2_key)
        log.info("✅ Sinkronisasi selesai. Bot masuk ke Mode Pantau.")

    async def upload_to_r2(self, session, file_url, r2_key):
        """Download dari sumber, upload ke R2 tanpa nyimpen di disk (RAM only)"""
        async with session.get(file_url) as resp:
            content = await resp.read()
            self.s3.put_object(Bucket=R2_BUCKET_NAME, Key=r2_key, Body=content)
            log.info(f"☁️ Berhasil upload {r2_key} ke R2")

    @sync_task.before_loop
    async def before_sync(self):
        await self.bot.wait_until_ready()