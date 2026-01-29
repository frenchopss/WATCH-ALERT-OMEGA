import os
import requests

def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        print("Webhook manquant")
        return

    requests.post(
        webhook,
        json={"content": "✅ TEST OK — GitHub Actions → Discord fonctionne"},
        timeout=10
    )

if __name__ == "__main__":
    main()
  
