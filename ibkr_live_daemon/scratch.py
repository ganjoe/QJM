import asyncio
from ib_insync import IB
async def main():
    ib = IB()
    await ib.connectAsync('127.0.0.1', 4002, clientId=10003)
    results = await ib.reqMatchingSymbolsAsync('Serve Robotics')
    for r in results:
        print(r.contract.symbol, r.contract.secType, r.contract.primaryExchange, r.contract.currency)
    ib.disconnect()
asyncio.run(main())
