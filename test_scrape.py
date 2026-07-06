import asyncio
import httpx
import urllib.parse

async def search_s2(query):
    async with httpx.AsyncClient() as client:
        url = f"https://api.semanticscholar.org/graph/v1/paper/search?query={urllib.parse.quote(query)}&limit=1&fields=title,abstract,authors"
        resp = await client.get(url)
        if resp.status_code == 200:
            data = resp.json()
            if data["data"]:
                p = data["data"][0]
                print(f"TITLE: {p['title']}\nABSTRACT: {p.get('abstract')}")
            else:
                print("No results")
        else:
            print(f"Failed {resp.status_code}")

asyncio.run(search_s2("Entropy measurements in partially filled Landau levels"))
