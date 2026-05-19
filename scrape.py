import aiohttp
import asyncio
import json

# ================= KONFIGURASI =================
# Ganti dengan URL website tempat lu ngambil kode JS ini
# Contoh: "https://manifest.website-target.com"
API_BASE_URL = "https://www.depotgame.my.id/" 

# Ganti dengan App ID game yang mau lu download
TARGET_APP_ID = "252490" # Contoh: Rust
# ===============================================

async def download_manifest_via_api():
    print(f"🚀 Menghubungkan ke API Manifest Hub: {API_BASE_URL}...")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        # ---------------------------------------------------------
        # STEP 1: Cek Ketersediaan Game (Niru fungsi checkAvailabilityAndPreview)
        # ---------------------------------------------------------
        check_url = f"{API_BASE_URL}/api/check-availability"
        check_payload = {"app_id": TARGET_APP_ID}
        
        print(f"🔍 Mengecek ketersediaan App ID: {TARGET_APP_ID}...")
        try:
            async with session.post(check_url, json=check_payload) as resp:
                data = await resp.json()
                if not data.get("success") or not data.get("available"):
                    print("❌ Game tidak tersedia di database server ini!")
                    return
                print("✅ Game tersedia! Lanjut proses download...")
        except Exception as e:
            print(f"💥 Gagal menghubungi API ketersediaan: {e}")
            return

        # ---------------------------------------------------------
        # STEP 2: Generate & Download ZIP (Niru fungsi generateZip)
        # ---------------------------------------------------------
        gen_url = f"{API_BASE_URL}/api/generate"
        
        # Payload ini disalin persis dari baris 790 di JS lu
        gen_payload = {
            "app_id": TARGET_APP_ID,
            "depot_id": str(int(TARGET_APP_ID) + 1), # Default fallback biasanya AppID + 1
            "manifest_id": "7884779798207988041",   # Fallback default dari JS lu
            "depot_key": "",
            "branch": "public",
            "game_name": f"App {TARGET_APP_ID}",
            "use_ryuu_api": True
        }

        print("📦 Memerintahkan server untuk meracik file ZIP...")
        try:
            async with session.post(gen_url, json=gen_payload) as resp:
                if resp.status == 200:
                    # Server akan langsung mengirim byte file ZIP
                    zip_content = await resp.read()
                    
                    filename = f"[{TARGET_APP_ID}].zip"
                    with open(filename, "wb") as f:
                        f.write(zip_content)
                        
                    print(f"🎉 BINGO! File berhasil diamankan: {filename}")
                else:
                    error_text = await resp.text()
                    print(f"❌ Server gagal meracik ZIP (HTTP {resp.status}): {error_text[:100]}")
        except Exception as e:
            print(f"💥 Terjadi kesalahan saat download: {e}")

if __name__ == "__main__":
    asyncio.run(download_manifest_via_api())