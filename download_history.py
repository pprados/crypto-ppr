# https://www.cryptodatadownload.com/data/binance/
#https://www.cryptodatadownload.com/cdd/Binance_BTCUSDT_d.csv
#https://www.cryptodatadownload.com/cdd/Binance_ETHUSDT_1h.csv
#https://www.cryptodatadownload.com/cdd/Binance_BTCUSDT_minute.csv

#https://www.cryptodatadownload.com/cdd/Binance_ETHUSDT_d.csv
from pathlib import Path

import pandas as pd

import urllib.request

import ssl

ssl._create_default_https_context = ssl._create_unverified_context

def download_historical_values(symbol:str, interval:str) -> pd.DataFrame:
    assert interval in ("1d","1h","1m")
    filename = Path(f'caches/{symbol}_{interval}.csv')
    if not filename.exists():
        if interval == "1d":
            t="d"
        elif interval == "1h":
            t="1h"
        else:
            t="minute"
        context = ssl._create_unverified_context()
        response = urllib.request.urlopen(f"http://www.cryptodatadownload.com/cdd/Binance_{symbol}_{t}.csv",context=context)
        response.readline()
        open(filename, 'wb').write(response.read())
    data = pd.read_csv(filename)
    data = data.reindex(index=data.index[::-1])
    return data
