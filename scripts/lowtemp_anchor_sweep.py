"""Test the low-temp 'buy high-confidence favorite(s)' configs at DIFFERENT anchor (entry) times.

Builds, per (city, event_date, anchor), the favorite's last-trade price + win, for anchors spanning
8 PM -> 2 AM local (the daily low settles through the evening/overnight, so the anchor changes how
'settled' the favorite is). Then runs the best-by-type configs (band 0.93-0.95, $250) at each anchor.

Anchors are minutes from the event-day's local midnight: 20:00..23:30 are same-day; 00:00/01:00/02:00
are the next calendar morning (the day-D low is locked by then -> near-settled). Reads the local
low-temp backfill. Truth = settlement_value. Last-trade (no ask) -> optimistic.

Usage: python scripts/lowtemp_anchor_sweep.py [--rebuild]
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np, pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from scripts.late_night_coverage import LOW, load_series, find_base
from scripts.lowtemp_2city_algo import REGION

CACHE = _ROOT / "data" / "lowtemp_anchor_events.parquet"
ANCHORS = [(1200,"20:00"),(1260,"21:00"),(1320,"22:00"),(1380,"23:00"),(1410,"23:30"),
           (1440,"00:00"),(1500,"01:00"),(1560,"02:00")]
A,B,BR=0.93,0.95,250.0


def build(base):
    rows=[]
    for s,(label,tz_name) in LOW.items():
        if not (base/s).is_dir(): continue
        df=load_series(base,s)
        if df.empty: continue
        tz=ZoneInfo(tz_name)
        for ev,day in df.groupby("event_date"):
            w=day.loc[day["settlement_value"]=="yes","ticker"].unique()
            if len(w)!=1: continue
            win=w[0]; d=day.dropna(subset=["yes_price_cents"]).sort_values("timestamp")
            if d.empty: continue
            mid=pd.Timestamp(ev.year,ev.month,ev.day,0,0,tz=tz)
            for mins,lab in ANCHORS:
                snap=(mid+pd.Timedelta(minutes=mins)).tz_convert("UTC")
                pre=d[d["timestamp"]<=snap]
                if pre.empty: continue
                last=pre.groupby("ticker")["yes_price_cents"].last().sort_values(ascending=False)
                if last.size<3: continue
                fav=last.index[0]
                rows.append({"series":s,"city":label,"region":REGION[s],"date":ev,"anchor":lab,
                             "price":last.iloc[0]/100.0,"win":bool(fav==win)})
        print(f"  built {s}",flush=True)
    out=pd.DataFrame(rows); out.to_parquet(CACHE,index=False); return out


def sim(df_anchor,nc,mode,sizing):
    bal=BR; eq=[bal]; daily=[]; pk=0; wn=0; am=0; pp=[]
    for d,day in df_anchor.sort_values("date").groupby("date"):
        cand=day[(day.price>=A)&(day.price<=B)].sort_values("price",ascending=False)
        if cand.empty: continue
        if mode=='uncorr':
            chosen=[]; used=set()
            for _,r in cand.iterrows():
                if r.region not in used: chosen.append(r); used.add(r.region)
                if len(chosen)>=nc: break
        else: chosen=[r for _,r in cand.head(nc).iterrows()]
        n=len(chosen); pp.append(n)
        if all(not c.win for c in chosen): am+=1
        per=(0.50/n) if sizing=='full' else (0.50/nc); tot=per*n; pay=0.0
        for c in chosen:
            pay+= per*bal/(c.price*(1+0.07*(1-c.price))) if c.win else 0.0; pk+=1; wn+=int(c.win)
        bal=bal-tot*bal+pay; daily.append(bal-eq[-1]); eq.append(bal)
    eq=np.array(eq); daily=np.array(daily); peak=np.maximum.accumulate(eq); nn=max(1,len(daily))
    return dict(hit=wn/pk if pk else float('nan'),fin=bal,pnl=bal-BR,ad=(bal-BR)/nn,
                ddp=float((eq/peak-1).min()) if len(eq)>1 else 0,worst=daily.min() if len(daily) else 0,
                am=am,avgpk=np.mean(pp) if pp else 0,nights=nn)


def main(argv=None):
    ap=argparse.ArgumentParser(); ap.add_argument("--rebuild",action="store_true"); a=ap.parse_args(argv)
    if CACHE.exists() and not a.rebuild: big=pd.read_parquet(CACHE); print(f"loaded {CACHE} ({len(big)})")
    else: print("building anchor table from low-temp backfill..."); big=build(find_base())
    big["date"]=pd.to_datetime(big["date"])
    CFG=[("MAX RET 3c-full",3,'top','full'),("MIN DD 3c-16.7%",3,'top','throttle'),("2c-full",2,'top','full')]
    print(f"\n$250 | band 0.93-0.95 | low-temp configs by ANCHOR time (last-trade, optimistic)\n")
    for name,nc,mode,sz in CFG:
        print(f"=== {name} ===")
        print(f"{'anchor':>7}{'nights':>7}{'avgPk':>6}{'hit':>7}{'end$':>7}{'PnL':>7}{'$/day':>7}{'maxDD%':>7}{'worst':>7}{'allmiss':>8}")
        for mins,lab in ANCHORS:
            sub=big[big.anchor==lab]
            if sub.empty: continue
            r=sim(sub,nc,mode,sz)
            print(f"{lab:>7}{r['nights']:>7}{r['avgpk']:>6.1f}{r['hit']:>7.1%}{r['fin']:>7.0f}{r['pnl']:>+7.0f}"
                  f"{r['ad']:>+7.2f}{r['ddp']:>7.0%}{r['worst']:>+7.0f}{r['am']:>5}/{r['nights']:<3}")
        print()
    return 0


if __name__=="__main__":
    sys.exit(main())
