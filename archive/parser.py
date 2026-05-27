import requests
import pandas as pd
from io import BytesIO
from datetime import datetime, timezone
from metar import Metar

today = datetime.now(timezone.utc)

def fetch_metar(year_start, year_end, station):
    url = (
        "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
        f"?station={station}&data=metar"
        f"&year1={year_start}&month1=1&day1=1"
        f"&year2={year_end}&month2=1&day2=1"
        "&tz=UTC&format=onlycomma&missing=M"
        "&report_type=3&report_type=4"
    )
    return pd.read_csv(BytesIO(requests.get(url).content))

def fetch_taf(start_iso, end_iso, station):
    url = (
        "https://mesonet.agron.iastate.edu/cgi-bin/request/taf.py"
        f"?station=K{station}&sts={start_iso}&ets={end_iso}&fmt=csv"
    )
    return pd.read_csv(BytesIO(requests.get(url).content))

def parse_metar(raw):
    try:
        obs = Metar.Metar(raw)
        return {
            "temp_c":   obs.temp.value("C") if obs.temp else None,
            "dewp_c":   obs.dewpt.value("C") if obs.dewpt else None,
            "wdir":     obs.wind_dir.value() if obs.wind_dir else None,
            "wspd_kt":  obs.wind_speed.value("KT") if obs.wind_speed else None,
            "gust_kt":  obs.wind_gust.value("KT") if obs.wind_gust else None,
            "max6_c":   obs.max_temp_6hr.value("C") if obs.max_temp_6hr else None,
            "min6_c":   obs.min_temp_6hr.value("C") if obs.min_temp_6hr else None,
            "max24_c":  obs.max_temp_24hr.value("C") if obs.max_temp_24hr else None,
            "min24_c":  obs.min_temp_24hr.value("C") if obs.min_temp_24hr else None,
            "press_mb": obs.press.value("MB") if obs.press else None,
        }
    except Exception:
        return {}

if __name__ == "__main__":
    df = fetch_metar(2020, today.year + (1 if today.month == 12 and today.day == 31 else 0))
    parsed = df["metar"].apply(parse_metar).apply(pd.Series)
    df = pd.concat([df[["station", "valid"]], parsed], axis=1)
    df["valid"] = pd.to_datetime(df["valid"], utc=True)
    df.to_parquet("kmdw_metar.parquet")

    taf = fetch_taf("2020-01-01T00:00Z", today.strftime("%Y-%m-%dT%H:%MZ"))
    taf.to_parquet("kmdw_taf.parquet")