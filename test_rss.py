import asyncio
import logging
from scraper import fetch_wiley_papers, fetch_acs_papers, fetch_aip_papers, fetch_nature_papers, fetch_aps_papers

async def main():
    print("Testing Wiley...")
    wiley = await fetch_wiley_papers()
    print(f"Wiley returned {len(wiley)} papers")
    
    print("Testing ACS...")
    acs = await fetch_acs_papers()
    print(f"ACS returned {len(acs)} papers")
    
    print("Testing AIP...")
    aip = await fetch_aip_papers()
    print(f"AIP returned {len(aip)} papers")
    
    print("Testing Nature extensions...")
    nat = await fetch_nature_papers()
    print(f"Nature returned {len(nat)} papers")

if __name__ == "__main__":
    asyncio.run(main())
